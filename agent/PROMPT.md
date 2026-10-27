You build one adapter for a Telegram transcription bot. An adapter is a small program that turns one KIND of Telegram
message into text. You work inside a sandbox; you are done when /adapter (your working directory, it is kept) contains a
working adapter. Nobody can answer questions: decide yourself, be efficient, your budget is limited.

## The message that no adapter could handle

```json
{message_json}
```

Save it to /tmp/message.json to test with.

## What an adapter is

A directory (/adapter) with these files:

- `Dockerfile`: starts with `FROM transcribe-base`, then `COPY . /adapter`, plus `RUN` lines that install whatever you
  need (for example `RUN pip install --no-cache-dir yt-dlp`). The host builds the image from this file alone, with
  network, as root.
- `match`: an executable script. It reads the Telegram message (JSON) on stdin and prints a confidence from 0 to 1 on the
  last line of stdout. It must answer within a few seconds and be cheap: decide from the fields of the message (text,
  entities, kinds of attachments, mime types, domains of links, durations). High (0.8 or more) only for input this adapter
  really handles, 0 for everything else. Never download media in `match`.
- `run`: an executable script. It reads the same JSON on stdin and prints the transcript to stdout: plain UTF-8 text and
  nothing else. Logs and errors go to stderr; on failure exit non-zero with a short reason on stderr.

Both scripts start with a shebang (`#!/usr/local/bin/python` for Python in the base image). They run as an unprivileged
user on a read-only filesystem; the only writable place is /tmp (512 MB). Limits: `match` 10 s, `run` 15 min.

## Make it general

The adapter will be used for every future message of the same KIND, not only this one. `match` must recognise the kind
(any TikTok link, not this exact URL; any video attachment, not this file id) and `run` must not hard-code anything of
this message.

## Environment (the same in your sandbox and when the adapter runs)

- Internet: public internet works. Private networks are blocked. Everything you upload (requests, not downloads) is
  limited to 5 MB per run.
- Telegram files: you have no bot token. Use the proxy at `$TB_PROXY`:
  `GET $TB_PROXY/telegram/getFile?file_id=<file_id>` returns JSON `{"ok": true, "result": {"file_path": "..."}}`, then
  `GET $TB_PROXY/telegram/file/<file_path>` returns the bytes (Telegram serves files up to 20 MB only). See the reference
  adapter below.
- Transcription: the command `transcribe FILE [-l LANG]` takes an audio or video file, splits long recordings itself, uses
  OpenAI through the proxy and prints the text. Use it; do not call OpenAI yourself.
- The base image has Python 3.12, ffmpeg and the `openai` package. Anything else: install it with pip (or apt-get during
  the image build) in the Dockerfile.
- Scratch files go to /tmp.

## How to work

1. Look at the message and decide what KIND of input it is.
2. Write the files in /adapter. Test them for real in this sandbox: `/adapter/match < /tmp/message.json` and
   `/adapter/run < /tmp/message.json`. This sandbox has the same network and proxy. Install the libraries you need here
   with pip as well, and put the same installs into the Dockerfile: the final image is built from the Dockerfile only,
   a missing dependency there is the most common reason for failure.
3. After you finish, the host builds the image from your Dockerfile and runs `match` and `run` on this very message. It
   must pass both: `match` at 0.5 or above, `run` printing a non-empty transcript.
4. Keep it simple and small. No tests, no README, no extra files.
   Be honest in the output: never fail silently. If you can handle only part of the input (a long playlist, a huge
   file) or you skip something, say so in the output itself, in Russian (the users read Russian), for example a last
   line `Транскрибировано 10 из 29 видео, остальные не обработаны (лимит времени).`. Do not invent arbitrary caps such
   as "the first 10 items": the time limit is the only cap. Work through the input in order, check the clock, and stop
   at about 12 minutes (keep a margin below the 15 minute limit), then say how many items were done. If nothing can be done, exit
   non-zero with the reason on stderr, or print a short explanation of why; never print an empty or misleading result.
5. Your final message must be exactly one line: a name for the adapter, lowercase letters, digits and underscores,
   starting with a letter, at most 30 characters (for example `tiktok_links`). Nothing else.

## Reference adapter (it ships with the bot and handles Telegram voice messages)

{example_files}
{failure}
