FROM transcribe-base
# Claude Code, run non-interactively (claude -p) as the agent. Same tools as an adapter has (python, ffmpeg, transcribe).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force
# No telemetry or updates (the sandbox has no business with them); only the model families the key proxy allows.
ENV DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 \
    ANTHROPIC_MODEL=claude-sonnet-5-5 ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-5-5 \
    ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-5-5 ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-5-5
