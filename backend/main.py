from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import subprocess, base64, uuid, os, re, shutil, json, tempfile, urllib.request, asyncio, time
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

CACHE_DIR = Path("/tmp/ytdlp_cache")
CACHE_DIR.mkdir(exist_ok=True)

jobs: dict = {}
INFO_CACHE: dict = {}
INFO_CACHE_TTL = 3600  # 1 hour cache

# Dynamic Cookie Resolver
def get_cookies_file() -> tuple[str | None, str]:
    """Find valid YouTube cookies from bundled cookies, /etc/secrets, Base64 env var, or raw env var."""
    # 0. Check bundled cookies in backend directory
    bundled = Path(__file__).parent / "cookies.txt"
    if bundled.exists() and bundled.stat().st_size > 100:
        return str(bundled), "bundled_file:cookies.txt"
    # 1. Check secret files in /etc/secrets
    sec_dir = Path("/etc/secrets")
    if sec_dir.exists():
        for f in sec_dir.rglob("*"):
            if f.is_file() and not f.name.startswith(".."):
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                    if "youtube.com" in content:
                        return str(f), f"secret_file:{f.name}"
                except Exception:
                    pass

    # 2. Check env vars (try Base64 first to avoid HTML newline stripping)
    for k, v in os.environ.items():
        if "COOKIE" in k.upper() or "YT" in k.upper():
            val = v.strip()
            # Try base64 decoding
            try:
                decoded = base64.b64decode(val).decode("utf-8", errors="ignore")
                if "youtube.com" in decoded and len(decoded.splitlines()) > 5:
                    cf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
                    cf.write(decoded)
                    cf.close()
                    return cf.name, f"env_base64:{k}"
            except Exception:
                pass

            # Try raw env var
            if "youtube.com" in val:
                raw = val.replace("\\n", "\n")
                cf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
                cf.write(raw)
                cf.close()
                return cf.name, f"env_raw:{k}"

    return None, "none"

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

def get_video_id(url: str) -> str:
    m = re.search(r'(?:v=|\/|be\/|shorts\/)([0-9A-Za-z_-]{11})', url)
    return m.group(1) if m else url

# QUALITY MAP: Strictly prioritizes AVC (H.264) + AAC (M4A) for zero-transcode instant cutting
QUALITY_MAP = {
    "2160p": "bv*[vcodec^=avc][height<=2160]+ba[ext=m4a]/bv*[height<=2160][ext=mp4]+ba[ext=m4a]/bv*[height<=2160]+ba/b[height<=2160]/best",
    "1440p": "bv*[vcodec^=avc][height<=1440]+ba[ext=m4a]/bv*[height<=1440][ext=mp4]+ba[ext=m4a]/bv*[height<=1440]+ba/b[height<=1440]/best",
    "1080p": "bv*[vcodec^=avc][height<=1080]+ba[ext=m4a]/bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/best",
    "720p":  "22/bv*[vcodec^=avc][height<=720]+ba[ext=m4a]/bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/best",
    "480p":  "bv*[vcodec^=avc][height<=480]+ba[ext=m4a]/bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/best",
    "360p":  "18/bv*[vcodec^=avc][height<=360]+ba[ext=m4a]/bv*[height<=360][ext=mp4]+ba[ext=m4a]/b[height<=360][ext=mp4]/best",
    "240p":  "bv*[vcodec^=avc][height<=240]+ba[ext=m4a]/bv*[height<=240][ext=mp4]+ba[ext=m4a]/b[height<=240][ext=mp4]/best",
    "144p":  "bv*[vcodec^=avc][height<=144]+ba[ext=m4a]/bv*[height<=144][ext=mp4]+ba[ext=m4a]/b[height<=144][ext=mp4]/best",
    "audio": "ba[ext=m4a]/ba/best",
}

def clean_quality_key(quality: str) -> str:
    if not quality:
        return "720p"
    q = quality.strip().lower()
    if "audio" in q:
        return "audio"
    return q.split()[0]

def build_ytdlp_cmd(extra: list = [], use_cookies: bool = True) -> list:
    cmd = [
        "yt-dlp",
        "-4",
        "--no-playlist",
        "--no-warnings",
        "--cache-dir", str(CACHE_DIR),
        "--no-check-formats",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--extractor-args", "youtube:skip=translated_subs,comments",
    ]
    if use_cookies:
        cf, source = get_cookies_file()
        if cf:
            cmd += ["--cookies", cf]
    else:
        cmd += ["--extractor-args", "youtube:player_client=android,visionos;skip=translated_subs,comments"]
    return cmd + extra

def fetch_oembed(url: str) -> dict | None:
    """Fast video metadata via YouTube oEmbed (instant, no bot checks)."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={quote(url)}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
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

@app.get("/diag")
async def diag(url: str = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"):
    cf, source = get_cookies_file()
    results = {"cookie_source": source, "cookie_file": cf}
    try:
        deno_res = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5)
        results["deno"] = deno_res.stdout.splitlines()[0] if deno_res.stdout else "empty"
    except Exception as e:
        results["deno"] = str(e)

    cmd_cookie = ["yt-dlp", "-4", "--no-warnings", "--cache-dir", str(CACHE_DIR)]
    if cf:
        cmd_cookie += ["--cookies", cf]
    cmd_cookie += ["-F", url]
    r1 = subprocess.run(cmd_cookie, capture_output=True, text=True, timeout=25)
    results["with_cookies"] = {
        "code": r1.returncode,
        "formats_found": len([l for l in r1.stdout.splitlines() if any(x in l for x in ["mp4", "webm", "18", "398", "720"])]),
        "stdout_sample": [l for l in r1.stdout.splitlines() if any(x in l for x in ["mp4", "webm", "18", "398", "720"])][:4],
        "stderr": r1.stderr[-400:] if r1.stderr else ""
    }
    return results

@app.get("/version")
async def version():
    cf, source = get_cookies_file()
    lines_count = 0
    size = 0
    if cf:
        try:
            p = Path(cf)
            size = p.stat().st_size
            lines_count = len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            pass

    sec_files = []
    if Path("/etc/secrets").exists():
        for f in Path("/etc/secrets").rglob("*"):
            if f.is_file() and not f.name.startswith(".."):
                sec_files.append({"name": f.name, "size": f.stat().st_size})

    return {
        "version": "4.2-turbo-native-direct",
        "has_cookies": bool(cf),
        "cookie_source": source,
        "cookie_size": size,
        "cookie_lines": lines_count,
        "secret_files_found": sec_files, "env_keys": [k for k in os.environ if any(x in k.upper() for x in ["COOKIE", "YT", "SECRET"])]
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.post("/info")
async def get_video_info(req: InfoRequest):
    vid = get_video_id(req.url)
    info_json_file = CACHE_DIR / f"{vid}.info.json"
    now = time.time()

    # Cache hit: instant 1ms response
    if vid in INFO_CACHE and info_json_file.exists():
        cached_time, cached_data = INFO_CACHE[vid]
        if (now - cached_time) < INFO_CACHE_TTL:
            return cached_data

    # Primary: Run yt-dlp to extract exact stream formats, duration and metadata
    cmd = build_ytdlp_cmd(["--dump-json", req.url])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if result.returncode == 0 and result.stdout.strip():
            # Save raw info.json directly to disk for 0-second clip download reuse
            try:
                info_json_file.write_text(result.stdout, encoding="utf-8")
            except Exception:
                pass

            data = json.loads(result.stdout)
            formats = data.get("formats", [])

            ORDER = ["2160p (4K)", "1440p (2K)", "1080p", "720p", "480p", "360p", "240p", "144p"]
            found_tiers = set()
            for f in formats:
                h = f.get("height")
                w = f.get("width")
                v = f.get("vcodec")
                if h and isinstance(h, int) and v and v != "none":
                    eff = min(h, w) if (w and isinstance(w, int) and w > 0 and h > w) else h
                    if eff >= 2160: found_tiers.add("2160p (4K)")
                    elif eff >= 1440: found_tiers.add("1440p (2K)")
                    elif eff >= 1080: found_tiers.add("1080p")
                    elif eff >= 720:  found_tiers.add("720p")
                    elif eff >= 480:  found_tiers.add("480p")
                    elif eff >= 360:  found_tiers.add("360p")
                    elif eff >= 240:  found_tiers.add("240p")
                    elif eff >= 144:  found_tiers.add("144p")

            res_labels = [t for t in ORDER if t in found_tiers]
            if not res_labels:
                res_labels = ["1080p", "720p", "480p", "360p"]
            res_labels.append("Audio only")

            duration = int(data.get("duration") or 0)
            res_dict = {
                "title":       data.get("title", "YouTube Video"),
                "duration":    duration,
                "thumbnail":   data.get("thumbnail", ""),
                "resolutions": res_labels,
            }
            INFO_CACHE[vid] = (now, res_dict)
            return res_dict
    except Exception:
        pass

    # Secondary fallback: oEmbed
    fallback = fetch_oembed(req.url)
    if fallback:
        return fallback

    raise HTTPException(400, detail="Could not fetch video info. Please verify the URL and try again.")


def _run_clip(job_id: str, url: str, start_hms: str, end_hms: str, quality: str):
    t_start = time.time()
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    q_key = clean_quality_key(quality)
    ext = "m4a" if q_key == "audio" else "mp4"
    out_path = out_dir / f"clip.{ext}"
    raw_path = out_dir / f"raw.{ext}"
    fmt = QUALITY_MAP.get(q_key, QUALITY_MAP["720p"])

    vid = get_video_id(url)
    info_json_file = CACHE_DIR / f"{vid}.info.json"

    try:
        jobs[job_id]["status"] = "downloading"
        r_dl = None

        # ATTEMPT 1 (Turbo Stream Pipeline): Native 8-thread download via cached info-json
        # Completely skips YouTube extraction and Deno runtime; streams directly into raw.mp4
        if info_json_file.exists() and info_json_file.stat().st_size > 1000:
            cmd_dl_cached = build_ytdlp_cmd([
                "--load-info-json", str(info_json_file),
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", fmt,
                "--merge-output-format", ext,
                "-o", str(raw_path),
            ], use_cookies=True)
            r_dl = subprocess.run(cmd_dl_cached, capture_output=True, text=True, timeout=80)

        # ATTEMPT 2: Native download using direct URL with requested format
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            cmd_dl_url = build_ytdlp_cmd([
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", fmt,
                "--merge-output-format", ext,
                "-o", str(raw_path),
                url,
            ], use_cookies=True)
            r_dl = subprocess.run(cmd_dl_url, capture_output=True, text=True, timeout=80)

        # ATTEMPT 3: Fallback on best stream (mp4 + m4a)
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            cmd_dl_best = build_ytdlp_cmd([
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b/best",
                "--merge-output-format", ext,
                "-o", str(raw_path),
                url,
            ], use_cookies=True)
            r_dl = subprocess.run(cmd_dl_best, capture_output=True, text=True, timeout=80)

        # ATTEMPT 4: Fallback without cookies
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            cmd_dl_anon = build_ytdlp_cmd([
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b/best",
                "--merge-output-format", ext,
                "-o", str(raw_path),
                url,
            ], use_cookies=False)
            r_dl = subprocess.run(cmd_dl_anon, capture_output=True, text=True, timeout=80)

        # Step 2: Instant local cut via FFmpeg (lossless stream copy in <0.1s)
        if raw_path.exists() and raw_path.stat().st_size > 0:
            jobs[job_id]["status"] = "processing"
            cmd_cut = [
                "ffmpeg", "-y",
                "-ss", start_hms,
                "-to", end_hms,
                "-i", str(raw_path),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(out_path),
            ]
            subprocess.run(cmd_cut, capture_output=True, text=True, timeout=20)
            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass

        elapsed = round(time.time() - t_start, 2)
        if out_path.exists() and out_path.stat().st_size > 0:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_clip_{start_hms.replace(':','-')}_{end_hms.replace(':','-')}.{ext}"
            jobs[job_id]["time_taken"] = elapsed
        else:
            err = r_dl.stderr[-400:] if (r_dl and r_dl.stderr) else "Unknown download failure"
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Clip generation failed: {err}"
            jobs[job_id]["debug"] = {
                "r_dl_code": r_dl.returncode if r_dl else -1,
                "r_dl_stderr": err,
            }
    except subprocess.TimeoutExpired:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Operation timed out. Video may be too large or restricted."
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"Server error: {str(e)}"


def _run_download(job_id: str, url: str, quality: str):
    t_start = time.time()
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    q_key = clean_quality_key(quality)
    ext = "m4a" if q_key == "audio" else "mp4"
    out_path = out_dir / f"video.{ext}"
    fmt = QUALITY_MAP.get(q_key, QUALITY_MAP["720p"])

    vid = get_video_id(url)
    info_json_file = CACHE_DIR / f"{vid}.info.json"

    try:
        jobs[job_id]["status"] = "downloading"
        r = None

        # Attempt 1: Using cached info.json if available
        if info_json_file.exists() and info_json_file.stat().st_size > 1000:
            cmd = build_ytdlp_cmd([
                "--load-info-json", str(info_json_file),
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", fmt,
                "--merge-output-format", ext,
                "-o", str(out_path),
            ], use_cookies=True)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)

        # Attempt 2: Direct URL
        if not out_path.exists() or out_path.stat().st_size == 0:
            cmd = build_ytdlp_cmd([
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", fmt,
                "--merge-output-format", ext,
                "-o", str(out_path),
                url,
            ], use_cookies=True)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # Attempt 3: Fallback to best format with cookies
        if not out_path.exists() or out_path.stat().st_size == 0:
            fallback_cmd = build_ytdlp_cmd([
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b/best",
                "--merge-output-format", ext,
                "-o", str(out_path),
                url,
            ], use_cookies=True)
            r = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=300)

        elapsed = round(time.time() - t_start, 2)
        if out_path.exists() and out_path.stat().st_size > 0:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_video.{ext}"
            jobs[job_id]["time_taken"] = elapsed
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = r.stderr[-400:] if (r and r.stderr) else "Download failed"
            jobs[job_id]["time_taken"] = elapsed
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


async def delayed_cleanup(job_id: str, delay: int = 300):
    await asyncio.sleep(delay)
    shutil.rmtree(CLIPS_DIR / job_id, ignore_errors=True)
    jobs.pop(job_id, None)


@app.get("/file/{job_id}")
async def get_file(job_id: str, bg: BackgroundTasks):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, detail="File not ready")

    bg.add_task(delayed_cleanup, job_id, 300)
    return FileResponse(
        job["file"],
        filename=job["filename"],
        media_type="application/octet-stream"
    )
