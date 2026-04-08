"""Local TTS via F5-TTS — zero-shot voice cloning, runs fully on-device.

Chạy on Apple Silicon M-series via MPS backend (PyTorch) hoặc MLX.
Không cần internet sau khi download model lần đầu.

Setup (một lần duy nhất):
  1. Tạo virtual env với Python 3.13:
       python3.13 -m venv ~/.venv-tts
       source ~/.venv-tts/bin/activate
  2. Install:
       pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
       pip install f5-tts
  3. Lấy voice sample từ Vbee (10-30 giây WAV/MP3)
  4. Set trong .env:
       LOCAL_TTS_ENABLED=true
       LOCAL_TTS_VENV=~/.venv-tts
       LOCAL_TTS_VOICE_SAMPLE=audio/voice_samples/thientam.wav
       LOCAL_TTS_REF_TEXT=  (để trống → auto-transcribe bằng Whisper)

Model sẽ tự download từ HuggingFace lần đầu chạy (~3GB).
Các lần sau: instant load từ cache ~/.cache/huggingface/
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

_VENV_PYTHON_KEY = "LOCAL_TTS_VENV"          # path to venv dir or python binary
_SAMPLE_KEY = "LOCAL_TTS_VOICE_SAMPLE"        # path to reference WAV/MP3
_REF_TEXT_KEY = "LOCAL_TTS_REF_TEXT"          # text spoken in sample (auto if empty)
_ENABLED_KEY = "LOCAL_TTS_ENABLED"            # "true" to enable


def _resolve_python() -> str | None:
    """Return the python binary to use for F5-TTS inference."""
    venv = os.environ.get(_VENV_PYTHON_KEY, "").strip()
    if not venv:
        return None
    venv_path = Path(venv).expanduser()
    # Accept either venv dir or direct python path
    if venv_path.is_dir():
        candidates = [
            venv_path / "bin" / "python",
            venv_path / "bin" / "python3",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    elif venv_path.exists() and os.access(str(venv_path), os.X_OK):
        return str(venv_path)
    return None


def _check_f5tts_available(python: str) -> bool:
    """Check if f5-tts is importable in the target Python environment."""
    try:
        r = subprocess.run(
            [python, "-c", "import f5_tts; print('ok')"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False


# ── Subprocess runner script (injected as string, avoids temp file issues) ────

_RUNNER_SCRIPT = """
import sys, json, os, time, tempfile

args = json.loads(sys.argv[1])
ref_file  = args["ref_file"]
ref_text  = args.get("ref_text", "")
gen_text  = args["gen_text"]
out_path  = args["output_path"]
_seed     = args.get("seed", -1)
seed      = None if _seed == -1 else int(_seed)
speed     = float(args.get("speed", 1.0))

import soundfile as sf

# ── Trim ref audio to 8s max — F5-TTS clips at 12s and loses sync with ref_text
MAX_REF_SECS = 8
ref_data, ref_sr = sf.read(ref_file)
if len(ref_data) / ref_sr > MAX_REF_SECS:
    ref_data = ref_data[:int(ref_sr * MAX_REF_SECS)]
    tmp_ref = tempfile.mktemp(suffix=".wav")
    sf.write(tmp_ref, ref_data, ref_sr)
    ref_file = tmp_ref
    ref_text = ""  # force re-transcribe to match trimmed audio

# Auto-transcribe reference if no ref_text
if not ref_text:
    try:
        import whisper
        wm = whisper.load_model("base")
        result = wm.transcribe(ref_file, language="vi")
        ref_text = result["text"].strip()
    except Exception as e:
        ref_text = "Đây là giọng đọc mẫu tiếng Việt."

from f5_tts.api import F5TTS
model = F5TTS()

wav, sr, _ = model.infer(
    ref_file=ref_file,
    ref_text=ref_text,
    gen_text=gen_text,
    seed=seed,
    speed=speed,
)

sf.write(out_path, wav, sr)
print(json.dumps({"ok": True, "output": out_path, "sr": sr, "ref_text": ref_text}))
"""


class LocalTTS(BaseTool):
    """F5-TTS local voice cloning — zero-shot, Apple M-series optimised."""

    name = "local_tts"
    version = "1.0.0"
    tier = ToolTier.VOICE
    capability = "text_to_speech"
    provider = "local_f5tts"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["f5-tts>=1.0", "soundfile", "torch"]
    install_instructions = (
        "python3.13 -m venv ~/.venv-tts && source ~/.venv-tts/bin/activate\n"
        "pip install torch torchaudio f5-tts soundfile\n"
        "Set LOCAL_TTS_VENV=~/.venv-tts in .env\n"
        "Set LOCAL_TTS_VOICE_SAMPLE=audio/voice_samples/sample.wav in .env"
    )
    agent_skills = ["tts", "voice-cloning"]

    capabilities = ["text_to_speech", "voice_cloning"]
    supports = {"voice_cloning": True, "vietnamese": True, "local": True}
    best_for = [
        "Chạy hoàn toàn offline sau khi setup",
        "Clone giọng từ 10-30s audio sample",
        "Apple M-series: tốc độ ~50x realtime",
        "Không tốn API credit",
    ]
    not_good_for = [
        "Chưa setup venv + f5-tts",
        "Máy không có LOCAL_TTS_VOICE_SAMPLE",
    ]
    fallback_tools = ["vbee_tts"]

    input_schema = {
        "type": "object",
        "required": ["text", "output_path"],
        "properties": {
            "text": {"type": "string"},
            "output_path": {"type": "string"},
            "ref_audio": {
                "type": "string",
                "description": "Path to reference audio. Defaults to LOCAL_TTS_VOICE_SAMPLE env var.",
            },
            "ref_text": {
                "type": "string",
                "description": "Text spoken in ref_audio. Auto-transcribed if empty.",
            },
            "speed": {
                "type": "number",
                "default": 1.0,
                "minimum": 0.5,
                "maximum": 2.0,
            },
            "seed": {
                "type": "integer",
                "default": -1,
                "description": "-1 = random",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=4096, vram_mb=0, disk_mb=3500, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout"])
    idempotency_key_fields = ["text", "ref_audio", "seed"]
    side_effects = ["writes audio file to output_path"]
    user_visible_verification = ["Listen to generated audio for voice quality"]

    def _python(self) -> str | None:
        return _resolve_python()

    def get_status(self) -> ToolStatus:
        if os.environ.get(_ENABLED_KEY, "").lower() != "true":
            return ToolStatus.UNAVAILABLE
        py = self._python()
        if not py:
            return ToolStatus.UNAVAILABLE
        sample = os.environ.get(_SAMPLE_KEY, "").strip()
        if not sample or not Path(sample).expanduser().exists():
            return ToolStatus.UNAVAILABLE
        if not _check_f5tts_available(py):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0  # fully local, no API cost

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        py = self._python()
        if not py:
            return ToolResult(
                success=False,
                error=f"No Python found. Set {_VENV_PYTHON_KEY} in .env.\n{self.install_instructions}",
            )

        # Resolve reference audio
        ref_audio = (inputs.get("ref_audio") or "").strip()
        if not ref_audio:
            ref_audio = os.environ.get(_SAMPLE_KEY, "").strip()
        if ref_audio:
            ref_audio = str(Path(ref_audio).expanduser().resolve())
        if not ref_audio or not Path(ref_audio).exists():
            return ToolResult(
                success=False,
                error=f"Voice sample không tìm thấy: '{ref_audio}'. Set {_SAMPLE_KEY} trong .env.",
            )

        ref_text = (inputs.get("ref_text") or os.environ.get(_REF_TEXT_KEY, "")).strip()
        output_path = str(Path(inputs["output_path"]).resolve())
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Build args payload
        args = json.dumps({
            "ref_file": ref_audio,
            "ref_text": ref_text,
            "gen_text": inputs["text"],
            "output_path": output_path,
            "seed": int(inputs.get("seed", -1)),
            "speed": float(inputs.get("speed", 1.0)),
        })

        start = time.time()
        try:
            r = subprocess.run(
                [py, "-c", _RUNNER_SCRIPT, args],
                capture_output=True,
                text=True,
                timeout=300,  # 5 min max — first run downloads model
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="F5-TTS timeout (>5 min). Model đang download?")
        except Exception as e:
            return ToolResult(success=False, error=f"Subprocess error: {e}")

        elapsed = time.time() - start

        if r.returncode != 0:
            err = (r.stderr or r.stdout or "")[-500:]
            return ToolResult(success=False, error=f"F5-TTS failed:\n{err}")

        # Parse output
        try:
            # Last JSON line from stdout
            for line in reversed(r.stdout.strip().splitlines()):
                if line.startswith("{"):
                    data = json.loads(line)
                    break
            else:
                data = {}
        except Exception:
            data = {}

        if not Path(output_path).exists() or Path(output_path).stat().st_size < 100:
            return ToolResult(success=False, error=f"Output file missing or empty. stderr: {r.stderr[-300:]}")

        return ToolResult(
            success=True,
            data={
                "provider": "local_f5tts",
                "output": output_path,
                "ref_audio": ref_audio,
                "text_length": len(inputs["text"]),
                "sample_rate": data.get("sr", 24000),
            },
            artifacts=[output_path],
            cost_usd=0.0,
            duration_seconds=round(elapsed, 2),
            model="f5-tts/F5-TTS",
        )


# ── Convenience: smart TTS that tries LocalTTS → Vbee fallback ────────────────

def smart_tts(inputs: dict[str, Any]) -> ToolResult:
    """Try LocalTTS (F5-TTS) first; fall back to Vbee on failure or unavailability.

    Drop-in replacement for VbeeTTS().execute(inputs) in pipelines.
    Logs are returned inside ToolResult.data["tts_engine"] and "tts_note".
    """
    # Reload .env so vars set after server start are picked up
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass

    local = LocalTTS()
    status = local.get_status()

    if status == ToolStatus.AVAILABLE:
        result = local.execute(inputs)
        if result.success:
            if result.data:
                result.data["tts_engine"] = "local_f5tts"
            return result
        fallback_note = f"LocalTTS failed: {(result.error or '')[:120]}"
    else:
        # Build a human-readable reason for UNAVAILABLE
        reasons = []
        if os.environ.get(_ENABLED_KEY, "").lower() != "true":
            reasons.append(f"{_ENABLED_KEY} not set to 'true'")
        elif not _resolve_python():
            reasons.append(f"{_VENV_PYTHON_KEY} not set or venv not found")
        else:
            sample = os.environ.get(_SAMPLE_KEY, "").strip()
            if not sample:
                reasons.append(f"{_SAMPLE_KEY} not set")
            elif not Path(sample).expanduser().exists():
                reasons.append(f"voice sample not found: {sample}")
            else:
                reasons.append("f5-tts not importable in venv")
        fallback_note = "LocalTTS unavailable — " + "; ".join(reasons)

    from tools.audio.vbee_tts import VbeeTTS
    result = VbeeTTS().execute(inputs)
    if result.success and result.data:
        result.data["tts_engine"] = "vbee"
        result.data["tts_note"] = fallback_note
    elif not result.success and result.data is None:
        result = ToolResult(
            success=result.success,
            data={"tts_engine": "vbee", "tts_note": fallback_note},
            error=result.error,
        )
    return result
