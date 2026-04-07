"""OpenMontage Web API — FastAPI backend for the video production UI."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, File, UploadFile, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="OpenMontage", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Model cache (pre-warmed at startup) ─────────────────────────────────────

_WHISPER_MODEL: Any = None
_MUSICGEN_READY = False


def _prewarm_models() -> None:
    """Load heavy models into memory at startup so first request is instant."""
    global _WHISPER_MODEL, _MUSICGEN_READY
    try:
        import whisper  # type: ignore
        _WHISPER_MODEL = whisper.load_model("base")
    except Exception:
        pass
    try:
        from transformers import AutoProcessor, MusicgenForConditionalGeneration  # noqa: F401
        AutoProcessor.from_pretrained("facebook/musicgen-small")
        MusicgenForConditionalGeneration.from_pretrained("facebook/musicgen-small")
        _MUSICGEN_READY = True
    except Exception:
        pass


@app.on_event("startup")
async def startup_event() -> None:
    threading.Thread(target=_prewarm_models, daemon=True, name="prewarm").start()

# Job store — persisted to disk so restarts don't lose completed jobs
JOBS_DIR = Path("jobs")
JOBS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# Clip sessions (sheet → raw clips, no TTS/music/compose)
CLIPS_DIR = Path("clips")
CLIPS_DIR.mkdir(exist_ok=True)
CLIP_SESSIONS: dict[str, dict] = {}  # in-memory; also persisted to clips/{id}/session.json


def _clip_session_path(session_id: str) -> Path:
    return CLIPS_DIR / session_id / "session.json"


def _save_clip_session(session: dict) -> None:
    p = _clip_session_path(session["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_clip_session(session_id: str) -> dict | None:
    p = _clip_session_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _save_job(job: dict[str, Any]) -> None:
    try:
        _job_path(job["id"]).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_job(job_id: str) -> Optional[dict[str, Any]]:
    p = _job_path(job_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


# In-memory cache (populated from disk on first access)
JOBS: dict[str, dict[str, Any]] = {}

# ─── Static files ────────────────────────────────────────────────────────────

AUDIO_DIR = Path("audio")
AUDIO_DIR.mkdir(exist_ok=True)

app.mount("/projects", StaticFiles(directory="projects"), name="projects")
app.mount("/audio", StaticFiles(directory="audio"), name="audio")
app.mount("/clips", StaticFiles(directory="clips"), name="clips")
app.mount("/static", StaticFiles(directory="web"), name="static")


# ─── Config helpers ──────────────────────────────────────────────────────────

_ENV_FILE = Path(".env")


def _set_env_var(key: str, value: str) -> None:
    """Update .env file and os.environ immediately."""
    import re
    os.environ[key] = value
    env_text = _ENV_FILE.read_text(encoding="utf-8") if _ENV_FILE.exists() else ""
    safe_val = value.replace("\n", "\\n")
    pattern = rf'^{re.escape(key)}=.*$'
    replacement = f'{key}={safe_val}'
    if re.search(pattern, env_text, flags=re.MULTILINE):
        env_text = re.sub(pattern, replacement, env_text, flags=re.MULTILINE)
    else:
        env_text += f'\n{replacement}\n'
    _ENV_FILE.write_text(env_text, encoding="utf-8")


# ─── useapi.net config ────────────────────────────────────────────────────────

@app.get("/api/useapi/config")
def get_useapi_config():
    return JSONResponse({
        "token_set": bool(os.environ.get("USEAPI_TOKEN")),
        "email": os.environ.get("USEAPI_GOOGLE_FLOW_EMAIL", ""),
    })


@app.post("/api/useapi/config")
async def save_useapi_config(request: Request):
    body = await request.json()
    token = (body.get("token") or "").strip()
    email = (body.get("email") or "").strip()
    if not token or not email:
        return JSONResponse({"error": "token and email required"}, status_code=400)
    _set_env_var("USEAPI_TOKEN", token)
    _set_env_var("USEAPI_GOOGLE_FLOW_EMAIL", email)
    return JSONResponse({"ok": True})


# ─── Opera CDP control ───────────────────────────────────────────────────────

@app.get("/api/opera/status")
def opera_status():
    from tools.video.google_flow_video import _cdp_is_alive, _CDP_PORT
    alive = _cdp_is_alive()
    return JSONResponse({
        "running": alive,
        "port": _CDP_PORT,
        "message": "Opera đang chạy với CDP" if alive else "Opera chưa chạy với debug port",
    })


@app.post("/api/opera/launch")
def opera_launch():
    from tools.video.google_flow_video import _cdp_is_alive, _launch_opera_with_cdp, _CDP_PORT
    if _cdp_is_alive():
        return JSONResponse({"ok": True, "message": f"Opera đã chạy trên port {_CDP_PORT}"})
    ok = _launch_opera_with_cdp()
    if ok:
        return JSONResponse({"ok": True, "message": f"✅ Opera đã launch với CDP port {_CDP_PORT}. Đăng nhập Google Flow rồi generate."})
    return JSONResponse({"ok": False, "message": "❌ Không thể launch Opera. Kiểm tra /Applications/Opera.app"}, status_code=500)


# ─── Google Flow config ──────────────────────────────────────────────────────

@app.get("/api/google-flow/config")
def get_google_flow_config():
    email = os.environ.get("GOOGLE_FLOW_EMAIL", "")
    cookies_set = bool(os.environ.get("GOOGLE_FLOW_COOKIES"))
    return JSONResponse({"email": email, "cookies_set": cookies_set})


@app.post("/api/google-flow/login")
def start_google_flow_login():
    """Open Brave to Google Flow for user to login."""
    from tools.video.google_flow_video import _login_state
    if _login_state.get("status") in ("launching", "waiting_confirm"):
        return JSONResponse({"status": "already_running"})
    def _run():
        from tools.video.google_flow_video import launch_login_flow
        launch_login_flow()
    threading.Thread(target=_run, daemon=True, name="gflow-login").start()
    return JSONResponse({"status": "launched"})


@app.post("/api/google-flow/login/confirm")
def confirm_google_flow_login():
    """User confirms they have logged in — capture cookies from open browser."""
    from tools.video.google_flow_video import capture_cookies
    result = capture_cookies()
    return JSONResponse(result)


@app.get("/api/google-flow/login/status")
def google_flow_login_status():
    from tools.video.google_flow_video import _login_state
    return JSONResponse({
        "status": _login_state.get("status", "idle"),
        "email": _login_state.get("email") or os.environ.get("GOOGLE_FLOW_EMAIL", ""),
        "cookies_set": bool(_login_state.get("cookies") or os.environ.get("GOOGLE_FLOW_COOKIES")),
        "error": _login_state.get("error"),
    })


@app.post("/api/google-flow/config")
async def save_google_flow_config(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip()
    cookies = (body.get("cookies") or "").strip()
    if not email or not cookies:
        return JSONResponse({"error": "email and cookies required"}, status_code=400)

    try:
        _set_env_var("GOOGLE_FLOW_EMAIL", email)
        _set_env_var("GOOGLE_FLOW_COOKIES", cookies)
    except Exception as e:
        return JSONResponse({"error": f"Failed to write .env: {e}"}, status_code=500)

    return JSONResponse({"ok": True})


# ─── Audio library ───────────────────────────────────────────────────────────

@app.get("/api/audio/list")
def list_audio():
    """Return available local background music files from audio/ directory."""
    exts = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
    files = []
    for f in sorted(AUDIO_DIR.iterdir()):
        if f.suffix.lower() in exts:
            files.append({
                "filename": f.name,
                "url": f"/audio/{f.name}",
                "size_mb": round(f.stat().st_size / 1_048_576, 1),
            })
    return JSONResponse({"files": files})


@app.post("/api/audio/upload")
async def upload_audio(file: UploadFile = File(...)):
    """Upload a new background music file to the audio/ directory."""
    ALLOWED_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
    MAX_SIZE_MB = 50

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTS:
        return JSONResponse(
            {"error": f"Định dạng không hỗ trợ: {suffix}. Chấp nhận: {', '.join(ALLOWED_EXTS)}"},
            status_code=400,
        )

    content = await file.read()
    size_mb = len(content) / 1_048_576
    if size_mb > MAX_SIZE_MB:
        return JSONResponse(
            {"error": f"File quá lớn ({size_mb:.1f} MB). Tối đa {MAX_SIZE_MB} MB."},
            status_code=400,
        )

    # Sanitise filename — keep only safe chars
    import re as _re
    safe_name = _re.sub(r"[^\w\-. ]", "_", Path(file.filename).stem)
    safe_name = safe_name.strip(" ._")[:80] or "track"
    dest = AUDIO_DIR / f"{safe_name}{suffix}"

    # Avoid overwriting: append counter if name exists
    counter = 1
    while dest.exists():
        dest = AUDIO_DIR / f"{safe_name}_{counter}{suffix}"
        counter += 1

    dest.write_bytes(content)
    return JSONResponse({
        "ok": True,
        "filename": dest.name,
        "size_mb": round(size_mb, 1),
    })


@app.delete("/api/audio/{filename}")
def delete_audio(filename: str):
    """Delete a background music file from the audio/ directory."""
    # Prevent path traversal
    safe = Path(filename).name
    target = AUDIO_DIR / safe
    if not target.exists():
        return JSONResponse({"error": "File không tồn tại"}, status_code=404)
    target.unlink()
    return JSONResponse({"ok": True})


# ─── Voices ──────────────────────────────────────────────────────────────────

@app.get("/api/voices")
def get_voices():
    """Fetch available Vietnamese voices from Vbee."""
    import requests as req
    token = os.environ.get("VBEE_API_KEY")
    app_id = os.environ.get("VBEE_APP_ID")
    if not token:
        # Return hardcoded fallback voices
        return JSONResponse({"voices": _fallback_voices(), "source": "fallback"})
    try:
        r = req.get(
            "https://vbee.vn/api/public/v1/voices",
            headers={"Authorization": f"Bearer {token}", "App-Id": app_id or ""},
            params={"language_code": "vi-VN", "limit": 100},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            voices = data.get("result", data.get("data", []))
            return JSONResponse({"voices": voices, "source": "vbee"})
    except Exception:
        pass
    return JSONResponse({"voices": _fallback_voices(), "source": "fallback"})


def _fallback_voices() -> list[dict]:
    return [
        {"code": "s_sg_male_thientam_ytstable_vc", "name": "Thiện Tâm (Sài Gòn Nam)", "gender": "male", "language_code": "vi-VN"},
        {"code": "hn_male_manhdung_news_48k-fhg",  "name": "Mạnh Dũng (Hà Nội Nam)", "gender": "male", "language_code": "vi-VN"},
        {"code": "hn_male_thanhlong_talk_48k-fhg", "name": "Thanh Long (Hà Nội Nam)", "gender": "male", "language_code": "vi-VN"},
        {"code": "hn_female_ngochuyen_full_48k-fhg","name": "Ngọc Huyền (Hà Nội Nữ)", "gender": "female", "language_code": "vi-VN"},
        {"code": "sg_male_trungkien_vdts_48k-fhg",  "name": "Trung Kiên (Sài Gòn Nam)", "gender": "male", "language_code": "vi-VN"},
        {"code": "sg_female_lantrinh_vdts_48k-fhg", "name": "Lan Trinh (Sài Gòn Nữ)", "gender": "female", "language_code": "vi-VN"},
        {"code": "hue_female_huonggiang_full_48k-fhg","name": "Hương Giang (Huế Nữ)", "gender": "female", "language_code": "vi-VN"},
    ]


# ─── Generate ────────────────────────────────────────────────────────────────

@app.post("/api/generate")
async def generate_video(
    background_tasks: BackgroundTasks,
    script: str = Form(...),
    voice_code: str = Form("s_sg_male_thientam_ytstable_vc"),
    duration: int = Form(30),
    aspect_ratio: str = Form("9:16"),
    speed_rate: float = Form(0.85),
    llm_system_prompt: str = Form(""),
    veo_style_seed: str = Form(""),
    subtitle_enabled: bool = Form(True),
    subtitle_fontsize: int = Form(12),
    subtitle_color: str = Form("white"),
    subtitle_position: str = Form("bottom"),
    watermark_text: str = Form(""),
    watermark_position: str = Form("top_left"),
    enable_music: bool = Form(True),
    music_file: str = Form("bamboo-breath_bKoDGfET.wav"),
    image: Optional[UploadFile] = File(None),
    video: Optional[UploadFile] = File(None),
    watermark_image: Optional[UploadFile] = File(None),
):
    job_id = str(uuid.uuid4())[:8]
    project_slug = f"project-{job_id}"
    project_dir = Path(f"projects/{project_slug}")

    # Save uploaded files
    image_path = None
    video_path = None
    watermark_image_path = None
    if image and image.filename:
        image_path = str(UPLOAD_DIR / f"{job_id}_{image.filename}")
        with open(image_path, "wb") as f:
            f.write(await image.read())
    if video and video.filename:
        video_path = str(UPLOAD_DIR / f"{job_id}_{video.filename}")
        with open(video_path, "wb") as f:
            f.write(await video.read())
    if watermark_image and watermark_image.filename:
        watermark_image_path = str(UPLOAD_DIR / f"{job_id}_wm_{watermark_image.filename}")
        with open(watermark_image_path, "wb") as f:
            f.write(await watermark_image.read())

    JOBS[job_id] = {
        "id": job_id,
        "type": "video",
        "status": "queued",
        "progress": 0,
        "stage": "Đang khởi động...",
        "log": [],
        "output": None,
        "error": None,
        "created_at": time.time(),
        "params": {
            "script": script,
            "voice_code": voice_code,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "speed_rate": speed_rate,
            "llm_system_prompt": llm_system_prompt,
            "veo_style_seed": veo_style_seed,
            "subtitle_enabled": subtitle_enabled,
            "subtitle_fontsize": subtitle_fontsize,
            "subtitle_color": subtitle_color,
            "subtitle_position": subtitle_position,
            "watermark_text": watermark_text,
            "watermark_position": watermark_position,
            "enable_music": enable_music,
            "music_file": music_file,
            "image_path": image_path,
            "video_path": video_path,
            "watermark_image_path": watermark_image_path,
            "project_dir": str(project_dir),
            "project_slug": project_slug,
        },
    }

    background_tasks.add_task(run_pipeline, job_id)
    return JSONResponse({"job_id": job_id})


# ─── Job status ──────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id) or _load_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    JOBS[job_id] = job  # re-cache
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/download")
def download_video(job_id: str):
    job = JOBS.get(job_id) or _load_job(job_id)
    if not job or not job.get("output"):
        return JSONResponse({"error": "Video not ready"}, status_code=404)
    path = Path(job["output"])
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=f"openmontage_{job_id}.mp4")


# ─── Job list / stats / media ────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(status: str = None, limit: int = 100, offset: int = 0):
    """List all jobs from disk, newest first."""
    all_jobs: list[dict] = []
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
            if status and job.get("status") != status:
                continue
            # Omit heavy log to keep response small
            slim = {k: v for k, v in job.items() if k != "log"}
            all_jobs.append(slim)
        except Exception:
            pass
    total = len(all_jobs)
    return JSONResponse({"jobs": all_jobs[offset : offset + limit], "total": total})


@app.get("/api/stats")
def get_stats():
    """Aggregate stats from all jobs on disk."""
    from datetime import datetime, timezone
    counts: dict[str, int] = {"total": 0, "queued": 0, "running": 0, "done": 0, "error": 0}
    # Daily counts for last 7 days
    today = datetime.now(timezone.utc)
    days: dict[str, int] = {}
    for i in range(6, -1, -1):
        d = (today.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - i * 86400)
        label = datetime.fromtimestamp(d, tz=timezone.utc).strftime("%d/%m")
        days[label] = 0

    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        counts["total"] += 1
        s = job.get("status", "")
        if s in counts:
            counts[s] += 1
        created = job.get("created_at")
        if created:
            label = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%d/%m")
            if label in days:
                days[label] += 1

    return JSONResponse({**counts, "daily": days})


@app.get("/api/media")
def list_media():
    """List all generated video files from projects/."""
    items: list[dict] = []
    projects_dir = Path("projects")
    if not projects_dir.exists():
        return JSONResponse({"items": []})
    for proj in sorted(projects_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not proj.is_dir():
            continue
        renders = proj / "renders"
        if not renders.exists():
            continue
        for f in renders.glob("*.mp4"):
            job_id = proj.name.replace("project-", "")
            job = _load_job(job_id)
            items.append({
                "job_id": job_id,
                "filename": f.name,
                "url": f"/projects/{proj.name}/renders/{f.name}",
                "size_mb": round(f.stat().st_size / 1_048_576, 1),
                "created_at": f.stat().st_mtime,
                "aspect_ratio": (job or {}).get("params", {}).get("aspect_ratio", "9:16"),
                "script_preview": ((job or {}).get("params", {}).get("script", ""))[:80],
            })
    return JSONResponse({"items": items})


@app.post("/api/generate/batch")
async def generate_batch(request: Request, background_tasks: BackgroundTasks):
    """Create multiple jobs from a list of prompt objects."""
    body = await request.json()
    prompts: list[dict] = body.get("prompts", [])
    if not prompts:
        return JSONResponse({"error": "prompts list required"}, status_code=400)
    prompts = prompts[:20]  # hard cap

    batch_id = str(uuid.uuid4())[:8]
    job_ids: list[str] = []

    for item in prompts:
        job_id = str(uuid.uuid4())[:8]
        project_slug = f"project-{job_id}"
        project_dir = Path(f"projects/{project_slug}")
        JOBS[job_id] = {
            "id": job_id,
            "type": "video",
            "batch_id": batch_id,
            "status": "queued",
            "progress": 0,
            "stage": "Đang chờ...",
            "log": [],
            "output": None,
            "error": None,
            "created_at": time.time(),
            "params": {
                "script": item.get("script", ""),
                "voice_code": item.get("voice_code", "s_sg_male_thientam_ytstable_vc"),
                "duration": int(item.get("duration", 30)),
                "aspect_ratio": item.get("aspect_ratio", "9:16"),
                "speed_rate": float(item.get("speed_rate", 0.85)),
                "llm_system_prompt": item.get("llm_system_prompt", ""),
                "veo_style_seed": item.get("veo_style_seed", ""),
                "subtitle_enabled": bool(item.get("subtitle_enabled", True)),
                "subtitle_fontsize": int(item.get("subtitle_fontsize", 12)),
                "subtitle_color": item.get("subtitle_color", "white"),
                "subtitle_position": item.get("subtitle_position", "bottom"),
                "watermark_text": item.get("watermark_text", ""),
                "watermark_position": item.get("watermark_position", "top_left"),
                "enable_music": bool(item.get("enable_music", True)),
                "music_file": item.get("music_file", "bamboo-breath_bKoDGfET.wav"),
                "image_path": None,
                "video_path": None,
                "watermark_image_path": None,
                "project_dir": str(project_dir),
                "project_slug": project_slug,
            },
        }
        _save_job(JOBS[job_id])
        background_tasks.add_task(run_pipeline, job_id)
        job_ids.append(job_id)

    return JSONResponse({"batch_id": batch_id, "job_ids": job_ids, "count": len(job_ids)})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Delete a job record and its project directory."""
    job = JOBS.pop(job_id, None) or _load_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    # Remove job json
    _job_path(job_id).unlink(missing_ok=True)
    # Remove project dir
    proj = Path(job.get("params", {}).get("project_dir", f"projects/project-{job_id}"))
    if proj.exists():
        shutil.rmtree(proj, ignore_errors=True)
    return JSONResponse({"ok": True})


# ─── Clip Factory — generate raw 8s clips from Google Sheet ─────────────────

@app.get("/api/clips")
def list_clip_sessions():
    """List all clip sessions (newest first)."""
    sessions = []
    for d in sorted(CLIPS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        sess = CLIP_SESSIONS.get(d.name) or _load_clip_session(d.name)
        if sess:
            # Return slim version (no full row details)
            sessions.append({
                k: v for k, v in sess.items() if k != "rows"
            } | {"row_count": len(sess.get("rows", []))})
    return JSONResponse({"sessions": sessions})


@app.get("/api/clips/{session_id}")
def get_clip_session(session_id: str):
    sess = CLIP_SESSIONS.get(session_id) or _load_clip_session(session_id)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    CLIP_SESSIONS[session_id] = sess
    return JSONResponse(sess)


@app.post("/api/clips/start")
async def start_clip_session(request: Request, background_tasks: BackgroundTasks):
    """Read prompts from a Google Sheet and generate one raw 8s clip per row.

    Body:
      sheet_url: str         — Google Sheets URL (or spreadsheet ID)
      folder_name: str       — optional custom folder name (default: sheet gid)
      aspect_ratio: str      — "9:16" | "16:9" | "1:1"  (default: "9:16")
      model: str             — Veo model (default: "veo-3.1-fast")
      prompt_col: int        — 0-indexed column with prompts (default: 1 = col B)
      header_rows: int       — rows to skip at top (default: 1)
      col_link: str          — column letter to write clip URL (default: "C")
      col_updated: str       — column letter to write timestamp (default: "D")
      col_status: str        — column letter to write status (default: "E")
      max_rows: int          — max prompts to process (default: 50)
      concurrency: int       — parallel Veo requests 1-4 (default: 2)
    """
    body = await request.json()
    sheet_url = (body.get("sheet_url") or "").strip()
    if not sheet_url:
        return JSONResponse({"error": "sheet_url required"}, status_code=400)

    aspect_ratio = body.get("aspect_ratio", "9:16")
    model = body.get("model", "veo-3.1-fast")
    prompt_col = int(body.get("prompt_col", 1))
    header_rows = int(body.get("header_rows", 1))
    col_link = body.get("col_link", "C")
    col_updated = body.get("col_updated", "D")
    col_status = body.get("col_status", "E")
    max_rows = min(int(body.get("max_rows", 50)), 200)
    concurrency = max(1, min(int(body.get("concurrency", 2)), 4))

    # Read the sheet
    try:
        from tools.sheets.gsheets import read_csv, extract_prompts, parse_sheet_url
        rows = read_csv(sheet_url)
        prompts = extract_prompts(rows, prompt_col=prompt_col, header_rows=header_rows)[:max_rows]
    except Exception as e:
        return JSONResponse({"error": f"Failed to read sheet: {e}"}, status_code=400)

    if not prompts:
        return JSONResponse({"error": "No prompts found in sheet"}, status_code=400)

    _, gid = parse_sheet_url(sheet_url)
    session_id = str(uuid.uuid4())[:8]
    folder_name = (body.get("folder_name") or "").strip() or f"sheet_{gid}"
    folder = CLIPS_DIR / session_id
    folder.mkdir(parents=True, exist_ok=True)

    session: dict = {
        "id": session_id,
        "sheet_url": sheet_url,
        "folder_name": folder_name,
        "folder": str(folder),
        "status": "running",
        "created_at": time.time(),
        "aspect_ratio": aspect_ratio,
        "model": model,
        "total": len(prompts),
        "done": 0,
        "failed": 0,
        "col_link": col_link,
        "col_updated": col_updated,
        "col_status": col_status,
        "rows": [
            {
                "row_num": p["row_num"],
                "prompt": p["prompt"],
                "status": "pending",
                "clip_path": None,
                "clip_url": None,
                "error": None,
            }
            for p in prompts
        ],
    }
    CLIP_SESSIONS[session_id] = session
    _save_clip_session(session)

    background_tasks.add_task(run_clip_session, session_id, concurrency)
    return JSONResponse({"session_id": session_id, "total": len(prompts), "folder": str(folder)})


@app.delete("/api/clips/{session_id}")
def delete_clip_session(session_id: str):
    sess = CLIP_SESSIONS.pop(session_id, None)
    folder = CLIPS_DIR / session_id
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return JSONResponse({"ok": True})


def run_clip_session(session_id: str, concurrency: int = 2) -> None:
    """Background worker: generates one Veo clip per row using thread pool."""
    sess = CLIP_SESSIONS.get(session_id)
    if not sess:
        return

    from tools.sheets.gsheets import update_row
    from tools.video.google_flow_useapi import GoogleFlowUseAPI

    tool = GoogleFlowUseAPI()
    folder = Path(sess["folder"])
    aspect = sess["aspect_ratio"]
    model = sess["model"]
    sheet_url = sess["sheet_url"]
    col_link = sess["col_link"]
    col_updated = sess["col_updated"]
    col_status = sess["col_status"]

    # Map aspect_ratio "9:16" → "portrait", "16:9" → "landscape", "1:1" → "portrait"
    ar_map = {"9:16": "portrait", "16:9": "landscape", "1:1": "portrait"}
    veo_ar = ar_map.get(aspect, "portrait")

    def process_row(row: dict) -> None:
        row_num = row["row_num"]
        prompt = row["prompt"]
        row["status"] = "running"
        _save_clip_session(sess)

        # Update sheet: mark as Running
        update_row(sheet_url, row_num, col_link, col_updated, col_status, "", "⚡ Running")

        clip_filename = f"row_{row_num:03d}.mp4"
        clip_path = folder / clip_filename

        try:
            result = tool.execute({
                "prompt": prompt,
                "model": model,
                "aspect_ratio": veo_ar,
                "operation": "text_to_video",
                "output_path": str(clip_path),
            })

            if result.success:
                row["status"] = "done"
                row["clip_path"] = str(clip_path)
                row["clip_url"] = f"/clips/{session_id}/{clip_filename}"
                sess["done"] += 1
                update_row(sheet_url, row_num, col_link, col_updated, col_status,
                           f"http://localhost:8000/clips/{session_id}/{clip_filename}", "✅ Done")
            else:
                row["status"] = "error"
                row["error"] = result.error or "generation failed"
                sess["failed"] += 1
                update_row(sheet_url, row_num, col_link, col_updated, col_status,
                           "", f"❌ {(result.error or '')[:60]}")
        except Exception as e:
            row["status"] = "error"
            row["error"] = str(e)
            sess["failed"] += 1
            update_row(sheet_url, row_num, col_link, col_updated, col_status, "", f"❌ {str(e)[:60]}")

        _save_clip_session(sess)

    # Run with thread pool respecting concurrency limit
    pending_rows = [r for r in sess["rows"] if r["status"] == "pending"]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(process_row, row): row for row in pending_rows}
        for _ in as_completed(futures):
            pass  # results handled inside process_row

    sess["status"] = "done" if sess["failed"] == 0 else ("partial" if sess["done"] > 0 else "error")
    _save_clip_session(sess)


# ─── Pipeline runner (runs in background thread) ─────────────────────────────

def run_pipeline(job_id: str):
    job = JOBS[job_id]
    p = job["params"]

    # Thread-safe log helper (list.append is GIL-protected in CPython)
    def log(msg: str, progress: int = None, stage: str = None):
        job["log"].append(msg)
        if progress is not None:
            job["progress"] = progress
        if stage:
            job["stage"] = stage

    try:
        job["status"] = "running"
        project_dir = Path(p["project_dir"])
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "assets/audio").mkdir(parents=True, exist_ok=True)
        (project_dir / "assets/video").mkdir(parents=True, exist_ok=True)
        (project_dir / "assets/music").mkdir(parents=True, exist_ok=True)
        (project_dir / "renders").mkdir(parents=True, exist_ok=True)

        script = p["script"]
        voice_code = p["voice_code"]
        duration = p["duration"]
        aspect_ratio = p["aspect_ratio"]
        speed_rate = p["speed_rate"]
        image_path = p.get("image_path")
        video_path = p.get("video_path")
        subtitle_enabled = p.get("subtitle_enabled", False)
        subtitle_fontsize = p.get("subtitle_fontsize", 24)
        subtitle_color = p.get("subtitle_color", "white")
        subtitle_position = p.get("subtitle_position", "bottom")
        watermark_text = p.get("watermark_text", "")
        watermark_position = p.get("watermark_position", "bottom_right")
        watermark_image_path = p.get("watermark_image_path")
        enable_music = p.get("enable_music", True)
        music_file = p.get("music_file", "bamboo-breath_bKoDGfET.wav")
        llm_system_prompt = p.get("llm_system_prompt", "").strip()
        veo_style_seed = p.get("veo_style_seed", "").strip()

        narration_path = str(project_dir / "assets/audio/narration.mp3")
        music_path_str = str(project_dir / "assets/music/background.mp3")

        # ── Pre-compute scenes (instant, no I/O) ─────────────────────────────
        scenes = _plan_scenes(script, duration, aspect_ratio, image_path, video_path, llm_system_prompt, veo_style_seed)
        log(f"📋 {len(scenes)} scenes planned", progress=3, stage="Khởi động song song...")

        # ── Shared result holders ─────────────────────────────────────────────
        music_ok = False
        subtitle_srt = None
        clip_results: dict[int, dict] = {}
        errors: list[str] = []

        # ── Worker: TTS (+ Whisper when done) ────────────────────────────────
        def do_tts():
            log("🎙️ [TTS] Bắt đầu Vbee TTS...", stage="Tạo audio")
            from tools.audio.vbee_tts import VbeeTTS
            result = VbeeTTS().execute({
                "text": _preprocess_script_tts(script),
                "voice_code": voice_code,
                "speed_rate": speed_rate,
                "audio_type": "mp3",
                "output_path": narration_path,
            })
            if not result.success:
                errors.append(f"TTS failed: {result.error}")
                return
            log(f"✅ [TTS] Xong — {result.data.get('text_length')} ký tự")
            if subtitle_enabled:
                log("📝 [Whisper] Tạo phụ đề...")
                nonlocal subtitle_srt
                subtitle_srt = _generate_subtitles(narration_path, project_dir)
                log("✅ [Whisper] Phụ đề xong" if subtitle_srt else "⚠️ [Whisper] Thất bại")

        # ── Worker: Music — Local audio → Suno (nếu user chọn) → Không có ──────
        def do_music():
            nonlocal music_ok
            if not enable_music:
                log("⏩ [Nhạc] Bỏ qua")
                return

            # 1. Local audio file (mặc định, tức thì)
            local_src = AUDIO_DIR / music_file if music_file else None
            if local_src and local_src.exists():
                import shutil as _sh
                _sh.copy2(str(local_src), music_path_str)
                music_ok = True
                log(f"✅ [Nhạc] Local: {local_src.name}")
                return
            if music_file:
                log(f"⚠️ [Nhạc] Không tìm thấy {music_file} trong audio/ — thử Suno...")

            # 2. Suno (chỉ khi không có local hoặc file không tồn tại)
            from tools.audio.suno_music import SunoMusic
            suno = SunoMusic()
            if suno.get_status().name == "AVAILABLE":
                log("🎵 [Suno] Tạo nhạc nền...")
                result = suno.execute({
                    "prompt": (
                        "meditative ambient music, soft singing bowls, gentle drone, "
                        "temple bells, peaceful sacred atmosphere, instrumental"
                    ),
                    "instrumental": True,
                    "model": "V4",
                    "output_path": music_path_str,
                })
                if result.success:
                    music_ok = True
                    log(f"✅ [Suno] Nhạc xong: {result.data.get('title', 'track')}")
                    return
                log(f"⚠️ [Suno] Thất bại ({result.error})")

            log("⚠️ Không có nhạc nền — tiếp tục chỉ với narration")

        # Sliding window rate limiter: max 2 requests per 60s
        _veo_rl_lock = threading.Lock()
        _veo_rl_timestamps: list[float] = []

        def _veo_acquire():
            """Block until a Veo request slot is available (≤2 req/60s)."""
            while True:
                with _veo_rl_lock:
                    now = time.monotonic()
                    # Drop timestamps older than 60s
                    _veo_rl_timestamps[:] = [t for t in _veo_rl_timestamps if now - t < 60]
                    if len(_veo_rl_timestamps) < 2:
                        _veo_rl_timestamps.append(now)
                        return
                    oldest = min(_veo_rl_timestamps)
                    wait = 60 - (now - oldest) + 0.5
                time.sleep(wait)

        # Tool priority: useapi.net (stable, no cookies) → Direct HTTP → Playwright
        from tools.video.google_flow_useapi import GoogleFlowUseAPI
        from tools.video.google_flow_direct import GoogleFlowDirect
        from tools.video.google_flow_video import GoogleFlowVideo
        _useapi_tool = GoogleFlowUseAPI()
        _direct_tool = GoogleFlowDirect()
        _playwright_tool = GoogleFlowVideo()
        _use_useapi = _useapi_tool.get_status().name == "AVAILABLE"
        _use_direct = not _use_useapi and _direct_tool.get_status().name == "AVAILABLE"

        def do_clip(i: int, scene: dict):
            clip_path = str(project_dir / f"assets/video/scene-{i+1:02d}.mp4")
            model = "veo-3.1-fast-relaxed"
            inputs: dict[str, Any] = {
                "prompt": scene["prompt"],
                "model": model,
                "duration_seconds": 8,
                "aspect_ratio": "portrait" if aspect_ratio == "9:16" else "landscape",
                "output_path": clip_path,
            }
            if scene.get("image_path"):
                inputs["operation"] = "image_to_video"
                inputs["image_path"] = scene["image_path"]

            if _use_useapi:
                method = "useapi.net"
            elif _use_direct:
                method = "Direct"
            else:
                method = "Playwright"

            # ── Log full Veo prompt ───────────────────────────────────────────
            op = inputs.get("operation", "text_to_video")
            log(f"🎬 [Veo] Scene {i+1}/{len(scenes)} · {method} · {model} · {op}")
            log(f"   📝 Prompt: {scene['prompt']}")
            if inputs.get("image_path"):
                log(f"   🖼️  Image: {Path(inputs['image_path']).name}")
            log(f"   ⚙️  aspect={inputs['aspect_ratio']} duration={inputs['duration_seconds']}s")

            # Rate limit chỉ cần cho Direct/Playwright (gọi thẳng Google API).
            # useapi.net có queue riêng, không cần rate limit từ phía client.
            if not _use_useapi:
                _veo_acquire()

            def _done(label: str, result: Any) -> bool:
                if result.success:
                    clip_results[i] = {"path": clip_path, "duration": scene["duration"]}
                    done_count = len(clip_results)
                    log(f"✅ [{label}] Scene {i+1} xong ({done_count}/{len(scenes)})",
                        progress=40 + int(40 * done_count / len(scenes)))
                    return True
                return False

            # 1. useapi.net (preferred)
            if _use_useapi:
                result = _useapi_tool.execute(inputs)
                if _done("UseAPI", result):
                    return
                # Rate limited → retryable; bad token → stop
                if "401" in (result.error or "") or "Invalid USEAPI_TOKEN" in (result.error or ""):
                    errors.append(f"Scene {i+1} failed: {result.error}")
                    return
                log(f"⚠️ useapi.net thất bại ({result.error[:80]}) — fallback Direct HTTP")

            # 2. Direct HTTP (reverse-engineered Google API)
            if _use_useapi or _use_direct:
                result = _direct_tool.execute(inputs)
                if _done("Direct", result):
                    return
                if "expired" in (result.error or "").lower() or "401" in (result.error or ""):
                    errors.append(f"Scene {i+1} failed: {result.error}")
                    return
                log(f"⚠️ Direct API thất bại — fallback Playwright")

            # 3. Playwright browser automation (last resort)
            result = _playwright_tool.execute(inputs)
            if _done("Flow", result):
                return

            errors.append(f"Scene {i+1} failed: {result.error}")

        # ── Check Nano Banana availability ───────────────────────────────────────
        from tools.graphics.google_flow_image import GoogleFlowImage
        _img_tool = GoogleFlowImage()
        _use_nano_banana = _img_tool.get_status().name == "AVAILABLE"
        # Serialize browser sessions — GoogleFlowImage uses Playwright (same Opera profile)
        _img_browser_lock = threading.Lock()

        def do_image(i: int, scene: dict) -> Optional[str]:
            """Generate a scene image via Nano Banana Pro → feeds Veo as image-to-video."""
            out_dir = project_dir / "assets/images"
            out_dir.mkdir(parents=True, exist_ok=True)
            nb_prompt = _nano_banana_prompt(scene["prompt"], aspect_ratio)
            log(f"🖼️ [NanoBanana] Scene {i+1}/{len(scenes)}: {nb_prompt[:55]}...")
            with _img_browser_lock:
                result = _img_tool.execute({
                    "prompt": nb_prompt,
                    "model": "nano-banana-pro",
                    "aspect_ratio": aspect_ratio,
                    "count": 1,
                    "output_dir": str(out_dir),
                })
            if result.success:
                img_path = result.data.get("output")
                log(f"✅ [NanoBanana] Scene {i+1} ảnh xong: {Path(img_path).name}")
                if i == 0:
                    job["overview_image"] = img_path
                    _save_job(job)
                return img_path
            log(f"⚠️ [NanoBanana] Scene {i+1} thất bại: {(result.error or '')[:70]}")
            return None

        # ── Launch everything in parallel ─────────────────────────────────────
        # Phase: TTS + Music + NanoBanana images all run together.
        # Clips wait for their scene image (Future.result()), then submit to Veo.
        # Veo: 2 concurrent max (2 RPM Paid Tier 1); useapi.net queues server-side.
        if _use_nano_banana:
            log("🚀 Chạy song song: TTS + Nhạc + NanoBanana images + Video clips...", progress=5)
        else:
            log("🚀 Chạy song song: TTS + Nhạc + Video clips...", progress=5)

        n = len(scenes)
        # Workers: TTS + Music + N images + N clips (clips block on images, not CPU)
        total_workers = 2 + n + (n if _use_nano_banana else 0)

        with ThreadPoolExecutor(max_workers=max(total_workers, 4)) as pool:
            tts_fut = pool.submit(do_tts)
            music_fut = pool.submit(do_music)

            if _use_nano_banana:
                image_futs = [pool.submit(do_image, i, scene) for i, scene in enumerate(scenes)]

                def do_clip_with_image(i: int, scene: dict):
                    img_path = image_futs[i].result()   # wait for this scene's image
                    sc = dict(scene)
                    # Only inject generated image if user didn't upload one for this scene
                    if img_path and not sc.get("image_path"):
                        sc["image_path"] = img_path
                    do_clip(i, sc)

                clip_futs = [pool.submit(do_clip_with_image, i, scene) for i, scene in enumerate(scenes)]
            else:
                clip_futs = [pool.submit(do_clip, i, scene) for i, scene in enumerate(scenes)]

            # Wait in submission order; collect exceptions
            for fut in [tts_fut, music_fut, *clip_futs]:
                exc = fut.exception()
                if exc:
                    errors.append(str(exc))

        if errors:
            raise RuntimeError(errors[0])

        # ── Collect ordered clip list ─────────────────────────────────────────
        clip_paths = [clip_results[i] for i in sorted(clip_results)]
        if not clip_paths:
            raise RuntimeError("No video clips were generated")

        # Probe narration duration
        import subprocess
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "stream=duration",
             "-of", "csv=p=0", narration_path],
            capture_output=True, text=True,
        )
        narr_dur = float(probe.stdout.strip().split("\n")[0]) if probe.stdout.strip() else duration * 0.7
        log(f"   Narration: {narr_dur:.1f}s | Clips: {len(clip_paths)} | Nhạc: {'✅' if music_ok else '❌'}")

        # ── Compose ───────────────────────────────────────────────────────────
        log("🎞️ Đang render video cuối...", progress=82, stage="Render video")
        output_path = str(project_dir / "renders/final.mp4")
        _compose(
            clip_paths, narration_path,
            music_path_str if music_ok else None,
            output_path, duration,
            subtitle_srt=subtitle_srt,
            subtitle_fontsize=subtitle_fontsize,
            subtitle_color=subtitle_color,
            subtitle_position=subtitle_position,
            watermark_text=watermark_text,
            watermark_image_path=watermark_image_path,
            watermark_position=watermark_position,
        )
        log(f"✅ Video render xong: {output_path}", progress=100, stage="Hoàn thành")

        job["status"] = "done"
        job["output"] = output_path
        _save_job(job)

    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        job["stage"] = "Lỗi"
        job["log"].append(f"❌ Lỗi: {exc}")
        _save_job(job)


def _preprocess_script_tts(script: str) -> str:
    """Prepare script for TTS — convert artistic pauses to TTS-friendly form.

    … and ... → comma + space so TTS engine produces a natural brief pause.
    Consecutive blank lines collapsed so engine doesn't read silence weirdly.
    """
    import re
    text = script
    # Ellipsis variants → short pause (comma works across TTS engines)
    text = text.replace("…", ", ")
    text = re.sub(r'\.{3,}', ", ", text)
    # Collapse multiple spaces/commas created by substitution
    text = re.sub(r',\s*,', ",", text)
    text = re.sub(r'  +', " ", text)
    return text.strip()


def _parse_script_chunks(script: str) -> list[dict]:
    """Split script into semantic chunks with scene type annotations.

    Priority:
      1. [SCENE] / [SCENE:CLOSING] explicit markers (user-controlled)
      2. Paragraph breaks (double newline)
      3. Single line breaks (fallback)

    Returns list of {"text": str, "type": "default"|"closing"}.
    """
    import re

    CLOSING_KEYWORDS = ["nam mô", "a di đà", "niệm phật", "nam mo", "a di da"]

    def _detect_type(text: str, force_closing: bool = False) -> str:
        if force_closing:
            return "closing"
        low = text.lower()
        if any(kw in low for kw in CLOSING_KEYWORDS):
            return "closing"
        return "default"

    # 1. [SCENE] marker parsing
    if re.search(r'\[SCENE', script, re.IGNORECASE):
        parts = re.split(r'(\[SCENE(?::[A-Z_]+)?\])', script, flags=re.IGNORECASE)
        chunks = []
        current_type = "default"
        for part in parts:
            m = re.match(r'\[SCENE(?::([A-Z_]+))?\]', part.strip(), re.IGNORECASE)
            if m:
                tag = (m.group(1) or "DEFAULT").upper()
                current_type = "closing" if tag == "CLOSING" else "default"
            elif part.strip():
                chunks.append({"text": part.strip(), "type": current_type})
        if chunks:
            return chunks

    # 2. Paragraph breaks
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', script) if p.strip()]
    if len(paragraphs) >= 2:
        result = []
        for i, p in enumerate(paragraphs):
            is_last = (i == len(paragraphs) - 1)
            result.append({"text": p, "type": _detect_type(p, force_closing=False)})
        return result

    # 3. Line fallback
    lines = [l.strip() for l in script.split("\n") if l.strip()]
    return [{"text": l, "type": _detect_type(l)} for l in lines] if lines else [{"text": script, "type": "default"}]


_VEO_VISUAL_TEMPLATES = [
    "cinematic meditation scene, golden light, sacred Buddhist temple atmosphere, slow motion 4K",
    "golden lotus flower floating on still water, warm amber light, peaceful sacred atmosphere",
    "soft golden particles of light floating in dark space, ethereal meditative atmosphere",
    "ancient Buddha statue with warm golden aura, incense smoke, serene sacred atmosphere",
    "person meditating in lotus position, golden light emanating from within, inner peace",
    "wide shot of tranquil temple garden, golden hour light, lotus pond, sacred stillness",
]
_VEO_CLOSING_TEMPLATE = (
    "ancient Buddha statue with warm golden aura, incense smoke spiraling upward, "
    "Nam Mo A Di Da Phat, soft devotional light, sacred atmosphere, cinematic slow motion 4K"
)


def _plan_scenes_llm(
    script: str, duration: int, aspect_ratio: str,
    image_path: Optional[str], llm_prompt: str, veo_style: str,
    n_scenes: int,
) -> list[dict]:
    """Use Claude to generate optimised Veo prompts from script + context."""
    import anthropic, json

    style_hint = veo_style or "cinematic slow motion, golden hour, 4K, Buddhist meditation aesthetics"
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=(
            llm_prompt or
            "You are a creative director for Buddhist meditation video content. "
            "Generate cinematic, emotionally resonant visual prompts for Google Veo."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Script:\n{script}\n\n"
                f"Generate exactly {n_scenes} Veo video prompts for this script.\n"
                f"Style: {style_hint}\n"
                f"Aspect ratio: {aspect_ratio}\n"
                f"Rules:\n"
                f"- Each prompt must be visual and cinematic (no text, no abstract concepts)\n"
                f"- Last prompt should feature a Buddha statue or closing sacred visual\n"
                f"- Return ONLY a JSON array: [{{\"prompt\": \"...\"}}]\n"
                f"- No markdown, no explanation, just the JSON array."
            ),
        }],
    )

    raw = resp.content[0].text.strip()
    # Strip possible markdown code fences
    import re
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    data = json.loads(raw)

    scenes = []
    for i, item in enumerate(data[:n_scenes]):
        scene: dict[str, Any] = {
            "id": i + 1,
            "prompt": item["prompt"],
            "duration": 8.0,
        }
        if i == 0 and image_path:
            scene["image_path"] = image_path
        scenes.append(scene)
    return scenes


def _nano_banana_prompt(veo_prompt: str, aspect_ratio: str) -> str:
    """Convert a Veo motion prompt → Nano Banana Pro static image brief.

    Follows the Perfect Prompt formula: Subject + Action + Context + Composition + Lighting.
    Strips video-specific motion directives that confuse image generators.
    """
    import re
    p = veo_prompt
    # Drop motion-only phrases
    for phrase in [
        "slow motion", "camera pan", "tracking shot", "dolly", "zoom in", "zoom out",
        "camera move", "cinematic motion", "4K", "8K", "loop", "seamless loop",
    ]:
        p = re.sub(re.escape(phrase), "", p, flags=re.IGNORECASE)
    p = re.sub(r"\s{2,}", " ", p).strip(" ,")
    # Append Nano Banana quality anchors (no keyword soup — full sentences)
    orient = "vertical composition" if aspect_ratio == "9:16" else "horizontal composition"
    p = (
        f"{p}. "
        f"Rendered with cinematic depth of field, 85mm lens at f/2.0. "
        f"Warm volumetric golden light with soft atmospheric haze. "
        f"{orient.capitalize()}, photorealistic, 4K production quality."
    )
    return p


def _plan_scenes(
    script: str, duration: int, aspect_ratio: str,
    image_path: Optional[str], video_path: Optional[str],
    llm_system_prompt: str = "", veo_style_seed: str = "",
) -> list[dict]:
    """Plan video scenes with visual prompts.

    1. Try LLM-based planning via Claude (if ANTHROPIC_API_KEY set).
    2. Fallback: rule-based with [SCENE] markers → paragraphs → lines.

    Clip count respects Veo 2 RPM limit:
      ≤32s → 2 clips, ≤60s → 3, else → 4.
    """
    import math

    raw_needed = math.ceil(duration / 8)
    if duration <= 32:
        n_scenes = 2
    elif duration <= 60:
        n_scenes = min(raw_needed, 3)
    else:
        n_scenes = min(raw_needed, 4)
    n_scenes = max(2, n_scenes)

    # ── LLM path (fast, high quality) ────────────────────────────────────────
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _plan_scenes_llm(
                script, duration, aspect_ratio, image_path,
                llm_system_prompt, veo_style_seed, n_scenes,
            )
        except Exception:
            pass  # fall through to rule-based

    # ── Rule-based path ───────────────────────────────────────────────────────
    chunks = _parse_script_chunks(script)

    scenes = []
    for i in range(n_scenes):
        chunk_idx = int(i * len(chunks) / n_scenes)
        chunk = chunks[chunk_idx] if chunk_idx < len(chunks) else chunks[-1]
        text = chunk["text"]
        is_closing = chunk["type"] == "closing" or (
            i == n_scenes - 1 and any(
                kw in text.lower()
                for kw in ["nam mô", "a di đà", "niệm phật", "nam mo", "a di da"]
            )
        )
        template = _VEO_CLOSING_TEMPLATE if is_closing else _VEO_VISUAL_TEMPLATES[i % len(_VEO_VISUAL_TEMPLATES)]
        prompt = f"{text[:80]}, {template}"
        if veo_style_seed:
            prompt = f"{prompt}, {veo_style_seed}"

        scene: dict[str, Any] = {
            "id": i + 1,
            "prompt": prompt,
            "duration": 8.0,
        }
        if i == 0 and image_path:
            scene["image_path"] = image_path
        scenes.append(scene)

    return scenes


def _generate_subtitles(narration_path: str, project_dir: Path) -> Optional[str]:
    """Whisper transcription → SRT file for Vietnamese narration."""
    try:
        import whisper  # type: ignore
        model = _WHISPER_MODEL or whisper.load_model("base")
        result = model.transcribe(narration_path, language="vi", task="transcribe")
        srt_path = project_dir / "assets/audio/subtitles.srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(result.get("segments", []), 1):
                start = _srt_time(seg["start"])
                end = _srt_time(seg["end"])
                text = seg["text"].strip()
                if text:
                    f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
        return str(srt_path)
    except Exception:
        return None


def _srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wm_xy(position: str) -> tuple[str, str]:
    """Return FFmpeg drawtext x/y expressions for a named position."""
    pad = "20"
    positions = {
        "top_left":     ("20", "20"),
        "top_center":   ("(w-text_w)/2", "20"),
        "top_right":    ("w-text_w-20", "20"),
        "center":       ("(w-text_w)/2", "(h-text_h)/2"),
        "bottom_left":  ("20", "h-text_h-20"),
        "bottom_center":("(w-text_w)/2", "h-text_h-20"),
        "bottom_right": ("w-text_w-20", "h-text_h-20"),
    }
    return positions.get(position, (f"w-text_w-{pad}", f"h-text_h-{pad}"))


def _compose(
    clip_paths: list[dict],
    narration_path: str,
    music_path: Optional[str],
    output_path: str,
    duration: int,
    subtitle_srt: Optional[str] = None,
    subtitle_fontsize: int = 24,
    subtitle_color: str = "white",
    subtitle_position: str = "bottom",
    watermark_text: str = "",
    watermark_image_path: Optional[str] = None,
    watermark_position: str = "bottom_right",
):
    """FFmpeg: trim + concat clips, mix audio, burn subtitles, apply watermark."""
    import subprocess

    if not clip_paths:
        raise RuntimeError("No clips to compose")

    out_dir = Path(output_path).parent

    # ── Trim each clip ────────────────────────────────────────────────────────
    trimmed = []
    for i, clip in enumerate(clip_paths):
        clip_file = Path(clip["path"]).resolve()   # absolute path
        if not clip_file.exists():
            continue
        trimmed_path = clip_file.parent / f"trim_{i+1:02d}.mp4"
        trim_dur = min(clip["duration"], 8.0)
        r = subprocess.run([
            "ffmpeg", "-y", "-i", str(clip_file),
            "-t", str(trim_dur), "-an",
            "-c:v", "libx264", "-crf", "20",
            str(trimmed_path)
        ], capture_output=True)
        if trimmed_path.exists():
            trimmed.append(trimmed_path)

    if not trimmed:
        raise RuntimeError("All clip trim steps failed — no trimmed files produced")

    # ── Concat with xfade crossfade ───────────────────────────────────────────
    import math
    XFADE_DUR = 0.5  # seconds crossfade between clips

    concat_file = out_dir / "concat.txt"  # kept for cleanup tracking
    total_clip_dur = sum(min(c["duration"], 8.0) for c in clip_paths if Path(c["path"]).exists())
    loop_count = max(0, math.ceil(duration / total_clip_dur) - 1) if total_clip_dur > 0 else 0

    raw_video = str(out_dir / "raw_video.mp4")
    concat_once_path = out_dir / "concat_once.mp4"

    if len(trimmed) == 1:
        # Single clip — no xfade needed
        concat_once = str(trimmed[0])
    else:
        # Build xfade filter_complex chain
        # offset_i = (clip_dur - xfade_dur) * i  for i-th transition
        clip_dur = 8.0
        inputs_args: list[str] = []
        for t in trimmed:
            inputs_args += ["-i", str(t)]

        filter_parts: list[str] = []
        prev = "[0:v]"
        for i in range(1, len(trimmed)):
            offset = (clip_dur - XFADE_DUR) * i
            out_label = "[v]" if i == len(trimmed) - 1 else f"[x{i:02d}]"
            filter_parts.append(
                f"{prev}[{i}:v]xfade=transition=fade:duration={XFADE_DUR}:offset={offset:.2f}{out_label}"
            )
            prev = out_label

        r = subprocess.run([
            "ffmpeg", "-y", *inputs_args,
            "-filter_complex", ";".join(filter_parts),
            "-map", "[v]",
            "-c:v", "libx264", "-crf", "20",
            str(concat_once_path),
        ], capture_output=True)
        if not concat_once_path.exists():
            # xfade failed (e.g. old FFmpeg) — fall back to plain concat
            with open(concat_file, "w") as f:
                for t in trimmed:
                    abs_path = str(t.resolve()).replace("'", "'\\''")
                    f.write(f"file '{abs_path}'\n")
            r = subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_file), "-an",
                "-c:v", "libx264", "-crf", "20",
                str(concat_once_path),
            ], capture_output=True)
            if not concat_once_path.exists():
                raise RuntimeError(f"FFmpeg concat failed: {r.stderr.decode(errors='replace')[-400:]}")
        concat_once = str(concat_once_path)

    if loop_count > 0:
        r = subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", str(loop_count),
            "-i", concat_once, "-an",
            "-c:v", "libx264", "-crf", "20",
            "-t", str(duration), raw_video,
        ], capture_output=True)
    else:
        r = subprocess.run([
            "ffmpeg", "-y", "-i", concat_once, "-an",
            "-c:v", "libx264", "-crf", "20",
            "-t", str(duration), raw_video,
        ], capture_output=True)

    if not Path(raw_video).exists():
        raise RuntimeError(f"FFmpeg concat/loop failed: {r.stderr.decode(errors='replace')[-400:]}")

    # ── Build video filter chain (subtitles + watermark) ─────────────────────
    vf_parts: list[str] = []

    if subtitle_srt and Path(subtitle_srt).exists():
        # Escape colons in path (Windows-safe; on POSIX paths shouldn't have colons)
        srt_escaped = subtitle_srt.replace("'", "\\'")
        margin_v = 40 if subtitle_position == "bottom" else 0
        # Map color name to ASS hex color (&HAABBGGRR format, AA=00 = opaque)
        color_map = {
            "white":  "&H00FFFFFF",
            "yellow": "&H0000FFFF",
            "cyan":   "&H00FFFF00",
            "orange": "&H000080FF",
            "red":    "&H000000FF",
        }
        ass_color = color_map.get(subtitle_color, "&H00FFFFFF")
        force_style = (
            f"FontName=Arial,FontSize={subtitle_fontsize},"
            f"PrimaryColour={ass_color},"
            f"OutlineColour=&H00000000,Outline=2,Shadow=1,"
            f"Alignment={'2' if subtitle_position == 'bottom' else '8'},"
            f"MarginV={margin_v}"
        )
        vf_parts.append(f"subtitles='{srt_escaped}':force_style='{force_style}'")

    if watermark_text:
        x, y = _wm_xy(watermark_position)
        # Escape single quotes in watermark text
        safe_text = watermark_text.replace("'", "\\'").replace(":", "\\:")
        vf_parts.append(
            f"drawtext=text='{safe_text}':x={x}:y={y}:"
            f"fontsize=24:fontcolor=white@0.75:"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.5"
        )

    # ── Audio mix → final output ──────────────────────────────────────────────
    # Decide if we need an image watermark overlay (separate FFmpeg input)
    use_wm_img = watermark_image_path and Path(watermark_image_path).exists()

    # Step A: audio mix (with or without music) → intermediary if we still need vf
    need_vf = bool(vf_parts) or use_wm_img
    audio_out = str(out_dir / "with_audio.mp4") if need_vf else output_path

    vf_str = ",".join(vf_parts) if vf_parts else None

    if music_path and Path(music_path).exists():
        audio_filter = (
            f"[1:a]apad,atrim=0:{duration}[narr];"
            f"[2:a]atrim=0:{duration},"
            f"afade=t=in:st=0:d=1,"
            f"afade=t=out:st={duration-3}:d=3,"
            f"volume=0.2[music];"
            f"[narr][music]amix=inputs=2:duration=longest:dropout_transition=3[aout]"
        )
        # -stream_loop -1 loops the music file indefinitely; atrim in filter cuts to duration
        cmd = [
            "ffmpeg", "-y",
            "-i", raw_video,
            "-i", narration_path,
            "-stream_loop", "-1", "-i", music_path,
            "-filter_complex", audio_filter,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration), audio_out,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", raw_video,
            "-i", narration_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac",
            "-t", str(duration), audio_out,
        ]
    r = subprocess.run(cmd, capture_output=True)
    if not Path(audio_out).exists():
        raise RuntimeError(f"FFmpeg audio mix failed: {r.stderr.decode(errors='replace')[-400:]}")

    # Step B: apply subtitle/text watermark vf if needed
    if need_vf:
        if use_wm_img:
            # Scale watermark image to ~15% of video width, then overlay
            wm_pos_map = {
                "top_left": "10:10",
                "top_right": "main_w-overlay_w-10:10",
                "bottom_left": "10:main_h-overlay_h-10",
                "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
                "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
            }
            ov_xy = wm_pos_map.get(watermark_position, "main_w-overlay_w-10:main_h-overlay_h-10")

            # Build filter: scale wm image, then overlay, then optional subtitle/drawtext
            fc_parts = [
                f"[1:v]scale=iw*0.15:-1,format=rgba,colorchannelmixer=aa=0.75[wm]",
                f"[0:v][wm]overlay={ov_xy}[ovr]",
            ]
            last_label = "[ovr]"
            if vf_parts:
                # vf_parts need to be applied after overlay using setpts trick
                vf_chain = ",".join(vf_parts)
                fc_parts.append(f"{last_label}{vf_chain}[final]")
                last_label = "[final]"
            else:
                fc_parts[-1] = fc_parts[-1].replace("[ovr]", "[final]")
                last_label = "[final]"

            fc_str = ";".join(fc_parts)
            subprocess.run([
                "ffmpeg", "-y",
                "-i", audio_out,
                "-i", watermark_image_path,
                "-filter_complex", fc_str,
                "-map", last_label, "-map", "0:a",
                "-c:v", "libx264", "-crf", "20",
                "-c:a", "copy",
                output_path,
            ], capture_output=True)
        elif vf_str:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", audio_out,
                "-vf", vf_str,
                "-c:v", "libx264", "-crf", "20",
                "-c:a", "copy",
                output_path,
            ], capture_output=True)
        else:
            # no-op, already at output_path
            import shutil as _shutil
            _shutil.copy2(audio_out, output_path)

    # ── Cleanup intermediate files ────────────────────────────────────────────
    intermediates = [
        raw_video,
        audio_out if audio_out != output_path else None,
        str(out_dir / "concat_once.mp4"),
        str(concat_file),
        *[str(t) for t in trimmed],
    ]
    for f in intermediates:
        if f:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass


# ─── Serve UI ────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse("web/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
