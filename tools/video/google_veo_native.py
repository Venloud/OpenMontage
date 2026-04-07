"""Google Veo 3.1 video generation via Google AI (Gemini) SDK directly.

Uses the `google-genai` Python SDK with your Google AI Studio API key.
Supports text-to-video and image-to-video operations.

Model: veo-3.0-generate-preview (latest available via AI Studio)
SDK: pip install google-genai
Docs: https://ai.google.dev/api/generate-videos
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


class GoogleVeoNative(BaseTool):
    name = "google_veo_native"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "google_veo"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["google-genai>=1.0"]
    install_instructions = (
        "Set GOOGLE_API_KEY to your Google AI Studio API key.\n"
        "  Get one at https://aistudio.google.com/apikey\n"
        "  Install SDK: pip install google-genai"
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "native_audio": True,
        "dialogue_generation": True,
        "ambient_sound": True,
    }
    best_for = [
        "cutting-edge quality from Google DeepMind — Veo 3.1",
        "videos with synchronized dialogue and ambient audio",
        "direct Google AI Studio API (no fal.ai routing)",
    ]
    not_good_for = ["budget projects", "quick iteration (2-3 min per clip)", "offline generation"]
    fallback_tools = ["veo_video", "kling_video", "minimax_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Detailed scene description"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "enum": [
                    "veo-3.1-generate-preview",
                    "veo-3.1-fast-generate-preview",
                    "veo-3.0-generate-001",
                    "veo-3.0-fast-generate-001",
                    "veo-2.0-generate-001",
                ],
                "default": "veo-3.1-generate-preview",
                "description": "Veo model. veo-3.1 = latest, veo-3.1-fast = cheaper/faster, veo-2.0 = most stable",
            },
            "duration_seconds": {
                "type": "integer",
                "enum": [4, 5, 6, 7, 8],
                "default": 8,
                "description": "Clip duration in seconds (4-8s)",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16"],
                "default": "16:9",
            },
            "generate_audio": {
                "type": "boolean",
                "default": True,
                "description": "Generate synchronized ambient sound and dialogue",
            },
            "negative_prompt": {
                "type": "string",
                "description": "Elements to avoid in the video",
            },
            "number_of_videos": {
                "type": "integer",
                "default": 1,
                "minimum": 1,
                "maximum": 4,
                "description": "Number of video variants to generate (returns first by default)",
            },
            "image_path": {
                "type": "string",
                "description": "Local image path for image_to_video operation",
            },
            "image_url": {
                "type": "string",
                "description": "Image URL for image_to_video operation",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model", "duration_seconds", "operation"]
    side_effects = ["writes video file to output_path", "calls Google AI (Gemini) API"]
    user_visible_verification = [
        "Watch generated clip for visual quality and motion",
        "Listen for audio synchronization if generate_audio=True",
    ]

    _POLL_INTERVAL = 10   # seconds
    _MAX_WAIT = 300        # 5 minutes

    def _get_api_key(self) -> str | None:
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

    def get_status(self) -> ToolStatus:
        if not self._get_api_key():
            return ToolStatus.UNAVAILABLE
        try:
            import google.genai  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Veo 3 via Google AI Studio: ~$0.35/second
        duration = inputs.get("duration_seconds", 8)
        n = inputs.get("number_of_videos", 1)
        return round(0.35 * duration * n, 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 150.0  # ~2.5 minutes average

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="GOOGLE_API_KEY not set. " + self.install_instructions,
            )

        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            return ToolResult(
                success=False,
                error="google-genai not installed. Run: pip install google-genai",
            )

        start = time.time()
        operation_type = inputs.get("operation", "text_to_video")
        model = inputs.get("model", "veo-3.0-generate-preview")
        duration = inputs.get("duration_seconds", 8)
        aspect_ratio = inputs.get("aspect_ratio", "16:9")
        generate_audio = inputs.get("generate_audio", True)
        n_videos = inputs.get("number_of_videos", 1)

        try:
            client = genai.Client(api_key=api_key)

            config_kwargs: dict[str, Any] = {
                "aspect_ratio": aspect_ratio,
                "duration_seconds": duration,
                "number_of_videos": n_videos,
            }
            if inputs.get("negative_prompt"):
                config_kwargs["negative_prompt"] = inputs["negative_prompt"]
            # Note: enhance_prompt and generate_audio not supported on Gemini API
            config = genai_types.GenerateVideosConfig(**config_kwargs)

            # Build generation call
            if operation_type == "image_to_video":
                image_ref = self._load_image(inputs, client)
                if image_ref is None:
                    return ToolResult(
                        success=False,
                        error="image_to_video requires image_path or image_url",
                    )
                operation = client.models.generate_videos(
                    model=model,
                    prompt=inputs["prompt"],
                    image=image_ref,
                    config=config,
                )
            else:
                operation = client.models.generate_videos(
                    model=model,
                    prompt=inputs["prompt"],
                    config=config,
                )

            # Poll until done
            elapsed = 0
            while not operation.done and elapsed < self._MAX_WAIT:
                time.sleep(self._POLL_INTERVAL)
                elapsed += self._POLL_INTERVAL
                operation = client.operations.get(operation)

            if not operation.done:
                return ToolResult(
                    success=False,
                    error=f"Veo generation timed out after {self._MAX_WAIT}s",
                )

            if operation.error:
                return ToolResult(
                    success=False,
                    error=f"Veo generation error: {operation.error}",
                )

            # Download first video
            generated_videos = operation.response.generated_videos
            if not generated_videos:
                return ToolResult(success=False, error="Veo returned no videos")

            import requests as req_lib
            video = generated_videos[0]
            video_uri = video.video.uri if video.video and video.video.uri else None

            if not video_uri:
                return ToolResult(success=False, error="No video URI in Veo response")

            # Download via URI with API key
            sep = "&" if "?" in video_uri else "?"
            download_url = f"{video_uri}{sep}key={api_key}"
            dl_resp = req_lib.get(download_url, timeout=120)
            dl_resp.raise_for_status()

            output_path = Path(inputs.get("output_path", "veo_native_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(dl_resp.content)

        except Exception as exc:
            return ToolResult(success=False, error=f"Google Veo native generation failed: {exc}")

        return ToolResult(
            success=True,
            data={
                "provider": "google_veo",
                "model": model,
                "prompt": inputs["prompt"],
                "output": str(output_path),
                "has_audio": generate_audio,
                "operation": operation_type,
                "duration_seconds": duration,
                "aspect_ratio": aspect_ratio,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )

    def _load_image(self, inputs: dict[str, Any], client: Any) -> Any:
        """Load image for image_to_video operation."""
        try:
            from google.genai import types as genai_types
        except ImportError:
            return None

        image_path = inputs.get("image_path")
        image_url = inputs.get("image_url")

        if image_path:
            path = Path(image_path)
            if not path.exists():
                raise FileNotFoundError(f"Image not found: {image_path}")
            import mimetypes
            mime_type, _ = mimetypes.guess_type(path.name)
            mime_type = mime_type or "image/jpeg"
            return genai_types.Image(image_bytes=path.read_bytes(), mime_type=mime_type)

        if image_url:
            import requests
            resp = requests.get(image_url, timeout=30)
            resp.raise_for_status()
            import mimetypes
            content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
            return genai_types.Image(image_bytes=resp.content, mime_type=content_type)

        return None
