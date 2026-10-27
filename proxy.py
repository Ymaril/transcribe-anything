"""Key-injecting proxy: sandboxed code calls it, never sees real keys.

OpenAI:   POST /openai/v1/audio/transcriptions (only allowed models)
Telegram: /telegram/getFile and /telegram/file/<path> (download only)
No auth: it is reachable only from the internal container network. A runaway container is stopped by its timeout.
"""
import re, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPENAI_MODELS = {b"gpt-4o-transcribe", b"gpt-4o-mini-transcribe", b"whisper-1"}
MAX_BODY = 30 * 1024 * 1024
TG_PATH = re.compile(r"^[A-Za-z0-9_\-./]+$")


class Proxy:
    def __init__(self, openai_key, telegram_token,
                 openai_base="https://api.openai.com", telegram_base="https://api.telegram.org"):
        self.openai_key, self.telegram_token = openai_key, telegram_token
        self.openai_base, self.telegram_base = openai_base, telegram_base

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
            if not TG_PATH.match(rest) or ".." in rest:
                return 400
            return f"{self.telegram_base}/file/bot{self.telegram_token}/{rest}", {}
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
                self.send_response(up.status)
                self.send_header("Content-Type", up.headers.get("Content-Type", "application/octet-stream"))
                self.end_headers()
                while chunk := up.read(65536):
                    self.wfile.write(chunk)

            do_GET = do_POST = handle_any

        return ThreadingHTTPServer((host, port), H)


if __name__ == "__main__":  # self-check: python3 proxy.py
    seen = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def handle_any(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(n)))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"upstream-ok")

        do_GET = do_POST = handle_any

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    base = f"http://127.0.0.1:{fake.server_address[1]}"
    srv = Proxy("sk-real", "123:TG", base, base).server("127.0.0.1", 0)
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
    assert call("GET", "/telegram/getFile?file_id=abc") == (200, b"upstream-ok")
    assert seen[-1][1] == "/bot123:TG/getFile?file_id=abc", seen[-1]
    assert call("GET", "/telegram/file/voice/file_1.ogg") == (200, b"upstream-ok")
    assert seen[-1][1] == "/file/bot123:TG/voice/file_1.ogg", seen[-1]

    n = len(seen)
    assert call("POST", TR, form(b"gpt-4o"), MP)[0] == 403              # model not allowed
    assert call("POST", "/openai/v1/chat/completions", b"{}")[0] == 403
    assert call("POST", "/telegram/sendMessage", b"x")[0] == 403
    assert call("GET", "/telegram/file/../getUpdates")[0] == 400
    assert len(seen) == n, "rejected requests reached upstream"

    print("ok")
