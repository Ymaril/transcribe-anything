import os, subprocess, uuid

from sidecar import sidecar


BASE_IMAGE = "transcribe-base"
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters", "base")


def build_base():
    """Adapter images are built FROM this one."""
    r = subprocess.run(["docker", "build", "-q", "-t", BASE_IMAGE, BASE_DIR], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"base image build failed: {r.stderr}")


def run_sandboxed(image, cmd, workdir, timeout=300, net="none", env=None):
    """Run cmd from image in a throwaway container. workdir is mounted rw at /work.
    net: "none" or "container:<sidecar>" (see sidecar.up)."""
    name = f"tb-{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "--rm", "--name", name, "--network", net,
            "--read-only", "--tmpfs", "/tmp:size=512m", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--memory", "1g", "--cpus", "1", "--user", "65534:65534",
            "-e", "HOME=/tmp", *[a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")],
            "-v", f"{workdir}:/work", "-w", "/work", image, *cmd]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        raise


def run_networked(image, cmd, workdir, timeout=300, quota=sidecar.QUOTA, keys=None):
    """Like run_sandboxed, but with filtered internet access.
    keys (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN): served by the key proxy, the container only gets its address."""
    sc = sidecar.up(quota, keys)
    env = {"OPENAI_BASE_URL": f"{sidecar.PROXY_URL}/openai/v1", "OPENAI_API_KEY": "unused",
           "TB_PROXY": sidecar.PROXY_URL} if keys else None
    try:
        return run_sandboxed(image, cmd, workdir, timeout, net=f"container:{sc}", env=env)
    finally:
        sidecar.down(sc)


if __name__ == "__main__":  # self-check: python3 sandbox.py (needs internet)
    import json, os, tempfile
    docker = sidecar.docker
    IMG = "python:3.12-slim"
    d = tempfile.mkdtemp()
    os.chmod(d, 0o777)  # container runs as nobody

    r = run_sandboxed(IMG, ["python", "-c", "import urllib.request as u; u.urlopen('http://1.1.1.1', timeout=3)"], d)
    assert r.returncode != 0, "network is not blocked"

    r = run_sandboxed(IMG, ["sh", "-c", "touch ok"], d)
    assert r.returncode == 0 and os.path.exists(f"{d}/ok"), f"/work not writable: {r.stderr}"

    r = run_sandboxed(IMG, ["sh", "-c", "touch /etc/x"], d)
    assert r.returncode != 0, "root fs is writable"

    # adapter image = its own image built from a base
    ad = tempfile.mkdtemp()
    with open(f"{ad}/Dockerfile", "w") as f:
        f.write(f"FROM {IMG}\nRUN echo derived > /marker\n")
    subprocess.run(["docker", "build", "-q", "-t", "tb-selfcheck", ad], check=True, capture_output=True)
    try:
        r = run_sandboxed("tb-selfcheck", ["cat", "/marker"], d)
        assert r.stdout.strip() == "derived", f"derived image not used: {r.stderr}"
    finally:
        subprocess.run(["docker", "rmi", "tb-selfcheck"], capture_output=True)

    try:
        run_sandboxed(IMG, ["sleep", "30"], d, timeout=2)
        raise AssertionError("timeout did not fire")
    except subprocess.TimeoutExpired:
        pass

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
    with open(f"{d}/probe.py", "w") as f:
        f.write(probe)
    neighbour = "tb-selfcheck-neighbour"
    docker("rm", "-f", neighbour)
    assert docker("run", "-d", "--rm", "--name", neighbour, IMG, "python", "-m", "http.server", "8000").returncode == 0
    try:
        ip = docker("inspect", "-f", "{{.NetworkSettings.Networks.bridge.IPAddress}}", neighbour).stdout.strip()
        subprocess.run(["sleep", "1"])
        control = subprocess.run(["docker", "run", "--rm", IMG, "python", "-c",
                                  f"import socket; socket.create_connection(('{ip}', 8000), timeout=3)"])
        assert control.returncode == 0, "control failed: neighbour must be reachable without the filter"
        r = run_networked(IMG, ["python", "probe.py", ip], d)
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
        run_sandboxed(IMG, ["python", "-c", send], d, net=f"container:{sc}")
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
    "env_has_key": any("bogus" in v for v in os.environ.values()),
    "base_url": os.environ["OPENAI_BASE_URL"],
}))
"""
    with open(f"{d}/probe.py", "w") as f:
        f.write(probe)
    sc = sidecar.up(quota=5000, keys=keys)  # quota is far below the big upload: only the exemption lets it through
    try:
        env = {"TB_PROXY": sidecar.PROXY_URL, "OPENAI_BASE_URL": f"{sidecar.PROXY_URL}/openai/v1"}
        r = run_sandboxed(IMG, ["python", "probe.py"], d, net=f"container:{sc}", env=env)
        assert r.returncode == 0, r.stderr
        got = json.loads(r.stdout)
        assert got == {"tg_reached": 401, "openai_reached": 401, "big_upload_reached": 401, "bad_model": 403,
                       "send_message": 403, "env_has_key": False, "base_url": env["OPENAI_BASE_URL"]}, got
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

    # run_networked passes the proxy address to the container and cleans up
    r = run_networked(IMG, ["python", "-c", "import os; print(os.environ['TB_PROXY'], os.environ['OPENAI_API_KEY'])"],
                      d, keys=keys)
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
