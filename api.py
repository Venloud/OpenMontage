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

from fastapi import FastAPI, Form, File, UploadFile, BackgroundTasks
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

app.mount("/projects", StaticFiles(directory="projects"), name="projects")
app.mount("/static", StaticFiles(directory="web"), name="static")


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
    speed_rate: float = Form(0.9),
    system_prompt: str = Form(""),
    subtitle_enabled: bool = Form(False),
    subtitle_fontsize: int = Form(24),
    subtitle_color: str = Form("white"),
    subtitle_position: str = Form("bottom"),
    watermark_text: str = Form(""),
    watermark_position: str = Form("bottom_right"),
    enable_music: bool = Form(True),
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
        "status": "queued",
        "progress": 0,
        "stage": "Đang khởi động...",
        "log": [],
        "output": None,
        "error": None,
        "params": {
            "script": script,
            "voice_code": voice_code,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "speed_rate": speed_rate,
            "system_prompt": system_prompt,
            "subtitle_enabled": subtitle_enabled,
            "subtitle_fontsize": subtitle_fontsize,
            "subtitle_color": subtitle_color,
            "subtitle_position": subtitle_position,
            "watermark_text": watermark_text,
            "watermark_position": watermark_position,
            "enable_music": enable_music,
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

        narration_path = str(project_dir / "assets/audio/narration.mp3")
        music_path_str = str(project_dir / "assets/music/background.mp3")

        # ── Pre-compute scenes (instant, no I/O) ─────────────────────────────
        scenes = _plan_scenes(script, duration, aspect_ratio, image_path, video_path)
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
                "text": script,
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

        # ── Worker: Music (Suno → MusicGen fallback) ─────────────────────────
        _music_prompt = (
            "meditative ambient music, soft singing bowls, gentle drone, "
            "temple bells, peaceful sacred atmosphere, instrumental"
        )

        def do_music():
            nonlocal music_ok
            if not enable_music:
                log("⏩ [Nhạc] Bỏ qua — chế độ nhanh")
                return
            # 1. Try Suno first
            from tools.audio.suno_music import SunoMusic
            suno = SunoMusic()
            if suno.get_status().name == "AVAILABLE":
                log("🎵 [Suno] Bắt đầu tạo nhạc nền...")
                result = suno.execute({
                    "prompt": _music_prompt,
                    "instrumental": True,
                    "model": "V4",
                    "output_path": music_path_str,
                })
                if result.success:
                    music_ok = True
                    log(f"✅ [Suno] Nhạc xong: {result.data.get('title', 'track')}")
                    return
                log(f"⚠️ [Suno] Thất bại ({result.error}) — thử MusicGen local...")
            else:
                log("⚠️ [Suno] Không khả dụng — dùng MusicGen local...")

            # 2. Fallback: Meta MusicGen (local, royalty-free)
            # Generate only 15s → loop in FFmpeg → ~70s faster than generating full duration
            from tools.audio.meta_musicgen import MetaMusicGen
            mg = MetaMusicGen()
            if mg.get_status().name == "AVAILABLE":
                gen_dur = 15  # short clip, will be looped to fill video duration
                log(f"🎸 [MusicGen] Tạo nhạc {gen_dur}s (loop → {duration}s)...")
                result = mg.execute({
                    "prompt": _music_prompt,
                    "duration_seconds": gen_dur,
                    "model": "facebook/musicgen-small",
                    "output_path": music_path_str,
                })
                if result.success:
                    music_ok = True
                    log(f"✅ [MusicGen] Nhạc xong: {result.data.get('duration_seconds')}s (CC0, không bản quyền)")
                    return
                log(f"⚠️ [MusicGen] Thất bại ({result.error})")

            log("⚠️ Không có nhạc nền — tiếp tục chỉ với narration")

        # Model fallback chain: fast first, then standard when rate-limited
        _VEO_MODELS = [
            "veo-3.1-fast-generate-preview",  # 2 RPM, 10 RPD — fastest
            "veo-3.0-generate-001",            # 2 RPM, 10 RPD — separate quota
            "veo-2.0-generate-001",            # stable fallback
        ]
        # Semaphore: Paid Tier 1 allows 2 RPM — never send more than 2 at once
        _veo_sem = threading.Semaphore(2)

        def _is_rate_limit(error_str: str) -> bool:
            s = error_str.upper()
            return "429" in s or "RESOURCE_EXHAUSTED" in s or "RATE" in s or "QUOTA" in s

        def do_clip(i: int, scene: dict):
            clip_path = str(project_dir / f"assets/video/scene-{i+1:02d}.mp4")
            from tools.video.google_veo_native import GoogleVeoNative
            veo = GoogleVeoNative()
            base_inputs: dict[str, Any] = {
                "prompt": scene["prompt"],
                "duration_seconds": 8,
                "aspect_ratio": aspect_ratio,
                "output_path": clip_path,
            }
            if scene.get("image_path"):
                base_inputs["operation"] = "image_to_video"
                base_inputs["image_path"] = scene["image_path"]

            last_error = ""
            for model in _VEO_MODELS:
                inputs = {**base_inputs, "model": model}
                log(f"🎬 [Veo] Scene {i+1}/{len(scenes)} [{model.split('-')[1]}]: {scene['prompt'][:45]}...")
                with _veo_sem:               # respect 2 RPM hard limit
                    result = veo.execute(inputs)

                if result.success:
                    clip_results[i] = {"path": clip_path, "duration": scene["duration"]}
                    done = len(clip_results)
                    log(f"✅ [Veo] Scene {i+1} xong ({done}/{len(scenes)}) [{model.split('-')[1]}]",
                        progress=40 + int(40 * done / len(scenes)))
                    return

                last_error = result.error or ""
                if _is_rate_limit(last_error):
                    log(f"⚠️ [Veo] {model.split('-')[1]} rate-limited — thử model tiếp...")
                    time.sleep(61)           # wait out the RPM window before switching
                else:
                    # Non-rate-limit error: retry once then try next model
                    time.sleep(5)
                    with _veo_sem:
                        result2 = veo.execute(inputs)
                    if result2.success:
                        clip_results[i] = {"path": clip_path, "duration": scene["duration"]}
                        done = len(clip_results)
                        log(f"✅ [Veo] Scene {i+1} retry OK ({done}/{len(scenes)})",
                            progress=40 + int(40 * done / len(scenes)))
                        return
                    last_error = result2.error or last_error

            errors.append(f"Scene {i+1} failed (all models exhausted): {last_error}")

        # ── Launch everything in parallel ─────────────────────────────────────
        # Veo: 2 concurrent max (matches 2 RPM Paid Tier 1 limit)
        # TTS + Music run in parallel alongside Veo
        log("🚀 Chạy song song: TTS + Nhạc + Video clips...", progress=5)
        max_veo = min(len(scenes), 2)          # hard cap at 2 = RPM limit
        total_workers = 2 + max_veo            # TTS, Music, N×Veo

        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            tts_fut = pool.submit(do_tts)
            music_fut = pool.submit(do_music)
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


def _plan_scenes(script: str, duration: int, aspect_ratio: str,
                  image_path: Optional[str], video_path: Optional[str]) -> list[dict]:
    """Split script into scenes with visual prompts.

    Uses as few clips as possible (each 8s) then loops in FFmpeg to fill duration.
    This respects Veo's 2 RPM limit and minimises generation time.

    Clips needed: ceil(duration / 8), capped at:
      - 2 clips for duration ≤ 32s   (fits in 1 RPM batch)
      - 3 clips for duration ≤ 60s   (2 batches max)
      - 4 clips for longer videos
    """
    lines = [l.strip() for l in script.strip().split("\n") if l.strip()]
    if not lines:
        lines = [script[:200]]

    import math
    raw_needed = math.ceil(duration / 8)       # clips needed without looping
    if duration <= 32:
        n_scenes = 2                            # 1 RPM batch, loop to fill
    elif duration <= 60:
        n_scenes = min(raw_needed, 3)
    else:
        n_scenes = min(raw_needed, 4)
    n_scenes = max(2, n_scenes)

    scene_dur = 8.0   # always generate 8s clips; loop fills remaining time

    # Visual prompt templates from the script text
    visual_templates = [
        "cinematic meditation scene, golden light, sacred Buddhist temple atmosphere, slow motion 4K",
        "golden lotus flower floating on still water, warm amber light, peaceful sacred atmosphere",
        "soft golden particles of light floating in dark space, ethereal meditative atmosphere",
        "ancient Buddha statue with warm golden aura, incense smoke, serene sacred atmosphere",
        "person meditating in lotus position, golden light emanating from within, inner peace",
        "wide shot of tranquil temple garden, golden hour light, lotus pond, sacred stillness",
    ]

    scenes = []
    for i in range(n_scenes):
        line_idx = int(i * len(lines) / n_scenes)
        text = lines[line_idx] if line_idx < len(lines) else lines[-1]
        prompt = f"{text[:80]}, {visual_templates[i % len(visual_templates)]}"

        scene: dict[str, Any] = {
            "id": i + 1,
            "prompt": prompt,
            "duration": scene_dur,
        }
        # Use uploaded image for first scene if provided
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

    # ── Concat ────────────────────────────────────────────────────────────────
    # Use absolute paths so FFmpeg resolves correctly regardless of concat.txt location
    concat_file = out_dir / "concat.txt"
    with open(concat_file, "w") as f:
        for t in trimmed:
            abs_path = str(t.resolve()).replace("'", "'\\''")  # escape single quotes
            f.write(f"file '{abs_path}'\n")

    # Total duration of all trimmed clips
    total_clip_dur = sum(min(c["duration"], 8.0) for c in clip_paths if Path(c["path"]).exists())
    import math
    loop_count = max(0, math.ceil(duration / total_clip_dur) - 1) if total_clip_dur > 0 else 0

    raw_video = str(out_dir / "raw_video.mp4")
    if loop_count > 0:
        # Concat first, then loop the entire sequence to fill duration
        concat_out = str(out_dir / "concat_once.mp4")
        r = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file), "-an",
            "-c:v", "libx264", "-crf", "20",
            concat_out
        ], capture_output=True)
        if not Path(concat_out).exists():
            raise RuntimeError(f"FFmpeg concat failed: {r.stderr.decode(errors='replace')[-400:]}")
        # Loop and trim to exact duration
        r = subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", str(loop_count),
            "-i", concat_out, "-an",
            "-c:v", "libx264", "-crf", "20",
            "-t", str(duration), raw_video
        ], capture_output=True)
    else:
        r = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file), "-an",
            "-c:v", "libx264", "-crf", "20",
            "-t", str(duration), raw_video
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


# ─── Serve UI ────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse("web/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
