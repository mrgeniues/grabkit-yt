FROM python:3.12-slim

# Deno (JS runtime needed by yt-dlp for YouTube signature solving)
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}" \
    DENO_DIR="/root/.deno"

RUN pip install --no-cache-dir fastapi uvicorn "yt-dlp[default]" requests

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 10000
CMD sh -c 'uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}'
