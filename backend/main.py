from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess, uuid, os, re, shutil, json
from pathlib import Path

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

# ── Models ──────────────────────────────────────────────
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

# ── Helpers ─────────────────────────────────────────────
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
    "2160p": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
    "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

# ── Routes ───────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/info")
async def get_video_info(req: InfoRequest):
    """Fetch video title, duration, thumbnail, available formats"""
    try:
        result = subprocess.run([
            "yt-dlp",
            "--dump-json",
            "--no-playlist",
            "--no-warnings",
            req.url
        ], capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise HTTPException(400, detail="Could not fetch video info. Check the URL.")

        data = json.loads(result.stdout)

        # Extract available resolutions
        formats = data.get("formats", [])
        resolutions = set()
        for f in formats:
            h = f.get("height")
            if h and f.get("vcodec") != "none":
                if h >= 360:
                    resolutions.add(h)

        sorted_res = sorted(resolutions, reverse=True)
        res_labels = []
        for h in sorted_res:
            if h >= 2160: res_labels.append("2160p")
            elif h >= 1080: res_labels.append("1080p")
            elif h >= 720: res_labels.append("720p")
            elif h >= 480: res_labels.append("480p")
            elif h >= 360: res_labels.append("360p")

        # Deduplicate preserving order
        seen = set()
        unique_res = []
        for r in res_labels:
            if r not in seen:
                seen.add(r)
                unique_res.append(r)

        unique_res.append("Audio only")

        return {
            "title":     data.get("title", "Unknown"),
            "duration":  data.get("duration", 0),
            "thumbnail": data.get("thumbnail", ""),
            "resolutions": unique_res,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, detail="Request timed out. Try again.")
    except json.JSONDecodeError:
        raise HTTPException(400, detail="Invalid video URL.")

def _run_clip(job_id: str, url: str, start_hms: str, end_hms: str, quality: str):
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    ext = "m4a" if quality == "audio" else "mp4"
    out_path = out_dir / f"clip.{ext}"
    fmt = QUALITY_MAP.get(quality, QUALITY_MAP["720p"])

    cmd = [
        "yt-dlp",
        "--download-sections", f"*{start_hms}-{end_hms}",
        "--force-keyframes-at-cuts",
        "-f", fmt,
        "--merge-output-format", ext,
        "-o", str(out_path),
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    try:
        jobs[job_id]["status"] = "downloading"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and out_path.exists():
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_clip_{start_hms.replace(':','-')}_{end_hms.replace(':','-')}.{ext}"
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = r.stderr[-400:] if r.stderr else "Download failed"
    except subprocess.TimeoutExpired:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Timed out"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

def _run_download(job_id: str, url: str, quality: str):
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    ext = "m4a" if quality == "audio" else "mp4"
    out_path = out_dir / f"video.{ext}"
    fmt = QUALITY_MAP.get(quality, QUALITY_MAP["720p"])

    cmd = [
        "yt-dlp",
        "-f", fmt,
        "--merge-output-format", ext,
        "-o", str(out_path),
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    try:
        jobs[job_id]["status"] = "downloading"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and out_path.exists():
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_video.{ext}"
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = r.stderr[-400:] if r.stderr else "Download failed"
    except subprocess.TimeoutExpired:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Timed out"
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
