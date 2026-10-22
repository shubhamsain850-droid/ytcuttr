from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import subprocess, uuid, os, re, shutil, json, tempfile, urllib.request
from pathlib import Path
from urllib.parse import quote

app = FastAPI(title="YTCuttr API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CLIPS_DIR = Path("/tmp/ytcuttr")
CLIPS_DIR.mkdir(exist_ok=True)

jobs: dict = {}

# Cookie support via env variable or Render secret file
COOKIES_FILE = None
_cookies_content = os.environ.get("YT_COOKIES", "").strip()
if _cookies_content:
    if "\\n" in _cookies_content and "\n" not in _cookies_content:
        _cookies_content = _cookies_content.replace("\\n", "\n")
    _cf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    _cf.write(_cookies_content)
    _cf.close()
    COOKIES_FILE = _cf.name
elif os.path.exists("/etc/secrets/cookies.txt"):
    COOKIES_FILE = "/etc/secrets/cookies.txt"
elif (Path(__file__).parent / "cookies.txt").exists():
    COOKIES_FILE = str(Path(__file__).parent / "cookies.txt")

# Player clients to try (in order).
# Do not force fake user-agent, let yt-dlp send matching native headers.
PLAYER_CLIENTS = ["android", "ios", "mweb"]

# Models
class InfoRequest(BaseModel):
    url: str

class ClipRequest(BaseModel):
    url: str
    start: str
    end: str
    quality: str = "720p"

class DownloadRequest(BaseModel):
    url: str
    quality: str = "720p"

# Helpers
def parse_time(t: str) -> int:
    t = t.strip()
    if re.match(r'^\d+$', t):
        return int(t)
    parts = list(map(int, t.split(":")))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Invalid time format: {t}")

def to_hms(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"

QUALITY_MAP = {
    "2160p": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "audio": "bestaudio/best",
}

def build_ytdlp_cmd(extra: list = [], client_override: str = None) -> list:
    """Build base yt-dlp command with IPv4, cache cleanup and native headers."""
    cmd = [
        "yt-dlp",
        "-4",
        "--no-playlist",
        "--no-warnings",
        "--rm-cache-dir",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
    ]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    else:
        chosen = client_override if client_override else ",".join(PLAYER_CLIENTS)
        cmd += ["--extractor-args", f"youtube:player_client={chosen}"]
    return cmd + extra

def fetch_oembed(url: str) -> dict | None:
    """Fallback: fetch basic video info from YouTube oEmbed (no bot check)."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={quote(url)}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return {
                "title":       data.get("title", "Unknown Title"),
                "thumbnail":   data.get("thumbnail_url", ""),
                "duration":    0,
                "resolutions": ["1080p", "720p", "480p", "360p", "Audio only"],
            }
    except Exception:
        return None

# Routes

@app.get("/version")
async def version():
    return {
        "version": "2.2-local-cut-fix",
        "clients": PLAYER_CLIENTS,
        "has_cookies": bool(COOKIES_FILE)
    }

@app.get("/diag")
async def diag(url: str = "https://www.youtube.com/watch?v=aqz-KE-bpKQ", client: str = "android"):
    cmd = build_ytdlp_cmd(["--dump-json", "--skip-download", url], client_override=client)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {
        "client": client,
        "returncode": r.returncode,
        "stdout_len": len(r.stdout),
        "stderr": r.stderr[-500:],
        "has_cookies": bool(COOKIES_FILE)
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.post("/info")
async def get_video_info(req: InfoRequest):
    """Fetch video title, duration, thumbnail, available formats.
    Strategy: oEmbed first (fast, 3-5s), yt-dlp as upgrade fallback.
    """
    fallback = fetch_oembed(req.url)
    if fallback:
        try:
            cmd = build_ytdlp_cmd(["--dump-json", req.url])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                formats = data.get("formats", [])
                resolutions = set()
                for f in formats:
                    h = f.get("height")
                    if h and f.get("vcodec") != "none" and h >= 360:
                        resolutions.add(h)
                sorted_res = sorted(resolutions, reverse=True)
                res_labels = []
                for h in sorted_res:
                    if h >= 2160: res_labels.append("2160p")
                    elif h >= 1080: res_labels.append("1080p")
                    elif h >= 720:  res_labels.append("720p")
                    elif h >= 480:  res_labels.append("480p")
                    elif h >= 360:  res_labels.append("360p")
                seen = set(); unique_res = []
                for r in res_labels:
                    if r not in seen:
                        seen.add(r); unique_res.append(r)
                unique_res.append("Audio only")
                return {
                    "title":       data.get("title", fallback["title"]),
                    "duration":    data.get("duration", 0),
                    "thumbnail":   data.get("thumbnail", fallback["thumbnail"]),
                    "resolutions": unique_res,
                }
        except Exception:
            pass
        return fallback

    cmd = build_ytdlp_cmd(["--dump-json", req.url])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            formats = data.get("formats", [])
            resolutions = set()
            for f in formats:
                h = f.get("height")
                if h and f.get("vcodec") != "none" and h >= 360:
                    resolutions.add(h)
            sorted_res = sorted(resolutions, reverse=True)
            res_labels = []
            for h in sorted_res:
                if h >= 2160: res_labels.append("2160p")
                elif h >= 1080: res_labels.append("1080p")
                elif h >= 720:  res_labels.append("720p")
                elif h >= 480:  res_labels.append("480p")
                elif h >= 360:  res_labels.append("360p")
            seen = set(); unique_res = []
            for r in res_labels:
                if r not in seen:
                    seen.add(r); unique_res.append(r)
            unique_res.append("Audio only")
            return {
                "title":       data.get("title", "Unknown"),
                "duration":    data.get("duration", 0),
                "thumbnail":   data.get("thumbnail", ""),
                "resolutions": unique_res,
            }
        raise HTTPException(400, detail="Could not fetch video info. Check the URL or try again.")
    except subprocess.TimeoutExpired:
        raise HTTPException(408, detail="Request timed out. Try again.")
    except json.JSONDecodeError:
        raise HTTPException(400, detail="Invalid video URL.")


def _run_clip(job_id: str, url: str, start_hms: str, end_hms: str, quality: str):
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    ext = "m4a" if quality == "audio" else "mp4"
    out_path = out_dir / f"clip.{ext}"
    raw_path = out_dir / f"raw.{ext}"
    fmt = QUALITY_MAP.get(quality, QUALITY_MAP["720p"])

    try:
        jobs[job_id]["status"] = "downloading"

        # Strategy A: Direct section download
        cmd_fast = build_ytdlp_cmd([
            "--download-sections", f"*{start_hms}-{end_hms}",
            "--force-keyframes-at-cuts",
            "-f", fmt,
            "--merge-output-format", ext,
            "-o", str(out_path),
            url,
        ])
        r = subprocess.run(cmd_fast, capture_output=True, text=True, timeout=90)

        # Strategy B: If fast section download fails (e.g. ffmpeg 403 on remote URL),
        # download video stream locally with yt-dlp then cut with ffmpeg locally.
        if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            cmd_dl = build_ytdlp_cmd([
                "-f", fmt,
                "--merge-output-format", ext,
                "-o", str(raw_path),
                url,
            ])
            r_dl = subprocess.run(cmd_dl, capture_output=True, text=True, timeout=180)

            if r_dl.returncode != 0 or not raw_path.exists() or raw_path.stat().st_size == 0:
                cmd_dl_best = build_ytdlp_cmd([
                    "-f", "best",
                    "--merge-output-format", ext,
                    "-o", str(raw_path),
                    url,
                ])
                r_dl = subprocess.run(cmd_dl_best, capture_output=True, text=True, timeout=180)

            if r_dl.returncode == 0 and raw_path.exists() and raw_path.stat().st_size > 0:
                cmd_cut = [
                    "ffmpeg", "-y",
                    "-ss", start_hms,
                    "-to", end_hms,
                    "-i", str(raw_path),
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    str(out_path),
                ]
                subprocess.run(cmd_cut, capture_output=True, text=True, timeout=60)
                try:
                    raw_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if out_path.exists() and out_path.stat().st_size > 0:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_clip_{start_hms.replace(':','-')}_{end_hms.replace(':','-')}.{ext}"
        else:
            err = ""
            if 'r_dl' in locals() and r_dl and r_dl.stderr:
                err = r_dl.stderr[-400:]
            elif r and r.stderr:
                err = r.stderr[-400:]
            else:
                err = "Could not process clip. Please try another video or resolution."
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = err
    except subprocess.TimeoutExpired:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Processing timed out. Please try a shorter segment."
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


def _run_download(job_id: str, url: str, quality: str):
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    ext = "m4a" if quality == "audio" else "mp4"
    out_path = out_dir / f"video.{ext}"
    fmt = QUALITY_MAP.get(quality, QUALITY_MAP["720p"])

    cmd = build_ytdlp_cmd([
        "-f", fmt,
        "--merge-output-format", ext,
        "-o", str(out_path),
        url,
    ])
    try:
        jobs[job_id]["status"] = "downloading"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            fallback_cmd = build_ytdlp_cmd([
                "-f", "best",
                "--merge-output-format", ext,
                "-o", str(out_path),
                url,
            ])
            r = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=300)

        if out_path.exists() and out_path.stat().st_size > 0:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_video.{ext}"
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = r.stderr[-400:] if (r and r.stderr) else "Download failed"
    except subprocess.TimeoutExpired:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Download timed out."
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.post("/clip")
async def create_clip(req: ClipRequest, bg: BackgroundTasks):
    try:
        start_sec = parse_time(req.start)
        end_sec   = parse_time(req.end)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

    if end_sec <= start_sec:
        raise HTTPException(400, detail="End time must be after start time.")
    if (end_sec - start_sec) > 600:
        raise HTTPException(400, detail="Max clip length is 10 minutes.")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued"}
    bg.add_task(_run_clip, job_id, req.url, to_hms(start_sec), to_hms(end_sec), req.quality)
    return {"job_id": job_id}


@app.post("/download")
async def download_video(req: DownloadRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued"}
    bg.add_task(_run_download, job_id, req.url, req.quality)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    return job


@app.get("/file/{job_id}")
async def get_file(job_id: str, bg: BackgroundTasks):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, detail="File not ready")

    def cleanup():
        shutil.rmtree(CLIPS_DIR / job_id, ignore_errors=True)
        jobs.pop(job_id, None)

    bg.add_task(cleanup)
    return FileResponse(
        job["file"],
        filename=job["filename"],
        media_type="application/octet-stream"
    )
