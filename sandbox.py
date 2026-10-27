import subprocess, uuid


def run_sandboxed(image, cmd, workdir, timeout=300):
    """Run cmd from image in a throwaway container without network. workdir is mounted rw at /work."""
    name = f"tb-{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "--rm", "--name", name, "--network", "none",
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


if __name__ == "__main__":  # self-check: python3 sandbox.py
    import os, tempfile
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
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=tb-"], capture_output=True, text=True).stdout
    assert not left.strip(), "container left running after timeout"

    print("ok")
