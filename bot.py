"""Telegram bot: python3 bot.py (self-check: python3 bot.py --check).

Message -> an instant status reply -> the user's own and shared adapters -> match -> best one -> run.
The result is a new message (it notifies), the status message is removed.
"""
import contextlib, json, os, sys, tempfile, threading, time, traceback, urllib.error, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor

import dispatch, sandbox
from sidecar import sidecar

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.telegram.org"
LIMIT = 4000  # Telegram allows 4096 characters per message: a longer text goes as a file


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


def _call(token, method, data, content_type, timeout):
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data, headers={"Content-Type": content_type})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))["result"]
    except urllib.error.HTTPError as e:  # not re-raised as is: the message must never carry the URL with the token
        raise TelegramError(e.code, json.loads(e.read() or b"{}").get("description", ""))


def api(token, method, params=None, timeout=60):
    return _call(token, method, json.dumps(params or {}).encode(), "application/json", timeout)


def upload(token, method, fields, filename, content, timeout=120):
    """multipart/form-data: text fields plus one text file in the `document` field."""
    boundary = uuid.uuid4().hex
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
             "Content-Type: text/plain; charset=utf-8\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return _call(token, method, body, f"multipart/form-data; boundary={boundary}", timeout)


class Telegram:
    def __init__(self, token):
        self.token = token

    def send(self, chat_id, text, reply_to):
        """Replies with the text, or with a file if it does not fit into a message; returns the message id."""
        reply = {"message_id": reply_to, "allow_sending_without_reply": True}
        if len(text) <= LIMIT:
            return api(self.token, "sendMessage", {"chat_id": chat_id, "text": text, "reply_parameters": reply})["message_id"]
        return upload(self.token, "sendDocument",
                      {"chat_id": chat_id, "reply_parameters": json.dumps(reply),
                       "caption": "Текст не поместился в сообщение, прикладываю файлом."},
                      "transcript.txt", text.encode())["message_id"]

    def edit(self, chat_id, message_id, text):
        try:
            api(self.token, "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:LIMIT]})
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


def start(message, tg, allowed=None):
    """The instant part, run by the polling thread before the message waits for a free worker.
    Returns the id of the status message, or None if the message is not for us."""
    user = message.get("from")
    if not user or user.get("is_bot") or message["chat"].get("type") != "private":  # private chats only
        return None
    allow = not allowed or user["id"] in allowed
    print(f"message from {user['id']} @{user.get('username', '-')}" + ("" if allow else " (ignored)"), flush=True)
    return tg.send(message["chat"]["id"], "Ищу подходящий адаптер…", message["message_id"]) if allow else None


def handle(message, progress, tg, keys=None, root=dispatch.ROOT):
    """The rest of one message. progress is the id of the status message that start() sent.
    tg has send(chat, text, reply_to) -> id, edit(chat, id, text), delete(chat, id)."""
    user, chat, mid = message["from"], message["chat"]["id"], message["message_id"]

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

    sandbox.build_base()
    print(f"started as @{api(token, 'getMe')['username']}; "
          + (f"allowed users: {sorted(allowed)}" if allowed else "OPEN to everyone"), flush=True)
    offset = None
    with ThreadPoolExecutor(64) as pool:  # threads are cheap: the container slots in dispatch limit the load
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
                if "message" not in u:
                    continue
                try:
                    progress = start(u["message"], tg, allowed)  # answered here, not when a worker gets free
                except Exception:
                    traceback.print_exc()
                    continue
                if progress:
                    pool.submit(safe, u["message"], progress)


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
    global API
    # the Telegram wrapper against a fake Bot API on localhost: real requests, real multipart encoding
    import email
    from http.server import BaseHTTPRequestHandler, HTTPServer
    requests = []

    class FakeApi(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            ctype = self.headers["Content-Type"]
            if ctype.startswith("multipart/"):
                msg = email.message_from_bytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
                fields = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                          for p in msg.get_payload()}
                filename = [p.get_filename() for p in msg.get_payload() if p.get_filename()]
                requests.append((self.path, fields, filename))
            else:
                requests.append((self.path, json.loads(body), None))
            if requests[-1][1].get("text") == "same":
                code, answer = 400, {"ok": False, "description": "Bad Request: message is not modified"}
            else:
                code, answer = 200, {"ok": True, "result": {"message_id": 100 + len(requests)}}
            out = json.dumps(answer).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_api_url, API = API, f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        tg = Telegram("tok")
        assert tg.send(7, "short", 55) == 101                             # fits: a plain message
        path, body, _ = requests[-1]
        assert path == "/bottok/sendMessage" and body["text"] == "short", requests[-1]
        assert body["reply_parameters"]["message_id"] == 55

        long_text = "".join(f"строка {i}\n" for i in range(3000))          # does not fit: one file, nothing lost
        assert len(long_text) > LIMIT and tg.send(7, long_text, 56) == 102
        path, fields, filename = requests[-1]
        assert path == "/bottok/sendDocument" and filename == ["transcript.txt"], requests[-1]
        assert fields["document"].decode() == long_text, "the file differs from the text"
        assert fields["chat_id"] == b"7" and json.loads(fields["reply_parameters"])["message_id"] == 56
        assert "файлом" in fields["caption"].decode()
        assert tg.send(7, "x" * LIMIT, 57) == 103 and requests[-1][0] == "/bottok/sendMessage"   # exactly at the limit
        assert tg.send(7, "x" * (LIMIT + 1), 57) == 104 and requests[-1][0] == "/bottok/sendDocument"

        tg.edit(7, 101, "same")                                           # "not modified" is not an error
        n = len(requests)
        tg.edit(7, 101, "y" * 9000)
        assert len(requests[-1][1]["text"]) == LIMIT and len(requests) == n + 1
        try:
            Telegram("tok").delete(7, 101)                                # the fake answers ok: just proves the call
            api("tok", "editMessageText", {"text": "same"})
            raise AssertionError("an error answer did not raise")
        except TelegramError as e:
            assert "not modified" in str(e) and "tok" not in str(e)       # and the token is not in the message
    finally:
        API = real_api_url

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
        assert tg.replies() == ["Не нашёл подходящий адаптер. Проверено адаптеров: 0."], tg.ops
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
    finally:
        import shutil
        for a in dispatch.visible(42, root):
            os.system(f"docker rmi -f {dispatch.image_tag(a)} >/dev/null 2>&1")
        shutil.rmtree(root, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    check() if sys.argv[1:] == ["--check"] else main()
