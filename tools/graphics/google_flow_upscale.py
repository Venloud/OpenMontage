"""Google Flow image upscaling via browser automation.

Upscales nano-banana (or other) images to 2K or 4K using Google Flow's
built-in upscale feature.

Setup: same as google_flow_video — GOOGLE_FLOW_COOKIES + GOOGLE_FLOW_EMAIL in .env.
Requires: playwright>=1.40
Install:  pip3 install playwright && playwright install chromium
"""

from __future__ import annotations

import os
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

# Reuse shared helpers from the image tool
try:
    from tools.graphics.google_flow_image import (
        _parse_cookies,
        _build_playwright_cookies,
    )
    from tools.video.google_flow_video import (
        _FLOW_URL,
        _OPERA_PATH,
        _OPERA_USER_DATA,
    )
except ImportError:
    _FLOW_URL = "https://labs.google/fx/tools/flow"
    _OPERA_PATH = "/Applications/Opera.app/Contents/MacOS/Opera"
    _OPERA_USER_DATA = str(Path.home() / "Library/Application Support/com.operasoftware.Opera")

    import json

    def _parse_cookies(raw: str) -> list[dict]:  # type: ignore[misc]
        raw = raw.strip()
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except Exception:
                pass
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

    def _build_playwright_cookies(cookies: list[dict]) -> list[dict]:  # type: ignore[misc]
        _same_site_map = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None", "unspecified": "None"}
        result = []
        for c in cookies:
            domain = c.get("domain", ".google.com")
            if not domain.startswith("."):
                domain = "." + domain.lstrip(".")
            if "google" not in domain.lower():
                continue
            raw_ss = str(c.get("sameSite", "None")).lower()
            cookie: dict = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": domain,
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": _same_site_map.get(raw_ss, "None"),
            }
            if c.get("expires"):
                cookie["expires"] = float(c["expires"])
            result.append(cookie)
        return result


class GoogleFlowUpscale(BaseTool):
    name = "google_flow_upscale"
    version = "0.1.0"
    tier = ToolTier.ENHANCE
    capability = "enhancement"
    provider = "google_flow"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL  # runs local Playwright browser

    dependencies = ["playwright>=1.40"]
    install_instructions = (
        "1. pip3 install playwright && playwright install chromium\n"
        "2. Đăng nhập Google Flow trên Opera/Brave (không dùng Chrome)\n"
        "3. DevTools → Application → Cookies → https://accounts.google.com/ → Ctrl+A Ctrl+C\n"
        "4. Set GOOGLE_FLOW_COOKIES=<json_array_of_cookie_objects> trong .env\n"
        "5. Set GOOGLE_FLOW_EMAIL=your_email@gmail.com trong .env"
    )
    agent_skills = ["fal-ai-media"]

    capabilities = ["upscale_image", "enhance_resolution"]
    supports = {
        "upscale_2k": True,
        "upscale_4k": True,
    }
    best_for = [
        "Upscaling nano-banana generated images to 2K or 4K",
        "Improving resolution of any Google Flow output image",
    ]
    not_good_for = [
        "Upscaling arbitrary photos (best with Flow-generated images)",
        "Headless server without display",
    ]
    fallback_tools = []

    input_schema = {
        "type": "object",
        "required": ["image_path"],
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Local path to the image to upscale",
            },
            "resolution": {
                "type": "string",
                "enum": ["2k", "4k"],
                "default": "2k",
                "description": "Target resolution: 2k=2¢, 4k=5¢",
            },
            "output_path": {
                "type": "string",
                "description": "Output file path (optional — defaults to input name with _upscaled suffix)",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=300, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "rate_limit"])
    idempotency_key_fields = ["image_path", "resolution"]
    side_effects = ["writes upscaled image to output_path", "launches Playwright browser"]
    user_visible_verification = ["Compare original and upscaled images for resolution improvement"]

    _MAX_WAIT = 180  # 3 minutes

    def _get_cookies_raw(self) -> str | None:
        return os.environ.get("GOOGLE_FLOW_COOKIES")

    def _get_email(self) -> str | None:
        return os.environ.get("GOOGLE_FLOW_EMAIL")

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
        resolution = inputs.get("resolution", "2k")
        return 0.05 if resolution == "4k" else 0.02

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        cookies_raw = self._get_cookies_raw()
        if not cookies_raw:
            return ToolResult(
                success=False,
                error="GOOGLE_FLOW_COOKIES not set. " + self.install_instructions,
            )
        if not self._get_email():
            return ToolResult(
                success=False,
                error="GOOGLE_FLOW_EMAIL not set. " + self.install_instructions,
            )

        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            return ToolResult(
                success=False,
                error="playwright not installed. Run: pip3 install playwright && playwright install chromium",
            )

        image_path = Path(inputs["image_path"])
        if not image_path.exists():
            return ToolResult(success=False, error=f"Image not found: {image_path}")

        resolution = inputs.get("resolution", "2k")
        if inputs.get("output_path"):
            output_path = Path(inputs["output_path"])
        else:
            output_path = image_path.with_name(
                f"{image_path.stem}_upscaled_{resolution}{image_path.suffix}"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            cookies = _parse_cookies(cookies_raw)
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to parse GOOGLE_FLOW_COOKIES: {e}")

        start = time.time()

        try:
            result_path = self._upscale_via_page(
                cookies=cookies,
                image_path=image_path,
                resolution=resolution,
                output_path=output_path,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Google Flow upscale automation failed: {e}")

        if not result_path or not Path(result_path).exists():
            return ToolResult(
                success=False,
                error="Google Flow upscale did not return an image. Session may have expired.",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "google_flow",
                "input": str(image_path),
                "output": result_path,
                "resolution": resolution,
            },
            artifacts=[result_path],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
        )

    def _upscale_via_page(
        self,
        *,
        cookies: list[dict],
        image_path: Path,
        resolution: str,
        output_path: Path,
    ) -> str | None:
        """Drive Google Flow UI to upscale an image. Returns saved file path."""
        import random
        import requests
        from playwright.sync_api import sync_playwright

        captured_image_urls: list[str] = []

        def handle_response(response: Any) -> None:
            url = response.url
            if any(kw in url for kw in ["upscale", "enhance", "super", "resolution", "aisandbox", "generativeai"]):
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="replace")
                    if any(kw in text for kw in ["fifeUrl", "imageUrl", "image_url", "bytesBase64"]):
                        for pattern in [
                            r'"fifeUrl"\s*:\s*"(https?://[^"]+)"',
                            r'"imageUrl"\s*:\s*"(https?://[^"]+)"',
                            r'"url"\s*:\s*"(https?://[^"]+(?:png|jpg|jpeg|webp)[^"]*)"',
                        ]:
                            for m in re.finditer(pattern, text):
                                img_url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
                                if img_url not in captured_image_urls:
                                    captured_image_urls.append(img_url)
                except Exception:
                    pass

        def _jitter(ms_min: int = 300, ms_max: int = 900) -> None:
            page.wait_for_timeout(random.randint(ms_min, ms_max))

        use_real_browser = Path(_OPERA_PATH).exists()

        with sync_playwright() as pw:
            if use_real_browser:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=_OPERA_USER_DATA,
                    executable_path=_OPERA_PATH,
                    headless=False,
                    args=["--no-first-run", "--no-default-browser-check"],
                    viewport={"width": 1280, "height": 900},
                )
                browser = None
            else:
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
                    timezone_id="America/Los_Angeles",
                )
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    window.chrome = { runtime: {} };
                """)
                playwright_cookies = _build_playwright_cookies(cookies)
                context.add_cookies(playwright_cookies)

            page = context.new_page()
            page.on("response", handle_response)

            saved_path: str | None = None

            try:
                page.goto(_FLOW_URL, wait_until="networkidle", timeout=30000)
                _jitter(800, 2000)

                # Navigate to Image mode
                try:
                    page.get_by_role("tab", name=re.compile(r"^image$", re.IGNORECASE)).first.click(timeout=5000)
                    _jitter(500, 1000)
                except Exception:
                    pass

                # Look for Upscale button or section
                upscale_clicked = False
                try:
                    upscale_btn = page.get_by_role(
                        "button", name=re.compile(r"upscale", re.IGNORECASE)
                    ).first
                    upscale_btn.click(timeout=5000)
                    _jitter(500, 1000)
                    upscale_clicked = True
                except Exception:
                    pass

                if not upscale_clicked:
                    try:
                        page.locator("text=Upscale").first.click(timeout=3000)
                        _jitter(500, 1000)
                        upscale_clicked = True
                    except Exception:
                        pass

                # Upload the image to upscale
                try:
                    file_input = page.locator('input[type="file"]').first
                    file_input.set_input_files(str(image_path))
                    _jitter(800, 2000)
                except Exception:
                    # Try clicking an upload area first
                    try:
                        upload_area = page.get_by_role(
                            "button", name=re.compile(r"upload|choose|select.?file", re.IGNORECASE)
                        ).first
                        upload_area.click(timeout=3000)
                        _jitter(300, 700)
                        page.locator('input[type="file"]').last.set_input_files(str(image_path))
                        _jitter(800, 2000)
                    except Exception:
                        pass

                # Select resolution (2K or 4K)
                resolution_label = resolution.upper()
                try:
                    page.get_by_role(
                        "button", name=re.compile(rf"{re.escape(resolution_label)}", re.IGNORECASE)
                    ).first.click(timeout=3000)
                    _jitter(300, 600)
                except Exception:
                    try:
                        page.locator(f"text={resolution_label}").first.click(timeout=2000)
                        _jitter(300, 600)
                    except Exception:
                        pass

                # Click Upscale / Generate
                try:
                    action_btn = page.get_by_role(
                        "button", name=re.compile(r"upscale|enhance|generate", re.IGNORECASE)
                    ).first
                    action_btn.hover()
                    _jitter(200, 500)
                    action_btn.click(timeout=5000)
                except Exception:
                    page.keyboard.press("Enter")

                # Wait for result
                deadline = time.time() + self._MAX_WAIT
                while time.time() < deadline:
                    page.wait_for_timeout(4000)

                    if captured_image_urls:
                        break

                    # Check DOM for upscaled result image
                    try:
                        img_urls: list[str] = page.evaluate("""() => {
                            const imgs = document.querySelectorAll('img[src*="googleusercontent"], img[src*="aisandbox"]');
                            return Array.from(imgs).map(i => i.src).filter(s => s && s.startsWith('http'));
                        }""")
                        if img_urls:
                            captured_image_urls.extend(img_urls)
                            break
                    except Exception:
                        pass

                # Download the first (best) result
                if captured_image_urls:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    for img_url in captured_image_urls[:1]:
                        try:
                            resp = requests.get(img_url, headers=headers, timeout=120)
                            resp.raise_for_status()
                            output_path.write_bytes(resp.content)
                            saved_path = str(output_path)
                            break
                        except Exception:
                            try:
                                with page.expect_download(timeout=60000) as dl_info:
                                    page.evaluate(f"""() => {{
                                        const a = document.createElement('a');
                                        a.href = '{img_url}';
                                        a.download = 'upscaled.jpg';
                                        a.click();
                                    }}""")
                                dl = dl_info.value
                                dl.save_as(str(output_path))
                                saved_path = str(output_path)
                                break
                            except Exception:
                                pass

            finally:
                try:
                    if browser:
                        browser.close()
                    else:
                        context.close()
                except Exception:
                    pass

        return saved_path
