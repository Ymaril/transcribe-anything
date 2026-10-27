"""Key-injecting proxy: sandboxed code calls it, never sees real keys.

OpenAI:   POST /openai/v1/audio/transcriptions (only allowed models)
Telegram: /telegram/getFile and /telegram/file/<path> (download only). Telegram numbers files sequentially, so a
          real file_path could be guessed to reach the neighbours' files. The file_path in the getFile answer is
          therefore replaced by an opaque token (encrypted and signed with a key that lives in this process only);
          only such a token can be downloaded. Nothing is stored, the shape of the Telegram API is unchanged.
No auth: it listens on the loopback of the sidecar's network namespace, which only the sandboxed container
of the same run shares. A runaway container is stopped by its timeout.
"""
import base64, hashlib, hmac, json, os, re, sys, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPENAI_MODELS = {b"gpt-4o-transcribe", b"gpt-4o-mini-transcribe", b"whisper-1"}
MAX_BODY = 30 * 1024 * 1024
TG_PATH = re.compile(r"^[A-Za-z0-9_\-./]+$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")


class Proxy:
    def __init__(self, openai_key, telegram_token,
                 openai_base="https://api.openai.com", telegram_base="https://api.telegram.org"):
        self.openai_key, self.telegram_token = openai_key, telegram_token
        self.openai_base, self.telegram_base = openai_base, telegram_base
        master = os.urandom(32)  # lives in memory only: tokens are valid for this proxy (this run) and nowhere else
        self.enc_key = hmac.new(master, b"enc", hashlib.sha256).digest()
        self.mac_key = hmac.new(master, b"mac", hashlib.sha256).digest()

    def _stream(self, nonce, n):  # a keystream from HMAC-SHA256 (a PRF), used like a one-time pad per nonce
        out, i = b"", 0
        while len(out) < n:
            out += hmac.new(self.enc_key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
            i += 1
        return out[:n]

    def _tag(self, nonce, ct):
        return hmac.new(self.mac_key, nonce + ct, hashlib.sha256).digest()[:16]

    def seal(self, text):
        """An opaque token for text: encrypt-then-MAC, a fresh nonce every time."""
        nonce, data = os.urandom(12), text.encode()
        ct = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        return base64.urlsafe_b64encode(nonce + ct + self._tag(nonce, ct)).decode().rstrip("=")

    def unseal(self, token):
        """The text of a token made by this proxy, or None if it is not ours or was changed."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except ValueError:
            return None
        if len(raw) < 28:
            return None
        nonce, ct, tag = raw[:12], raw[12:-16], raw[-16:]
        if not hmac.compare_digest(tag, self._tag(nonce, ct)):
            return None
        return bytes(a ^ b for a, b in zip(ct, self._stream(nonce, len(ct)))).decode()

    def _hide_path(self, answer):
        try:
            obj = json.loads(answer)
            obj["result"]["file_path"] = self.seal(obj["result"]["file_path"])
        except (ValueError, KeyError, TypeError):  # an error answer has no path
            return answer
        return json.dumps(obj).encode()

    def _route(self, method, path, body):  # -> (url, headers) or error status
        path, _, query = path.partition("?")
        if path == "/openai/v1/audio/transcriptions" and method == "POST":
            m = re.search(rb'name="model"\r\n\r\n([^\r]+)\r\n', body)
            if not m or m.group(1) not in OPENAI_MODELS:
                return 403
            return (self.openai_base + path[len("/openai"):],
                    {"Authorization": f"Bearer {self.openai_key}"})
        if path == "/telegram/getFile":
            return f"{self.telegram_base}/bot{self.telegram_token}/getFile?{query}", {}
        if path.startswith("/telegram/file/"):
            rest = path[len("/telegram/file/"):]
            if ".." in rest:
                return 400
            real = self.unseal(rest) if TOKEN.match(rest) else None
            if real is None or not TG_PATH.match(real):
                return 403
            return f"{self.telegram_base}/file/bot{self.telegram_token}/{real}", {}
        return 403

    def server(self, host, port):
        proxy = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def reply(self, code, body=b"", ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self.reply(413)
                body = self.rfile.read(n) if n else b""
                r = proxy._route(self.command, self.path, body)
                if isinstance(r, int):
                    return self.reply(r)
                url, headers = r
                if ct := self.headers.get("Content-Type"):
                    headers["Content-Type"] = ct
                req = urllib.request.Request(url, data=body or None, headers=headers, method=self.command)
                try:
                    up = urllib.request.urlopen(req, timeout=300)
                except urllib.error.HTTPError as e:
                    up = e
                except Exception:  # no details: the error text may contain the upstream URL with the bot token
                    return self.reply(502)
                hide = self.path.partition("?")[0] == "/telegram/getFile"
                answer = proxy._hide_path(up.read(1 << 20)) if hide else None
                self.send_response(up.status)
                self.send_header("Content-Type", up.headers.get("Content-Type", "application/octet-stream"))
                self.end_headers()
                if hide:
                    return self.wfile.write(answer)
                while chunk := up.read(65536):
                    self.wfile.write(chunk)

            do_GET = do_POST = handle_any

        return ThreadingHTTPServer((host, port), H)


if __name__ == "__main__" and sys.argv[1:] == ["serve"]:  # sidecar entry point: keys come from the environment
    Proxy(os.environ["OPENAI_API_KEY"], os.environ["TELEGRAM_BOT_TOKEN"]).server("127.0.0.1", 8080).serve_forever()
elif __name__ == "__main__":  # self-check: python3 sidecar/proxy.py
    seen = []
    REAL = {"file_id": "abc", "file_unique_id": "u1", "file_size": 1234, "file_path": "voice/file_1.ogg"}
    GETFILE = json.dumps({"ok": True, "result": REAL}).encode()
    ERROR = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: file is too big"}).encode()

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def handle_any(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(n)))
            refused = "file_id=big" in self.path
            self.send_response(400 if refused else 200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(ERROR if refused else GETFILE if "/getFile" in self.path else b"upstream-ok")

        do_GET = do_POST = handle_any

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    base = f"http://127.0.0.1:{fake.server_address[1]}"
    proxy = Proxy("sk-real", "123:TG", base, base)
    srv = proxy.server("127.0.0.1", 0)
    for s in (fake, srv):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None, ctype=None):
        h = {"Content-Type": ctype} if ctype else {}
        try:
            r = urllib.request.urlopen(urllib.request.Request(url + path, data=body, headers=h, method=method))
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def form(model):
        return (b'--B\r\nContent-Disposition: form-data; name="model"\r\n\r\n' + model +
                b'\r\n--B\r\nContent-Disposition: form-data; name="file"; filename="a.ogg"\r\n\r\nDATA\r\n--B--\r\n')

    MP, TR = "multipart/form-data; boundary=B", "/openai/v1/audio/transcriptions"

    assert call("POST", TR, form(b"whisper-1"), MP) == (200, b"upstream-ok")
    assert seen[-1][:3] == ("POST", "/v1/audio/transcriptions", "Bearer sk-real"), seen[-1]
    # getFile: the same shape of answer, but file_path is an opaque token; the real path never reaches the sandbox
    code, answer = call("GET", "/telegram/getFile?file_id=abc")
    got = json.loads(answer)
    token = got["result"]["file_path"]
    assert code == 200 and seen[-1][1] == "/bot123:TG/getFile?file_id=abc", seen[-1]
    assert got["ok"] is True and {**got["result"], "file_path": 0} == {**REAL, "file_path": 0}, got   # rest untouched
    assert "voice" not in token and "file_1" not in token and TOKEN.match(token), token
    assert call("GET", "/telegram/file/" + token) == (200, b"upstream-ok")
    assert seen[-1][1] == "/file/bot123:TG/voice/file_1.ogg", seen[-1]
    token2 = json.loads(call("GET", "/telegram/getFile?file_id=abc")[1])["result"]["file_path"]
    assert token2 != token and call("GET", "/telegram/file/" + token2)[0] == 200   # a fresh nonce every time
    assert call("GET", "/telegram/getFile?file_id=big") == (400, ERROR)            # Telegram's errors pass unchanged

    # nothing but a token made by this proxy can be downloaded
    n = len(seen)
    middle = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]
    for bad in ("voice/file_1.ogg", "voice/file_2.ogg", "file_1.ogg", middle, token[:-6] + "AAAAAA", token[:-1], "x", ""):
        assert call("GET", "/telegram/file/" + bad)[0] == 403, bad
    other = Proxy("k", "t", base, base)                                  # another run has another key
    assert other.unseal(token) is None and proxy.unseal(token) == "voice/file_1.ogg"
    for text in ("", "a", "voice/file_1.ogg", "путь/файл_1.oga", "x" * 100):
        assert proxy.unseal(proxy.seal(text)) == text and other.unseal(proxy.seal(text)) is None, text
    assert len(seen) == n, "a rejected download reached upstream"

    n = len(seen)
    assert call("POST", TR, form(b"gpt-4o"), MP)[0] == 403              # model not allowed
    assert call("POST", "/openai/v1/chat/completions", b"{}")[0] == 403
    assert call("POST", "/telegram/sendMessage", b"x")[0] == 403
    assert call("GET", "/telegram/file/../getUpdates")[0] == 400
    assert len(seen) == n, "rejected requests reached upstream"

    print("ok")
