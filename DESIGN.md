# Transcribe bot: design

A Telegram bot you can send anything to (a voice message, a video, a link, a file, text) and get a transcript back.
For every new kind of input an agent writes an adapter by itself. Nothing is stored except the adapters.

## Code structure

Layers, dependencies only go downwards:

- `bot.py`: the conversation flow and startup (configuration from `.env`, Telegram polling, status, buttons);
- `telegram_api.py`: a Bot API client (JSON calls, sending a file); knows nothing about adapters;
- `agent.py` + `agent/`: the agent (the last resort): runs Claude Code, checks the files, acceptance;
  the bot knows it only as the function `create_adapter(user_id, message)`; without the agent everything else works;
- `dispatch.py`: adapters: discovery, image build (policy), `match`, choice, `run`, global slots;
- `sandbox.py`: runs containers as utilities (stdin/stdout) and everything that knows docker for images;
- `sidecar/`: network and keys: `iptables` rules, the proxy (`proxy.py`), `up`/`down`, the environment contract (`env()`);
- `adapters/`: `base/` (the base image), `shared/` (common ones), `users/<id>/` (personal ones).

Every module checks itself (`python3 module.py`, for the bot `python3 bot.py --check`); `./check_all.sh` runs all the
checks one after another and also makes sure that no containers are left behind.

## Input

The only input of the whole system is a Telegram `Message` object (JSON). There is no notion of a "file" at the
level of the bot. The adapter itself downloads attachments by `file_id`.

## Adapter

A directory with sources:

- `Dockerfile`: dependencies are installed when the image is built (the image is built lazily on first use; the host
  names the tag after the hash of the directory content, so a changed or replaced adapter is rebuilt).
- The image must contain executable `/adapter/match` and `/adapter/run` (usually `COPY . /adapter`).
- They are utilities: the message (JSON) arrives on stdin, nothing is mounted, temporary files go to `/tmp` (tmpfs).
- `match` prints a number 0..1 to stdout (confidence; the last non-empty line is taken). Timeout 10 s, output up to
  64 KB, any error (a crash, a timeout, not a number) means a score of 0.
- `run` prints the transcript to stdout: non-empty, up to 2 MB. stderr goes to the log (the last 4 KB are kept).
  The output is read with a size limit so that an adapter that prints endlessly cannot eat the host's memory.
  A failed run (a crash, a timeout, a broken contract) is an `AdapterError`: a short reason for the user and
  details for the log.

One image, two entry points. `match` and `run` may use the network (to download an attachment, to check a link).

The base image `transcribe-base` (python:slim, ffmpeg, an unprivileged user, `WORKDIR /tmp`) contains the utility
`transcribe <file>`: it sends audio to OpenAI through the proxy (it splits and compresses files over the 25 MB limit)
and prints the text. Adapters are built `FROM transcribe-base` and install their specific dependencies (yt-dlp etc.)
themselves. The security restrictions are set by the run flags (`run_sandboxed`), not by the image.

Location:

- `adapters/shared/<name>/`: common ones, verified by definition. The owner moves them in by hand.
- `adapters/users/<user_id>/<name>/`: personal ones. Created by the agent.

`match` is only a hint. Technically any adapter can be run; a button in the bot runs the chosen one without `match`.

## Flow

1. A message arrives.
2. `match` over the user's own and the shared adapters (all at once, timeout ~10 s). How many containers really run
   at the same time is decided by global slots shared by all messages: 3 for `match` and 4 for `run` (separate, so
   that a long transcription does not keep new messages from finding an adapter). On equal scores the user's own
   adapter beats a shared one.
3. A winner above the threshold: `run` it and reply to the user.
4. Nothing fits: the bot replies to the message with a question and buttons: "No suitable adapter among yours and
   the verified ones. I can look among other users' adapters. They are not verified, and even matching may hand them
   your message and attachments. Check?"
   - Yes: `match` over other users' personal adapters, the winner is run at once. Nothing fits: step 5.
   - No: step 5.
   If there are no other users' adapters at all, the question is not asked (there is nothing to ask about) and the
   bot goes straight to step 5.
5. The last resort: the agent creates a new adapter in `adapters/users/<user_id>/`, then it runs. The agent is a
   branch: the rest of the system (adapters, choice, running, consent) works without it and knows nothing about it.
   Without the agent the bot honestly answers "I cannot create new adapters yet."
6. The adapter failed: the user gets a short error. The adapter stays as it is, nothing is repaired.

The buttons work without state: the question is sent as a reply to the original message, and the original is taken
from `callback_query.message.reply_to_message`. `callback_data` is a short identifier (the limit is 64 bytes). If
the original is unavailable (deleted or too old), the bot asks to send it again.

## Bot

- Private chats only (groups and channels are ignored). A list of allowed users is optional (`ALLOWED_USER_IDS` in
  `.env`); the id of everyone who writes is printed to the console.
- The instant reply "Looking for a suitable adapter…" is sent by the polling thread, before the message is queued
  for processing (otherwise, with all workers busy, the user would get nothing at all). There are many workers
  (threads are cheap); the load is limited by the container slots.
- The status is one message that is edited: the stage, the adapter name, the elapsed time (every 5 s). There is no
  real progress from an adapter; this is an honest "working, this much time has passed". A failure to edit the
  status does not affect the result.
- The result arrives as a new message (an edit does not notify), the status is deleted.
- The question about other users' adapters is a new message, a reply to the user's message, with the buttons
  "Yes, check" / "No". A press arrives as a `callback_query`, inside which Telegram returns the original message
  (`reply_to_message`), so the bot stores nothing. The question message becomes the status and is deleted after the
  answer. Protection: only the author of the original message can press, a repeated press is ignored (already
  answered questions are remembered in the process memory), a vanished original gives "please send it again".
- A text longer than 4000 characters is not cut into messages (a long series runs into Telegram's rate limits and
  clutters the chat): it is sent as one file, `transcript.txt`, with a caption.

## Sandbox

Everything foreign runs in disposable docker containers: read-only, `--cap-drop ALL`, `no-new-privileges`, limits on
memory/CPU/processes, a timeout, an unprivileged user. Later: gVisor (`--runtime=runsc`).

Network. For every container run a sidecar is started (`sidecar/`): a container with `NET_ADMIN` that owns the network
namespace. The adapter container joins it (`--network container:<sidecar>`) without privileges and cannot change the
rules. No host configuration is needed. The rules (iptables inside the sidecar, `sidecar/rules.sh`):

- the internet and DNS are open (any protocol, UDP included), private ranges are closed: the host, the LAN,
  neighbouring containers (SSRF protection);
- a limit on outgoing bytes per run (5 MB by default): a lot can be downloaded, only crumbs can be carried out.
  A download costs ~0.5% of its size in ACK packets;
- IPv6 is disabled.

There are no keys inside the adapter container. The proxy (`sidecar/proxy.py`) runs inside the sidecar on
`127.0.0.1:8080` and adds the OpenAI and Telegram keys (downloading attachments); only the agent's sidecar also gets
the Anthropic route and key. The proxy's traffic (uid 10001) is excluded from the quota and the filter: it goes only
to trusted APIs. The adapter container receives `OPENAI_BASE_URL`, `OPENAI_API_KEY=unused`, `ANTHROPIC_BASE_URL`,
`ANTHROPIC_API_KEY=unused`, `TB_PROXY`.

The residual risk is accepted: a leak is not zero but it is bounded by the outgoing byte limit.

The proxy must not be a back door to the keys:

- Telegram: only `getFile` and downloading a file. The other Bot API methods (sendMessage etc.) are closed.
  Telegram numbers files sequentially (`voice/file_41`, `42`, ...; checked on stickers: the next number can be
  guessed exactly, and a path can be downloaded right after someone's `getFile`), so a real path through the proxy
  is dangerous: a malicious adapter would try the neighbouring numbers and take other users' files. The shape of
  the API is kept (`file_id` -> `getFile` -> `file_path` -> download), but `file_path` in the `getFile` answer is
  replaced with an opaque token: it is encrypted and signed with a key that exists only in the memory of this proxy
  (that is, of one run). Only a token made by this proxy can be downloaded. Nothing is stored.
- OpenAI: only the transcription endpoint with fixed models.
- Anthropic: only `/v1/messages` and `/v1/messages/count_tokens` (with the query string, Claude Code calls
  `?beta=true`), only sonnet and haiku models, the answer is streamed through as it arrives. The key is given only
  to the agent's sidecar; the bot never passes it to the sidecars of adapters.
- There is no authentication: the proxy listens on the sidecar's loopback, which only the container of this run
  can reach. A looping container is stopped by its timeout.

## Image builds

An adapter image is arbitrary: the agent writes its own `Dockerfile`, the image content is not restricted (the
`run_sandboxed` flags apply at run time anyway). The build itself is restricted, because `RUN` is arbitrary code with
network access on the host. The build context holds no secrets and no user data (only the adapter directory). Stage 1:

- a timeout on the build (the `--memory` and `--cpu-quota` limits are silently ignored by BuildKit, checked: the
  build allocated 400 MB with `--memory 100m`; limiting build resources is open, see below);
- the build network is NOT covered by the sidecar yet (`docker build` uses the ordinary bridge, `RUN` can reach the
  host and the LAN): an open question, see below;
- a Dockerfile with a `# syntax=` directive is rejected;
- the host names the image, images are not pushed, no credentials are mounted;
- a quota on the number of images per user, periodic cleanup.

Later: rootless docker as a whole (it also removes "docker group = root"), for an open bot builds in a microVM
(KVM is needed). The docker socket never gets into containers.

## The agent

- Called only when there is no adapter. It always creates a new adapter and never edits one. Enabled only if
  `.env` has `ANTHROPIC_API_KEY` and `ALLOWED_USER_IDS` is set (the agent costs money: for a closed circle).
- It is Claude Code (`claude -p`) in the `tb-agent` container (the `agent/Dockerfile` image on top of
  `transcribe-base`) with the same sandbox and network as adapters. The Anthropic key goes through the sidecar's
  proxy and is not inside the container; only the agent's sidecar has it, the sidecars of adapters do not. Only
  sonnet and haiku models. The instructions are `agent/PROMPT.md` (the adapter contract, the environment, how to
  download Telegram files, the `telegram_voice` reference, the message itself).
- **The only exception to "containers without mounts":** the agent gets a writable volume. The host mounts the
  directory `adapters/users/<id>/.staging/<random>` at `/adapter` (adapter discovery does not see dot-directories, so
  nobody builds an unfinished adapter). The agent runs with the host's uid: everything it creates belongs to us and
  can be read, moved and deleted without a root helper. The volume survives between attempts, so a retry works on
  the same files.
- The agent's word is not accepted. The host checks the tree (no symlinks or special files, up to 100 files, up to
  1 MB per file and 5 MB in total, `Dockerfile`, `match` and `run` exist; an unreadable directory is an error),
  builds the image, runs `match` on this message (at or above the threshold) and `run` (a non-empty transcript).
  The transcript of the acceptance run is the user's answer and is not run again.
- Two attempts: if the acceptance fails, the agent gets the error text and the same files. A ceiling of $2 per
  attempt (`--max-budget-usd`) and 20 minutes. Images of failed attempts are deleted. If it fails: the user gets a
  short reason, nothing lands in `adapters/` (the details go to the console).
- The agent names the adapter in the last line of its answer; if that does not fit `[a-z][a-z0-9_]{0,29}` (quotes,
  backticks, asterisks and a trailing full stop are tolerated) or is taken: a random name or a suffix. The adapter
  is moved from staging into place with `rename` (the content and so the image tag do not change).
- Honesty is part of the instructions: never fail silently, say in the output when the result is partial, do not
  invent arbitrary caps ("the first 10 items"), the time limit is the only cap.
- One agent works at a time.

## Data

Nothing is stored. The only persistent state is `adapters/`. Per-user limits (later) are kept in the bot's memory and
may reset on restart.

## Not doing

Repairing and deleting adapters, verification in the bot, an admin role, history, search over transcripts, formatting
of the result, tests of `match` on synthetic data, payments, a web interface.

## Next (decided, not done)

- Do not wait for all `match` runs to finish: take an adapter as soon as a suitable one is found.
- Logs to a file (now only the console). Adapter errors are visible only while you watch the terminal.
- Pack the bot into docker; the whole system in docker, with compose.
- Image builds of the agent: decide the build resources, the build network and the cleanup of stale images together.
- Per-user limits (the bot is open to everyone if `ALLOWED_USER_IDS` is not set).
- Restart handling: delete the `tb-*` containers at startup (orphaned sidecars live up to a day).

## Open questions

- Pick the outgoing byte limit from real yt-dlp requests.
- Image build resources (memory, CPU, disk): `docker build` flags do not work under BuildKit; options: the classic
  builder (`DOCKER_BUILDKIT=0`), BuildKit daemon configuration, building inside a container with limits.
- The image build network: `docker build --network container:<sidecar>` is not supported by BuildKit (only by the
  classic builder); options: the classic builder, building inside a sandbox (buildah/rootless BuildKit in a
  container), accepting the stage 1 risk.
- A sidecar is started for every container run (every `match` included): the start time grows with the number of
  adapters. The sidecar image is built once per process (changes to the rules need a bot restart).
- A cheap prefilter for `match` (without network), if there get to be many adapters or the exposure to other users'
  adapters starts to worry.
- Per-user limits: size, number of requests, adapters created per day, a global budget for the agent.
- Cleanup of docker images that have not been used for long.
- The Bot API limit: files up to 20 MB can be downloaded (more only through a local Bot API server).
