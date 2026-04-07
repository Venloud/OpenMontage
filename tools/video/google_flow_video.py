"""Google Flow video generation via real Opera browser (CDP connection).

Kết nối vào Opera ĐANG CHẠY THẬT qua Chrome DevTools Protocol — không launch
browser giả, không inject webdriver flag, Google không phát hiện automation.

Setup (một lần):
  1. Tắt Opera nếu đang chạy
  2. Chạy script bên dưới để mở Opera với debug port:
       python tools/video/google_flow_video.py --launch-opera
     HOẶC tự chạy:
       /Applications/Opera.app/Contents/MacOS/Opera --remote-debugging-port=9222 --no-first-run
  3. Đăng nhập Google Flow trong Opera như bình thường
  4. Xong — tool sẽ tự connect vào session này mỗi khi generate

Lưu ý: Opera phải đang chạy với port 9222 khi generate video.
Nếu Opera chưa chạy, tool tự launch Opera với debug port.

Requires: playwright>=1.40
Install:  pip3 install playwright && playwright install chromium
"""

from __future__ import annotations

import json
import os
import random
import re
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

_FLOW_URL = "https://labs.google/fx/tools/flow"
_OPERA_PATH = "/Applications/Opera.app/Contents/MacOS/Opera"
_OPERA_USER_DATA = str(Path.home() / "Library/Application Support/com.operasoftware.Opera")
_CDP_PORT = int(os.environ.get("OPERA_CDP_PORT", "9222"))
_CDP_URL = f"http://localhost:{_CDP_PORT}"


# ─── CDP helpers ──────────────────────────────────────────────────────────────

def _cdp_is_alive() -> bool:
    """Check if Opera is running with remote debugging on _CDP_PORT."""
    import urllib.request
    try:
        urllib.request.urlopen(f"{_CDP_URL}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _launch_opera_with_cdp() -> bool:
    """Launch Opera with --remote-debugging-port if not already running. Returns True on success."""
    import subprocess, time
    if not Path(_OPERA_PATH).exists():
        return False
    try:
        subprocess.Popen(
            [
                _OPERA_PATH,
                f"--remote-debugging-port={_CDP_PORT}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for debug port to be ready
        for _ in range(20):
            time.sleep(0.5)
            if _cdp_is_alive():
                return True
        return False
    except Exception:
        return False

# Shared login state
_login_state: dict = {"status": "idle", "cookies": None, "email": None, "error": None}
_browser_ref: dict = {"browser": None, "context": None, "page": None, "pw": None}


def launch_login_flow() -> None:
    """Open Brave to Google Flow and wait for user to confirm login."""
    _login_state.update({"status": "launching", "cookies": None, "email": None, "error": None})

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _login_state.update({"status": "error", "error": "playwright not installed"})
        return

    try:
        pw = sync_playwright().start()
        if not Path(_OPERA_PATH).exists():
            _login_state.update({"status": "error", "error": f"Không tìm thấy Opera tại {_OPERA_PATH}"})
            return

        # Dùng profile thật của Opera — Google không detect là bot
        context = pw.chromium.launch_persistent_context(
            user_data_dir=_OPERA_USER_DATA,
            executable_path=_OPERA_PATH,
            headless=False,
            args=["--no-first-run"],
            viewport={"width": 1280, "height": 900},
        )
        browser = None  # persistent context không có browser object riêng
        page = context.new_page()

        _browser_ref.update({"browser": None, "context": context, "page": page, "pw": pw})

        page.goto(_FLOW_URL, wait_until="domcontentloaded", timeout=30000)
        _login_state["status"] = "waiting_confirm"

    except Exception as e:
        _login_state.update({"status": "error", "error": str(e)})
        _close_browser()


def capture_cookies() -> dict:
    """Capture cookies from the open browser and save to env + .env file.

    Called after user confirms they have logged in.
    """
    context = _browser_ref.get("context")
    page = _browser_ref.get("page")

    if not context:
        _login_state.update({"status": "error", "error": "Browser not open. Launch login first."})
        return _login_state

    try:
        all_cookies = context.cookies()

        # Try to get email from current page
        email = ""
        try:
            email = page.evaluate("""() => {
                const imgs = document.querySelectorAll('img[alt]');
                for (const img of imgs) {
                    if (img.alt && img.alt.includes('@')) return img.alt;
                }
                const meta = document.querySelector('meta[name="user-email"]');
                if (meta) return meta.content;
                return '';
            }""")
        except Exception:
            pass

        if not all_cookies:
            _login_state.update({"status": "error", "error": "Không có cookies. Hãy đảm bảo đã đăng nhập Google."})
            return _login_state

        cookies_json = json.dumps(all_cookies)
        os.environ["GOOGLE_FLOW_COOKIES"] = cookies_json
        if email:
            os.environ["GOOGLE_FLOW_EMAIL"] = email

        _persist_env("GOOGLE_FLOW_COOKIES", cookies_json)
        if email:
            _persist_env("GOOGLE_FLOW_EMAIL", email)

        _login_state.update({
            "status": "done",
            "cookies": cookies_json,
            "email": email,
            "error": None,
        })

    except Exception as e:
        _login_state.update({"status": "error", "error": str(e)})
    finally:
        _close_browser()

    return _login_state


def _close_browser() -> None:
    try:
        if _browser_ref.get("context"):
            _browser_ref["context"].close()
        if _browser_ref.get("pw"):
            _browser_ref["pw"].stop()
    except Exception:
        pass
    _browser_ref.update({"browser": None, "context": None, "page": None, "pw": None})


def _persist_env(key: str, value: str) -> None:
    import re
    env_path = Path(".env")
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    safe_val = value.replace("\n", "\\n")
    pattern = rf'^{re.escape(key)}=.*$'
    replacement = f'{key}={safe_val}'
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
    else:
        text += f'\n{replacement}\n'
    env_path.write_text(text, encoding="utf-8")


class GoogleFlowVideo(BaseTool):
    name = "google_flow_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "google_flow"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL  # runs local Playwright browser

    dependencies = ["playwright>=1.40"]
    install_instructions = (
        "1. pip3 install playwright && playwright install chromium\n"
        "2. Đăng nhập Google Flow trên Opera/Brave (không dùng Chrome)\n"
        "3. DevTools → Application → Cookies → https://accounts.google.com/ → Ctrl+A Ctrl+C\n"
        "4. Set GOOGLE_FLOW_COOKIES=<json_array_of_cookie_objects> trong .env\n"
        "5. Set GOOGLE_FLOW_EMAIL=your_email@gmail.com trong .env\n"
        "   Optional: Set GOOGLE_FLOW_MODEL=veo-3.1-fast (default) | veo-3.1-lite | veo-3.1-quality | veo-3.1-fast-relaxed"
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
        "Veo 3.1 quality video generation without useapi.net proxy fee",
        "Direct Google Flow access using your own account",
        "Free lower-priority generation with veo-3.1-fast-relaxed (Ultra plan)",
    ]
    not_good_for = [
        "Headless server without display (needs Playwright Chromium)",
        "First run if browser not installed (run playwright install chromium)",
    ]
    fallback_tools = ["google_veo_native"]

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
                "description": "text_to_video=T2V, image_to_video=I2V (start frame), image_to_video_fl=I2V-FL (frame interpolation with start+end), reference_to_video=R2V (reference style images)",
            },
            "image_path": {
                "type": "string",
                "description": "Local image path for image_to_video / image_to_video_fl (start frame)",
            },
            "end_image_path": {
                "type": "string",
                "description": "Local image path for end frame (image_to_video_fl mode only)",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Reference-style images for R2V mode (max 3). Only works with veo-3.1-fast and veo-3.1-fast-relaxed.",
            },
            "voice": {
                "type": "string",
                "description": "Narration voice for R2V mode — selected from voice dropdown in UI",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "default": 1,
                "description": "Number of video variations to generate (1–4)",
            },
            "seed": {
                "type": "integer",
                "description": "Seed value for reproducible generation",
            },
            "duration_seconds": {
                "type": "integer",
                "enum": [4, 5, 6, 7, 8],
                "default": 8,
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "rate_limit"])
    idempotency_key_fields = ["prompt", "model", "aspect_ratio", "operation"]
    side_effects = ["writes video file to output_path", "launches Playwright browser"]
    user_visible_verification = ["Watch generated clip for visual quality and motion"]

    _MAX_WAIT = 300  # 5 minutes

    def _get_cookies_raw(self) -> str | None:
        return os.environ.get("GOOGLE_FLOW_COOKIES")

    def _get_email(self) -> str | None:
        return os.environ.get("GOOGLE_FLOW_EMAIL")

    def _get_default_model(self) -> str:
        return os.environ.get("GOOGLE_FLOW_MODEL", "veo-3.1-fast")

    def _cookies_are_fresh(self, cookies_raw: str) -> bool:
        """Check if critical Google auth cookies are still valid (not expired)."""
        try:
            cookies = json.loads(cookies_raw)
            now = time.time()
            # SAPISID and SID are the core auth cookies
            auth_cookies = {c["name"]: c for c in cookies if c.get("name") in ("SAPISID", "SID", "__Secure-1PSID")}
            if not auth_cookies:
                return True  # can't check, assume ok
            for c in auth_cookies.values():
                exp = c.get("expires") or c.get("expirationDate")
                if exp and float(exp) < now:
                    return False
            return True
        except Exception:
            return True  # parse error, let runtime detect

    def get_status(self) -> ToolStatus:
        if not self._get_cookies_raw():
            return ToolStatus.UNAVAILABLE
        if not self._get_email():
            return ToolStatus.UNAVAILABLE
        try:
            import playwright  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        model = inputs.get("model", self._get_default_model())
        count = max(1, int(inputs.get("count", 1)))
        costs = {
            "veo-3.1-quality": 0.50,
            "veo-3.1-fast": 0.05,
            "veo-3.1-lite": 0.025,
            "veo-3.1-fast-relaxed": 0.0,
        }
        return costs.get(model, 0.05) * count

    def _parse_cookies(self, raw: str) -> list[dict]:
        """Parse cookies from JSON array (DevTools export) or Netscape format."""
        raw = raw.strip()

        # JSON array: [{name, value, domain, ...}, ...]
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass

        # DevTools tab-separated: Name\tValue\tDomain\t...
        cookies = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                cookies.append({
                    "name": parts[0].strip(),
                    "value": parts[1].strip(),
                    "domain": parts[2].strip() if len(parts) > 2 else ".google.com",
                    "path": parts[3].strip() if len(parts) > 3 else "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "None",
                })
        return cookies

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        cookies_raw = self._get_cookies_raw()
        if not cookies_raw:
            return ToolResult(
                success=False,
                error="GOOGLE_FLOW_COOKIES not set. " + self.install_instructions,
            )
        email = self._get_email()
        if not email:
            return ToolResult(
                success=False,
                error="GOOGLE_FLOW_EMAIL not set. " + self.install_instructions,
            )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return ToolResult(
                success=False,
                error="playwright not installed. Run: pip3 install playwright && playwright install chromium",
            )

        start = time.time()
        prompt = inputs["prompt"]
        model = inputs.get("model", self._get_default_model())
        aspect_ratio = inputs.get("aspect_ratio", "portrait")
        operation = inputs.get("operation", "text_to_video")
        image_path = inputs.get("image_path")
        end_image_path = inputs.get("end_image_path")
        reference_image_paths = inputs.get("reference_image_paths") or []
        voice = inputs.get("voice")
        count = max(1, int(inputs.get("count", 1)))
        seed = inputs.get("seed")
        output_path = Path(inputs.get("output_path", "google_flow_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Warn if cookies look stale (non-blocking — runtime will confirm)
        if not self._cookies_are_fresh(cookies_raw):
            import warnings
            warnings.warn("GOOGLE_FLOW_COOKIES may be expired. If generation fails, recapture via UI.")

        try:
            cookies = self._parse_cookies(cookies_raw)
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to parse GOOGLE_FLOW_COOKIES: {e}")

        video_url: str | None = None
        intercepted_requests: list[dict] = []

        try:
            with sync_playwright() as pw:
                browser = None
                context = None

                # ── Strategy 1: Connect to real Opera via CDP ─────────────────
                # Opera đang chạy thật với --remote-debugging-port=9222
                # Playwright chỉ gửi CDP commands — không inject webdriver flag
                if _cdp_is_alive():
                    try:
                        browser = pw.chromium.connect_over_cdp(_CDP_URL)
                        # Dùng context đầu tiên (session đang login)
                        if browser.contexts:
                            context = browser.contexts[0]
                        else:
                            context = browser.new_context()
                    except Exception:
                        browser = None
                        context = None

                # ── Strategy 2: Launch Opera với CDP port ─────────────────────
                # Opera chưa chạy — tự launch với debug port
                if context is None and Path(_OPERA_PATH).exists():
                    launched = _launch_opera_with_cdp()
                    if launched:
                        try:
                            browser = pw.chromium.connect_over_cdp(_CDP_URL)
                            if browser.contexts:
                                context = browser.contexts[0]
                            else:
                                context = browser.new_context()
                        except Exception:
                            browser = None
                            context = None

                # ── Strategy 3: Fallback persistent context (cũ) ─────────────
                # Dùng khi không thể dùng CDP — ít stealth hơn
                if context is None and Path(_OPERA_PATH).exists():
                    context = pw.chromium.launch_persistent_context(
                        user_data_dir=_OPERA_USER_DATA,
                        executable_path=_OPERA_PATH,
                        headless=False,
                        args=["--no-first-run", "--no-default-browser-check"],
                        viewport={"width": 1280, "height": 900},
                    )
                    browser = None

                # ── Strategy 4: Headless Chromium với cookies inject ──────────
                if context is None:
                    browser = pw.chromium.launch(
                        headless=False,
                        args=[
                            "--no-sandbox",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-dev-shm-usage",
                            "--window-size=1280,900",
                        ],
                    )
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 900},
                        locale="en-US",
                    )
                    context.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    )
                    _same_site_map = {"strict":"Strict","lax":"Lax","none":"None","no_restriction":"None","unspecified":"None"}
                    playwright_cookies = []
                    for c in cookies:
                        domain = c.get("domain", ".google.com")
                        if not domain.startswith("."): domain = "." + domain.lstrip(".")
                        if "google" not in domain.lower(): continue
                        raw_ss = str(c.get("sameSite","None")).lower()
                        ck = {
                            "name": c.get("name",""), "value": c.get("value",""),
                            "domain": domain, "path": c.get("path","/"),
                            "httpOnly": bool(c.get("httpOnly",False)),
                            "secure": bool(c.get("secure",True)),
                            "sameSite": _same_site_map.get(raw_ss,"None"),
                        }
                        if c.get("expires"): ck["expires"] = float(c["expires"])
                        playwright_cookies.append(ck)
                    context.add_cookies(playwright_cookies)

                # Intercept API responses to capture video generation results
                captured: dict = {}

                def handle_response(response):
                    url = response.url
                    if any(kw in url for kw in [
                        "generate_video", "generateVideo", "createVideo",
                        "flow/video", "aisandbox", "generativeai",
                        "veo", "/media", "/generate",
                    ]):
                        try:
                            body = response.body()
                            text = body.decode("utf-8", errors="replace")
                            if any(kw in text for kw in ["videoUrl", "fifeUrl", "video_url", "mp4"]):
                                captured["response_url"] = url
                                captured["response_body"] = text
                        except Exception:
                            pass

                page = context.new_page()
                page.on("response", handle_response)

                # Navigate to Google Flow
                page.goto(_FLOW_URL, wait_until="networkidle", timeout=30000)

                # Verify login
                page_content = page.content()
                if "sign in" in page_content.lower() and "new project" not in page_content.lower():
                    page.close()
                    return ToolResult(
                        success=False,
                        error="Google Flow session expired — đăng nhập lại Opera rồi thử lại",
                    )

                # Find and use the video generation flow
                video_url = self._generate_via_page(
                    page, context, prompt, model, aspect_ratio,
                    operation, image_path, captured, output_path,
                    end_image_path=end_image_path,
                    reference_image_paths=reference_image_paths,
                    voice=voice,
                    count=count,
                    seed=seed,
                )

                # Đóng tab vừa tạo, KHÔNG đóng browser (Opera thật vẫn chạy)
                try:
                    page.close()
                except Exception:
                    pass

        except Exception as e:
            return ToolResult(success=False, error=f"Google Flow automation failed: {e}")

        if not video_url:
            return ToolResult(
                success=False,
                error="Google Flow did not return a video URL. Session may have expired.",
            )

        # Collect all output paths (single or multi-count)
        if isinstance(video_url, list):
            # _generate_via_page returned multiple paths when count > 1
            output_paths = video_url
        else:
            output_paths = [str(output_path)]

        existing_outputs = [p for p in output_paths if Path(p).exists()]
        if not existing_outputs:
            return ToolResult(
                success=False,
                error="Google Flow video URL found but file was not saved to disk.",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "google_flow",
                "model": model,
                "prompt": prompt,
                "output": existing_outputs[0],
                "outputs": existing_outputs,
                "aspect_ratio": aspect_ratio,
                "operation": operation,
                "duration_seconds": inputs.get("duration_seconds", 8),
                "count": count,
                "seed": seed,
            },
            artifacts=existing_outputs,
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )

    def _generate_via_page(
        self,
        page: Any,
        context: Any,
        prompt: str,
        model: str,
        aspect_ratio: str,
        operation: str,
        image_path: str | None,
        captured: dict,
        output_path: Path,
        *,
        end_image_path: str | None = None,
        reference_image_paths: list[str] | None = None,
        voice: str | None = None,
        count: int = 1,
        seed: int | None = None,
    ) -> str | list[str] | None:
        """Navigate Google Flow UI to generate a video.

        Returns a single video URL string (count=1) or list of output file paths
        (count>1, base name is derived from output_path with _1/_2... suffix).
        """
        import requests

        # Model display name → UI selector mapping
        model_labels = {
            "veo-3.1-quality": "Quality",
            "veo-3.1-fast": "Fast",
            "veo-3.1-fast-relaxed": "Fast [Lower Priority]",
            "veo-3.1-lite": "Lite",
        }
        model_label = model_labels.get(model, "Fast")

        def _jitter(ms_min: int = 400, ms_max: int = 1200) -> None:
            """Random human-like delay."""
            page.wait_for_timeout(random.randint(ms_min, ms_max))

        def _human_fill(locator: Any, text: str) -> None:
            """Type text character by character with random delays."""
            locator.click()
            _jitter(200, 500)
            # Clear existing content
            locator.press("Control+a")
            locator.press("Delete")
            _jitter(100, 300)
            # Type with realistic speed (50-150ms per char)
            for char in text:
                locator.type(char, delay=random.randint(50, 150))

        # Brief pause after page load — mimic user reading the page
        _jitter(800, 2000)

        # Click "New project" or "Video" tab
        try:
            page.get_by_role("button", name=re.compile(r"new project", re.IGNORECASE)).first.click(timeout=5000)
            _jitter(600, 1500)
        except Exception:
            pass

        # Select Video mode
        try:
            page.get_by_role("tab", name=re.compile(r"video", re.IGNORECASE)).first.click(timeout=5000)
            _jitter(500, 1000)
        except Exception:
            pass

        # Set aspect ratio
        try:
            if aspect_ratio == "portrait":
                page.get_by_role("button", name=re.compile(r"9:16|portrait", re.IGNORECASE)).first.click(timeout=3000)
            else:
                page.get_by_role("button", name=re.compile(r"16:9|landscape", re.IGNORECASE)).first.click(timeout=3000)
            _jitter(300, 700)
        except Exception:
            pass

        # Select model
        try:
            page.get_by_role("combobox").first.click(timeout=3000)
            _jitter(400, 800)
            page.get_by_role("option", name=re.compile(model_label, re.IGNORECASE)).first.click(timeout=3000)
            _jitter(300, 600)
        except Exception:
            try:
                page.locator(f"text={model_label}").first.click(timeout=3000)
                _jitter(300, 600)
            except Exception:
                pass

        # Upload start image for I2V or I2V-FL
        if operation in ("image_to_video", "image_to_video_fl") and image_path and Path(image_path).exists():
            try:
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(image_path)
                _jitter(800, 2000)
            except Exception:
                pass

        # Upload end image for I2V-FL (frame interpolation)
        if operation == "image_to_video_fl" and end_image_path and Path(end_image_path).exists():
            try:
                # Try to find a second file input or "end frame" specific input
                file_inputs = page.locator('input[type="file"]').all()
                if len(file_inputs) >= 2:
                    file_inputs[1].set_input_files(end_image_path)
                else:
                    # Try end-frame labeled upload button
                    end_btn = page.get_by_role(
                        "button", name=re.compile(r"end.?frame|last.?frame", re.IGNORECASE)
                    ).first
                    end_btn.click(timeout=3000)
                    _jitter(300, 600)
                    # After clicking, a file input may appear
                    try:
                        page.locator('input[type="file"]').last.set_input_files(end_image_path)
                    except Exception:
                        pass
                _jitter(800, 1500)
            except Exception:
                pass

        # Upload reference images for R2V mode
        if operation == "reference_to_video" and reference_image_paths:
            for ref_path in (reference_image_paths or [])[:3]:
                if not Path(ref_path).exists():
                    continue
                try:
                    # Look for reference image upload button
                    ref_btn = page.get_by_role(
                        "button", name=re.compile(r"add.?reference|reference.?image|upload.?reference", re.IGNORECASE)
                    ).first
                    ref_btn.click(timeout=3000)
                    _jitter(300, 700)
                    page.locator('input[type="file"]').last.set_input_files(ref_path)
                    _jitter(600, 1200)
                except Exception:
                    pass

        # Select voice for R2V mode
        if operation == "reference_to_video" and voice:
            try:
                voice_dropdown = page.get_by_role(
                    "combobox", name=re.compile(r"voice|narrator", re.IGNORECASE)
                ).first
                voice_dropdown.click(timeout=3000)
                _jitter(300, 600)
                page.get_by_role("option", name=re.compile(re.escape(voice), re.IGNORECASE)).first.click(timeout=3000)
                _jitter(300, 600)
            except Exception:
                try:
                    page.locator(f'text={voice}').first.click(timeout=2000)
                    _jitter(300, 600)
                except Exception:
                    pass

        # Set count (variations stepper)
        if count > 1:
            try:
                # Try a numeric input or spinner labeled "count" / "variations"
                count_input = page.locator(
                    'input[aria-label*="count" i], input[aria-label*="variation" i], input[name="count"]'
                ).first
                count_input.fill(str(count))
                _jitter(300, 600)
            except Exception:
                try:
                    # Try clicking a "+" stepper button count-1 times
                    plus_btn = page.locator(
                        'button[aria-label*="increase" i], button[aria-label*="+" i]'
                    ).first
                    for _ in range(count - 1):
                        plus_btn.click()
                        _jitter(150, 300)
                except Exception:
                    pass  # count UI not found — will generate single video

        # Set seed
        if seed is not None:
            try:
                seed_input = page.locator(
                    'input[aria-label*="seed" i], input[placeholder*="seed" i], input[name="seed"]'
                ).first
                seed_input.fill(str(seed))
                _jitter(300, 600)
            except Exception:
                pass

        # Enter prompt — type like a human, not instant fill
        try:
            prompt_area = page.locator("textarea").first
            _human_fill(prompt_area, prompt)
            _jitter(400, 900)
        except Exception:
            try:
                _human_fill(page.get_by_role("textbox").first, prompt)
                _jitter(400, 900)
            except Exception:
                pass

        # Hover over Generate button before clicking — human behavior
        try:
            gen_btn = page.get_by_role("button", name=re.compile(r"generate|create", re.IGNORECASE)).first
            gen_btn.hover()
            _jitter(300, 700)
            gen_btn.click(timeout=5000)
        except Exception:
            page.keyboard.press("Enter")

        # Wait for generation (up to _MAX_WAIT seconds)
        deadline = time.time() + self._MAX_WAIT
        video_url: str | None = None

        while time.time() < deadline:
            page.wait_for_timeout(5000)

            # Check for intercepted API response
            if captured.get("response_body"):
                video_url = self._extract_video_url(captured["response_body"])
                if video_url:
                    break

            # Check for video elements in DOM
            try:
                video_src = page.evaluate("""() => {
                    const v = document.querySelector('video[src*="mp4"], video source[src*="mp4"]');
                    if (v) return v.src || v.getAttribute('src');
                    // Check blob URLs
                    const blobs = document.querySelectorAll('video');
                    for (const b of blobs) {
                        if (b.src && b.src.includes('blob:')) {
                            return null; // blob needs different handling
                        }
                    }
                    return null;
                }""")
                if video_src and "mp4" in str(video_src):
                    video_url = video_src
                    break
            except Exception:
                pass

            # Check for download button / video URL in page source
            try:
                src = page.content()
                found = self._extract_video_url(src)
                if found:
                    video_url = found
                    break
            except Exception:
                pass

        if not video_url:
            return None

        # Collect all video URLs found in page (for multi-count generation)
        all_urls: list[str] = []
        if isinstance(video_url, list):
            all_urls = video_url
        else:
            all_urls = [video_url]

        # Also scan captured body for additional URLs
        if captured.get("response_body") and len(all_urls) < count:
            body = captured["response_body"]
            for pattern in [
                r'"videoUrl"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
                r'"fifeUrl"\s*:\s*"(https?://[^"]+)"',
                r'"video_url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
            ]:
                for m in re.finditer(pattern, body):
                    url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
                    if url not in all_urls:
                        all_urls.append(url)
            all_urls = all_urls[:count]

        # Download each video
        saved_paths: list[str] = []
        headers = {"User-Agent": "Mozilla/5.0"}
        for i, url in enumerate(all_urls):
            if count == 1:
                dest = output_path
            else:
                stem = output_path.stem
                suffix = output_path.suffix or ".mp4"
                dest = output_path.with_name(f"{stem}_{i + 1}{suffix}")
            try:
                resp = requests.get(url, headers=headers, timeout=120)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                saved_paths.append(str(dest))
            except Exception:
                try:
                    with page.expect_download(timeout=60000) as dl_info:
                        page.evaluate(f"""() => {{
                            const a = document.createElement('a');
                            a.href = '{url}';
                            a.download = 'flow_video.mp4';
                            a.click();
                        }}""")
                    dl = dl_info.value
                    dl.save_as(str(dest))
                    saved_paths.append(str(dest))
                except Exception:
                    pass

        if not saved_paths:
            return None
        return saved_paths[0] if count == 1 else saved_paths

    def _extract_video_url(self, text: str) -> str | None:
        """Extract MP4 or signed video URL from response text."""
        # Direct MP4 URLs
        patterns = [
            r'"videoUrl"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
            r'"fifeUrl"\s*:\s*"(https?://[^"]+)"',
            r'"video_url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
            r'(https://[a-z0-9\-]+\.storage\.googleapis\.com/[^"\s]+\.mp4[^"\s]*)',
            r'(https://aisandbox[^"\s]+\.mp4[^"\s]*)',
            r'(https://[^"\s]+/video[^"\s]+\.mp4[^"\s]*)',
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                url = m.group(1)
                # Unescape JSON unicode
                url = url.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
                return url
        return None


# ─── CLI helper ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--launch-opera" in sys.argv:
        print(f"Launching Opera with --remote-debugging-port={_CDP_PORT}...")
        ok = _launch_opera_with_cdp()
        if ok:
            print(f"✅ Opera is running with CDP on port {_CDP_PORT}")
            print("   Đăng nhập Google Flow trong Opera rồi generate video bình thường.")
        else:
            print("❌ Failed to launch Opera. Check _OPERA_PATH.")
    elif "--check-cdp" in sys.argv:
        alive = _cdp_is_alive()
        print(f"CDP port {_CDP_PORT}: {'✅ ALIVE' if alive else '❌ NOT running'}")
        if not alive:
            print(f"  Start Opera with: {_OPERA_PATH} --remote-debugging-port={_CDP_PORT} --no-first-run")
