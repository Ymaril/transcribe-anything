FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir openai
COPY --chmod=755 transcribe /usr/local/bin/transcribe
WORKDIR /work
