"""Adapter discovery, image building, match and run.

An adapter is a folder with a Dockerfile; the image must contain executable /adapter/match and /adapter/run.
They are utilities: the Telegram message (JSON) comes on stdin.
match prints a confidence 0..1 (last non-empty line of stdout); run prints the transcript to stdout.
stderr is for the log; scratch space is /tmp.
"""
import hashlib, json, os, re, shutil, subprocess, tempfile, threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import sandbox

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters")
THRESHOLD = 0.5  # ponytail: a guess, tune on real adapters
MATCH_TIMEOUT = 10
MATCH_OUTPUT = 64 * 1024
BUILD_TIMEOUT = 600
RUN_TIMEOUT = 900
MAX_TRANSCRIPT = 2 * 1024 * 1024  # bytes
# Containers running at the same moment, over all messages (every one is a sidecar plus a container; 4 cores).
# Short matches and long runs have separate slots: a long transcription must not stop other messages from matching.
MATCH_SLOTS = 3
RUN_SLOTS = 4
_match_slots = threading.BoundedSemaphore(MATCH_SLOTS)
_run_slots = threading.BoundedSemaphore(RUN_SLOTS)
# ponytail: one lock for all builds: they are rare (first use of an adapter), and two messages never build the same image twice
_build_lock = threading.Lock()


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


class AdapterError(Exception):
    """reason is short and safe to show to the user; detail (stderr and the like) is for the log."""
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _dirs(path):
    """Subdirectories, except hidden ones: the agent works in users/<id>/.staging, unfinished adapters stay unseen."""
    return sorted(os.path.join(path, n) for n in os.listdir(path)
                  if not n.startswith(".") and os.path.isdir(os.path.join(path, n))) if os.path.isdir(path) else []


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
    if sandbox.image_exists(tag):  # the usual case never waits for somebody else's long build
        return tag
    with _build_lock:
        if sandbox.image_exists(tag):  # built by someone else while we waited
            return tag
        with open(f"{adapter.path}/Dockerfile") as f:
            if re.search(r"(?im)^\s*#\s*syntax\s*=", f.read()):  # would pull an arbitrary build frontend
                raise ValueError("Dockerfile with a syntax directive is rejected")
        sandbox.build_image(tag, adapter.path, BUILD_TIMEOUT)
        return tag


def _match(adapter, message, keys):
    try:
        tag = build(adapter)
        with _match_slots:
            r = sandbox.run_networked(tag, ["/adapter/match"], json.dumps(message),
                                      timeout=MATCH_TIMEOUT, keys=keys, max_output=MATCH_OUTPUT)
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
    except sandbox.OutputTooLarge:
        return Result(adapter, 0.0, "match printed too much")
    except (ValueError, RuntimeError, OSError) as e:
        return Result(adapter, 0.0, str(e))


def match_all(adapters, message, keys=None):
    """All matches at once: the slots, not this pool, decide how many containers really run."""
    with ThreadPoolExecutor(max(1, len(adapters))) as pool:
        return list(pool.map(lambda a: _match(a, message, keys), adapters))


def run(adapter, message, keys=None, timeout=RUN_TIMEOUT, max_bytes=MAX_TRANSCRIPT):
    """Run the adapter on the message and return the transcript (its stdout), or raise AdapterError."""
    try:
        tag = build(adapter)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        raise AdapterError("cannot build the adapter", str(e))
    try:
        with _run_slots:
            r = sandbox.run_networked(tag, ["/adapter/run"], json.dumps(message),
                                      timeout=timeout, keys=keys, max_output=max_bytes)
    except subprocess.TimeoutExpired:
        raise AdapterError("timeout")
    except sandbox.OutputTooLarge:
        raise AdapterError("transcript is too large")
    if r.returncode:
        raise AdapterError(f"run exited with {r.returncode}", r.stderr.strip()[-500:])
    if not r.stdout.strip():
        raise AdapterError("transcript is empty")
    return r.stdout.strip()


def pick(results):
    """The best result above the threshold; on equal scores the user's own adapter wins."""
    good = [r for r in results if r.score >= THRESHOLD]
    return max(good, key=lambda r: (r.score, r.adapter.own), default=None)


if __name__ == "__main__":  # self-check: python3 dispatch.py
    root, root2 = tempfile.mkdtemp(), tempfile.mkdtemp()

    def script(path, code):
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/local/bin/python\nimport json, sys, time\nm = json.load(sys.stdin)\n" + code)
        os.chmod(path, 0o755)

    def adapter(path, match_code, run_code=None, base=root):
        os.makedirs(f"{base}/{path}")
        with open(f"{base}/{path}/Dockerfile", "w") as f:
            f.write("FROM python:3.12-slim\nCOPY . /adapter\n")
        script(f"{base}/{path}/match", match_code)
        if run_code is not None:
            script(f"{base}/{path}/run", run_code)

    for name, code in {
        "ok": "sys.stderr.write('log line\\n'); print('  текст: ' + m['text'] + '\\n')",
        "none": "pass",
        "blank": "print('   ')",
        "crash": "sys.stderr.write('boom'); sys.exit(5)",
        "sleepy": "time.sleep(60)",
        "huge": "print('x' * 2000)",
    }.items():
        adapter(f"users/43/{name}", "print(0.0)", code, base=root2)
    adapter("users/43/norun", "print(0.0)", base=root2)

    adapter("shared/voice", "print(0.9 if 'voice' in m else 0.0)")
    adapter("users/42/tiktok", "print('noise'); print(0.8 if 'tiktok.com' in m.get('text', '') else 0.0)")
    adapter("users/42/slow", "time.sleep(60)")
    adapter("users/42/broken", "print('hello')")
    adapter("users/42/crash", "raise SystemExit(3)")
    adapter("users/42/chatty", "print('x' * 1_000_000)")
    adapter("users/42/evil", "print(1.0)")
    with open(f"{root}/users/42/evil/Dockerfile", "w") as f:
        f.write("# syntax=evil/frontend\nFROM python:3.12-slim\nCOPY . /adapter\n")
    adapter("users/7/stranger_voice", "print(0.95 if 'voice' in m else 0.0)")
    adapter("users/42/same_as_shared", "print(0.9 if 'voice' in m else 0.0)")
    open(f"{root}/shared/README", "w").write("not an adapter")
    os.makedirs(f"{root}/users/42/.staging/abc")             # an unfinished adapter of the agent: not an adapter yet
    os.makedirs(f"{root}/users/.hidden/x")

    names = lambda xs: sorted(a.name for a in xs)
    assert names(visible(42, root)) == ["shared/voice", "users/42/broken", "users/42/chatty", "users/42/crash",
                                        "users/42/evil", "users/42/same_as_shared", "users/42/slow",
                                        "users/42/tiktok"]
    assert names(foreign(42, root)) == ["users/7/stranger_voice"]
    assert names(visible(99, root)) == ["shared/voice"]

    mine = visible(42, root)
    tags = [image_tag(a) for a in mine + foreign(42, root) + visible(43, root2)]  # before the edit below changes one
    try:
        res = {r.adapter.name: r for r in match_all(mine, {"voice": {"file_id": "x"}})}
        assert res["shared/voice"].score == 0.9 and res["users/42/tiktok"].score == 0.0, res
        assert res["users/42/slow"].error == "timeout", res["users/42/slow"]
        assert "no score" in res["users/42/broken"].error, res["users/42/broken"]
        assert "exited with 3" in res["users/42/crash"].error, res["users/42/crash"]
        assert "printed too much" in res["users/42/chatty"].error, res["users/42/chatty"]
        assert "syntax directive" in res["users/42/evil"].error, res["users/42/evil"]
        for n in ("slow", "broken", "crash", "chatty", "evil"):
            assert res[f"users/42/{n}"].score == 0.0, n

        # equal scores: the user's own adapter wins
        assert pick(list(res.values())).adapter.name == "users/42/same_as_shared"
        # a tiktok link: only that adapter is confident
        link = {r.adapter.name: r for r in match_all(mine, {"text": "https://tiktok.com/@a/video/1"})}
        assert pick(link.values()).adapter.name == "users/42/tiktok", link
        # nothing fits: no pick
        assert pick(match_all(mine, {"text": "hello"})) is None
        # unverified adapters are matched only on request
        assert pick(match_all(foreign(42, root), {"voice": {}})).adapter.name == "users/7/stranger_voice"

        # run: stdout is the transcript, stderr is only for the log
        A = {a.name: a for a in visible(43, root2)}
        assert run(A["users/43/ok"], {"text": "привет"}) == "текст: привет"

        def fails(name, reason, **kw):
            try:
                run(A[f"users/43/{name}"], {}, **kw)
            except AdapterError as e:
                assert reason in e.reason, (name, e.reason)
                return e
            raise AssertionError(f"{name} did not fail")

        assert "boom" in fails("crash", "exited with 5").detail
        fails("none", "empty")
        fails("blank", "empty")
        fails("sleepy", "timeout", timeout=5)
        fails("huge", "too large", max_bytes=1000)
        fails("norun", "exited with")

        # the image is named by content: stable while unchanged, new after an edit
        a = [x for x in mine if x.name == "users/42/tiktok"][0]
        assert image_tag(a) == tags[mine.index(a)]
        open(f"{a.path}/match", "a").write("# edited\n")
        assert image_tag(a) != tags[mine.index(a)]

        # a long build of one adapter must not hold up an adapter that is already built
        import time
        slow = os.path.join(root2, "users/43/slow_build")
        os.makedirs(slow)
        with open(f"{slow}/Dockerfile", "w") as f:  # a unique command: Docker must not serve this layer from its cache
            f.write(f"FROM python:3.12-slim\nRUN sleep 8; echo {os.urandom(8).hex()}\nCOPY . /adapter\n")
        script(f"{slow}/match", "print(0.0)")
        slow_adapter = Adapter(slow, "users/43/slow_build", True)
        tags.append(image_tag(slow_adapter))
        building = threading.Thread(target=build, args=(slow_adapter,))
        building.start()
        time.sleep(1.5)                              # the slow build is in progress and holds the build lock
        t0 = time.time()
        build(A["users/43/ok"])                      # already built by the run checks above
        assert time.time() - t0 < 3, "a built adapter waited for somebody else's build"
        building.join()
        assert sandbox.image_exists(image_tag(slow_adapter))
    finally:
        for t in tags:
            subprocess.run(["docker", "rmi", "-f", t], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(root2, ignore_errors=True)

    # slots: however many messages arrive at once, only MATCH_SLOTS matches and RUN_SLOTS runs are in flight
    import time

    class Gauge:
        def __init__(self):
            self.now, self.top, self.lock = {"match": 0, "run": 0}, {"match": 0, "run": 0}, threading.Lock()

        def __call__(self, image, cmd, stdin="", **kw):
            kind = "match" if cmd[0] == "/adapter/match" else "run"
            with self.lock:
                self.now[kind] += 1
                self.top[kind] = max(self.top[kind], self.now[kind])
            time.sleep(0.3 if kind == "match" else 1.0)
            with self.lock:
                self.now[kind] -= 1
            return sandbox.Completed(0, "0.9" if kind == "match" else "text", "")

    gauge, real_run_networked, real_build = Gauge(), sandbox.run_networked, build
    sandbox.run_networked, build = gauge, lambda a: "tag"
    try:
        fakes = [Adapter(f"/x/{i}", f"shared/a{i}", False) for i in range(5)]
        with ThreadPoolExecutor(8) as ex:  # four messages at once, five adapters each
            assert all(len(rs) == 5 for rs in ex.map(lambda _: match_all(fakes, {}), range(4)))
        assert gauge.top["match"] == MATCH_SLOTS, gauge.top

        with ThreadPoolExecutor(10) as ex:
            assert list(ex.map(lambda _: run(fakes[0], {}), range(10))) == ["text"] * 10
        assert gauge.top["run"] == RUN_SLOTS, gauge.top

        # every run slot is busy for a second: a new message can still be matched meanwhile
        with ThreadPoolExecutor(RUN_SLOTS) as ex:
            futures = [ex.submit(run, fakes[0], {}) for _ in range(RUN_SLOTS)]
            while gauge.now["run"] < RUN_SLOTS:
                time.sleep(0.01)
            assert len(match_all(fakes[:1], {})) == 1
            assert gauge.now["run"] == RUN_SLOTS, "the match had to wait for the runs"
            [f.result() for f in futures]
    finally:
        sandbox.run_networked, build = real_run_networked, real_build

    left = subprocess.run(["docker", "ps", "-aq", "--filter", "name=tb-"], capture_output=True, text=True).stdout
    assert not left.strip(), "containers left"
    print("ok")
