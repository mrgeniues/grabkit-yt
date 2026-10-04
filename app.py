"""grabkit-yt: tiny YouTube resolver microservice.
POST /resolve {"url": "<youtube watch/shorts url>"} -> video metadata + progressive download formats
GET  /dl?u=<googlevideo stream url>&n=<filename>            -> proxied download (SSRF-guarded)
GET  /merge?url=<youtube url>&q=720&n=<name>                 -> yt-dlp downloads best video<=q + audio, ffmpeg-merges to MP4
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
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


def normalize_yt_url(url: str) -> str:
    """Convert youtu.be / shorts / live / embed URLs to canonical watch URLs.
    yt-dlp's PO-token path is flaky on short/redirect URL forms, so normalize
    everything to https://www.youtube.com/watch?v=... first."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        vid = p.path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={vid}"
    m = re.match(r"^/(shorts|live|embed)/([^/?#]+)", p.path or "")
    if m and host.endswith("youtube.com"):
        return f"https://www.youtube.com/watch?v={m.group(2)}"
    return url


def ytdlp_json(url: str) -> dict:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        # Try several YouTube player clients; with a valid PO token (from the
        # local bgutil server) these pass YouTube's bot checks on datacenter IPs.
        "--extractor-args",
        "youtube:player_client=web,web_embedded,android,ios,tv",
        # PO-token provider served by the local bgutil sidecar (port 4416)
        "--extractor-args",
        "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
        "-J",
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-800:] if p.stderr else "yt-dlp failed")
    return json.loads(p.stdout)


@app.get("/")
def health():
    return {"ok": True, "service": "grabkit-yt"}


@app.get("/debug")
def debug():
    """Diagnose the container: is deno on PATH? does yt-dlp see a JS runtime?"""
    import shutil

    info: dict = {}
    info["deno_path"] = shutil.which("deno")
    try:
        p = subprocess.run(
            ["deno", "--version"], capture_output=True, text=True, timeout=15
        )
        info["deno_version"] = (p.stdout or p.stderr or "").strip().split("\n")[0]
    except Exception as e:
        info["deno_version"] = f"FAILED: {e}"
    try:
        p = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True, timeout=15
        )
        info["ytdlp_version"] = (p.stdout or "").strip()
    except Exception as e:
        info["ytdlp_version"] = f"FAILED: {e}"
    # Is the bgutil PO-token sidecar listening on 127.0.0.1:4416?
    import socket

    try:
        s = socket.create_connection(("127.0.0.1", 4416), timeout=5)
        s.close()
        info["pot_server"] = "listening"
    except Exception as e:
        info["pot_server"] = f"NOT LISTENING: {e}"
    return info


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
        d = ytdlp_json(normalize_yt_url(url))
    except Exception as e:
        # Include the underlying error detail so we can diagnose deployment issues
        return JSONResponse(
            {"status": "error", "code": "fetch_failed", "detail": str(e)[-800:]}
        )

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

    # Video-only (DASH/SABR) options with direct URLs, deduped by height.
    # These need server-side merging -> served via /merge.
    dash = []
    seen_h: set[int] = set()
    all_fmts = d.get("formats", []) or []
    for f in sorted(all_fmts, key=lambda x: (x.get("height") or 0), reverse=True):
        if not f.get("url"):
            continue
        v, a_ = f.get("vcodec"), f.get("acodec")
        h = f.get("height") or 0
        if v and v != "none" and (not a_ or a_ == "none") and h and h not in seen_h:
            seen_h.add(h)
            dash.append({"height": h, "label": f"{h}p", "id": f["format_id"]})

    return {
        "status": "ok",
        "title": d.get("title"),
        "author": d.get("uploader"),
        "thumbnail": d.get("thumbnail"),
        "duration": d.get("duration"),
        "views": d.get("view_count"),
        "formats": formats,
        "audio": audio,
        "dash": dash,
        "stats": {
            "total_formats": len(all_fmts),
            "with_url": sum(1 for f in all_fmts if f.get("url")),
        },
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


MERGE_LOCK = threading.Lock()


@app.get("/merge")
def merge(url: str, q: int = 720, n: str = "video.mp4"):
    """Download best video (<=q p) + best audio via yt-dlp and ffmpeg-merge to
    MP4. Used for qualities above 360p (DASH video-only + audio-only)."""
    url = (url or "").strip()
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in YT_HOSTS:
        raise HTTPException(400, "not a youtube url")
    q = max(144, min(int(q or 720), 2160))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", n)[:80] or "video.mp4"
    if not safe.lower().endswith(".mp4"):
        safe += ".mp4"

    if not MERGE_LOCK.acquire(blocking=False):
        raise HTTPException(429, "another merge in progress, try again in a bit")
    tmpdir = tempfile.mkdtemp(prefix="gkyt-")
    try:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--extractor-args",
            "youtube:player_client=web,web_embedded,android,ios,tv",
            "--extractor-args",
            "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
            "-f",
            f"bv[height<={q}]+ba/b[height<={q}]/b",
            "--merge-output-format",
            "mp4",
            "-o",
            os.path.join(tmpdir, "out.%(ext)s"),
            normalize_yt_url(url),
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "merge timed out (video too long?)")
        if p.returncode != 0:
            raise HTTPException(502, f"merge failed: {(p.stderr or '')[-400:]}")
        files = [
            f
            for f in os.listdir(tmpdir)
            if os.path.isfile(os.path.join(tmpdir, f))
            and not f.endswith((".part", ".ytdl", ".temp"))
        ]
        if not files:
            raise HTTPException(502, "merge produced no file")
        best = max(files, key=lambda f: os.path.getsize(os.path.join(tmpdir, f)))
        fpath = os.path.join(tmpdir, best)
        size = os.path.getsize(fpath)

        def gen():
            try:
                with open(fpath, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                MERGE_LOCK.release()

        return StreamingResponse(
            gen(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{safe}"',
                "Content-Length": str(size),
            },
        )
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        MERGE_LOCK.release()
        raise
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        MERGE_LOCK.release()
        raise HTTPException(500, f"merge error: {e}")
