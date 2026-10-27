"""Telegram bot: python3 bot.py (self-check: python3 bot.py --check).

Message -> the user's own and shared adapters -> match -> best one -> run -> the transcript is the reply.
"""
import json, os, sys, tempfile, time, traceback, urllib.error, urllib.request
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


def handle(message, send, keys=None, root=dispatch.ROOT, allowed=None):
    """One incoming message. send(chat_id, text, reply_to_message_id) delivers the reply."""
    user = message.get("from")
    if not user or user.get("is_bot") or (allowed and user["id"] not in allowed):
        return
    chat, mid = message["chat"]["id"], message["message_id"]
    results = dispatch.match_all(dispatch.visible(user["id"], root), message, keys)
    for r in results:
        if r.error:
            print(f"match {r.adapter.name}: {r.error}", file=sys.stderr, flush=True)
    best = dispatch.pick(results)
    if not best:
        failed = sum(1 for r in results if r.error)
        send(chat, f"Не нашёл подходящий адаптер. Проверено адаптеров: {len(results)}"
                   + (f", из них с ошибкой: {failed}" if failed else "") + ".", mid)
        return
    try:
        send(chat, dispatch.run(best.adapter, message, keys), mid)
    except dispatch.AdapterError as e:
        print(f"run {best.adapter.name}: {e.reason}: {e.detail}", file=sys.stderr, flush=True)
        send(chat, f"Адаптер {best.adapter.name} не смог обработать сообщение: {e.reason}.", mid)


def main():
    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    keys = {k: env[k] for k in sidecar.KEY_NAMES}
    allowed = {int(x) for x in env.get("ALLOWED_USER_IDS", "").split(",") if x.strip()} or None

    def send(chat_id, text, reply_to):
        for part in chunks(text):
            api(token, "sendMessage", {"chat_id": chat_id, "text": part,
                                       "reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}})

    def safe(message):
        try:
            handle(message, send, keys, allowed=allowed)
        except Exception:
            traceback.print_exc()
            try:
                send(message["chat"]["id"], "Что-то пошло не так.", message["message_id"])
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


def check():
    text = "".join(f"line {i}\n" for i in range(1500))
    parts = chunks(text)
    assert all(len(p) <= CHUNK for p in parts) and len(parts) > 1, [len(p) for p in parts]
    assert "\n".join(parts).split() == text.split(), "text lost or reordered"
    assert chunks("short") == ["short"] and chunks("x" * 9000)[0] == "x" * CHUNK

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

    sent = []
    send = lambda chat, text, reply_to: sent.append((chat, text, reply_to))

    def message(text, user=42, **extra):
        return {"message_id": 5, "chat": {"id": 100 + user}, "from": {"id": user}, "text": text, **extra}

    try:
        # no adapters at all: an honest answer, nothing is run
        handle(message("hello"), send, root=root)
        assert sent == [(142, "Не нашёл подходящий адаптер. Проверено адаптеров: 0.", 5)], sent

        adapter("shared/hello", "print(0.9 if m.get('text') == 'hello' else 0.0)", "print('world')")
        adapter("users/42/broken", "print(0.8 if m.get('text') == 'break' else 0.0)",
                "sys.stderr.write('secret-detail'); sys.exit(4)")
        adapter("users/42/bad_match", "raise SystemExit(2)", "print('never')")

        sent.clear()
        handle(message("hello"), send, root=root)
        assert sent == [(142, "world", 5)], sent

        sent.clear()
        handle(message("something else"), send, root=root)
        assert len(sent) == 1 and "Проверено адаптеров: 3, из них с ошибкой: 1" in sent[0][1], sent

        sent.clear()
        handle(message("break"), send, root=root)
        assert len(sent) == 1 and "users/42/broken" in sent[0][1] and "exited with 4" in sent[0][1], sent
        assert "secret-detail" not in sent[0][1], "stderr leaked to the user"

        # another user sees only the shared adapter; strangers, bots and non-users are ignored
        sent.clear()
        handle(message("break", user=7), send, root=root)
        assert len(sent) == 1 and "Проверено адаптеров: 1." in sent[0][1], sent
        sent.clear()
        handle(message("hello", user=8), send, root=root, allowed={42})
        handle({**message("hello"), "from": {"id": 9, "is_bot": True}}, send, root=root)
        handle({"message_id": 1, "chat": {"id": 1}, "text": "channel post"}, send, root=root)
        assert sent == [], sent
    finally:
        import shutil
        for a in dispatch.visible(42, root):
            os.system(f"docker rmi -f {dispatch.image_tag(a)} >/dev/null 2>&1")
        shutil.rmtree(root, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    check() if sys.argv[1:] == ["--check"] else main()
