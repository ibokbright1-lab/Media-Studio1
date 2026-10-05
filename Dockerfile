FROM python:3.12-slim

# ffmpeg: merging / trimming.  Deno: the JavaScript runtime yt-dlp needs to solve YouTube's challenges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn "yt-dlp[default]"

COPY . .
ENV PYTHONUNBUFFERED=1

# Pull the newest yt-dlp (nightly) on every start -- YouTube breaks it often. Never block startup if it fails.
# ONE worker: jobs live in memory, so several workers would lose track of each other's jobs.
# Change "app:app" if your Flask object lives elsewhere.
CMD ["sh", "-c", "pip install -q -U --pre 'yt-dlp[default]' || true; exec gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT:-10000}"]
