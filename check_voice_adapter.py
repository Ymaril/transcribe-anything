"""Self-check for adapters/shared/telegram_voice: python3 check_voice_adapter.py

The adapter runs in its own image, against a fake Telegram and OpenAI served from inside the container.
"""
import dispatch, sandbox

# runs inside the adapter image
TEST = r'''
import json, os, subprocess, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=2", "-c:a", "libopus", "/tmp/src.oga"], check=True)
audio = open("/tmp/src.oga", "rb").read()
seen, refuse = [], []

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        seen.append(("GET", u.path, u.query))
        if u.path == "/telegram/getFile":
            if refuse:
                return self.reply(400, json.dumps({"ok": False, "description": "Bad Request: file is too big"}).encode())
            return self.reply(200, json.dumps({"ok": True, "result": {"file_path": "voice/file_1.oga"}}).encode())
        if u.path == "/telegram/file/voice/file_1.oga":
            return self.reply(200, audio, "application/octet-stream")
        self.reply(404, b"")
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        seen.append(("POST", self.path, len(body) > 5000))  # transcribe re-encodes: the audio itself is in there
        self.reply(200, json.dumps({"text": "привет из голосового"}).encode())

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
env = {**os.environ, "TB_PROXY": base, "OPENAI_BASE_URL": base + "/openai/v1", "OPENAI_API_KEY": "unused"}

def adapter(cmd, message):
    return subprocess.run([f"/adapter/{cmd}"], input=json.dumps(message), env=env, capture_output=True, text=True)

voice = {"voice": {"file_id": "abc&x=1", "duration": 2}}
assert float(adapter("match", voice).stdout) >= 0.9
for other in ({"text": "hi"}, {"audio": {"file_id": "x"}}, {"photo": []}):
    assert float(adapter("match", other).stdout) == 0.0, other

r = adapter("run", voice)
assert r.returncode == 0 and r.stdout.strip() == "привет из голосового", (r.stdout, r.stderr)
get_file = [s for s in seen if s[1] == "/telegram/getFile"]
assert len(get_file) == 1 and urllib.parse.parse_qs(get_file[0][2]) == {"file_id": ["abc&x=1"]}, seen  # encoded, not injected
assert ("GET", "/telegram/file/voice/file_1.oga", "") in seen, seen
posts = [s for s in seen if s[0] == "POST"]
assert posts == [("POST", "/openai/v1/audio/transcriptions", True)], posts      # the downloaded audio was sent

refuse.append(True)
r = adapter("run", voice)
assert r.returncode != 0 and "file is too big" in r.stderr, (r.returncode, r.stderr)
print("adapter-ok")
'''

sandbox.build_base()
adapter = dispatch.Adapter(f"{dispatch.ROOT}/shared/telegram_voice", "shared/telegram_voice", False)
r = sandbox.run_sandboxed(dispatch.build(adapter), ["python", "-"], TEST, timeout=300)
assert r.returncode == 0 and "adapter-ok" in r.stdout, r.stdout + r.stderr[-1500:]

# and through the dispatcher: sidecar, match protocol, threshold
res = dispatch.match_all([adapter], {"voice": {"file_id": "x"}})[0]
assert (res.score, res.error) == (0.95, ""), res
assert dispatch.pick([res]).adapter is adapter
assert dispatch.pick(dispatch.match_all([adapter], {"text": "hello"})) is None
print("ok")
