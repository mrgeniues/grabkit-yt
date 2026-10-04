"""grabkit-yt: tiny YouTube resolver microservice.
POST /resolve {"url": "<youtube watch/shorts url>"} -> video metadata + progressive download formats
GET  /dl?u=<googlevideo stream url>&n=<filename>            -> proxied download (SSRF-guarded)
"""
import json
import re
import subprocess
import time
import urllib.parse
from collections import defaultdict

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="grabkit-yt")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
RATE_BUCKET: dict[str, list[float]] = defaultdict(list)
RATE_MAX_PER_MIN = 12


def rate_ok(ip: str) -> bool:
    now = time.time()
    RATE_BUCKET[ip] = [t for t in RATE_BUCKET[ip] if now - t < 60]
    if len(RATE_BUCKET[ip]) >= RATE_MAX_PER_MIN:
        return False
    RATE_BUCKET[ip].append(now)
    return True


def ytdlp_json(url: str) -> dict:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--extractor-args",
        "youtube:player_client=web_embedded",
        "-J",
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-500:] if p.stderr else "yt-dlp failed")
    return json.loads(p.stdout)


@app.get("/")
def health():
    return {"ok": True, "service": "grabkit-yt"}


@app.post("/resolve")
async def resolve(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid json")
    url = (body.get("url") or "").strip()
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in YT_HOSTS:
        raise HTTPException(400, "not a youtube url")
    if not rate_ok(req.client.host if req.client else "unknown"):
        raise HTTPException(429, "rate limited, try again in a minute")

    try:
        d = ytdlp_json(url)
    except Exception:
        return JSONResponse({"status": "error", "code": "fetch_failed"})

    # Progressive formats only (video+audio in one file -> no ffmpeg needed)
    progressive = []
    for f in d.get("formats", []) or []:
        if not f.get("url"):
            continue
        v, a = f.get("vcodec"), f.get("acodec")
        if v and v != "none" and a and a != "none" and f.get("ext") in ("mp4", "webm"):
            progressive.append(f)
    # Dedupe by height, keep best per height (sorted desc, first wins)
    deduped: dict[int, dict] = {}
    for f in sorted(progressive, key=lambda x: (x.get("height") or 0), reverse=True):
        h = f.get("height") or 0
        if h not in deduped:
            deduped[h] = f
    formats = [
        {
            "id": f["format_id"],
            "label": f"{f.get('height') or '?'}p {f['ext'].upper()}",
            "ext": f["ext"],
            "height": f.get("height"),
            "url": f["url"],
            "size": f.get("filesize") or f.get("filesize_approx"),
        }
        for f in sorted(deduped.values(), key=lambda x: (x.get("height") or 0), reverse=True)
    ]

    audio = None
    auds = [
        f
        for f in d.get("formats", []) or []
        if f.get("url")
        and (not f.get("vcodec") or f.get("vcodec") == "none")
        and f.get("acodec")
        and f.get("acodec") != "none"
    ]
    if auds:
        a = max(auds, key=lambda x: x.get("abr") or 0)
        audio = {
            "id": a["format_id"],
            "label": f"Audio {a['ext'].upper()}",
            "ext": a["ext"],
            "url": a["url"],
        }

    return {
        "status": "ok",
        "title": d.get("title"),
        "author": d.get("uploader"),
        "thumbnail": d.get("thumbnail"),
        "duration": d.get("duration"),
        "views": d.get("view_count"),
        "formats": formats,
        "audio": audio,
    }


@app.get("/dl")
def dl(u: str, n: str = "video.mp4"):
    """Proxy a googlevideo stream URL so the browser can download it directly
    (avoids CORS + cross-IP issues). SSRF-guarded to *.googlevideo.com only."""
    host = (urllib.parse.urlparse(u).hostname or "").lower()
    if not host.endswith(".googlevideo.com"):
        raise HTTPException(400, "bad host")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", n)[:80] or "video.mp4"
    try:
        r = requests.get(
            u,
            stream=True,
            timeout=60,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
    except Exception:
        raise HTTPException(502, "upstream fetch failed")

    def gen():
        try:
            for chunk in r.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return StreamingResponse(
        gen(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )
