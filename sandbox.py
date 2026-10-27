"""Run untrusted code in throwaway containers, as utilities: stdin in, stdout/stderr out. No files are shared."""
import collections, os, subprocess, threading, uuid
from dataclasses import dataclass

from sidecar import sidecar

BASE_IMAGE = "transcribe-base"
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters", "base")
MAX_INPUT = 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024


class OutputTooLarge(Exception):
    pass


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str  # the last 4 KB only


def image_exists(tag):
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


def remove_image(tag):
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def build_image(tag, directory, timeout=None):
    """Build an image from a directory. Raises RuntimeError with the tail of docker's complaint."""
    r = subprocess.run(["docker", "build", "-q", "-t", tag, directory], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"image build failed: {r.stderr.strip()[-500:]}")


def build_base():
    """Adapter images are built FROM this one."""
    build_image(BASE_IMAGE, BASE_DIR)


def run_sandboxed(image, cmd, stdin="", timeout=300, net="none", env=None, max_output=MAX_OUTPUT,
                  mounts=None, user="65534:65534", memory="1g", tmp="512m", workdir="/tmp"):
    """Run cmd from image in a throwaway container. Scratch space is /tmp (tmpfs).
    net: "none" or "container:<sidecar>" (see sidecar.up). Raises subprocess.TimeoutExpired, OutputTooLarge.
    The last five are for the agent, the only container that gets a directory to write to (mounts: {host: container}),
    a larger budget, and the host's uid so that what it writes is ours to read, move and delete."""
    data = stdin.encode()
    if len(data) > MAX_INPUT:
        raise ValueError("input is too large")
    name = f"tb-{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "--rm", "-i", "--name", name, "--network", net,
            "--read-only", "--tmpfs", f"/tmp:size={tmp}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--memory", memory, "--cpus", "1", "--user", user,
            "-e", "HOME=/tmp", *[a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")],
            *[a for host, inside in (mounts or {}).items() for a in ("-v", f"{host}:{inside}")],
            "-w", workdir, image, *cmd]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, tail, too_big = bytearray(), collections.deque(maxlen=4096), []

    def kill():
        subprocess.run(["docker", "kill", name], capture_output=True)

    def read_out():
        while chunk := p.stdout.read(65536):  # keeps draining after the limit: a full pipe would block the container
            if too_big:
                continue
            if len(out) + len(chunk) > max_output:
                too_big.append(True)
                threading.Thread(target=kill, daemon=True).start()
            else:
                out.extend(chunk)

    def read_err():
        while chunk := p.stderr.read(4096):
            tail.extend(chunk)

    readers = [threading.Thread(target=f, daemon=True) for f in (read_out, read_err)]
    for t in readers:
        t.start()
    try:
        try:
            p.stdin.write(data)
            p.stdin.close()
        except BrokenPipeError:  # the program did not want its input
            pass
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        kill()
        p.wait()
        raise
    finally:
        for t in readers:
            t.join(10)
    if too_big:
        raise OutputTooLarge()
    return Completed(p.returncode, out.decode("utf-8", "replace"), bytes(tail).decode("utf-8", "replace"))


def run_networked(image, cmd, stdin="", timeout=300, quota=sidecar.QUOTA, keys=None, max_output=MAX_OUTPUT,
                  env=None, **options):
    """Like run_sandboxed, but with filtered internet access (options: see run_sandboxed).
    keys (see sidecar.KEY_NAMES): served by the key proxy, the container only gets its address."""
    sc = sidecar.up(quota, keys)
    env = {**(sidecar.env() if keys else {}), **(env or {})}
    try:
        return run_sandboxed(image, cmd, stdin, timeout, net=f"container:{sc}", env=env, max_output=max_output,
                             **options)
    finally:
        sidecar.down(sc)


if __name__ == "__main__":  # self-check: python3 sandbox.py (needs internet)
    import json, tempfile
    docker = sidecar.docker
    IMG = "python:3.12-slim"

    def py(code, **kw):
        return run_sandboxed(IMG, ["python", "-c", code], **kw)

    assert py("import urllib.request as u; u.urlopen('http://1.1.1.1', timeout=3)").returncode != 0, \
        "network is not blocked"
    assert run_sandboxed(IMG, ["sh", "-c", "touch /tmp/ok"]).returncode == 0, "/tmp is not writable"
    assert run_sandboxed(IMG, ["sh", "-c", "touch /etc/x"]).returncode != 0, "root fs is writable"
    assert run_sandboxed(IMG, ["sh", "-c", "touch /work/x"]).returncode != 0, "there must be no /work"

    # the agent's way: a directory to write to, the host's uid, a bigger budget: what it writes is ours
    import shutil
    mine = tempfile.mkdtemp()
    me = f"{os.getuid()}:{os.getgid()}"
    r = run_sandboxed(IMG, ["sh", "-c", "echo hi > f && mkdir sub && echo x > sub/g && df -m /tmp | tail -1"],
                      mounts={mine: "/adapter"}, user=me, workdir="/adapter", memory="2g", tmp="768m")
    assert r.returncode == 0, r.stderr
    assert open(f"{mine}/f").read() == "hi\n" and os.stat(f"{mine}/f").st_uid == os.getuid()
    assert " 768 " in r.stdout, r.stdout                                      # the larger /tmp
    shutil.rmtree(mine)                                                       # ours to delete, no root helper needed

    # stdin in, stdout/stderr out
    r = run_sandboxed(IMG, ["sh", "-c", "cat; echo err >&2; exit 3"], stdin="привет\n")
    assert (r.returncode, r.stdout, r.stderr) == (3, "привет\n", "err\n"), r
    assert py("import sys; sys.stderr.write('a' * 10000 + 'END')").stderr.endswith("END"), "stderr tail lost"
    assert len(py("import sys; sys.stderr.write('a' * 10000)").stderr) == 4096

    # limits: input, output and time
    try:
        run_sandboxed(IMG, ["cat"], stdin="x" * (MAX_INPUT + 1))
        raise AssertionError("huge input accepted")
    except ValueError:
        pass
    try:
        py("print('x' * 10_000_000)", max_output=1_000_000)
        raise AssertionError("huge output accepted")
    except OutputTooLarge:
        pass
    try:
        run_sandboxed(IMG, ["sleep", "30"], timeout=2)
        raise AssertionError("timeout did not fire")
    except subprocess.TimeoutExpired:
        pass

    # adapter image = its own image built from a base
    ad = tempfile.mkdtemp()
    with open(f"{ad}/Dockerfile", "w") as f:
        f.write(f"FROM {IMG}\nRUN echo derived > /marker\n")
    subprocess.run(["docker", "build", "-q", "-t", "tb-selfcheck", ad], check=True, capture_output=True)
    try:
        r = run_sandboxed("tb-selfcheck", ["cat", "/marker"])
        assert r.stdout.strip() == "derived", f"derived image not used: {r.stderr}"
    finally:
        subprocess.run(["docker", "rmi", "tb-selfcheck"], capture_output=True)

    # filtered network: internet and DNS yes; other containers (private ranges) and raw sockets no
    probe = """
import json, socket, sys
def tcp(host, port):
    try: socket.create_connection((host, port), timeout=3).close(); return True
    except OSError: return False
def dns():
    try: socket.getaddrinfo("example.com", 80); return True
    except OSError: return False
def raw():
    try: socket.socket(socket.AF_INET, socket.SOCK_RAW, 1); return True
    except PermissionError: return False
print(json.dumps({"internet": tcp("1.1.1.1", 80), "dns": dns(), "neighbour": tcp(sys.argv[1], 8000), "raw": raw()}))
"""
    neighbour = "tb-selfcheck-neighbour"
    docker("rm", "-f", neighbour)
    assert docker("run", "-d", "--rm", "--name", neighbour, IMG, "python", "-m", "http.server", "8000").returncode == 0
    try:
        ip = docker("inspect", "-f", "{{.NetworkSettings.Networks.bridge.IPAddress}}", neighbour).stdout.strip()
        subprocess.run(["sleep", "1"])
        control = subprocess.run(["docker", "run", "--rm", IMG, "python", "-c",
                                  f"import socket; socket.create_connection(('{ip}', 8000), timeout=3)"])
        assert control.returncode == 0, "control failed: neighbour must be reachable without the filter"
        r = run_networked(IMG, ["python", "-", ip], probe)
        assert r.returncode == 0, r.stderr
        got = json.loads(r.stdout)
        assert got == {"internet": True, "dns": True, "neighbour": False, "raw": False}, got
    finally:
        docker("rm", "-f", neighbour)

    # outbound quota: a big upload gets dropped once the quota is spent
    sc = sidecar.up(quota=50_000)
    try:
        send = ("import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                "for _ in range(2000):\n"
                "    try: s.sendto(b'x' * 1000, ('1.1.1.1', 9))\n"
                "    except OSError: pass\n")
        run_sandboxed(IMG, ["python", "-c", send], net=f"container:{sc}")
        rules = docker("exec", sc, "iptables", "-L", "OUTPUT", "-v", "-x", "-n").stdout.splitlines()
        drop_all = [l.split() for l in rules if l.split()[2:3] == ["DROP"] and l.split()[-1] == "0.0.0.0/0"
                    and l.split()[-2] == "0.0.0.0/0"]
        assert drop_all and int(drop_all[0][0]) > 0, f"quota did not cut the upload:\n" + "\n".join(rules)
    finally:
        sidecar.down(sc)

    # key proxy: the container reaches OpenAI/Telegram through it, without holding keys
    keys = {"OPENAI_API_KEY": "sk-bogus", "TELEGRAM_BOT_TOKEN": "123:bogus"}  # upstream answers 401: it was reached
    probe = """
import json, os, urllib.error, urllib.request
P = os.environ["TB_PROXY"]
def call(method, path, body=None, ctype=None):
    req = urllib.request.Request(P + path, data=body, method=method, headers={"Content-Type": ctype} if ctype else {})
    try: return urllib.request.urlopen(req, timeout=60).status
    except urllib.error.HTTPError as e: return e.code
def form(model, size=10):
    return (b'--B\\r\\nContent-Disposition: form-data; name="model"\\r\\n\\r\\n' + model +
            b'\\r\\n--B\\r\\nContent-Disposition: form-data; name="file"; filename="a.ogg"\\r\\n\\r\\n' + b"x" * size + b'\\r\\n--B--\\r\\n')
MP, TR = "multipart/form-data; boundary=B", "/openai/v1/audio/transcriptions"
print(json.dumps({
    "tg_reached": call("GET", "/telegram/getFile?file_id=x"),
    "openai_reached": call("POST", TR, form(b"whisper-1"), MP),
    "big_upload_reached": call("POST", TR, form(b"whisper-1", 300_000), MP),
    "bad_model": call("POST", TR, form(b"gpt-4o"), MP),
    "send_message": call("POST", "/telegram/sendMessage", b"x"),
    "anthropic": call("POST", "/anthropic/v1/messages", json.dumps({"model": "claude-haiku-5-5", "messages": []}).encode(),
                      "application/json"),
    "env_has_key": any("bogus" in v or "from-host" in v for v in os.environ.values()),
    "base_url": os.environ["OPENAI_BASE_URL"],
}))
"""
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-from-host-env"  # lying in our own environment: must never reach an adapter
    sc = sidecar.up(quota=5000, keys=keys)  # quota is far below the big upload: only the exemption lets it through
    try:
        env = sidecar.env()
        r = run_sandboxed(IMG, ["python", "-"], probe, net=f"container:{sc}", env=env)
        assert r.returncode == 0, r.stderr
        got = json.loads(r.stdout)
        assert got == {"tg_reached": 401, "openai_reached": 401, "big_upload_reached": 401, "bad_model": 403,
                       "send_message": 403, "anthropic": 403, "env_has_key": False,
                       "base_url": env["OPENAI_BASE_URL"]}, got
        rules = docker("exec", sc, "iptables", "-L", "OUTPUT", "-v", "-x", "-n").stdout.splitlines()
        proxy_rule = [l.split() for l in rules if "owner UID match 10001" in l][0]
        drop_all = [l.split() for l in rules if l.split()[2:3] == ["DROP"] and l.split()[-2:] == ["0.0.0.0/0"] * 2][0]
        assert int(proxy_rule[1]) > 5000, f"proxy traffic not seen by the exemption rule: {proxy_rule}"
        assert int(drop_all[0]) == 0, f"proxy traffic hit the quota: {drop_all}"
        # the proxy is not reachable from other containers
        sc_ip = docker("inspect", "-f", "{{.NetworkSettings.Networks.bridge.IPAddress}}", sc).stdout.strip()
        other = subprocess.run(["docker", "run", "--rm", IMG, "python", "-c",
                                f"import socket; socket.create_connection(('{sc_ip}', 8080), timeout=3)"],
                               capture_output=True)
        assert other.returncode != 0, "key proxy is reachable from outside the sidecar"
    finally:
        sidecar.down(sc)
        del os.environ["ANTHROPIC_API_KEY"]

    # the agent's sidecar is given the Anthropic key: the request reaches Anthropic (401 for a bogus key)
    agent_probe = """
import json, os, urllib.error, urllib.request
req = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", method="POST",
    data=json.dumps({"model": "claude-haiku-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
try: print(urllib.request.urlopen(req, timeout=60).status)
except urllib.error.HTTPError as e: print(e.code)
"""
    r = run_networked(IMG, ["python", "-"], agent_probe, keys={**keys, "ANTHROPIC_API_KEY": "sk-ant-bogus"})
    assert r.stdout.strip() == "401", (r.stdout, r.stderr)

    # run_networked passes the proxy address to the container and cleans up
    r = run_networked(IMG, ["python", "-c", "import os; print(os.environ['TB_PROXY'], os.environ['OPENAI_API_KEY'])"],
                      keys=keys)
    assert r.stdout.split() == [sidecar.PROXY_URL, "unused"], (r.stdout, r.stderr)

    # fail closed: broken rules must not leave a usable sidecar behind
    try:
        sidecar.up(quota="bad")
        raise AssertionError("sidecar.up accepted bad rules")
    except RuntimeError:
        pass

    left = docker("ps", "-q", "--filter", "name=tb-").stdout
    assert not left.strip(), "containers left running"

    print("ok")
