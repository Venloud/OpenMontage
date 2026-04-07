"""Google Flow video generation via useapi.net proxy.

Calls useapi.net's Google Flow API — no browser automation, no cookie management,
no reCAPTCHA. useapi.net handles all Google session management.

Setup:
  1. Đăng ký tại https://useapi.net ($15/month)
  2. Thêm Google account: POST https://api.useapi.net/v1/google-flow/accounts
  3. Set USEAPI_TOKEN=<your_useapi_api_token> trong .env
  4. Set USEAPI_GOOGLE_FLOW_EMAIL=<your_google_email_registered_with_useapi> trong .env

API docs: https://useapi.net/docs/api-google-flow-v1
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

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

_BASE_URL = "https://api.useapi.net/v1/google-flow"
_POLL_INTERVAL = 5   # giây giữa các poll
_MAX_WAIT = 360      # 6 phút timeout


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _upload_asset(token: str, email: str, file_path: str) -> str | None:
    """Upload an image asset to useapi.net. Returns mediaId or None."""
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                f"{_BASE_URL}/assets/{email}",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (path.name, f, _mime(path))},
                timeout=60,
            )
        if resp.ok:
            data = resp.json()
            # mediaId may be top-level or nested
            return (
                data.get("mediaId")
                or data.get("id")
                or data.get("asset", {}).get("mediaId")
            )
    except Exception:
        pass
    return None


def _mime(path: Path) -> str:
    import mimetypes
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "image/jpeg"


def _poll_job(token: str, job_id: str) -> dict | None:
    """Poll GET /jobs/{jobId} until completed or failed. Returns job dict or None."""
    url = f"{_BASE_URL}/jobs/{job_id}"
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + _MAX_WAIT

    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if not r.ok:
                continue
            data = r.json()
        except Exception:
            continue

        status = data.get("status", "")
        if status == "completed":
            return data
        if status == "failed":
            return data  # caller checks for "failed" status
        # else: created / started — keep polling

    return None


def _extract_video_urls(job: dict) -> list[str]:
    """Extract fifeUrl download links from a completed job.

    useapi.net response structure:
    {
      "response": {
        "operations": [
          { "operation": { "metadata": { "video": { "fifeUrl": "..." } } } }
        ]
      }
    }
    """
    urls: list[str] = []

    # Primary path: response.operations[].operation.metadata.video.fifeUrl
    response = job.get("response") or job
    for op_entry in response.get("operations", []):
        op = op_entry.get("operation", {})
        meta = op.get("metadata", {})
        video = meta.get("video", {})
        url = video.get("fifeUrl") or video.get("videoUrl") or video.get("servingBaseUri")
        if url and url not in urls:
            urls.append(url)

    # Fallback: scan full JSON for any fifeUrl (video URLs, not image serving URIs)
    if not urls:
        raw = json.dumps(job)
        import re
        for m in re.finditer(r'"fifeUrl"\s*:\s*"(https?://[^"]+)"', raw):
            u = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
            if u not in urls:
                urls.append(u)

    return urls


class GoogleFlowUseAPI(BaseTool):
    """Google Flow video generation via useapi.net — clean REST API, no browser needed."""

    name = "google_flow_useapi"
    version = "1.0.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "google_flow"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["requests>=2.28"]
    install_instructions = (
        "1. Đăng ký https://useapi.net ($15/month)\n"
        "2. Thêm Google account: xem https://useapi.net/docs/api-google-flow-v1\n"
        "3. Set USEAPI_TOKEN=<token> trong .env\n"
        "4. Set USEAPI_GOOGLE_FLOW_EMAIL=<google_email> trong .env"
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "image_to_video_fl", "reference_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "image_to_video_fl": True,
        "reference_to_video": True,
        "portrait": True,
        "landscape": True,
        "count": True,
        "seed": True,
    }
    best_for = [
        "Veo 3.1 via useapi.net — không cần browser, cookies, reCAPTCHA",
        "Most reliable Google Flow path — useapi.net manages Google sessions",
        "Supports T2V, I2V, I2V-FL, R2V with voice narration",
    ]
    not_good_for = [
        "Khi chưa có USEAPI_TOKEN ($15/month subscription)",
    ]
    fallback_tools = ["google_flow_direct", "google_flow_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Scene description for video generation",
            },
            "model": {
                "type": "string",
                "enum": ["veo-3.1-fast", "veo-3.1-fast-relaxed", "veo-3.1-lite", "veo-3.1-quality"],
                "default": "veo-3.1-fast",
                "description": "veo-3.1-fast=5¢, veo-3.1-lite=2.5¢, veo-3.1-quality=50¢, veo-3.1-fast-relaxed=free(Ultra)",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["portrait", "landscape"],
                "default": "portrait",
                "description": "portrait=9:16, landscape=16:9",
            },
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "image_to_video_fl", "reference_to_video"],
                "default": "text_to_video",
            },
            "image_path": {
                "type": "string",
                "description": "Local image path for I2V start frame",
            },
            "end_image_path": {
                "type": "string",
                "description": "Local image path for I2V-FL end frame",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Reference images for R2V mode (max 3)",
            },
            "voice": {
                "type": "string",
                "description": "Narration voice name for R2V mode",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "default": 1,
            },
            "seed": {
                "type": "integer",
                "description": "Seed for reproducible generation",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "rate_limit", "503"])
    idempotency_key_fields = ["prompt", "model", "aspect_ratio", "operation"]
    side_effects = ["writes video file to output_path", "uses useapi.net API credits"]
    user_visible_verification = ["Watch generated clip for visual quality and motion"]

    def _token(self) -> str | None:
        return os.environ.get("USEAPI_TOKEN", "").strip() or None

    def _email(self) -> str | None:
        return os.environ.get("USEAPI_GOOGLE_FLOW_EMAIL", "").strip() or None

    def get_status(self) -> ToolStatus:
        if not self._token():
            return ToolStatus.UNAVAILABLE
        if not self._email():
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        costs = {
            "veo-3.1-quality":      0.50,
            "veo-3.1-fast":         0.05,
            "veo-3.1-lite":         0.025,
            "veo-3.1-fast-relaxed": 0.0,
        }
        model = inputs.get("model", "veo-3.1-fast")
        count = max(1, int(inputs.get("count", 1)))
        return costs.get(model, 0.05) * count

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        token = self._token()
        if not token:
            return ToolResult(
                success=False,
                error="USEAPI_TOKEN not set. " + self.install_instructions,
            )
        email = self._email()
        if not email:
            return ToolResult(
                success=False,
                error="USEAPI_GOOGLE_FLOW_EMAIL not set. " + self.install_instructions,
            )

        start = time.time()
        prompt = inputs["prompt"]
        model = inputs.get("model", "veo-3.1-fast")
        aspect_ratio = inputs.get("aspect_ratio", "portrait")
        operation = inputs.get("operation", "text_to_video")
        image_path = inputs.get("image_path")
        end_image_path = inputs.get("end_image_path")
        reference_image_paths = inputs.get("reference_image_paths") or []
        voice = inputs.get("voice")
        count = max(1, int(inputs.get("count", 1)))
        seed = inputs.get("seed")
        output_path = Path(inputs.get("output_path", "google_flow_useapi_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Build request body ──────────────────────────────────────────────
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "aspectRatio": aspect_ratio,
            "count": count,
            "async": True,  # always async — poll via GET /jobs/{jobId}
        }

        if seed is not None:
            body["seed"] = seed

        # Upload and attach images
        if operation in ("image_to_video", "image_to_video_fl") and image_path:
            media_id = _upload_asset(token, email, image_path)
            if media_id:
                body["startImage"] = media_id
            elif image_path:
                return ToolResult(
                    success=False,
                    error=f"Failed to upload start image: {image_path}",
                )

        if operation == "image_to_video_fl" and end_image_path:
            media_id = _upload_asset(token, email, end_image_path)
            if media_id:
                body["endImage"] = media_id

        if operation == "reference_to_video" and reference_image_paths:
            for i, ref_path in enumerate(reference_image_paths[:3], start=1):
                media_id = _upload_asset(token, email, ref_path)
                if media_id:
                    body[f"referenceImage_{i}"] = media_id

        if voice:
            body["voice"] = voice

        # ── Submit job ──────────────────────────────────────────────────────
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{_BASE_URL}/videos",
                    headers=_auth_headers(token),
                    json=body,
                    timeout=60,
                )
                break
            except requests.Timeout:
                if attempt == 2:
                    return ToolResult(success=False, error="Request failed: useapi.net POST timed out after 3 attempts")
                time.sleep(5)
            except requests.RequestException as e:
                return ToolResult(success=False, error=f"Request failed: {e}")

        # 400 = content policy / bad params
        if resp.status_code == 400:
            return ToolResult(
                success=False,
                error=f"Bad request (400): {resp.text[:300]}",
            )

        # 401 = bad token
        if resp.status_code == 401:
            return ToolResult(
                success=False,
                error="Invalid USEAPI_TOKEN (401). Check your token at useapi.net",
            )

        # 429 / 503 = rate limit — caller's retry policy handles this
        if resp.status_code in (429, 503):
            return ToolResult(
                success=False,
                error=f"Rate limited ({resp.status_code}) — retry in 5-10s",
            )

        if not resp.ok:
            return ToolResult(
                success=False,
                error=f"useapi.net error {resp.status_code}: {resp.text[:300]}",
            )

        try:
            resp_data = resp.json()
        except Exception:
            return ToolResult(success=False, error=f"Invalid JSON from useapi.net: {resp.text[:200]}")

        # ── Synchronous response (200) — video URLs already present ────────
        if resp.status_code == 200:
            urls = _extract_video_urls(resp_data)
            if urls:
                return self._download_videos(
                    urls, output_path, count, model, prompt, operation,
                    inputs, time.time() - start,
                )

        # ── Async response (201) — poll job ─────────────────────────────────
        job_id = (
            resp_data.get("jobId")
            or resp_data.get("jobid")
            or resp_data.get("job_id")
            or resp_data.get("id")
        )
        if not job_id:
            return ToolResult(
                success=False,
                error=f"No jobId in response: {json.dumps(resp_data)[:300]}",
            )

        job = _poll_job(token, job_id)
        if not job:
            return ToolResult(
                success=False,
                error=f"Job {job_id} timed out after {_MAX_WAIT}s",
            )

        if job.get("status") == "failed":
            err = job.get("error") or job.get("message") or "generation failed"
            return ToolResult(success=False, error=f"Job failed: {err}")

        urls = _extract_video_urls(job)
        if not urls:
            return ToolResult(
                success=False,
                error=f"Job {job_id} completed but no video URLs found: {json.dumps(job)[:300]}",
            )

        return self._download_videos(
            urls, output_path, count, model, prompt, operation,
            inputs, time.time() - start,
        )

    def _download_videos(
        self,
        urls: list[str],
        output_path: Path,
        count: int,
        model: str,
        prompt: str,
        operation: str,
        inputs: dict,
        elapsed: float,
    ) -> ToolResult:
        saved: list[str] = []
        for i, url in enumerate(urls[:count]):
            if count == 1:
                dest = output_path
            else:
                dest = output_path.with_name(f"{output_path.stem}_{i + 1}{output_path.suffix or '.mp4'}")
            try:
                r = requests.get(url, timeout=120, stream=True)
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                if dest.exists() and dest.stat().st_size > 1000:
                    saved.append(str(dest))
            except Exception:
                pass

        if not saved:
            return ToolResult(
                success=False,
                error="Video URLs found but download failed for all",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "google_flow_useapi",
                "model": model,
                "prompt": prompt,
                "output": saved[0],
                "outputs": saved,
                "aspect_ratio": inputs.get("aspect_ratio", "portrait"),
                "operation": operation,
                "duration_seconds": inputs.get("duration_seconds", 8),
                "count": count,
                "seed": inputs.get("seed"),
            },
            artifacts=saved,
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(elapsed, 2),
            model=model,
        )
