"""Self-check for the transcribe-base image: python3 check_base.py (needs internet for the proxy part)."""
import os, tempfile

import sandbox

IMAGE = sandbox.BASE_IMAGE
sandbox.build_base()
d = tempfile.mkdtemp()
os.chmod(d, 0o777)  # container runs as nobody

# offline: chunking, models and formats against a fake OpenAI inside the container
fake = r'''
import json, os, re, subprocess, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

seen = []

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        seen.append((self.path, re.search(rb'name="model"\r\n\r\n([^\r]+)', body).group(1), len(body)))
        out = json.dumps({"text": f"part{len(seen)}"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
env = {**os.environ, "OPENAI_BASE_URL": f"http://127.0.0.1:{srv.server_address[1]}/v1", "OPENAI_API_KEY": "unused"}

def ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", *a], check=True)

def transcribe(path):
    return subprocess.run(["transcribe", path], env=env, capture_output=True, text=True)

ff("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1500", "/work/long.wav")
r = transcribe("/work/long.wav")
assert r.returncode == 0, r.stderr
assert r.stdout.split() == ["part1", "part2", "part3"], r.stdout          # 1500 s -> 3 chunks of 600 s
assert all(p == "/v1/audio/transcriptions" and m == b"gpt-4o-transcribe" and size < 5_000_000
           for p, m, size in seen), seen

ff("-f", "lavfi", "-i", "testsrc=d=3", "-f", "lavfi", "-i", "anullsrc", "-t", "3", "/work/video.mp4")
r = transcribe("/work/video.mp4")
assert r.returncode == 0 and r.stdout.split() == ["part4"], (r.stdout, r.stderr)   # video input works

ff("-f", "lavfi", "-i", "testsrc=d=3", "/work/silent.mp4")
r = transcribe("/work/silent.mp4")
assert r.returncode != 0 and "cannot extract audio" in r.stderr, (r.returncode, r.stderr)   # no audio track
assert len(seen) == 4, "a request was sent for a file without audio"
print("fake-ok")
'''
with open(f"{d}/fake.py", "w") as f:
    f.write(fake)
r = sandbox.run_sandboxed(IMAGE, ["python", "fake.py"], d, timeout=300)
assert r.returncode == 0 and "fake-ok" in r.stdout, r.stdout + r.stderr

# through the real proxy with bogus keys: the SDK request must pass the proxy and reach OpenAI (401)
keys = {"OPENAI_API_KEY": "sk-bogus", "TELEGRAM_BOT_TOKEN": "123:bogus"}
r = sandbox.run_networked(IMAGE, ["sh", "-c", "ffmpeg -v error -f lavfi -i anullsrc -t 2 a.wav && transcribe a.wav"],
                          d, keys=keys)
assert r.returncode != 0 and ("401" in r.stderr or "AuthenticationError" in r.stderr), r.stderr[-500:]
assert "403" not in r.stderr, "the proxy rejected the SDK request: " + r.stderr[-500:]

print("ok")
