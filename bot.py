"""Telegram bot: python3 bot.py (self-check: python3 bot.py --check).

Message -> an instant status reply -> the user's own and shared adapters -> match -> best one -> run.
The result is a new message (it notifies), the status message is removed.
"""
import contextlib, json, os, sys, tempfile, threading, time, traceback, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

import dispatch, sandbox
from sidecar import sidecar

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK = 4000  # Telegram allows 4096 characters per message


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code


def load_env(path=f"{HERE}/.env"):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("'\"")
    return {**env, **os.environ}


def api(token, method, params=None, timeout=60):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(params or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))["result"]
    except urllib.error.HTTPError as e:  # not re-raised as is: the message must never carry the URL with the token
        raise TelegramError(e.code, json.loads(e.read() or b"{}").get("description", ""))


def chunks(text, size=CHUNK):
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text]


class Telegram:
    def __init__(self, token):
        self.token = token

    def send(self, chat_id, text, reply_to):
        """Replies with text (split if long); returns the id of the first message."""
        ids = [api(self.token, "sendMessage",
                   {"chat_id": chat_id, "text": part,
                    "reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}})["message_id"]
               for part in chunks(text)]
        return ids[0]

    def edit(self, chat_id, message_id, text):
        try:
            api(self.token, "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:CHUNK]})
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def delete(self, chat_id, message_id):
        api(self.token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})


@contextlib.contextmanager
def status(update, text, every=5):
    """Shows `text` with the elapsed time while the block runs. update() may fail: progress is only cosmetic."""
    done, t0 = threading.Event(), time.time()

    def show(s):
        try:
            update(s)
        except Exception:
            pass

    def tick():
        while not done.wait(every):
            s = int(time.time() - t0)
            show(f"{text}… {s // 60}:{s % 60:02d}")

    show(f"{text}…")
    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    try:
        yield
    finally:
        done.set()
        ticker.join(10)


def handle(message, tg, keys=None, root=dispatch.ROOT, allowed=None):
    """One incoming message. tg has send(chat, text, reply_to) -> id, edit(chat, id, text), delete(chat, id)."""
    user = message.get("from")
    if not user or user.get("is_bot") or (allowed and user["id"] not in allowed):
        return
    chat, mid = message["chat"]["id"], message["message_id"]
    progress = tg.send(chat, "Ищу подходящий адаптер…", mid)  # first of all: the user must see that we are alive

    def finish(text):
        tg.send(chat, text, mid)
        try:
            tg.delete(chat, progress)
        except Exception:
            pass

    with status(lambda s: tg.edit(chat, progress, s), "Ищу подходящий адаптер"):
        results = dispatch.match_all(dispatch.visible(user["id"], root), message, keys)
    for r in results:
        if r.error:
            print(f"match {r.adapter.name}: {r.error}", file=sys.stderr, flush=True)
    best = dispatch.pick(results)
    if not best:
        failed = sum(1 for r in results if r.error)
        return finish(f"Не нашёл подходящий адаптер. Проверено адаптеров: {len(results)}"
                      + (f", из них с ошибкой: {failed}" if failed else "") + ".")
    try:
        with status(lambda s: tg.edit(chat, progress, s), f"Обрабатываю ({best.adapter.name})"):
            text = dispatch.run(best.adapter, message, keys)
    except dispatch.AdapterError as e:
        print(f"run {best.adapter.name}: {e.reason}: {e.detail}", file=sys.stderr, flush=True)
        return finish(f"Адаптер {best.adapter.name} не смог обработать сообщение: {e.reason}.")
    finish(text)


def main():
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    keys = {k: env[k] for k in sidecar.KEY_NAMES}
    allowed = {int(x) for x in env.get("ALLOWED_USER_IDS", "").split(",") if x.strip()} or None
    tg = Telegram(token)

    def safe(message):
        try:
            handle(message, tg, keys, allowed=allowed)
        except Exception:
            traceback.print_exc()
            try:
                tg.send(message["chat"]["id"], "Что-то пошло не так.", message["message_id"])
            except Exception:
                pass

    sandbox.build_base()
    print(f"started as @{api(token, 'getMe')['username']}; "
          + (f"allowed users: {sorted(allowed)}" if allowed else "OPEN to everyone"), flush=True)
    offset = None
    with ThreadPoolExecutor(4) as pool:
        while True:
            try:
                updates = api(token, "getUpdates", {"timeout": 30, "offset": offset, "allowed_updates": ["message"]}, timeout=45)
            except TelegramError as e:
                if e.code in (401, 409):  # bad token, or a webhook / another poller owns this bot
                    raise
                print(f"getUpdates: {e}", file=sys.stderr, flush=True)
                time.sleep(5)
                continue
            except OSError as e:
                print(f"getUpdates: {type(e).__name__}", file=sys.stderr, flush=True)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    pool.submit(safe, u["message"])


class FakeTelegram:
    def __init__(self, fail_edit=False):
        self.ops, self.fail_edit, self.n = [], fail_edit, 0

    def send(self, chat, text, reply_to):
        self.n += 1
        self.ops.append(("send", chat, text, reply_to, self.n))
        return self.n

    def edit(self, chat, mid, text):
        if self.fail_edit:
            raise RuntimeError("flood control")
        self.ops.append(("edit", chat, mid, text))

    def delete(self, chat, mid):
        self.ops.append(("delete", chat, mid))

    def replies(self):  # everything sent after the status message
        return [o[2] for o in self.ops if o[0] == "send"][1:]


def check():
    global api
    text = "".join(f"line {i}\n" for i in range(1500))
    parts = chunks(text)
    assert all(len(p) <= CHUNK for p in parts) and len(parts) > 1, [len(p) for p in parts]
    assert "\n".join(parts).split() == text.split(), "text lost or reordered"
    assert chunks("short") == ["short"] and chunks("x" * 9000)[0] == "x" * CHUNK

    # the Telegram wrapper, against a fake API
    calls, real_api = [], api
    def fake_api(token, method, params=None, timeout=60):
        calls.append((method, params))
        if method == "editMessageText" and params["text"] == "same":
            raise TelegramError(400, "Bad Request: message is not modified")
        if method == "editMessageText" and params["text"] == "boom":
            raise TelegramError(429, "Too Many Requests")
        return {"message_id": 100 + len(calls)}
    api = fake_api
    try:
        tg = Telegram("tok")
        assert tg.send(7, "x" * 9000, 55) == 101 and [c[0] for c in calls] == ["sendMessage"] * 3
        assert all(c[1]["reply_parameters"]["message_id"] == 55 for c in calls)
        tg.edit(7, 101, "same")                                   # "not modified" is not an error
        try:
            tg.edit(7, 101, "boom")
            raise AssertionError("a real error was swallowed")
        except TelegramError:
            pass
        tg.edit(7, 101, "y" * 9000)
        assert len(calls[-1][1]["text"]) == CHUNK
    finally:
        api = real_api

    # the status ticker
    seen = []
    with status(seen.append, "Работаю", every=0.05):
        time.sleep(0.35)
    n = len(seen)
    assert seen[0] == "Работаю…" and n >= 4 and seen[-1] == "Работаю… 0:00", seen
    time.sleep(0.2)
    assert len(seen) == n, "the ticker kept running after the block"
    with status(lambda s: 1 / 0, "Падает", every=0.05):          # a failing update must not break the block
        time.sleep(0.12)

    root = tempfile.mkdtemp()

    def adapter(path, match_code, run_code):
        d = f"{root}/{path}"
        os.makedirs(d)
        with open(f"{d}/Dockerfile", "w") as f:
            f.write("FROM python:3.12-slim\nCOPY . /adapter\n")
        for name, code in (("match", match_code), ("run", run_code)):
            with open(f"{d}/{name}", "w") as f:
                f.write("#!/usr/local/bin/python\nimport json, sys\nm = json.load(sys.stdin)\n" + code)
            os.chmod(f"{d}/{name}", 0o755)

    def message(text, user=42, **extra):
        return {"message_id": 5, "chat": {"id": 100 + user}, "from": {"id": user}, "text": text, **extra}

    try:
        # no adapters at all: the status comes first, then an honest answer; nothing is run
        tg = FakeTelegram()
        handle(message("hello"), tg, root=root)
        assert tg.ops[0] == ("send", 142, "Ищу подходящий адаптер…", 5, 1), tg.ops
        assert tg.replies() == ["Не нашёл подходящий адаптер. Проверено адаптеров: 0."], tg.ops
        assert tg.ops[-1] == ("delete", 142, 1), tg.ops              # the status message is removed at the end

        adapter("shared/hello", "print(0.9 if m.get('text') == 'hello' else 0.0)", "print('world')")
        adapter("users/42/broken", "print(0.8 if m.get('text') == 'break' else 0.0)",
                "sys.stderr.write('secret-detail'); sys.exit(4)")
        adapter("users/42/bad_match", "raise SystemExit(2)", "print('never')")

        tg = FakeTelegram()
        handle(message("hello"), tg, root=root)
        assert tg.replies() == ["world"], tg.ops
        assert ("edit", 142, 1, "Обрабатываю (shared/hello)…") in tg.ops, tg.ops   # the user sees which adapter works

        tg = FakeTelegram(fail_edit=True)                             # cosmetic failures do not cost the result
        handle(message("hello"), tg, root=root)
        assert tg.replies() == ["world"], tg.ops

        tg = FakeTelegram()
        handle(message("something else"), tg, root=root)
        assert len(tg.replies()) == 1 and "Проверено адаптеров: 3, из них с ошибкой: 1" in tg.replies()[0], tg.ops

        tg = FakeTelegram()
        handle(message("break"), tg, root=root)
        (reply,) = tg.replies()
        assert "users/42/broken" in reply and "exited with 4" in reply and "secret-detail" not in reply, reply

        # another user sees only the shared adapter; strangers, bots and non-users are ignored without any reply
        tg = FakeTelegram()
        handle(message("break", user=7), tg, root=root)
        assert "Проверено адаптеров: 1." in tg.replies()[0], tg.ops
        tg = FakeTelegram()
        handle(message("hello", user=8), tg, root=root, allowed={42})
        handle({**message("hello"), "from": {"id": 9, "is_bot": True}}, tg, root=root)
        handle({"message_id": 1, "chat": {"id": 1}, "text": "channel post"}, tg, root=root)
        assert tg.ops == [], tg.ops
    finally:
        import shutil
        for a in dispatch.visible(42, root):
            os.system(f"docker rmi -f {dispatch.image_tag(a)} >/dev/null 2>&1")
        shutil.rmtree(root, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    check() if sys.argv[1:] == ["--check"] else main()
