"""Telegram bot: python3 bot.py (self-check: python3 bot.py --check).

Message -> an instant status reply -> the user's own and shared adapters -> match -> best one -> run.
The result is a new message (it notifies), the status message is removed.
"""
import contextlib, os, sys, tempfile, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

import dispatch, sandbox
from sidecar import sidecar
from telegram_api import Telegram, TelegramError, api

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=f"{HERE}/.env"):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("'\"")
    return {**env, **os.environ}


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


def start(message, tg, allowed=None):
    """The instant part, run by the polling thread before the message waits for a free worker.
    Returns the id of the status message, or None if the message is not for us."""
    user = message.get("from")
    if not user or user.get("is_bot") or message["chat"].get("type") != "private":  # private chats only
        return None
    allow = not allowed or user["id"] in allowed
    print(f"message from {user['id']} @{user.get('username', '-')}" + ("" if allow else " (ignored)"), flush=True)
    return tg.send(message["chat"]["id"], "Ищу подходящий адаптер…", message["message_id"]) if allow else None


NO_AGENT = "Создавать новые адаптеры я пока не умею."
ASK_FOREIGN = ("Не нашёл подходящий адаптер среди ваших и проверенных. {checked}. Могу поискать среди адаптеров других "
               "пользователей. Они не проверены, и даже сама проверка может передать им ваше сообщение и вложения. Проверить?")
BUTTONS = [("Да, проверить", "f"), ("Нет", "n")]
_claimed, _claim_lock = set(), threading.Lock()  # questions already answered: a button can be pressed twice


def handle(message, progress, tg, keys=None, root=dispatch.ROOT, foreign=False):
    """The rest of one message. progress is the id of the status message that start() sent
    (or of the question the user has just answered). foreign=True: the user agreed to try other users' adapters.
    tg has send(chat, text, reply_to) -> id, ask(chat, text, reply_to, buttons) -> id, edit(chat, id, text),
    delete(chat, id)."""
    user, chat, mid = message["from"], message["chat"]["id"], message["message_id"]

    def finish(text, buttons=None):
        tg.ask(chat, text, mid, buttons) if buttons else tg.send(chat, text, mid)
        try:
            tg.delete(chat, progress)
        except Exception:
            pass

    adapters = dispatch.foreign(user["id"], root) if foreign else dispatch.visible(user["id"], root)
    with status(lambda s: tg.edit(chat, progress, s), "Ищу подходящий адаптер"):
        results = dispatch.match_all(adapters, message, keys)
    for r in results:
        if r.error:
            print(f"match {r.adapter.name}: {r.error}", file=sys.stderr, flush=True)
    best = dispatch.pick(results)
    if not best:
        failed = sum(1 for r in results if r.error)
        checked = f"Проверено адаптеров: {len(results)}" + (f", из них с ошибкой: {failed}" if failed else "")
        if foreign:
            return finish(f"Среди адаптеров других пользователей тоже ничего не подошло. {checked}. {NO_AGENT}")
        if dispatch.foreign(user["id"], root):  # there is somebody else's adapter to try, but only with consent
            return finish(ASK_FOREIGN.format(checked=checked), BUTTONS)
        return finish(f"Не нашёл подходящий адаптер. {checked}. {NO_AGENT}")
    try:
        with status(lambda s: tg.edit(chat, progress, s), f"Обрабатываю ({best.adapter.name})"):
            text = dispatch.run(best.adapter, message, keys)
    except dispatch.AdapterError as e:
        print(f"run {best.adapter.name}: {e.reason}: {e.detail}", file=sys.stderr, flush=True)
        return finish(f"Адаптер {best.adapter.name} не смог обработать сообщение: {e.reason}.")
    finish(text)


def on_callback(cb, tg, keys=None, root=dispatch.ROOT, allowed=None):
    """A button under our question. The question is a reply to the user's message, so Telegram hands the original
    message back inside the callback: no state is kept for this."""
    try:
        tg.answer(cb["id"])
    except TelegramError:  # "query is too old" after a downtime: the button still deserves its answer
        pass
    msg, user = cb.get("message") or {}, cb["from"]
    if msg.get("chat", {}).get("type") != "private" or (allowed and user["id"] not in allowed):
        return
    chat, question = msg["chat"]["id"], msg["message_id"]
    original = msg.get("reply_to_message")
    if not original:  # deleted, or too old for Telegram to hand it back
        return tg.edit(chat, question, "Не вижу исходного сообщения. Пришлите его ещё раз.")
    if original.get("from", {}).get("id") != user["id"]:
        return
    with _claim_lock:
        if (chat, question) in _claimed:
            return
        _claimed.add((chat, question))
    if cb.get("data") == "f":
        handle(original, question, tg, keys, root, foreign=True)
    else:
        tg.edit(chat, question, f"Хорошо. {NO_AGENT}")


def main():
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    keys = {k: env[k] for k in sidecar.KEY_NAMES}
    allowed = {int(x) for x in env.get("ALLOWED_USER_IDS", "").split(",") if x.strip()} or None
    tg = Telegram(token)

    def safe(message, progress):
        try:
            handle(message, progress, tg, keys)
        except Exception:
            traceback.print_exc()
            try:
                tg.send(message["chat"]["id"], "Что-то пошло не так.", message["message_id"])
                tg.delete(message["chat"]["id"], progress)
            except Exception:
                pass

    def safe_callback(cb):
        try:
            on_callback(cb, tg, keys, allowed=allowed)
        except Exception:
            traceback.print_exc()

    sandbox.build_base()
    print(f"started as @{api(token, 'getMe')['username']}; "
          + (f"allowed users: {sorted(allowed)}" if allowed else "OPEN to everyone"), flush=True)
    offset = None
    with ThreadPoolExecutor(64) as pool:  # threads are cheap: the container slots in dispatch limit the load
        while True:
            try:
                updates = api(token, "getUpdates", {"timeout": 30, "offset": offset,
                                                    "allowed_updates": ["message", "callback_query"]}, timeout=45)
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
                route(u, tg, allowed, lambda m, p: pool.submit(safe, m, p), lambda cb: pool.submit(safe_callback, cb))


def route(u, tg, allowed, on_message, on_button):
    """One update from getUpdates. on_message(message, progress) and on_button(callback) hand the work to a worker."""
    if "callback_query" in u:
        return on_button(u["callback_query"])
    if "message" in u:
        try:
            progress = start(u["message"], tg, allowed)  # answered here, not when a worker gets free
        except Exception:
            return traceback.print_exc()
        if progress:
            on_message(u["message"], progress)


class FakeTelegram:
    def __init__(self, fail_edit=False):
        self.ops, self.fail_edit, self.n = [], fail_edit, 0

    def send(self, chat, text, reply_to):
        self.n += 1
        self.ops.append(("send", chat, text, reply_to, self.n))
        return self.n

    def ask(self, chat, text, reply_to, buttons):
        self.n += 1
        self.ops.append(("send", chat, text, reply_to, self.n, buttons))
        return self.n

    def answer(self, callback_id):
        self.ops.append(("answer", callback_id))

    def edit(self, chat, mid, text):
        if self.fail_edit:
            raise RuntimeError("flood control")
        self.ops.append(("edit", chat, mid, text))

    def delete(self, chat, mid):
        self.ops.append(("delete", chat, mid))

    def replies(self):  # everything sent after the status message
        return [o[2] for o in self.ops if o[0] == "send"][1:]


def check():
    # routing of updates: no adapters, no docker, only who gets what
    got = []
    private = {"message_id": 5, "chat": {"id": 142, "type": "private"}, "from": {"id": 42}, "text": "hi"}
    route_it = lambda u, tg, allowed=None: route(u, tg, allowed, lambda m, p: got.append(("message", m, p)),
                                                 lambda cb: got.append(("button", cb)))
    tg = FakeTelegram()
    route_it({"update_id": 1, "message": private}, tg)
    assert got == [("message", private, 1)] and len(tg.ops) == 1, (got, tg.ops)   # answered first, then handed over
    got.clear()
    button = {"id": "c1", "from": {"id": 42}, "data": "f"}
    tg = FakeTelegram()
    route_it({"update_id": 2, "callback_query": button}, tg)
    assert got == [("button", button)] and tg.ops == [], (got, tg.ops)              # no instant answer for buttons
    got.clear()
    tg = FakeTelegram()
    for ignored in ({"update_id": 3, "edited_message": private}, {"update_id": 4},
                    {"update_id": 5, "message": {**private, "chat": {"id": 1, "type": "group"}}},
                    {"update_id": 6, "message": {**private, "from": {"id": 9, "is_bot": True}}}):
        route_it(ignored, tg)
    route_it({"update_id": 7, "message": private}, tg, allowed={7})                 # not on the list
    assert got == [] and tg.ops == [], (got, tg.ops)

    class Broken(FakeTelegram):                                                     # Telegram refuses our status
        def send(self, *a):
            raise TelegramError(500, "boom")
    route_it({"update_id": 8, "message": private}, Broken())                        # must not take the polling loop down
    assert got == []

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
        return {"message_id": 5, "chat": {"id": 100 + user, "type": "private"}, "from": {"id": user}, "text": text,
                **extra}

    def process(message, tg, allowed=None):  # what the polling loop and a worker do together
        progress = start(message, tg, allowed)
        if progress:
            handle(message, progress, tg, root=root)

    try:
        # the instant part alone: the status is out before any adapter work is done
        tg = FakeTelegram()
        assert start(message("hello"), tg) == 1 and tg.ops == [("send", 142, "Ищу подходящий адаптер…", 5, 1)], tg.ops

        # no adapters at all: the status comes first, then an honest answer; nothing is run
        tg = FakeTelegram()
        process(message("hello"), tg)
        assert tg.ops[0] == ("send", 142, "Ищу подходящий адаптер…", 5, 1), tg.ops
        assert tg.replies() == ["Не нашёл подходящий адаптер. Проверено адаптеров: 0. " + NO_AGENT], tg.ops
        assert tg.ops[-1] == ("delete", 142, 1), tg.ops              # the status message is removed at the end

        adapter("shared/hello", "print(0.9 if m.get('text') == 'hello' else 0.0)", "print('world')")
        adapter("users/42/broken", "print(0.8 if m.get('text') == 'break' else 0.0)",
                "sys.stderr.write('secret-detail'); sys.exit(4)")
        adapter("users/42/bad_match", "raise SystemExit(2)", "print('never')")

        tg = FakeTelegram()
        process(message("hello"), tg)
        assert tg.replies() == ["world"], tg.ops
        assert ("edit", 142, 1, "Обрабатываю (shared/hello)…") in tg.ops, tg.ops   # the user sees which adapter works

        tg = FakeTelegram(fail_edit=True)                             # cosmetic failures do not cost the result
        process(message("hello"), tg)
        assert tg.replies() == ["world"], tg.ops

        tg = FakeTelegram()
        process(message("something else"), tg)
        assert len(tg.replies()) == 1 and "Проверено адаптеров: 3, из них с ошибкой: 1" in tg.replies()[0], tg.ops

        tg = FakeTelegram()
        process(message("break"), tg)
        (reply,) = tg.replies()
        assert "users/42/broken" in reply and "exited with 4" in reply and "secret-detail" not in reply, reply

        # another user sees only the shared adapter; strangers, bots and non-users are ignored without any reply
        tg = FakeTelegram()
        process(message("break", user=7), tg)
        assert "Проверено адаптеров: 1." in tg.replies()[0], tg.ops
        tg = FakeTelegram()
        process(message("hello", user=8), tg, allowed={42})
        process({**message("hello"), "from": {"id": 9, "is_bot": True}}, tg)
        process({"message_id": 1, "chat": {"id": 1, "type": "channel"}, "text": "channel post"}, tg)
        process({**message("hello"), "chat": {"id": -5, "type": "group"}}, tg)   # groups: private chats only
        assert tg.ops == [], tg.ops

        # other users' adapters are tried only with the user's consent
        adapter("users/7/stranger", "print(0.9 if m.get('text') == 'stranger-only' else 0.0)", "print('from stranger')")
        sent = lambda tg: [o[2] for o in tg.ops if o[0] == "send"]
        tg = FakeTelegram()
        process(message("stranger-only"), tg)
        (ask,) = [o for o in tg.ops if o[0] == "send" and len(o) == 6]
        assert "Могу поискать среди адаптеров других пользователей" in ask[2] and "не проверены" in ask[2], ask
        assert ask[3] == 5 and [d for _, d in ask[5]] == ["f", "n"], ask      # a reply to the user's message
        assert tg.ops[-1] == ("delete", 142, 1), tg.ops                       # the status is gone, the question stays
        assert not [o for o in tg.ops if o[0] == "edit" and "stranger" in o[3]], "the stranger ran without consent"

        def pressed(data, user=42, original=None, question=9, cb_id="c1", chat_type="private"):
            return {"id": cb_id, "from": {"id": user}, "data": data,
                    "message": {"message_id": question, "chat": {"id": 142, "type": chat_type},
                                **({"reply_to_message": original} if original else {})}}

        original = message("stranger-only")
        tg = FakeTelegram()
        on_callback(pressed("f", original=original), tg, root=root)
        assert sent(tg) == ["from stranger"], tg.ops
        assert tg.ops[0] == ("answer", "c1") and tg.ops[-1] == ("delete", 142, 9), tg.ops   # the question became the status
        assert ("edit", 142, 9, "Обрабатываю (users/7/stranger)…") in tg.ops, tg.ops
        n = len(tg.ops)
        on_callback(pressed("f", original=original), tg, root=root)           # the same button pressed twice
        assert [o for o in tg.ops[n:] if o[0] != "answer"] == [], tg.ops[n:]

        class Late(FakeTelegram):                                             # the click was made while we were down
            def answer(self, callback_id):
                raise TelegramError(400, "Bad Request: query is too old and response timeout expired")

        tg = Late()
        on_callback(pressed("f", original=message("stranger-only"), question=16), tg, root=root)
        assert sent(tg) == ["from stranger"], tg.ops                          # and the button still works

        tg = FakeTelegram()
        on_callback(pressed("n", original=original, question=10), tg, root=root)
        assert tg.ops == [("answer", "c1"), ("edit", 142, 10, "Хорошо. " + NO_AGENT)], tg.ops   # nothing runs

        tg = FakeTelegram()
        on_callback(pressed("f", question=11), tg, root=root)                 # the original message is gone
        assert len(tg.ops) == 2 and "Не вижу исходного сообщения" in tg.ops[1][3], tg.ops

        tg = FakeTelegram()
        on_callback(pressed("f", original=message("stranger-only"), question=12, user=99), tg, root=root)
        on_callback(pressed("f", original=original, question=13), tg, root=root, allowed={7})
        on_callback(pressed("f", original=original, question=14, chat_type="group"), tg, root=root)
        assert tg.ops == [("answer", "c1")] * 3, tg.ops                       # not his question / not allowed / group

        tg = FakeTelegram()
        on_callback(pressed("f", original=message("nobody"), question=15), tg, root=root)
        assert sent(tg) == ["Среди адаптеров других пользователей тоже ничего не подошло. Проверено адаптеров: 1. "
                            + NO_AGENT], tg.ops
    finally:
        import shutil
        for a in dispatch.visible(42, root) + dispatch.foreign(42, root):
            os.system(f"docker rmi -f {dispatch.image_tag(a)} >/dev/null 2>&1")
        shutil.rmtree(root, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    check() if sys.argv[1:] == ["--check"] else main()
