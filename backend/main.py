from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import subprocess, base64, uuid, os, re, shutil, json, tempfile, urllib.request, asyncio, time, threading
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

@app.on_event("startup")
def startup_clean():
    """Purge any stale clips/cache on boot to ensure 100% zero leftover cache."""
    try:
        if CLIPS_DIR.exists():
            shutil.rmtree(CLIPS_DIR, ignore_errors=True)
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Startup clean error: {e}")

jobs: dict = {}
INFO_CACHE: dict = {}
INFO_CACHE_TTL = 3600  # 1 hour cache

# Concurrency & Smart Queue Manager (3 concurrent conversion slots; fast-track for <100MB)
MAX_CONCURRENT_JOBS = 3
MAX_QUEUE_CAP = 30
MAX_PER_IP = 3
COOLDOWN_DURATION = 600  # 10 minutes temporary block on excessive concurrent requests
QUEUE_LOCK = threading.Lock()
ACTIVE_JOBS: set[str] = set()
WAITING_QUEUE: list[str] = []
IP_ACTIVE_JOBS: dict[str, set[str]] = {}
IP_COOLDOWNS: dict[str, float] = {}

def _sweeper_worker():
    """Background daemon thread: sweeps temporary files, clips, and stale jobs every 60s."""
    while True:
        try:
            now = time.time()
            # 1. Clean clips older than 5 minutes (300 seconds)
            if CLIPS_DIR.exists():
                for item in list(CLIPS_DIR.iterdir()):
                    try:
                        mtime = item.stat().st_mtime
                        if now - mtime > 300:
                            if item.is_dir():
                                shutil.rmtree(item, ignore_errors=True)
                            else:
                                item.unlink(missing_ok=True)
                    except Exception:
                        pass

            # 2. Clean yt-dlp cache / temp files older than 5 minutes
            if CACHE_DIR.exists():
                for item in list(CACHE_DIR.iterdir()):
                    try:
                        if now - item.stat().st_mtime > 300:
                            if item.is_dir():
                                shutil.rmtree(item, ignore_errors=True)
                            else:
                                item.unlink(missing_ok=True)
                    except Exception:
                        pass

            # 3. Clean in-memory jobs older than 10 minutes
            with QUEUE_LOCK:
                stale_jobs = [jid for jid, jdata in jobs.items() if now - jdata.get("created_at", now) > 600]
                for jid in stale_jobs:
                    jobs.pop(jid, None)
                    _cleanup_ip_job(jid)
                    ACTIVE_JOBS.discard(jid)
                    if jid in WAITING_QUEUE:
                        WAITING_QUEUE.remove(jid)

        except Exception as e:
            pass

        time.sleep(60)

threading.Thread(target=_sweeper_worker, daemon=True, name="YTCuttrSweeper").start()

def get_client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else "unknown"

def _cleanup_ip_job(job_id: str):
    for ip, job_set in list(IP_ACTIVE_JOBS.items()):
        job_set.discard(job_id)
        if not job_set:
            del IP_ACTIVE_JOBS[ip]

def check_ip_rate_limit(client_ip: str):
    """
    Enforces per-IP anti-bot & rate-limit rules:
    - If IP is under 10-minute cooldown, block until cooldown expires.
    - If IP attempts > 5 concurrent jobs (> MAX_PER_IP), block IP for 10 minutes (600s).
    - Jobs from the same IP at different times (or after existing jobs finish) are allowed.
    - NO USER IS EVER PERMANENTLY BLOCKED: Cooldown strictly auto-expires after 10 minutes.
    """
    if not client_ip or client_ip == "unknown":
        return

    now = time.time()

    # 1. Global sweep: auto-expire and clear any cooldowns older than 10 minutes
    for ip, until in list(IP_COOLDOWNS.items()):
        if now >= until:
            del IP_COOLDOWNS[ip]
            IP_ACTIVE_JOBS.pop(ip, None)

    # 2. Check if this client IP is currently under temporary cooldown
    if client_ip in IP_COOLDOWNS:
        ban_until = IP_COOLDOWNS[client_ip]
        if now < ban_until:
            rem_min = max(1, int(ban_until - now + 59) // 60)
            raise HTTPException(
                status_code=429,
                detail=f"Too many concurrent requests detected from this IP. Your access is temporarily restricted for {rem_min} minute(s). Please try again later."
            )
        else:
            del IP_COOLDOWNS[client_ip]
            IP_ACTIVE_JOBS.pop(client_ip, None)

    # 3. Active-check prune: discard any ghost/completed/errored jobs for this IP
    ip_jobs = IP_ACTIVE_JOBS.setdefault(client_ip, set())
    active_now = set()
    for jid in ip_jobs:
        if (jid in ACTIVE_JOBS or jid in WAITING_QUEUE) and jobs.get(jid, {}).get("status") not in ["done", "error"]:
            active_now.add(jid)
    IP_ACTIVE_JOBS[client_ip] = active_now
    ip_jobs = active_now

    # 4. Check active concurrent jobs for this IP
    if len(ip_jobs) >= MAX_PER_IP:
        IP_COOLDOWNS[client_ip] = now + COOLDOWN_DURATION
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent requests detected from this IP (more than 5). Your access is paused for 10 minutes. Please try again after 10 minutes."
        )

# Dynamic Cookie Resolver
def get_cookies_file() -> tuple[str | None, str]:
    """Find valid YouTube cookies from /etc/secrets, Base64 env var, or raw env var."""
    # 1. Check secret files in /etc/secrets
    sec_dir = Path("/etc/secrets")
    if sec_dir.exists():
        for f in sec_dir.rglob("*"):
            if f.is_file() and not f.name.startswith(".."):
                try:
                    sec_content = f.read_text(encoding="utf-8", errors="ignore")
                    if "youtube.com" in sec_content:
                        return str(f), f"secret_file:{f.name}"
                except Exception:
                    pass

    # 2. Check bundled cookies.txt inside backend directory
    bundled_cookie = Path(__file__).parent / "cookies.txt"
    if bundled_cookie.exists():
        try:
            b_content = bundled_cookie.read_text(encoding="utf-8", errors="ignore")
            if "youtube.com" in b_content and len(b_content.strip()) > 100:
                return str(bundled_cookie), "bundled_file:cookies.txt"
        except Exception:
            pass

    # 3. Check env vars (try Base64 first to avoid HTML newline stripping)
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

# SAFETY LIMITS (Prevents massive cloud bandwidth spikes, disk exhaustion & abuse bans)
MAX_CLIP_SECONDS = 300           # 5 minutes maximum duration per clip
MAX_FULL_VIDEO_SECONDS = 1800     # 30 minutes maximum for full video download
MAX_FILE_SIZE_LIMIT = "500M"      # Hard limit: abort download if stream > 500 MB

def estimate_clip_mb(duration_sec: int, quality: str) -> float:
    """Estimate clip file size in MB based on duration and format quality."""
    q = quality.lower()
    if any(k in q for k in ["audio", "mp3", "wav", "flac", "m4a", "320k", "256k", "192k", "128k"]):
        return duration_sec * 0.04
    elif "2160" in q or "4k" in q:
        return duration_sec * 1.5
    elif "1440" in q or "2k" in q:
        return duration_sec * 0.8
    elif "1080" in q:
        return duration_sec * 0.44
    elif "720" in q:
        return duration_sec * 0.23
    elif "480" in q:
        return duration_sec * 0.15
    elif any(k in q for k in ["360", "240", "144"]):
        return duration_sec * 0.08
    return duration_sec * 0.25

# QUALITY MAP: Prioritizes AVC (H.264) + AAC (M4A) for zero-transcode cutting, with clean height bounds
QUALITY_MAP = {
    "2160p": "bv*[height<=2160]+ba[ext=m4a]/bv*[height<=2160]+ba/bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    "1440p": "bv*[height<=1440]+ba[ext=m4a]/bv*[height<=1440]+ba/bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
    "1080p": "bv*[height<=1080][vcodec^=avc]+ba[ext=m4a]/bv*[height<=1080]+ba/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p":  "bv*[height<=720][vcodec^=avc]+ba[ext=m4a]/22/bv*[height<=720]+ba/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p":  "bv*[height<=480][vcodec^=avc]+ba[ext=m4a]/bv*[height<=480]+ba/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p":  "18/bv*[height<=360][vcodec^=avc]+ba[ext=m4a]/bv*[height<=360]+ba/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "240p":  "bv*[height<=240]+ba/bestvideo[height<=240]+bestaudio/best[height<=240]/best",
    "144p":  "bv*[height<=144]+ba/bestvideo[height<=144]+bestaudio/best[height<=144]/best",
    "320k":  "ba[ext=m4a]/ba/best",
    "256k":  "ba[ext=m4a]/ba/best",
    "192k":  "ba[ext=m4a]/ba/best",
    "128k":  "ba[ext=m4a]/ba/best",
    "wav":   "ba[ext=m4a]/ba/best",
    "flac":  "ba[ext=m4a]/ba/best",
    "m4a":   "ba[ext=m4a]/ba/best",
    "audio": "ba[ext=m4a]/ba/best",
}

def clean_quality_key(quality: str) -> str:
    if not quality:
        return "720p"
    q = quality.strip().lower()
    if "wav" in q:
        return "wav"
    if "flac" in q:
        return "flac"
    for k in ["320k", "256k", "192k", "128k"]:
        if k in q:
            return k
    if "320" in q: return "320k"
    if "256" in q: return "256k"
    if "192" in q: return "192k"
    if "128" in q: return "128k"
    if "m4a" in q: return "m4a"
    if "audio" in q or "mp3" in q: return "320k"
    for r in ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]:
        if r in q:
            return r
    return q.split()[0]

from urllib.parse import quote

def run_process(cmd: list, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run external command with nice priority on Linux to protect Uvicorn/SSH."""
    full_cmd = ["nice", "-n", "10"] + cmd if os.name != "nt" else cmd
    return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)

def build_ytdlp_cmd(extra: list = [], use_cookies: bool = True, player_client: str = "") -> list:
    cmd = [
        "yt-dlp",
        "-4",
        "--no-playlist",
        "--no-warnings",
        "--js-runtimes", "node",
        "--cache-dir", str(CACHE_DIR),
        "--no-check-formats",
        "--max-filesize", MAX_FILE_SIZE_LIMIT,
        "--add-header", "Accept-Language:en-US,en;q=0.9",
    ]
    if player_client:
        cmd += ["--extractor-args", f"youtube:player_client={player_client};skip=translated_subs,comments"]
    if use_cookies:
        cf, source = get_cookies_file()
        if cf:
            cmd += ["--cookies", cf]
    return cmd + extra

def fetch_oembed(url: str) -> dict | None:
    """Fast video metadata via YouTube oEmbed (instant 50ms, zero bot-checks)."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={quote(url)}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
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
        deno_res = run_process(["deno", "--version"], timeout=5)
        results["deno"] = deno_res.stdout.splitlines()[0] if deno_res.stdout else "empty"
    except Exception as e:
        results["deno"] = str(e)

    cmd_cookie = ["yt-dlp", "-v", "-4", "--cache-dir", str(CACHE_DIR)]
    if cf:
        cmd_cookie += ["--cookies", cf]
    cmd_cookie += ["-F", url]
    r1 = run_process(cmd_cookie, timeout=25)
    results["with_cookies"] = {
        "raw_stdout": r1.stdout[:1500] if r1.stdout else "",
        "raw_stderr": r1.stderr[:2000] if r1.stderr else "",
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
        "version": "4.3-instant-oembed-fast",
        "has_cookies": bool(cf),
        "cookie_source": source,
        "cookie_size": size,
        "cookie_lines": lines_count,
        "secret_files_found": sec_files
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.post("/info")
def get_video_info(req: InfoRequest):
    """Fast non-blocking info endpoint: returns instant oEmbed in 50ms, falls back to yt-dlp."""
    vid = get_video_id(req.url)
    info_json_file = CACHE_DIR / f"{vid}.info.json"
    now = time.time()

    # 1. Cache hit: instant 1ms response
    if vid in INFO_CACHE:
        cached_time, cached_data = INFO_CACHE[vid]
        if (now - cached_time) < INFO_CACHE_TTL:
            return cached_data

    # 2. Fast oEmbed check (50ms response! Instant video display!)
    oembed_data = fetch_oembed(req.url)
    if oembed_data:
        INFO_CACHE[vid] = (now, oembed_data)
        return oembed_data

    # 3. Fallback to lightweight yt-dlp only if oEmbed fails
    cmd = build_ytdlp_cmd(["--dump-json", req.url], player_client="")
    try:
        result = run_process(cmd, timeout=8)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            duration = int(data.get("duration") or 0)
            res_dict = {
                "title":       data.get("title", "YouTube Video"),
                "duration":    duration,
                "thumbnail":   data.get("thumbnail", ""),
                "resolutions": ["1080p", "720p", "480p", "360p", "Audio only"],
            }
            INFO_CACHE[vid] = (now, res_dict)
            return res_dict
    except Exception:
        pass

    raise HTTPException(400, detail="Could not fetch video info. Please verify the URL and try again.")


def _run_clip(job_id: str, url: str, start_hms: str, end_hms: str, quality: str):
    t_start = time.time()
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    q_key = clean_quality_key(quality)
    is_wav = (q_key == "wav")
    is_flac = (q_key == "flac")
    is_mp3 = q_key in ["320k", "256k", "192k", "128k"]
    is_audio = is_wav or is_flac or is_mp3 or (q_key in ["m4a", "audio"])
    ext = "wav" if is_wav else ("flac" if is_flac else ("mp3" if is_mp3 else ("m4a" if is_audio else "mp4")))

    merge_fmt_args = [] if is_audio else ["--merge-output-format", "mp4"]
    target_height = 720
    if q_key.endswith("p"):
        try:
            target_height = int(q_key[:-1])
        except ValueError:
            target_height = 720

    fallback_fmt = "ba[ext=m4a]/ba/best" if is_audio else f"bv*[height<={target_height}]+ba/bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]/best"
    out_path = out_dir / f"clip.{ext}"
    raw_path = out_dir / f"raw.{'m4a' if is_audio else 'mp4'}"
    fmt = QUALITY_MAP.get(q_key, QUALITY_MAP["720p"])

    vid = get_video_id(url)
    info_json_file = CACHE_DIR / f"{vid}.info.json"

    try:
        jobs[job_id]["status"] = "downloading"
        r_dl = None

        section_spec = f"*{start_hms}-{end_hms}"
        section_target = str(out_path if (not is_audio or ext == "m4a") else raw_path)

        # ATTEMPT 1: Fast section download with cookies and target resolution
        cmd_dl_section = build_ytdlp_cmd([
            "--download-sections", section_spec,
            "--force-keyframes-at-cuts",
            "-N", "1",
            "-f", fmt,
            *merge_fmt_args,
            "-o", section_target,
            url,
        ], use_cookies=True, player_client="")
        r_dl = run_process(cmd_dl_section, timeout=90)

        # ATTEMPT 2: Fallback section download with height-matching format with cookies
        target_check = out_path if (not is_audio or ext == "m4a") else raw_path
        if not target_check.exists() or target_check.stat().st_size == 0:
            cmd_dl_best = build_ytdlp_cmd([
                "--download-sections", section_spec,
                "--force-keyframes-at-cuts",
                "-N", "1",
                "-f", fallback_fmt,
                *merge_fmt_args,
                "-o", section_target,
                url,
            ], use_cookies=True, player_client="")
            r_dl = run_process(cmd_dl_best, timeout=90)

        # ATTEMPT 3: Section download fallback without cookies
        if not target_check.exists() or target_check.stat().st_size == 0:
            cmd_dl_anon = build_ytdlp_cmd([
                "--download-sections", section_spec,
                "--force-keyframes-at-cuts",
                "-N", "1",
                "-f", fallback_fmt,
                *merge_fmt_args,
                "-o", section_target,
                url,
            ], use_cookies=False, player_client="")
            r_dl = run_process(cmd_dl_anon, timeout=90)

        # Step 2: Audio Conversion / Local FFmpeg cut if needed
        if (not out_path.exists() or out_path.stat().st_size == 0) and raw_path.exists() and raw_path.stat().st_size > 0:
            jobs[job_id]["status"] = "processing"
            # Check if raw_path is already the trimmed section
            raw_dur = 0
            try:
                probe_res = run_process(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(raw_path)],
                    timeout=5
                )
                raw_dur = float(probe_res.stdout.strip() or 0)
            except Exception:
                pass

            # If raw_dur is close to clip length, it was already trimmed by section downloader
            is_pretrimmed = (0 < raw_dur <= (clip_len + 8))
            trim_args = [] if is_pretrimmed else ["-ss", start_hms, "-to", end_hms]

            if is_wav:
                cmd_cut = ["ffmpeg", "-y", "-threads", "1", *trim_args, "-i", str(raw_path), "-vn", "-c:a", "pcm_s16le", "-ar", "44100", str(out_path)]
                run_process(cmd_cut, timeout=30)
            elif is_flac:
                cmd_cut = ["ffmpeg", "-y", "-threads", "1", *trim_args, "-i", str(raw_path), "-vn", "-c:a", "flac", str(out_path)]
                run_process(cmd_cut, timeout=30)
            elif is_mp3:
                cmd_cut = ["ffmpeg", "-y", "-threads", "1", *trim_args, "-i", str(raw_path), "-vn", "-c:a", "libmp3lame", "-b:a", q_key, str(out_path)]
                run_process(cmd_cut, timeout=30)
            elif ext == "m4a":
                cmd_cut = ["ffmpeg", "-y", "-threads", "1", *trim_args, "-i", str(raw_path), "-vn", "-c:a", "copy", "-avoid_negative_ts", "make_zero", str(out_path)]
                run_process(cmd_cut, timeout=20)
                if not out_path.exists() or out_path.stat().st_size == 0:
                    cmd_cut_aac = ["ffmpeg", "-y", "-threads", "1", *trim_args, "-i", str(raw_path), "-vn", "-c:a", "aac", "-b:a", "256k", str(out_path)]
                    run_process(cmd_cut_aac, timeout=30)
            else:
                cmd_cut = [
                    "ffmpeg", "-y", "-threads", "1",
                    *trim_args,
                    "-i", str(raw_path),
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    str(out_path),
                ]
                run_process(cmd_cut, timeout=25)
                # If stream-copy produced an empty/failed file, transcode accurately at high visual quality (CRF 18)
                if not out_path.exists() or out_path.stat().st_size == 0:
                    cmd_cut_hq = [
                        "ffmpeg", "-y", "-threads", "1",
                        *trim_args,
                        "-i", str(raw_path),
                        "-c:v", "libx264",
                        "-preset", "veryfast",
                        "-crf", "18",
                        "-c:a", "aac",
                        "-b:a", "192k",
                        "-avoid_negative_ts", "make_zero",
                        str(out_path),
                    ]
                    run_process(cmd_cut_hq, timeout=60)

            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass

        elapsed = round(time.time() - t_start, 2)
        if out_path.exists() and out_path.stat().st_size > 0:
            res_str = ""
            if not is_audio:
                try:
                    ff = subprocess.run(
                        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(out_path)],
                        capture_output=True, text=True, timeout=5
                    )
                    res_str = ff.stdout.strip()
                except Exception:
                    pass
            jobs[job_id]["status"] = "done"
            jobs[job_id]["completed_at"] = time.time()
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_{'audio' if is_audio else 'clip'}_{start_hms.replace(':','-')}_{end_hms.replace(':','-')}.{ext}"
            jobs[job_id]["time_taken"] = elapsed
            if res_str:
                jobs[job_id]["resolution"] = res_str
        else:
            err = r_dl.stderr[-400:] if (r_dl and r_dl.stderr) else "Unknown download failure"
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Clip generation failed: {err}"
            jobs[job_id]["debug"] = {
                "r_dl_code": r_dl.returncode if r_dl else -1,
                "r_dl_stderr": err,
            }
            shutil.rmtree(out_dir, ignore_errors=True)
    except subprocess.TimeoutExpired:
        shutil.rmtree(out_dir, ignore_errors=True)
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Operation timed out. Video may be too large or restricted."
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"Server error: {str(e)}"


def _run_download(job_id: str, url: str, quality: str):
    t_start = time.time()
    out_dir = CLIPS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    q_key = clean_quality_key(quality)
    is_wav = (q_key == "wav")
    is_flac = (q_key == "flac")
    is_mp3 = q_key in ["320k", "256k", "192k", "128k"]
    is_audio = is_wav or is_flac or is_mp3 or (q_key in ["m4a", "audio"])
    ext = "wav" if is_wav else ("flac" if is_flac else ("mp3" if is_mp3 else ("m4a" if is_audio else "mp4")))

    out_path = out_dir / f"video.{ext}"
    raw_path = out_dir / f"raw.{'m4a' if is_audio else 'mp4'}"
    merge_fmt_args = [] if is_audio else ["--merge-output-format", "mp4"]
    target_height = 720
    if q_key.endswith("p"):
        try:
            target_height = int(q_key[:-1])
        except ValueError:
            target_height = 720

    fallback_fmt = "ba[ext=m4a]/ba/best" if is_audio else f"bv*[height<={target_height}]+ba/bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]/best"
    fmt = QUALITY_MAP.get(q_key, QUALITY_MAP["720p"])

    vid = get_video_id(url)
    info_json_file = CACHE_DIR / f"{vid}.info.json"

    # Safety Check: Never download full videos longer than MAX_FULL_VIDEO_SECONDS (30 mins)
    if info_json_file.exists():
        try:
            cdata = json.loads(info_json_file.read_text(encoding="utf-8"))
            cdur = int(cdata.get("duration") or 0)
            if cdur > MAX_FULL_VIDEO_SECONDS:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"Full video download is limited to videos under 30 minutes to preserve server resources. Please use the Clip Cutter tab for clips up to 10 minutes."
                return
        except Exception:
            pass

    try:
        jobs[job_id]["status"] = "downloading"
        r = None
        target_check = raw_path if is_audio else out_path

        # Attempt 1: Direct native download with cookies and target resolution
        cmd = build_ytdlp_cmd([
            "-N", "8",
            "--concurrent-fragments", "8",
            "-f", fmt,
            *merge_fmt_args,
            "-o", str(raw_path if is_audio else out_path),
            url,
        ], use_cookies=True, player_client="")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # Attempt 2: Using cached info.json if available
        if (not target_check.exists() or target_check.stat().st_size == 0) and info_json_file.exists() and info_json_file.stat().st_size > 1000:
            cmd_cached = build_ytdlp_cmd([
                "--load-info-json", str(info_json_file),
                "-N", "8",
                "--concurrent-fragments", "8",
                "-f", fmt,
                *merge_fmt_args,
                "-o", str(raw_path if is_audio else out_path),
            ], use_cookies=True, player_client="")
            r = subprocess.run(cmd_cached, capture_output=True, text=True, timeout=240)

        # Attempt 3: Fallback on height-matching fallback with cookies
        if not target_check.exists() or target_check.stat().st_size == 0:
            fallback_cmd = build_ytdlp_cmd([
                "-N", "2",
                "--concurrent-fragments", "2",
                "-f", fallback_fmt,
                *merge_fmt_args,
                "-o", str(raw_path if is_audio else out_path),
                url,
            ], use_cookies=True, player_client="")
            r = run_process(fallback_cmd, timeout=300)

        # Attempt 4: Fallback with target resolution without cookies
        if not target_check.exists() or target_check.stat().st_size == 0:
            fallback_anon = build_ytdlp_cmd([
                "-N", "2",
                "--concurrent-fragments", "2",
                "-f", fmt,
                *merge_fmt_args,
                "-o", str(raw_path if is_audio else out_path),
                url,
            ], use_cookies=False, player_client="")
            r = run_process(fallback_anon, timeout=300)

        # Attempt 5: Fallback on height-matching fallback without cookies
        if not target_check.exists() or target_check.stat().st_size == 0:
            fallback_safe = build_ytdlp_cmd([
                "-N", "1",
                "-f", fallback_fmt,
                *merge_fmt_args,
                "-o", str(raw_path if is_audio else out_path),
                url,
            ], use_cookies=False, player_client="")
            r = run_process(fallback_safe, timeout=300)

        # If audio, convert/extract cleanly with FFmpeg
        if is_audio and raw_path.exists() and raw_path.stat().st_size > 0:
            jobs[job_id]["status"] = "processing"
            if is_wav:
                cmd_conv = [
                    "ffmpeg", "-y", "-threads", "1",
                    "-i", str(raw_path),
                    "-vn",
                    "-c:a", "pcm_s16le",
                    "-ar", "44100",
                    str(out_path),
                ]
                run_process(cmd_conv, timeout=60)
            elif is_flac:
                cmd_conv = [
                    "ffmpeg", "-y", "-threads", "1",
                    "-i", str(raw_path),
                    "-vn",
                    "-c:a", "flac",
                    str(out_path),
                ]
                run_process(cmd_conv, timeout=60)
            elif is_mp3:
                cmd_conv = [
                    "ffmpeg", "-y", "-threads", "1",
                    "-i", str(raw_path),
                    "-vn",
                    "-c:a", "libmp3lame",
                    "-b:a", q_key,
                    str(out_path),
                ]
                subprocess.run(cmd_conv, capture_output=True, text=True, timeout=60)
            elif ext == "m4a":
                cmd_conv = [
                    "ffmpeg", "-y",
                    "-i", str(raw_path),
                    "-vn",
                    "-c:a", "copy",
                    str(out_path),
                ]
                subprocess.run(cmd_conv, capture_output=True, text=True, timeout=30)
                if not out_path.exists() or out_path.stat().st_size == 0:
                    cmd_conv_aac = [
                        "ffmpeg", "-y",
                        "-i", str(raw_path),
                        "-vn",
                        "-c:a", "aac",
                        "-b:a", "256k",
                        str(out_path),
                    ]
                    subprocess.run(cmd_conv_aac, capture_output=True, text=True, timeout=60)
            try:
                raw_path.unlink(missing_ok=True)
            except Exception:
                pass

        elapsed = round(time.time() - t_start, 2)
        if out_path.exists() and out_path.stat().st_size > 0:
            res_str = ""
            if not is_audio:
                try:
                    ff = subprocess.run(
                        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(out_path)],
                        capture_output=True, text=True, timeout=5
                    )
                    res_str = ff.stdout.strip()
                except Exception:
                    pass
            jobs[job_id]["status"] = "done"
            jobs[job_id]["completed_at"] = time.time()
            jobs[job_id]["file"] = str(out_path)
            jobs[job_id]["filename"] = f"ytcuttr_{'audio' if is_audio else 'video'}.{ext}"
            jobs[job_id]["time_taken"] = elapsed
            if res_str:
                jobs[job_id]["resolution"] = res_str
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = r.stderr[-400:] if (r and r.stderr) else "Download failed"
            jobs[job_id]["time_taken"] = elapsed
            shutil.rmtree(out_dir, ignore_errors=True)
    except subprocess.TimeoutExpired:
        shutil.rmtree(out_dir, ignore_errors=True)
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Download timed out."
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


def _queue_worker(job_id: str, run_func, *args):
    """FIFO Queue Worker: waits until an active slot (< 5) is available and this job is at the head."""
    wait_start = time.time()
    while True:
        with QUEUE_LOCK:
            if job_id not in jobs:
                _cleanup_ip_job(job_id)
                return

            # Timeout after 10 minutes of waiting
            if time.time() - wait_start > 600:
                if job_id in WAITING_QUEUE:
                    WAITING_QUEUE.remove(job_id)
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "Queue wait timeout. Please try again."
                _cleanup_ip_job(job_id)
                return

            if job_id in ACTIVE_JOBS:
                break

            if len(ACTIVE_JOBS) < MAX_CONCURRENT_JOBS and WAITING_QUEUE and WAITING_QUEUE[0] == job_id:
                WAITING_QUEUE.pop(0)
                ACTIVE_JOBS.add(job_id)
                jobs[job_id]["status"] = "starting"
                jobs[job_id]["queue_position"] = 0
                for idx, q_id in enumerate(WAITING_QUEUE):
                    if q_id in jobs:
                        jobs[q_id]["queue_position"] = idx + 1
                break
            elif job_id in WAITING_QUEUE:
                jobs[job_id]["queue_position"] = WAITING_QUEUE.index(job_id) + 1

        time.sleep(0.5)

    try:
        run_func(job_id, *args)
    finally:
        with QUEUE_LOCK:
            ACTIVE_JOBS.discard(job_id)
            _cleanup_ip_job(job_id)
            for idx, q_id in enumerate(WAITING_QUEUE):
                if q_id in jobs:
                    jobs[q_id]["queue_position"] = idx + 1


@app.get("/queue")
async def get_queue_info():
    with QUEUE_LOCK:
        now = time.time()
        for ip, ban_until in list(IP_COOLDOWNS.items()):
            if now >= ban_until:
                del IP_COOLDOWNS[ip]
        return {
            "active_jobs": len(ACTIVE_JOBS),
            "waiting_jobs": len(WAITING_QUEUE),
            "max_concurrent": MAX_CONCURRENT_JOBS,
            "max_queue_cap": MAX_QUEUE_CAP,
            "restricted_ips": len(IP_COOLDOWNS),
            "status": "healthy"
        }


@app.post("/clip")
async def create_clip(req: ClipRequest, request: Request, bg: BackgroundTasks):
    try:
        start_sec = parse_time(req.start)
        end_sec   = parse_time(req.end)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

    if end_sec <= start_sec:
        raise HTTPException(400, detail="End time must be after start time.")
    if (end_sec - start_sec) > MAX_CLIP_SECONDS:
        raise HTTPException(400, detail=f"Max clip length is {MAX_CLIP_SECONDS // 60} minutes ({MAX_CLIP_SECONDS} seconds).")
    if (end_sec - start_sec) < 1:
        raise HTTPException(400, detail="Clip duration must be at least 1 second.")

    client_ip = get_client_ip(request)
    clip_dur = end_sec - start_sec
    est_mb = estimate_clip_mb(clip_dur, req.quality)
    is_under_100mb = (est_mb < 100.0)

    with QUEUE_LOCK:
        check_ip_rate_limit(client_ip)

        if len(WAITING_QUEUE) >= MAX_QUEUE_CAP:
            raise HTTPException(429, detail="Server is currently at maximum capacity. Please wait a minute and try again.")

        job_id = str(uuid.uuid4())
        IP_ACTIVE_JOBS[client_ip].add(job_id)

        # 100 MB Rule: If clip is under 100 MB -> NO WAIT (Fast-Track execution).
        # Heavy clips (>= 100 MB) follow the 3-user concurrency queue: 4th user waits in queue.
        if is_under_100mb or len(ACTIVE_JOBS) < MAX_CONCURRENT_JOBS:
            ACTIVE_JOBS.add(job_id)
            jobs[job_id] = {
                "status": "starting",
                "queue_position": 0,
                "created_at": time.time(),
                "client_ip": client_ip,
                "max_concurrent": MAX_CONCURRENT_JOBS,
                "est_mb": round(est_mb, 1),
                "is_fast_track": is_under_100mb
            }
        else:
            WAITING_QUEUE.append(job_id)
            pos = len(WAITING_QUEUE)
            jobs[job_id] = {
                "status": "waiting",
                "queue_position": pos,
                "created_at": time.time(),
                "client_ip": client_ip,
                "max_concurrent": MAX_CONCURRENT_JOBS,
                "est_mb": round(est_mb, 1),
                "is_fast_track": False
            }

    bg.add_task(_queue_worker, job_id, _run_clip, req.url, to_hms(start_sec), to_hms(end_sec), req.quality)
    return {"job_id": job_id, "status": jobs[job_id]["status"], "queue_position": jobs[job_id]["queue_position"]}


@app.post("/download")
async def download_video(req: DownloadRequest, request: Request, bg: BackgroundTasks):
    # Safety Guard: Check video duration before accepting full download job
    vid = get_video_id(req.url)
    video_dur = 0
    if vid in INFO_CACHE:
        video_dur = int(INFO_CACHE[vid][1].get("duration") or 0)
    else:
        info_json_file = CACHE_DIR / f"{vid}.info.json"
        if info_json_file.exists():
            try:
                data = json.loads(info_json_file.read_text(encoding="utf-8"))
                video_dur = int(data.get("duration") or 0)
            except Exception:
                pass

    if video_dur > MAX_FULL_VIDEO_SECONDS:
        dur_hms = to_hms(video_dur)
        raise HTTPException(
            400,
            detail=f"Full video download is restricted to videos under 30 minutes (this video is {dur_hms} long) to prevent cloud bandwidth overload. Please use the Clip Cutter tab to extract your desired segment up to 5 minutes!"
        )

    client_ip = get_client_ip(request)
    clip_dur = video_dur if video_dur > 0 else 600
    est_mb = estimate_clip_mb(clip_dur, req.quality)
    is_under_100mb = (est_mb < 100.0)

    with QUEUE_LOCK:
        check_ip_rate_limit(client_ip)

        if len(WAITING_QUEUE) >= MAX_QUEUE_CAP:
            raise HTTPException(429, detail="Server is currently at maximum capacity. Please wait a minute and try again.")

        job_id = str(uuid.uuid4())
        IP_ACTIVE_JOBS[client_ip].add(job_id)

        # 100 MB Rule: If clip is under 100 MB -> NO WAIT (Fast-Track execution).
        # Heavy clips (>= 100 MB) follow the 3-user concurrency queue: 4th user waits in queue.
        if is_under_100mb or len(ACTIVE_JOBS) < MAX_CONCURRENT_JOBS:
            ACTIVE_JOBS.add(job_id)
            jobs[job_id] = {
                "status": "starting",
                "queue_position": 0,
                "created_at": time.time(),
                "client_ip": client_ip,
                "max_concurrent": MAX_CONCURRENT_JOBS,
                "est_mb": round(est_mb, 1),
                "is_fast_track": is_under_100mb
            }
        else:
            WAITING_QUEUE.append(job_id)
            pos = len(WAITING_QUEUE)
            jobs[job_id] = {
                "status": "waiting",
                "queue_position": pos,
                "created_at": time.time(),
                "client_ip": client_ip,
                "max_concurrent": MAX_CONCURRENT_JOBS,
                "est_mb": round(est_mb, 1),
                "is_fast_track": False
            }

    bg.add_task(_queue_worker, job_id, _run_download, req.url, req.quality)
    return {"job_id": job_id, "status": jobs[job_id]["status"], "queue_position": jobs[job_id]["queue_position"]}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    with QUEUE_LOCK:
        job_copy = dict(job)
        job_copy["active_jobs"] = len(ACTIVE_JOBS)
        job_copy["waiting_count"] = len(WAITING_QUEUE)
        job_copy["max_concurrent"] = MAX_CONCURRENT_JOBS
    return job_copy


async def delayed_cleanup(job_id: str, delay: int = 30):
    await asyncio.sleep(delay)
    shutil.rmtree(CLIPS_DIR / job_id, ignore_errors=True)
    with QUEUE_LOCK:
        _cleanup_ip_job(job_id)
        if job_id in WAITING_QUEUE:
            WAITING_QUEUE.remove(job_id)
        ACTIVE_JOBS.discard(job_id)
    jobs.pop(job_id, None)


def iterfile(file_path):
    with open(file_path, mode="rb") as f:
        while chunk := f.read(1024 * 1024):
            yield chunk

@app.get("/download-file/{job_id}")
@app.get("/file/{job_id}")
async def get_file(job_id: str, bg: BackgroundTasks):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, detail="File not ready")

    file_path = Path(job["file"])
    if not file_path.exists():
        raise HTTPException(404, detail="File not found")

    bg.add_task(delayed_cleanup, job_id, 30)
    file_size = file_path.stat().st_size
    filename = job.get("filename", "clip.mp4")

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
    }
    return StreamingResponse(
        iterfile(file_path),
        media_type="application/octet-stream",
        headers=headers,
    )
