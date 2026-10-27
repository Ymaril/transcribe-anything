"""Per-run sidecar: owns a network namespace and filters its egress. The sandboxed container joins that namespace.
It also runs the key proxy on 127.0.0.1:8080, so the sandboxed container never holds real keys."""
import os, subprocess, threading, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "tb-sidecar"
QUOTA = 5 * 1024 * 1024  # outbound bytes per run
PROXY_URL = "http://127.0.0.1:8080"
KEY_NAMES = ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN")


def docker(*args, env=None):
    return subprocess.run(["docker", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)


_image_lock, _image_ready = threading.Lock(), False


def _build():
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, HERE], check=True, capture_output=True)


def _ensure_image():
    """Built once per process (the layer cache makes it quick): changed rules take effect after a restart."""
    global _image_ready
    with _image_lock:
        if not _image_ready:
            _build()
            _image_ready = True


def up(quota=QUOTA, keys=None):
    """Start a sidecar, apply the egress rules, start the key proxy if keys are given.
    Fails closed: if anything is wrong, no sidecar is left behind."""
    _ensure_image()
    name = f"tb-sc-{uuid.uuid4().hex[:12]}"
    r = docker("run", "-d", "--rm", "--name", name, "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
               "--security-opt", "no-new-privileges", "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
               "--memory", "64m", "--pids-limit", "32", IMAGE, "sleep", "86400")
    if r.returncode:
        raise RuntimeError(r.stderr)
    try:
        r = docker("exec", name, "sh", "/rules.sh", str(quota))
        if r.returncode:
            raise RuntimeError(f"sidecar rules failed: {r.stderr}")
        if keys:
            _start_proxy(name, keys)
    except BaseException:
        down(name)
        raise
    return name


def _start_proxy(name, keys):
    # keys go through the environment of the docker client (-e NAME without value), not the command line
    env = {**os.environ, **{k: keys[k] for k in KEY_NAMES}}
    r = docker("exec", "-d", "--user", "10001", *[a for k in KEY_NAMES for a in ("-e", k)],
               name, "python3", "/proxy.py", "serve", env=env)
    if r.returncode:
        raise RuntimeError(f"proxy start failed: {r.stderr}")
    ping = "import socket; socket.create_connection(('127.0.0.1', 8080), 1)"
    for _ in range(50):
        if docker("exec", name, "python3", "-c", ping).returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError("proxy did not come up")


def down(name):
    docker("rm", "-f", name)
