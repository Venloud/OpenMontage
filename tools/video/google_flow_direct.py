"""Google Flow Direct — Direct HTTP API calls (no Playwright/browser needed).

Reverse-engineered from Google Flow's internal network traffic.
Uses SAPISIDHASH auth + CapSolver reCAPTCHA v3 + direct HTTP polling.

Setup:
  GOOGLE_FLOW_COOKIES  — JSON array từ DevTools (đã có)
  GOOGLE_FLOW_EMAIL    — Gmail account (đã có)
  CAPSOLVER_API_KEY    — capsolver.com API key (~$3/1000 = $0.003/video)
                         FREE alternative: set CAPSOLVER_API_KEY=skip để bỏ qua captcha
                         (một số request không cần captcha với session hợp lệ)

Cách lấy CAPSOLVER_API_KEY:
  1. Đăng ký tại capsolver.com
  2. Nạp $5 (dùng được ~1666 video)
  3. Copy API key vào .env

Lưu ý: Nếu không có CapSolver, tool sẽ thử không có captcha token trước.
Nếu fail với 403, mới cần captcha.
"""

from __future__ import annotations

import hashlib
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

# ─── Google Flow internal API constants ──────────────────────────────────────

_FLOW_ORIGIN = "https://labs.google"
_FLOW_URL = "https://labs.google/fx/tools/flow"

# reCAPTCHA v3 site key của labs.google
# Verify bằng: DevTools → Network → filter "recaptcha" → xem sitekey param
_RECAPTCHA_SITE_KEY = os.environ.get("GOOGLE_FLOW_RECAPTCHA_KEY", "6LfBiBkpAAAAANq-5wUd4RlcFGUm2n3GhEojy0QU")

# Google Flow video generation endpoint
# Verify bằng: DevTools → Network → filter "aisandbox" khi click Generate
_GENERATE_ENDPOINT = os.environ.get(
    "GOOGLE_FLOW_ENDPOINT",
    "https://aisandbox-pa.googleapis.com/v1alpha/projects/google.com:aisandboxmobile"
    "/locations/us-central1/publishers/google/models/{model}:generateVideo",
)

# Polling endpoint cho operation status
_OPERATION_ENDPOINT = os.environ.get(
    "GOOGLE_FLOW_OPERATION_ENDPOINT",
    "https://aisandbox-pa.googleapis.com/v1alpha/{operation_name}",
)

# Model name mapping: Flow UI name → internal API name
_MODEL_MAP = {
    "veo-3.1-fast-relaxed": "veo-3.1-fast-relaxed-generate-preview",
    "veo-3.1-fast":          "veo-3.1-fast-generate-preview",
    "veo-3.1-lite":          "veo-3.1-lite-generate-preview",
    "veo-3.1-quality":       "veo-3.1-generate-preview",
}

# Aspect ratio mapping: portrait/landscape → API value
_AR_MAP = {
    "portrait":  "9:16",
    "landscape": "16:9",
    "9:16":      "9:16",
    "16:9":      "16:9",
}


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _extract_cookies(cookies_raw: str) -> dict[str, str]:
    """Parse cookie JSON array → flat dict {name: value}.
    Handles escaped newlines from .env storage (\\n → real newline).
    """
    raw = cookies_raw.strip()
    # Unescape \n stored in .env single-line format
    raw = raw.replace("\\n", "\n").replace("\\t", "\t")
    try:
        cookies = json.loads(raw)
        return {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
    except Exception:
        return {}


def _compute_sapisidhash(sapisid: str, origin: str = _FLOW_ORIGIN) -> str:
    """
    Compute Google SAPISIDHASH authorization header.
    Formula: SHA1(timestamp + " " + SAPISID + " " + origin)
    Ref: https://stackoverflow.com/a/32065323
    """
    ts = int(time.time())
    msg = f"{ts} {sapisid} {origin}"
    digest = hashlib.sha1(msg.encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _get_bearer_token(cookies: dict[str, str]) -> str | None:
    """
    Extract or derive Bearer token from Google cookies.
    Google Flow dùng SAPISIDHASH (không phải OAuth Bearer token thông thường).
    Returns the SAPISIDHASH string dùng làm Authorization header.
    """
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    if not sapisid:
        return None
    return _compute_sapisidhash(sapisid)


def _build_headers(cookies: dict[str, str], recaptcha_token: str | None = None) -> dict[str, str]:
    """Build request headers cho Google Flow API."""
    auth = _get_bearer_token(cookies)

    # Cookie string cho request
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()
                           if "google" in k.lower() or k in (
                               "SID", "HSID", "SSID", "APISID", "SAPISID",
                               "NID", "1P_JAR", "__Secure-1PSID", "__Secure-3PSID",
                               "__Secure-1PSIDTS", "__Secure-3PSIDTS",
                               "__Secure-1PAPISID", "__Secure-3PAPISID",
                           ))

    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": _FLOW_ORIGIN,
        "Referer": f"{_FLOW_URL}/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "X-Goog-Authuser": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie_str,
    }

    if auth:
        headers["Authorization"] = auth

    if recaptcha_token:
        headers["X-Recaptcha-Token"] = recaptcha_token

    return headers


# ─── reCAPTCHA solver ─────────────────────────────────────────────────────────

def _solve_recaptcha_v3(site_key: str = _RECAPTCHA_SITE_KEY,
                        page_url: str = _FLOW_URL,
                        action: str = "generate_video") -> str | None:
    """
    Giải reCAPTCHA v3 dùng CapSolver.
    Returns token string hoặc None nếu không cấu hình / thất bại.
    """
    api_key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if not api_key or api_key == "skip":
        return None  # thử không có captcha trước

    try:
        import capsolver  # pip install capsolver
        capsolver.api_key = api_key
        solution = capsolver.solve({
            "type": "ReCaptchaV3TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": action,
            "minScore": 0.7,
        })
        return solution.get("gRecaptchaResponse")
    except ImportError:
        # Manual HTTP fallback nếu không cài capsolver package
        return _solve_recaptcha_manual(api_key, site_key, page_url, action)
    except Exception:
        return None


def _solve_recaptcha_manual(api_key: str, site_key: str,
                             page_url: str, action: str) -> str | None:
    """CapSolver via raw HTTP (không cần package)."""
    try:
        r = requests.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "ReCaptchaV3TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                    "pageAction": action,
                    "minScore": 0.7,
                },
            },
            timeout=15,
        )
        task_id = r.json().get("taskId")
        if not task_id:
            return None

        for _ in range(30):  # poll tối đa 30s
            time.sleep(1)
            res = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=10,
            ).json()
            if res.get("status") == "ready":
                return res.get("solution", {}).get("gRecaptchaResponse")
    except Exception:
        pass
    return None


# ─── Main tool class ──────────────────────────────────────────────────────────

class GoogleFlowDirect(BaseTool):
    """Direct HTTP API calls to Google Flow — no browser, no Playwright."""

    name = "google_flow_direct"
    version = "1.0.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "google_flow"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API  # Pure HTTP — no local browser

    dependencies = ["requests>=2.28"]
    install_instructions = (
        "Set GOOGLE_FLOW_COOKIES + GOOGLE_FLOW_EMAIL trong .env\n"
        "Optional: Set CAPSOLVER_API_KEY=<key> để solve reCAPTCHA (~$0.003/video)\n"
        "  hoặc CAPSOLVER_API_KEY=skip để thử không có captcha (hoạt động nếu session mới)\n"
        "  Lấy key tại capsolver.com — nạp $5 dùng được ~1666 video"
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "portrait": True,
        "landscape": True,
    }
    best_for = [
        "Veo 3.1 generation không cần browser, nhanh hơn Playwright 10x",
        "Free veo-3.1-fast-relaxed với Ultra plan",
        "Stable — không bị DOM selector fragile",
    ]
    not_good_for = [
        "Lần đầu chưa có cookies",
        "Cookie expired (cần re-capture)",
    ]
    fallback_tools = ["google_flow_video", "google_veo_native"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {
                "type": "string",
                "enum": list(_MODEL_MAP.keys()),
                "default": "veo-3.1-fast-relaxed",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["portrait", "landscape", "9:16", "16:9"],
                "default": "portrait",
            },
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "image_path": {"type": "string"},
            "duration_seconds": {
                "type": "integer",
                "enum": [4, 5, 6, 7, 8],
                "default": 8,
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "rate_limit", "503"])
    idempotency_key_fields = ["prompt", "model", "aspect_ratio", "operation"]
    side_effects = ["writes video file to output_path", "uses CapSolver API credits"]
    user_visible_verification = ["Watch generated clip for visual quality"]

    _POLL_INTERVAL = 5   # giây giữa các poll
    _MAX_WAIT = 360       # 6 phút timeout

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> ToolStatus:
        if not os.environ.get("GOOGLE_FLOW_COOKIES"):
            return ToolStatus.UNAVAILABLE
        if not os.environ.get("GOOGLE_FLOW_EMAIL"):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        costs = {
            "veo-3.1-quality":       0.50,
            "veo-3.1-fast":          0.05,
            "veo-3.1-lite":          0.025,
            "veo-3.1-fast-relaxed":  0.0,
        }
        base = costs.get(inputs.get("model", "veo-3.1-fast-relaxed"), 0.0)
        captcha = 0.003 if os.environ.get("CAPSOLVER_API_KEY", "skip") != "skip" else 0.0
        return base + captcha

    # ── Execute ───────────────────────────────────────────────────────────────

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        cookies_raw = os.environ.get("GOOGLE_FLOW_COOKIES", "")
        if not cookies_raw:
            return ToolResult(success=False, error="GOOGLE_FLOW_COOKIES not set")

        cookies = _extract_cookies(cookies_raw)
        if not cookies:
            return ToolResult(success=False, error="Failed to parse GOOGLE_FLOW_COOKIES")

        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        if not sapisid:
            return ToolResult(
                success=False,
                error="SAPISID cookie not found — re-capture cookies from DevTools",
            )

        start = time.time()
        prompt      = inputs["prompt"]
        model_key   = inputs.get("model", "veo-3.1-fast-relaxed")
        api_model   = _MODEL_MAP.get(model_key, model_key)
        ar          = _AR_MAP.get(inputs.get("aspect_ratio", "portrait"), "9:16")
        operation   = inputs.get("operation", "text_to_video")
        duration    = inputs.get("duration_seconds", 8)
        output_path = Path(inputs.get("output_path", "flow_direct_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Step 1: Solve reCAPTCHA (optional — skip if no key)
        recaptcha_token = _solve_recaptcha_v3()

        # Step 2: Build request
        headers = _build_headers(cookies, recaptcha_token)
        endpoint = _GENERATE_ENDPOINT.format(model=api_model)

        payload = self._build_payload(prompt, api_model, ar, duration, operation, inputs)

        # Step 3: Submit generation job
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        except requests.RequestException as e:
            return ToolResult(success=False, error=f"Request failed: {e}")

        if resp.status_code == 403:
            if not recaptcha_token:
                # Retry with captcha
                recaptcha_token = _solve_recaptcha_v3()
                if recaptcha_token:
                    headers = _build_headers(cookies, recaptcha_token)
                    try:
                        resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
                    except requests.RequestException as e:
                        return ToolResult(success=False, error=f"Request failed on retry: {e}")

        if resp.status_code == 401:
            return ToolResult(
                success=False,
                error="Google session expired (401) — re-capture cookies từ DevTools",
            )

        if not resp.ok:
            return ToolResult(
                success=False,
                error=f"Google Flow API error {resp.status_code}: {resp.text[:300]}",
            )

        # Step 4: Parse operation name from response
        try:
            resp_data = resp.json()
        except Exception:
            return ToolResult(success=False, error=f"Invalid JSON response: {resp.text[:200]}")

        operation_name = self._extract_operation_name(resp_data)
        if not operation_name:
            # Maybe synchronous response with video URL already
            video_url = self._extract_video_url_from_response(resp_data)
            if video_url:
                return self._download_and_return(
                    video_url, output_path, headers, model_key, prompt, operation, duration,
                    time.time() - start, inputs
                )
            return ToolResult(
                success=False,
                error=f"No operation name or video URL in response: {json.dumps(resp_data)[:300]}",
            )

        # Step 5: Poll for completion
        video_url = self._poll_operation(operation_name, headers)
        if not video_url:
            return ToolResult(
                success=False,
                error=f"Generation timed out or failed after {self._MAX_WAIT}s",
            )

        # Step 6: Download video
        return self._download_and_return(
            video_url, output_path, headers, model_key, prompt, operation, duration,
            time.time() - start, inputs
        )

    def _build_payload(self, prompt: str, api_model: str, ar: str,
                       duration: int, operation: str, inputs: dict) -> dict:
        """Build generation request body."""
        body: dict[str, Any] = {
            "instances": [{
                "prompt": prompt,
                "videoGenerationConfig": {
                    "aspectRatio": ar,
                    "durationSeconds": duration,
                    "model": api_model,
                    "sampleCount": 1,
                    "enhancePrompt": False,
                },
            }],
            "parameters": {
                "model": api_model,
                "aspectRatio": ar,
                "durationSeconds": duration,
                "generateAudio": False,
            },
        }

        # image_to_video: embed image as base64
        if operation == "image_to_video" and inputs.get("image_path"):
            img_path = Path(inputs["image_path"])
            if img_path.exists():
                import base64, mimetypes
                mime, _ = mimetypes.guess_type(img_path.name)
                mime = mime or "image/jpeg"
                b64 = base64.b64encode(img_path.read_bytes()).decode()
                body["instances"][0]["image"] = {
                    "bytesBase64Encoded": b64,
                    "mimeType": mime,
                }

        return body

    def _extract_operation_name(self, data: dict) -> str | None:
        """Extract long-running operation name from response."""
        # Standard Google LRO format
        if "name" in data and "operations" in str(data.get("name", "")):
            return data["name"]
        # Nested format
        for key in ("operationName", "operation_name", "jobName", "name"):
            if key in data:
                val = data[key]
                if isinstance(val, str) and len(val) > 10:
                    return val
        return None

    def _extract_video_url_from_response(self, data: dict) -> str | None:
        """Extract video URL if response is synchronous (no polling needed)."""
        import re
        text = json.dumps(data)
        patterns = [
            r'"fifeUrl"\s*:\s*"(https?://[^"]+)"',
            r'"videoUrl"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
            r'"uri"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
            r'(https://lh[0-9]+\.googleusercontent\.com/[^"\'\\s]+)',
            r'(https://storage\.googleapis\.com/[^"\'\\s]+\.mp4[^"\'\\s]*)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
                return url
        return None

    def _poll_operation(self, operation_name: str, headers: dict) -> str | None:
        """Poll LRO until done, return video URL."""
        # Clean up operation name for URL
        op_path = operation_name.lstrip("/")
        if not op_path.startswith("projects/"):
            poll_url = _OPERATION_ENDPOINT.format(operation_name=op_path)
        else:
            poll_url = f"https://aisandbox-pa.googleapis.com/v1alpha/{op_path}"

        deadline = time.time() + self._MAX_WAIT
        while time.time() < deadline:
            time.sleep(self._POLL_INTERVAL)
            try:
                r = requests.get(poll_url, headers=headers, timeout=15)
                if not r.ok:
                    continue
                data = r.json()
            except Exception:
                continue

            # Check if done
            if data.get("done"):
                if data.get("error"):
                    return None
                return self._extract_video_url_from_response(data.get("response", data))

            # Check response field even if not explicitly done
            resp_field = data.get("response") or data.get("result") or {}
            url = self._extract_video_url_from_response(resp_field or data)
            if url:
                return url

        return None

    def _download_and_return(
        self, video_url: str, output_path: Path, headers: dict,
        model: str, prompt: str, operation: str, duration: int,
        elapsed: float, inputs: dict,
    ) -> ToolResult:
        """Download video from URL and return ToolResult."""
        # Use subset of headers for download (avoid CORS issues)
        dl_headers = {
            "User-Agent": headers.get("User-Agent", ""),
            "Cookie": headers.get("Cookie", ""),
            "Referer": _FLOW_URL,
        }
        try:
            r = requests.get(video_url, headers=dl_headers, timeout=120, stream=True)
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        except Exception as e:
            return ToolResult(success=False, error=f"Video download failed: {e}")

        if not output_path.exists() or output_path.stat().st_size < 1000:
            return ToolResult(success=False, error="Downloaded file is empty or corrupt")

        return ToolResult(
            success=True,
            data={
                "provider": "google_flow_direct",
                "model": model,
                "prompt": prompt,
                "output": str(output_path),
                "aspect_ratio": inputs.get("aspect_ratio", "portrait"),
                "operation": operation,
                "duration_seconds": duration,
                "video_url": video_url,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(elapsed, 2),
            model=model,
        )
