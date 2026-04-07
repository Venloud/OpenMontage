"""Meta MusicGen local AI music generation via HuggingFace transformers.

Completely offline after first model download. Output music belongs to you -
no licensing, no copyright, no API costs.

Models (downloaded on first use, cached in ~/.cache/huggingface):
  facebook/musicgen-small  ~300 MB -- fast, good for short clips
  facebook/musicgen-medium ~1.5 GB -- better quality

Requires: torch (already installed) + transformers + scipy
"""

from __future__ import annotations

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


class MetaMusicGen(BaseTool):
    name = "meta_musicgen"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "meta"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["torch", "transformers>=4.31", "scipy"]
    install_instructions = "pip3 install transformers scipy"
    fallback_tools = ["suno_music", "music_gen"]
    agent_skills = ["music"]

    capabilities = ["generate_background_music", "generate_instrumental"]
    supports = {
        "instrumental": True,
        "vocals": False,
        "offline": True,
        "royalty_free": True,
        "no_api_key": True,
    }
    best_for = [
        "royalty-free music -- output fully owned by you",
        "offline / no-internet generation",
        "zero API cost after model download",
        "short background clips (10-60s)",
    ]
    not_good_for = [
        "vocals / lyrics",
        "very long tracks >2 min (slow on CPU)",
        "first run without internet (needs model download ~300 MB)",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Music description, e.g. 'meditative ambient, soft piano, Buddhist temple'",
            },
            "duration_seconds": {
                "type": "number",
                "default": 30,
                "minimum": 5,
                "maximum": 120,
                "description": "Target duration in seconds",
            },
            "model": {
                "type": "string",
                "enum": ["facebook/musicgen-small", "facebook/musicgen-medium"],
                "default": "facebook/musicgen-small",
                "description": "small=~300MB fast; medium=~1.5GB better quality",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=4096, vram_mb=0, disk_mb=500, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=[])
    idempotency_key_fields = ["prompt", "duration_seconds", "model"]
    side_effects = ["writes audio file to output_path", "downloads model on first run"]
    user_visible_verification = ["Listen to generated music for mood and quality"]

    def get_status(self) -> ToolStatus:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            import torch
            from transformers import AutoProcessor, MusicgenForConditionalGeneration
        except ImportError:
            return ToolResult(
                success=False,
                error="transformers not installed. Run: pip3 install transformers scipy",
            )

        start = time.time()
        prompt = inputs["prompt"]
        duration = float(inputs.get("duration_seconds", 30))
        model_id = inputs.get("model", "facebook/musicgen-small")
        output_path = Path(inputs.get("output_path", "musicgen_output.wav"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            processor = AutoProcessor.from_pretrained(model_id)
            model = MusicgenForConditionalGeneration.from_pretrained(model_id)
            model.eval()

            # MusicGen EnCodec: 32kHz, ~50 tokens/sec
            tokens = int(duration * 51.2)

            enc_inputs = processor(text=[prompt], padding=True, return_tensors="pt")

            with torch.no_grad():
                audio_values = model.generate(
                    **enc_inputs,
                    max_new_tokens=tokens,
                    do_sample=True,
                    guidance_scale=3.0,
                )

            # audio_values shape: (batch, channels, samples)
            sampling_rate = model.config.audio_encoder.sampling_rate
            samples = audio_values[0, 0].cpu().numpy()

            # Write WAV first, then convert to MP3 if needed
            if output_path.suffix.lower() == ".mp3":
                wav_path = output_path.with_suffix(".wav")
            else:
                wav_path = output_path

            import numpy as np
            import scipy.io.wavfile as wav_io
            wav_io.write(str(wav_path), sampling_rate, (samples * 32767).astype(np.int16))

            if output_path.suffix.lower() == ".mp3":
                import subprocess
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(wav_path), "-b:a", "128k", str(output_path)],
                    capture_output=True,
                )
                wav_path.unlink(missing_ok=True)
                final_path = output_path
            else:
                final_path = wav_path

            actual_duration = round(len(samples) / sampling_rate, 1)

        except Exception as exc:
            return ToolResult(success=False, error=f"MusicGen failed: {exc}")

        return ToolResult(
            success=True,
            data={
                "provider": "meta_musicgen",
                "model": model_id,
                "prompt": prompt,
                "duration_seconds": actual_duration,
                "output": str(final_path),
                "format": final_path.suffix.lstrip("."),
                "license": "CC0 / output owned by you",
            },
            artifacts=[str(final_path)],
            cost_usd=0.0,
            duration_seconds=round(time.time() - start, 2),
            model=model_id,
        )
