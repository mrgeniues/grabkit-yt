FROM python:3.12-slim

# System deps: curl (deno installer), git + nodejs/npm (bgutil PO-token server)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates unzip git nodejs npm \
 && rm -rf /var/lib/apt/lists/*

# Deno (yt-dlp needs a JS runtime to solve YouTube's signature challenge)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}" \
    DENO_DIR="/root/.deno"

# bgutil PO-token server (proves to YouTube the request comes from a genuine
# client, which bypasses the 403 bot-wall on datacenter IPs)
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
 && cd /opt/bgutil/server && npm install --no-audit --no-fund && npx tsc

# Python deps (includes the yt-dlp PO-token provider plugin)
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" "yt-dlp[default]" requests bgutil-ytdlp-pot-provider

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 10000
# Start the PO-token server in the background, then the API
CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js --port 4416 & exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
