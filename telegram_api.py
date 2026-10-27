"""A small Telegram Bot API client: JSON calls, a text-file upload, and the few methods the bot needs.
Self-check: python3 telegram_api.py (a fake Bot API on localhost)."""
import json, threading, urllib.error, urllib.request, uuid

API = "https://api.telegram.org"
LIMIT = 4000  # Telegram allows 4096 characters per message: a longer text goes as a file


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code


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
            api(self.token, "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:LIMIT],
                                                "reply_markup": {"inline_keyboard": []}})  # a status has no buttons
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def delete(self, chat_id, message_id):
        api(self.token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def ask(self, chat_id, text, reply_to, buttons):
        """A question with inline buttons [(label, callback_data)] as a reply; returns the message id."""
        return api(self.token, "sendMessage", {
            "chat_id": chat_id, "text": text,
            "reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True},
            "reply_markup": {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}})["message_id"]

    def answer(self, callback_id):
        api(self.token, "answerCallbackQuery", {"callback_query_id": callback_id})  # stops the spinner on the button


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

        assert tg.ask(7, "Продолжить?", 58, [("Да", "f"), ("Нет", "n")]) == 100 + len(requests)   # buttons
        path, body, _ = requests[-1]
        assert path == "/bottok/sendMessage" and body["reply_markup"] == {"inline_keyboard": [[
            {"text": "Да", "callback_data": "f"}, {"text": "Нет", "callback_data": "n"}]]}, body
        assert body["reply_parameters"]["message_id"] == 58
        tg.answer("cb1")
        assert requests[-1][:2] == ("/bottok/answerCallbackQuery", {"callback_query_id": "cb1"}), requests[-1]
        tg.edit(7, 101, "a status")
        assert requests[-1][1]["reply_markup"] == {"inline_keyboard": []}   # editing removes the buttons

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
    print("ok")


if __name__ == "__main__":
    check()
