FROM python:3.12-slim

# System deps: curl (deno installer), git + nodejs/npm (bgutil PO-token server),
# ffmpeg (merging DASH video+audio for 720p/1080p)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates unzip git nodejs npm ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Deno (yt-dlp needs a JS runtime to solve YouTube's signature challenge)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}" \
    DENO_DIR="/root/.deno"

# bgutil PO-token server (proves to YouTube the request comes from a genuine
# client, which bypasses the 403 bot-wall on datacenter IPs)
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
 && cd /opt/bgutil/server && npm install --no-audit --no-fund && npx tsc

# Python deps (includes the yt-dlp PO-token provider plugin).
# yt-dlp is installed from the SABR-protocol PR branch so SABR-only sessions
# can still download full-quality streams; protobug is its dependency.
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" requests \
    bgutil-ytdlp-pot-provider protobug \
    "yt-dlp[default] @ git+https://github.com/yt-dlp/yt-dlp.git@6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6"

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 10000
# Start the PO-token server in the background (heap capped at 256MB so BotGuard
# challenge solving can't OOM the 512MB free-tier instance), then the API
CMD ["sh", "-c", "node --max-old-space-size=256 /opt/bgutil/server/build/main.js --port 4416 & exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
