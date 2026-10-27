"""Adapter discovery, image building and match.

An adapter is a folder with a Dockerfile; the image must contain executable /adapter/match and /adapter/run.
Both get the Telegram message as /work/message.json. match prints a confidence 0..1 (last non-empty line).
"""
import hashlib, json, os, re, shutil, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import sandbox

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters")
THRESHOLD = 0.5  # ponytail: a guess, tune on real adapters
MATCH_TIMEOUT = 10
BUILD_TIMEOUT = 600
PARALLEL = 3  # the machine has 4 cores; every match is a sidecar plus a container


@dataclass(frozen=True)
class Adapter:
    path: str
    name: str  # shared/<name> or users/<user_id>/<name>
    own: bool


@dataclass(frozen=True)
class Result:
    adapter: Adapter
    score: float
    error: str = ""


def _dirs(path):
    return sorted(os.path.join(path, n) for n in os.listdir(path)
                  if os.path.isdir(os.path.join(path, n))) if os.path.isdir(path) else []


def visible(user_id, root=ROOT):
    """The user's own adapters and the shared ones."""
    uid = str(int(user_id))
    shared = [Adapter(p, f"shared/{os.path.basename(p)}", False) for p in _dirs(f"{root}/shared")]
    own = [Adapter(p, f"users/{uid}/{os.path.basename(p)}", True) for p in _dirs(f"{root}/users/{uid}")]
    return shared + own


def foreign(user_id, root=ROOT):
    """Other users' adapters: unverified."""
    uid = str(int(user_id))
    return [Adapter(p, f"users/{os.path.basename(u)}/{os.path.basename(p)}", False)
            for u in _dirs(f"{root}/users") if os.path.basename(u) != uid for p in _dirs(u)]


def image_tag(adapter):
    """Named by the host from the adapter's content, so edited or replaced adapters are rebuilt."""
    h = hashlib.sha256()
    for dirpath, _, files in sorted(os.walk(adapter.path)):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            h.update(os.path.relpath(p, adapter.path).encode() + b"\0" + open(p, "rb").read() + b"\0")
    return "tb-ad-" + h.hexdigest()[:16]


def build(adapter):
    tag = image_tag(adapter)
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    with open(f"{adapter.path}/Dockerfile") as f:
        if re.search(r"(?im)^\s*#\s*syntax\s*=", f.read()):  # would pull an arbitrary build frontend
            raise ValueError("Dockerfile with a syntax directive is rejected")
    r = subprocess.run(["docker", "build", "-q", "-t", tag, adapter.path],
                       capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    if r.returncode:
        raise RuntimeError(f"build failed: {r.stderr.strip()[-500:]}")
    return tag


def _match(adapter, message, keys):
    try:
        tag = build(adapter)
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o777)  # containers run as nobody
            with open(f"{d}/message.json", "w") as f:
                json.dump(message, f)
            r = sandbox.run_networked(tag, ["/adapter/match"], d, timeout=MATCH_TIMEOUT, keys=keys)
        if r.returncode:
            return Result(adapter, 0.0, f"match exited with {r.returncode}: {r.stderr.strip()[-200:]}")
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        try:
            score = float(lines[-1])
        except (IndexError, ValueError):
            score = None
        if score is None or not 0 <= score <= 1:
            return Result(adapter, 0.0, f"match printed no score in 0..1: {r.stdout.strip()[-200:]!r}")
        return Result(adapter, score)
    except subprocess.TimeoutExpired:
        return Result(adapter, 0.0, "timeout")
    except (ValueError, RuntimeError, OSError) as e:
        return Result(adapter, 0.0, str(e))


def match_all(adapters, message, keys=None):
    with ThreadPoolExecutor(PARALLEL) as pool:
        return list(pool.map(lambda a: _match(a, message, keys), adapters))


def pick(results):
    """The best result above the threshold; on equal scores the user's own adapter wins."""
    good = [r for r in results if r.score >= THRESHOLD]
    return max(good, key=lambda r: (r.score, r.adapter.own), default=None)


if __name__ == "__main__":  # self-check: python3 dispatch.py
    root = tempfile.mkdtemp()

    def adapter(path, match_code, dockerfile_extra=""):
        os.makedirs(f"{root}/{path}")
        with open(f"{root}/{path}/Dockerfile", "w") as f:
            f.write("FROM python:3.12-slim\nCOPY . /adapter\n" + dockerfile_extra)
        with open(f"{root}/{path}/match", "w") as f:
            f.write("#!/usr/local/bin/python\nimport json, time\nm = json.load(open('/work/message.json'))\n" + match_code)
        os.chmod(f"{root}/{path}/match", 0o755)

    adapter("shared/voice", "print(0.9 if 'voice' in m else 0.0)")
    adapter("users/42/tiktok", "print('noise'); print(0.8 if 'tiktok.com' in m.get('text', '') else 0.0)")
    adapter("users/42/slow", "time.sleep(60)")
    adapter("users/42/broken", "print('hello')")
    adapter("users/42/crash", "raise SystemExit(3)")
    adapter("users/42/evil", "print(1.0)")
    with open(f"{root}/users/42/evil/Dockerfile", "w") as f:
        f.write("# syntax=evil/frontend\nFROM python:3.12-slim\nCOPY . /adapter\n")
    adapter("users/7/stranger_voice", "print(0.95 if 'voice' in m else 0.0)")
    adapter("users/42/same_as_shared", "print(0.9 if 'voice' in m else 0.0)")
    open(f"{root}/shared/README", "w").write("not an adapter")

    names = lambda xs: sorted(a.name for a in xs)
    assert names(visible(42, root)) == ["shared/voice", "users/42/broken", "users/42/crash", "users/42/evil",
                                        "users/42/same_as_shared", "users/42/slow", "users/42/tiktok"]
    assert names(foreign(42, root)) == ["users/7/stranger_voice"]
    assert names(visible(99, root)) == ["shared/voice"]

    mine = visible(42, root)
    tags = [image_tag(a) for a in mine + foreign(42, root)]  # taken before the edit below changes one of them
    try:
        res = {r.adapter.name: r for r in match_all(mine, {"voice": {"file_id": "x"}})}
        assert res["shared/voice"].score == 0.9 and res["users/42/tiktok"].score == 0.0, res
        assert res["users/42/slow"].error == "timeout", res["users/42/slow"]
        assert "no score" in res["users/42/broken"].error, res["users/42/broken"]
        assert "exited with 3" in res["users/42/crash"].error, res["users/42/crash"]
        assert "syntax directive" in res["users/42/evil"].error, res["users/42/evil"]
        assert all(res[n].score == 0.0 for n in ("users/42/slow", "users/42/broken", "users/42/crash", "users/42/evil"))

        # equal scores: the user's own adapter wins; a lower own score does not beat a higher shared one
        assert pick(list(res.values())).adapter.name == "users/42/same_as_shared"
        # a tiktok link: only that adapter is confident
        link = {r.adapter.name: r for r in match_all(mine, {"text": "https://tiktok.com/@a/video/1"})}
        assert pick(link.values()).adapter.name == "users/42/tiktok", link
        # nothing fits: no pick
        assert pick(match_all(mine, {"text": "hello"})) is None
        # unverified adapters are matched only on request
        assert pick(match_all(foreign(42, root), {"voice": {}})).adapter.name == "users/7/stranger_voice"

        # the image is named by content: stable while unchanged, new after an edit
        a = [x for x in mine if x.name == "users/42/tiktok"][0]
        assert image_tag(a) == tags[mine.index(a)]
        open(f"{a.path}/match", "a").write("# edited\n")
        assert image_tag(a) != tags[mine.index(a)]
    finally:
        for t in tags:
            subprocess.run(["docker", "rmi", "-f", t], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)

    left = subprocess.run(["docker", "ps", "-aq", "--filter", "name=tb-"], capture_output=True, text=True).stdout
    assert not left.strip(), "containers left"
    print("ok")
