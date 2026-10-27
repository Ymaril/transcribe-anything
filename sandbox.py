import os, subprocess, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
EGRESS_IMAGE = "tb-egress"
QUOTA = 5 * 1024 * 1024  # outbound bytes per run


def _docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def run_sandboxed(image, cmd, workdir, timeout=300, net="none"):
    """Run cmd from image in a throwaway container. workdir is mounted rw at /work.
    net: "none" or "container:<sidecar>" (see egress_up)."""
    name = f"tb-{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "--rm", "--name", name, "--network", net,
            "--read-only", "--tmpfs", "/tmp:size=512m", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--memory", "1g", "--cpus", "1", "--user", "65534:65534",
            "-e", "HOME=/tmp", "-v", f"{workdir}:/work", "-w", "/work", image, *cmd]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        raise


def egress_up(quota=QUOTA):
    """Start a sidecar that owns a network namespace and filters its egress. Fails closed."""
    if _docker("image", "inspect", EGRESS_IMAGE).returncode:
        subprocess.run(["docker", "build", "-q", "-t", EGRESS_IMAGE, f"{HERE}/egress"],
                       check=True, capture_output=True)
    name = f"tb-eg-{uuid.uuid4().hex[:12]}"
    r = _docker("run", "-d", "--rm", "--name", name, "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                "--security-opt", "no-new-privileges", "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
                "--memory", "64m", "--pids-limit", "32", EGRESS_IMAGE, "sleep", "86400")
    if r.returncode:
        raise RuntimeError(r.stderr)
    r = _docker("exec", name, "sh", "/rules.sh", str(quota))
    if r.returncode:
        egress_down(name)
        raise RuntimeError(f"egress rules failed: {r.stderr}")
    return name


def egress_down(name):
    _docker("rm", "-f", name)


def run_networked(image, cmd, workdir, timeout=300, quota=QUOTA):
    """Like run_sandboxed, but with filtered internet access."""
    sidecar = egress_up(quota)
    try:
        return run_sandboxed(image, cmd, workdir, timeout, net=f"container:{sidecar}")
    finally:
        egress_down(sidecar)


if __name__ == "__main__":  # self-check: python3 sandbox.py (needs internet)
    import json, tempfile
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
    _docker("rm", "-f", neighbour)
    assert _docker("run", "-d", "--rm", "--name", neighbour, IMG, "python", "-m", "http.server", "8000").returncode == 0
    try:
        ip = _docker("inspect", "-f", "{{.NetworkSettings.Networks.bridge.IPAddress}}", neighbour).stdout.strip()
        subprocess.run(["sleep", "1"])
        control = subprocess.run(["docker", "run", "--rm", IMG, "python", "-c",
                                  f"import socket; socket.create_connection(('{ip}', 8000), timeout=3)"])
        assert control.returncode == 0, "control failed: neighbour must be reachable without the filter"
        r = run_networked(IMG, ["python", "probe.py", ip], d)
        assert r.returncode == 0, r.stderr
        got = json.loads(r.stdout)
        assert got == {"internet": True, "dns": True, "neighbour": False, "raw": False}, got
    finally:
        _docker("rm", "-f", neighbour)

    # outbound quota: a big upload gets dropped once the quota is spent
    sc = egress_up(quota=50_000)
    try:
        send = ("import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                "for _ in range(2000):\n"
                "    try: s.sendto(b'x' * 1000, ('1.1.1.1', 9))\n"
                "    except OSError: pass\n")
        run_sandboxed(IMG, ["python", "-c", send], d, net=f"container:{sc}")
        rules = _docker("exec", sc, "iptables", "-L", "OUTPUT", "-v", "-x", "-n").stdout.splitlines()
        drop_all = [l.split() for l in rules if l.split()[2:3] == ["DROP"] and l.split()[-1] == "0.0.0.0/0"
                    and l.split()[-2] == "0.0.0.0/0"]
        assert drop_all and int(drop_all[0][0]) > 0, f"quota did not cut the upload:\n" + "\n".join(rules)
    finally:
        egress_down(sc)

    # fail closed: broken rules must not leave a usable sidecar behind
    try:
        egress_up(quota="bad")
        raise AssertionError("egress_up accepted bad rules")
    except RuntimeError:
        pass

    left = _docker("ps", "-q", "--filter", "name=tb-").stdout
    assert not left.strip(), "containers left running"

    print("ok")
