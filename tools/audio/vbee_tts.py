"""Vbee AI Text-to-Speech provider — Vietnamese-first TTS.

Async API: POST /tts to submit, poll GET /tts/{request_id} for the audio link.
Supports 200+ voices across 50+ languages with strong Vietnamese voice quality.

Docs: https://api-docs.vbee.vn/
"""

from __future__ import annotations

import os
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


class VbeeTTS(BaseTool):
    name = "vbee_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "vbee"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set VBEE_API_KEY to your Vbee Bearer token and VBEE_APP_ID to your App ID.\n"
        "  Create an app at https://studio.vbee.vn/apps to get both values.\n"
        "  Docs: https://api-docs.vbee.vn/"
    )
    fallback_tools = ["google_tts", "openai_tts", "piper_tts"]
    agent_skills = ["text-to-speech"]

    capabilities = ["text_to_speech", "voice_selection", "multilingual"]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
        "ssml": False,
        "vietnamese": True,
    }
    best_for = [
        "Vietnamese narration — natural-sounding voices for North/South/Central dialects",
        "200+ voices across 50+ languages",
        "affordable API-based TTS for batch production",
    ]
    not_good_for = [
        "voice cloning",
        "fully offline production",
        "sub-second latency (async API)",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Text to convert to speech"},
            "voice_code": {
                "type": "string",
                "default": "s_sg_male_thientam_ytstable_vc",
                "description": (
                    "Vbee voice code. Vietnamese examples:\n"
                    "  s_sg_male_thientam_ytstable_vc (Thiện Tâm — default, giọng nam Sài Gòn ấm)\n"
                    "  hn_male_manhdung_news_48k-fhg (Mạnh Dũng — Hà Nội male news)\n"
                    "  hn_male_thanhlong_talk_48k-fhg (Thanh Long — Hà Nội male casual)\n"
                    "  hn_female_ngochuyen_full_48k-fhg (Ngọc Huyền — Hà Nội female)\n"
                    "  sg_male_trungkien_vdts_48k-fhg (Trung Kiên — Sài Gòn male)\n"
                    "  sg_female_lantrinh_vdts_48k-fhg (Lan Trinh — Sài Gòn female)"
                ),
            },
            "speed_rate": {
                "type": "number",
                "default": 1.0,
                "minimum": 0.1,
                "maximum": 1.9,
                "description": "Speaking speed. 1.0 = normal, 0.7 = slower, 1.3 = faster",
            },
            "audio_type": {
                "type": "string",
                "default": "mp3",
                "enum": ["mp3", "wav"],
                "description": "Output audio format",
            },
            "bitrate": {
                "type": "integer",
                "default": 128,
                "enum": [8, 16, 32, 64, 128],
                "description": "MP3 bitrate in kbps (MP3 only)",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["text", "voice_code", "speed_rate", "audio_type"]
    side_effects = ["writes audio file to output_path", "calls Vbee TTS API"]
    user_visible_verification = ["Listen to generated audio for natural Vietnamese speech quality"]

    _BASE_URL = "https://vbee.vn/api/v1"
    _POLL_INTERVAL = 3   # seconds between status checks
    _MAX_WAIT = 120       # 2 minutes max

    def _get_credentials(self) -> tuple[str | None, str | None]:
        token = os.environ.get("VBEE_API_KEY")
        app_id = os.environ.get("VBEE_APP_ID") or token  # fallback: same value if UUID
        return token, app_id

    def get_status(self) -> ToolStatus:
        token, _ = self._get_credentials()
        if token:
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Vbee charges per character; roughly $0.001 per 100 characters
        char_count = len(inputs.get("text", ""))
        return round(char_count * 0.00001, 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        token, app_id = self._get_credentials()
        if not token:
            return ToolResult(
                success=False,
                error="VBEE_API_KEY not set. " + self.install_instructions,
            )

        start = time.time()
        try:
            request_id = self._submit(inputs, token, app_id)
            audio_link = self._poll(request_id, token)
            output_path = self._download(audio_link, inputs)
        except Exception as exc:
            return ToolResult(success=False, error=f"Vbee TTS failed: {exc}")

        voice_code = inputs.get("voice_code", "s_sg_male_thientam_ytstable_vc")
        audio_type = inputs.get("audio_type", "mp3")

        return ToolResult(
            success=True,
            data={
                "provider": "vbee",
                "voice_code": voice_code,
                "text_length": len(inputs["text"]),
                "output": str(output_path),
                "format": audio_type,
                "speed_rate": inputs.get("speed_rate", 1.0),
                "request_id": request_id,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=f"vbee/{voice_code}",
        )

    def _submit(self, inputs: dict[str, Any], token: str, app_id: str) -> str:
        """Submit TTS request and return request_id."""
        import requests

        voice_code = inputs.get("voice_code", "s_sg_male_thientam_ytstable_vc")
        audio_type = inputs.get("audio_type", "mp3")

        payload: dict[str, Any] = {
            "app_id": app_id,
            "input_text": inputs["text"],
            "voice_code": voice_code,
            "callback_url": "https://example.com/noop",   # required field; we poll instead
            "audio_type": audio_type,
            "speed_rate": inputs.get("speed_rate", 1.0),
        }
        if audio_type == "mp3":
            payload["bitrate"] = inputs.get("bitrate", 128)

        response = requests.post(
            f"{self._BASE_URL}/tts",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") != 1:
            raise RuntimeError(f"Vbee submission failed: {data}")

        request_id = data.get("result", {}).get("request_id")
        if not request_id:
            raise RuntimeError(f"No request_id in Vbee response: {data}")

        return request_id

    def _poll(self, request_id: str, token: str) -> str:
        """Poll for completion and return the audio_link."""
        import requests

        elapsed = 0
        while elapsed < self._MAX_WAIT:
            time.sleep(self._POLL_INTERVAL)
            elapsed += self._POLL_INTERVAL

            response = requests.get(
                f"{self._BASE_URL}/tts/{request_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            result = data.get("result", data)
            status = result.get("status", "")

            if status == "SUCCESS":
                audio_link = result.get("audio_link")
                if not audio_link:
                    raise RuntimeError("Vbee returned SUCCESS but no audio_link")
                return audio_link
            elif status == "FAILURE":
                raise RuntimeError(f"Vbee TTS request failed (request_id: {request_id})")
            # IN_PROGRESS — keep polling

        raise TimeoutError(f"Vbee TTS timed out after {self._MAX_WAIT}s (request_id: {request_id})")

    def _download(self, audio_link: str, inputs: dict[str, Any]) -> Path:
        """Download audio from the link (expires in 3 minutes)."""
        import requests

        audio_type = inputs.get("audio_type", "mp3")
        output_path = Path(inputs.get("output_path", f"vbee_output.{audio_type}"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        response = requests.get(audio_link, timeout=60)
        response.raise_for_status()
        output_path.write_bytes(response.content)

        return output_path
