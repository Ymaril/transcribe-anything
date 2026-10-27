"""The agent: builds a new adapter for a message that nobody could handle (the last resort of the flow).

Claude Code runs in a container (the agent image) with a writable directory mounted at /adapter. The host does not take
its word: it checks what was written, builds the image and runs match and run on the very message. If that fails the
agent gets the reason and the same files for another try.
Self-check: python3 agent.py (fake agents, no Claude is called).
"""
import json, os, re, shutil, subprocess, threading, uuid

import dispatch, sandbox

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "tb-agent"
IMAGE_DIR = f"{HERE}/agent"
MODEL = "claude-sonnet-5-5"
BUDGET_USD = 2.0  # per attempt, enforced by Claude Code itself
TIMEOUT = 20 * 60
ATTEMPTS = 2
MAX_FILES, MAX_FILE, MAX_TOTAL = 100, 1_000_000, 5_000_000
NAME = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
SYSTEM = ("You run non-interactively inside a sandbox container. Nobody can answer questions. "
          "Your budget is limited, so be efficient.")
_one_at_a_time = threading.BoundedSemaphore(1)  # the expensive and heavy part of the system
_image_lock, _image_ready = threading.Lock(), False


class AgentError(Exception):
    """reason is short and safe to show to the user; detail is for the log."""
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def ensure_image():
    """Built once per process; the first build takes minutes (node, Claude Code), later ones come from the cache."""
    global _image_ready
    with _image_lock:
        if not _image_ready:
            sandbox.build_image(IMAGE, IMAGE_DIR)
            _image_ready = True


def prompt(message, failure=""):
    example = ""
    ref = f"{dispatch.ROOT}/shared/telegram_voice"
    for name in ("Dockerfile", "match", "run") if os.path.isdir(ref) else ():
        example += f"### {name}\n\n```\n{open(f'{ref}/{name}').read()}```\n\n"
    text = open(f"{IMAGE_DIR}/PROMPT.md").read()
    if failure:
        failure = ("\n## The previous attempt failed the acceptance\n\n" + failure + "\n\nThe files of that attempt are "
                   "still in /adapter. Fix them (start again only if you must) and end with the name line again.\n")
    return (text.replace("{message_json}", json.dumps(message, ensure_ascii=False, indent=2))
                .replace("{example_files}", example or "(none)").replace("{failure}", failure))


def clean_name(raw):
    """The adapter's name from the last line of the agent's answer, or None if it is not a name. Agents like to wrap
    a name in quotes, backticks or asterisks, or to end it with a full stop: that is not worth a random name."""
    ticked = re.search(r"`([A-Za-z][A-Za-z0-9_]{0,29})`", raw)
    name = (ticked.group(1) if ticked else raw.strip().strip("`'\"*. ")).lower()
    return name if NAME.match(name) else None


def run_claude(staging, message, failure, keys):
    """One run of the agent in its container. Returns the name it chose for the adapter."""
    r = sandbox.run_networked(
        IMAGE, ["claude", "-p", "--model", MODEL, "--dangerously-skip-permissions", "--output-format", "json",
                "--no-session-persistence", "--max-budget-usd", str(BUDGET_USD), "--append-system-prompt", SYSTEM],
        prompt(message, failure), timeout=TIMEOUT, keys=keys, mounts={staging: "/adapter"},
        user=f"{os.getuid()}:{os.getgid()}", memory="2g", tmp="1g", workdir="/adapter")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise AgentError("the agent failed", f"exit {r.returncode}: {r.stdout[-300:]} {r.stderr[-500:]}")
    print(f"agent: ${out.get('total_cost_usd', 0):.2f}, {out.get('num_turns')} turns, {out.get('duration_ms', 0) // 1000} s",
          flush=True)
    if r.returncode or out.get("is_error"):
        raise AgentError("the agent failed", f"{out.get('subtype')}: {str(out.get('result'))[-500:]}")
    lines = [l.strip() for l in str(out.get("result", "")).splitlines() if l.strip()]
    return lines[-1] if lines else ""


def open_up(path):
    """The agent runs as our uid, so what it locked is ours to unlock (never through a symlink)."""
    if not os.path.islink(path) and os.path.isdir(path):
        os.chmod(path, 0o700)
        for n in os.listdir(path):
            open_up(os.path.join(path, n))


def remove_tree(path):
    open_up(path)
    shutil.rmtree(path, ignore_errors=True)


def _raise(error):
    raise error


def check_tree(path):
    """What the agent left must be a small, plain directory of files. Returns a problem or an empty string."""
    count = total = 0
    try:
        for dirpath, dirs, files in os.walk(path, onerror=_raise):  # by default walk() skips what it cannot read
            for n in dirs + files:
                p = os.path.join(dirpath, n)
                st = os.lstat(p)
                rel = os.path.relpath(p, path)
                if os.path.islink(p):
                    return f"{rel} is a symlink; symlinks are not allowed"
                if not (os.path.isdir(p) or os.path.isfile(p)):
                    return f"{rel} is not a regular file"
                if os.path.isfile(p):
                    count, total = count + 1, total + st.st_size
                    if st.st_size > MAX_FILE:
                        return f"{rel} is too large ({st.st_size} bytes, at most {MAX_FILE})"
    except OSError as e:
        return f"cannot read the directory: {e}"
    if count > MAX_FILES or total > MAX_TOTAL:
        return f"too many or too large files ({count} files, {total} bytes)"
    for needed in ("Dockerfile", "match", "run"):
        if not os.path.isfile(f"{path}/{needed}"):
            return f"/adapter/{needed} does not exist"
    for script in ("match", "run"):
        os.chmod(f"{path}/{script}", 0o755)  # forgetting chmod is not worth an attempt
    return ""


def accept(adapter, message, adapter_keys):
    """Build it, run match and run on the very message. Returns (problem, transcript)."""
    res = dispatch.match_all([adapter], message, adapter_keys)[0]
    if res.error:
        return f"match failed: {res.error}", None
    if res.score < dispatch.THRESHOLD:
        return f"match printed {res.score} for this very message; it must print at least {dispatch.THRESHOLD}", None
    try:
        return "", dispatch.run(adapter, message, adapter_keys)
    except dispatch.AdapterError as e:
        return f"run failed: {e.reason}. {e.detail}", None


def create(user_id, message, agent_keys, adapter_keys, root=dispatch.ROOT, run_agent=run_claude):
    """A new adapter for the user's message. Returns (adapter, transcript of the message) or raises AgentError."""
    uid = str(int(user_id))
    staging = f"{root}/users/{uid}/.staging/{uuid.uuid4().hex[:12]}"  # hidden from visible(): unfinished
    os.makedirs(staging)
    try:
        with _one_at_a_time:
            failure = ""
            for _ in range(ATTEMPTS):
                name = run_agent(staging, message, failure, agent_keys)
                failure = check_tree(staging)
                if not failure:
                    adapter = dispatch.Adapter(staging, f"users/{uid}/{name}", True)
                    failure, transcript = accept(adapter, message, adapter_keys)
                    if not failure:
                        break
                    sandbox.remove_image(dispatch.image_tag(adapter))  # an image of files we are about to change
            else:
                raise AgentError("the adapter did not pass the acceptance", failure)
        if not (clean := clean_name(name)):
            print(f"agent: unusable adapter name {name!r}", flush=True)
            clean = "adapter_" + uuid.uuid4().hex[:6]
        name = clean
        final, n = f"{root}/users/{uid}/{name}", 1
        while os.path.exists(final):
            n += 1
            final = f"{root}/users/{uid}/{name}_{n}"
        os.rename(staging, final)  # the content (so the image tag) does not change
        return dispatch.Adapter(final, f"users/{uid}/{os.path.basename(final)}", True), transcript
    finally:
        remove_tree(staging)


if __name__ == "__main__":  # self-check: python3 agent.py (needs docker; the "agents" here are fakes)
    import tempfile, time
    root = tempfile.mkdtemp()
    me = f"{os.getuid()}:{os.getgid()}"
    IMG = "python:3.12-slim"
    message = {"message_id": 1, "chat": {"id": 1, "type": "private"}, "from": {"id": 42}, "text": "make me"}

    def images():
        out = subprocess.run(["docker", "images", "-q", "tb-ad-*"], capture_output=True, text=True).stdout
        return sorted(set(out.split()))

    def files(match_ok=True, run_out="made"):
        """The three files of a toy adapter, as a shell script that writes them."""
        return (f"printf 'FROM {IMG}\\nCOPY . /adapter\\n' > Dockerfile; "
                f"printf '#!/usr/local/bin/python\\nimport json, sys\\nm = json.load(sys.stdin)\\n"
                f"print(0.9 if m.get(\"text\") == \"{'make me' if match_ok else 'never'}\" else 0.0)\\n' > match; "
                f"printf '#!/usr/local/bin/python\\nprint(\"{run_out}\")\\n' > run")

    def container_agent(*steps):
        """A fake agent that really runs in a container with the volume, as the real one does; one step per attempt."""
        calls = []

        def agent(staging, message, failure, keys):
            calls.append(failure)
            assert not any(".staging" in a.path for a in dispatch.visible(42, root)), "an unfinished adapter is visible"
            script, name = steps[min(len(calls), len(steps)) - 1]
            r = sandbox.run_sandboxed(IMG, ["sh", "-c", script], mounts={staging: "/adapter"}, user=me, workdir="/adapter")
            assert r.returncode == 0, r.stderr
            return name
        agent.calls = calls
        return agent

    def staging_is_empty():
        d = f"{root}/users/42/.staging"
        return not os.path.exists(d) or os.listdir(d) == []

    images_before = images()
    try:
        # the prompt carries everything the agent needs and nothing unfilled
        p = prompt({"text": "https://tiktok.com/x", "from": {"id": 7}}, "match failed: boom")
        assert "https://tiktok.com/x" in p and "FROM transcribe-base" in p and "/telegram/getFile" in p, "prompt is empty"
        assert "{message_json}" not in p and "{example_files}" not in p and "{failure}" not in p
        assert "previous attempt failed" in p and "match failed: boom" in p and "previous attempt" not in prompt({})

        # the tree checks
        t = tempfile.mkdtemp()
        assert "does not exist" in check_tree(t)
        for n in ("Dockerfile", "match", "run"):
            open(f"{t}/{n}", "w").write("x")
        os.chmod(f"{t}/match", 0o600)
        assert check_tree(t) == "" and os.stat(f"{t}/match").st_mode & 0o111, "not fixed to be executable"
        os.symlink("/etc/passwd", f"{t}/link")
        assert "symlink" in check_tree(t)
        os.remove(f"{t}/link")
        os.mkdir(f"{t}/sub"); os.symlink(".", f"{t}/sub/loop")
        assert "symlink" in check_tree(t)
        os.remove(f"{t}/sub/loop")
        open(f"{t}/big", "wb").write(b"x" * (MAX_FILE + 1))
        assert "too large" in check_tree(t)
        os.remove(f"{t}/big")
        for i in range(MAX_FILES):
            open(f"{t}/sub/f{i}", "w").write("x")
        assert "too many" in check_tree(t)
        shutil.rmtree(t)
        t = tempfile.mkdtemp()
        os.makedirs(f"{t}/locked/deep"); open(f"{t}/locked/deep/f", "w").write("x"); os.chmod(f"{t}/locked/deep", 0o500)
        os.chmod(f"{t}/locked", 0o000)
        assert "cannot read" in check_tree(t)
        remove_tree(t)                                           # a locked directory does not stop the cleanup
        assert not os.path.exists(t)

        # success on the first attempt: files written by a container, accepted, moved to the user's directory
        agent = container_agent((files(), "my_adapter"))
        adapter, text = create(42, message, {}, {}, root, agent)
        assert text == "made" and adapter.name == "users/42/my_adapter" and adapter.own, (adapter, text)
        assert [a.name for a in dispatch.visible(42, root)] == ["users/42/my_adapter"] and staging_is_empty()
        assert sorted(os.listdir(adapter.path)) == ["Dockerfile", "match", "run"], os.listdir(adapter.path)
        assert os.stat(f"{adapter.path}/run").st_uid == os.getuid() and agent.calls == [""]
        assert dispatch.pick(dispatch.match_all([adapter], message)).adapter == adapter   # and it is a normal adapter now

        # names as agents write them
        for raw, expected in (("tiktok_links", "tiktok_links"), ("`tiktok_links`", "tiktok_links"),
                              ("**Tiktok_Links**", "tiktok_links"), ("'youtube_playlist'.", "youtube_playlist"),
                              ("The adapter is `yt_playlists`.", "yt_playlists"), ("Name: tiktok_links", None),
                              ("tiktok-links", None), ("", None), ("1abc", None), ("a" * 31, None)):
            assert clean_name(raw) == expected, (raw, clean_name(raw), expected)

        # the name: a bad one is replaced, a taken one gets a suffix
        a2, _ = create(42, message, {}, {}, root, container_agent((files(), "Not A Name!")))
        assert re.match(r"^adapter_[0-9a-f]{6}$", os.path.basename(a2.path)), a2
        a3, _ = create(42, message, {}, {}, root, container_agent((files(), "my_adapter")))
        assert os.path.basename(a3.path) == "my_adapter_2", a3

        # a failed attempt: the agent is told why and works on the same files
        agent = container_agent((files(match_ok=False), "good_name"),
                                ("grep -q never match && " + files(), "good_name"))   # the second step sees the old files
        a4, text = create(42, message, {}, {}, root, agent)
        assert text == "made" and len(agent.calls) == 2 and agent.calls[0] == "", agent.calls
        assert "match printed 0.0" in agent.calls[1], agent.calls[1]

        # it never gets it right: an honest failure, nothing left behind, nothing visible
        before = sorted(a.name for a in dispatch.visible(42, root))
        agent = container_agent((files(match_ok=False), "x"), ("mkdir d && touch d/f && chmod 500 d && ln -s /etc/passwd l", "x"))
        try:
            create(42, message, {}, {}, root, agent)
            raise AssertionError("an adapter that fails was accepted")
        except AgentError as e:
            assert "acceptance" in e.reason and "symlink" in e.detail, (e.reason, e.detail)
        assert len(agent.calls) == ATTEMPTS and staging_is_empty()
        assert sorted(a.name for a in dispatch.visible(42, root)) == before

        # the agent itself failing is reported, not hidden
        def broken(staging, message, failure, keys):
            raise AgentError("the agent failed", "out of budget")
        try:
            create(42, message, {}, {}, root, broken)
            raise AssertionError("failure of the agent not reported")
        except AgentError as e:
            assert e.reason == "the agent failed" and staging_is_empty()

        # one agent at a time
        running, peak, lock = [0], [0], threading.Lock()

        def slow(staging, message, failure, keys):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.5)
            with lock:
                running[0] -= 1
            raise AgentError("the agent failed")

        def attempt():
            try:
                create(42, message, {}, {}, root, slow)
            except AgentError:
                pass
        threads = [threading.Thread(target=attempt) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak[0] == 1, peak

        # images: only the accepted adapters keep one; failed attempts left none behind
        assert len(set(images()) - set(images_before)) == 1, images()   # the accepted adapters share one content
    finally:
        for a in dispatch.visible(42, root):
            sandbox.remove_image(dispatch.image_tag(a))
        remove_tree(root)
    assert images() == images_before, "images left behind"
    print("ok")
