"""Google Flow image generation via browser automation.

Uses Playwright with your Google account cookies to call Google Flow
(labs.google/fx/tools/flow) directly — no proxy fee.

Supports Imagen-4 and nano-banana models with reference images (R2V style).

Setup: same as google_flow_video — GOOGLE_FLOW_COOKIES + GOOGLE_FLOW_EMAIL in .env.
Requires: playwright>=1.40
Install:  pip3 install playwright && playwright install chromium
"""

from __future__ import annotations

import json
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

# Reuse shared constants from the video tool module
try:
    from tools.video.google_flow_video import (
        _FLOW_URL,
        _OPERA_PATH,
        _OPERA_USER_DATA,
    )
except ImportError:
    _FLOW_URL = "https://labs.google/fx/tools/flow"
    _OPERA_PATH = "/Applications/Opera.app/Contents/MacOS/Opera"
    _OPERA_USER_DATA = str(Path.home() / "Library/Application Support/com.operasoftware.Opera")


def _parse_cookies(raw: str) -> list[dict]:
    """Parse cookies from JSON array (DevTools export) or tab-separated Netscape format."""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
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


def _build_playwright_cookies(cookies: list[dict]) -> list[dict]:
    """Normalise sameSite values and filter to Google domains only."""
    _same_site_map = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "None",
    }
    result = []
    for c in cookies:
        domain = c.get("domain", ".google.com")
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        if "google" not in domain.lower():
            continue
        raw_ss = str(c.get("sameSite", "None")).lower()
        cookie: dict[str, Any] = {
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


class GoogleFlowImage(BaseTool):
    name = "google_flow_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
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
        "5. Set GOOGLE_FLOW_EMAIL=your_email@gmail.com trong .env"
    )
    agent_skills = ["fal-ai-media"]

    capabilities = ["text_to_image", "reference_to_image"]
    supports = {
        "text_to_image": True,
        "reference_to_image": True,
        "aspect_ratio": True,
        "seed": True,
        "count": True,
    }
    best_for = [
        "Imagen-4 quality images without API key cost",
        "nano-banana creative/artistic image generation",
        "Reference-style image generation via R2V-style upload",
    ]
    not_good_for = [
        "Headless server without display (needs Playwright Chromium)",
        "Exact pixel dimensions (Flow uses aspect ratios)",
    ]
    fallback_tools = ["google_imagen", "flux_image"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Image description",
            },
            "model": {
                "type": "string",
                "enum": ["imagen-4", "nano-banana-2", "nano-banana-pro"],
                "default": "imagen-4",
                "description": "imagen-4=4¢/img, nano-banana-2=2¢/img, nano-banana-pro=5¢/img",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "4:3", "1:1", "3:4", "9:16"],
                "default": "16:9",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "default": 1,
                "description": "Number of image variations to generate",
            },
            "seed": {
                "type": "integer",
                "description": "Seed value for reproducible generation",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
                "description": "Reference-style images (max 10) for style transfer",
            },
            "output_dir": {
                "type": "string",
                "default": "google_flow_images",
                "description": "Directory to save generated images",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "rate_limit"])
    idempotency_key_fields = ["prompt", "model", "aspect_ratio"]
    side_effects = ["writes image files to output_dir", "launches Playwright browser"]
    user_visible_verification = ["Inspect generated images for quality and relevance"]

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
        model = inputs.get("model", "imagen-4")
        count = max(1, int(inputs.get("count", 1)))
        costs = {
            "imagen-4": 0.04,
            "nano-banana-2": 0.02,
            "nano-banana-pro": 0.05,
        }
        return costs.get(model, 0.04) * count

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
            from playwright.sync_api import sync_playwright
        except ImportError:
            return ToolResult(
                success=False,
                error="playwright not installed. Run: pip3 install playwright && playwright install chromium",
            )

        start = time.time()
        prompt = inputs["prompt"]
        model = inputs.get("model", "imagen-4")
        aspect_ratio = inputs.get("aspect_ratio", "16:9")
        count = max(1, int(inputs.get("count", 1)))
        seed = inputs.get("seed")
        reference_image_paths = inputs.get("reference_image_paths") or []
        output_dir = Path(inputs.get("output_dir", "google_flow_images"))
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            cookies = _parse_cookies(cookies_raw)
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to parse GOOGLE_FLOW_COOKIES: {e}")

        # Build a URL-safe slug for naming output files
        slug = re.sub(r"[^\w]+", "_", prompt[:40]).strip("_").lower() or "image"

        try:
            saved_paths = self._generate_via_page(
                cookies=cookies,
                prompt=prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                count=count,
                seed=seed,
                reference_image_paths=reference_image_paths,
                output_dir=output_dir,
                slug=slug,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Google Flow image automation failed: {e}")

        if not saved_paths:
            return ToolResult(
                success=False,
                error="Google Flow did not return any images. Session may have expired.",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "google_flow",
                "model": model,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "count": count,
                "seed": seed,
                "output": saved_paths[0],
                "outputs": saved_paths,
            },
            artifacts=saved_paths,
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )

    def _generate_via_page(
        self,
        *,
        cookies: list[dict],
        prompt: str,
        model: str,
        aspect_ratio: str,
        count: int,
        seed: int | None,
        reference_image_paths: list[str],
        output_dir: Path,
        slug: str,
    ) -> list[str]:
        """Drive Google Flow UI to generate images. Returns list of saved file paths."""
        import requests
        from playwright.sync_api import sync_playwright

        import random

        # Model label → UI text mapping
        model_labels = {
            "imagen-4": "Imagen 4",
            "nano-banana-2": "Nano Banana 2",
            "nano-banana-pro": "Nano Banana Pro",
        }
        model_label = model_labels.get(model, "Imagen 4")

        saved_paths: list[str] = []
        captured_image_urls: list[str] = []

        def handle_response(response: Any) -> None:
            url = response.url
            if any(kw in url for kw in ["generate", "imagen", "predict", "aisandbox", "generativeai"]):
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="replace")
                    if any(kw in text for kw in ["fifeUrl", "imageUrl", "image_url", "base64", "bytesBase64"]):
                        # Extract URLs from response
                        for pattern in [
                            r'"fifeUrl"\s*:\s*"(https?://[^"]+)"',
                            r'"imageUrl"\s*:\s*"(https?://[^"]+)"',
                            r'"image_url"\s*:\s*"(https?://[^"]+)"',
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
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    window.chrome = { runtime: {} };
                """)
                playwright_cookies = _build_playwright_cookies(cookies)
                context.add_cookies(playwright_cookies)

            page = context.new_page()
            page.on("response", handle_response)

            try:
                # Navigate to Google Flow
                page.goto(_FLOW_URL, wait_until="networkidle", timeout=30000)
                _jitter(800, 2000)

                # Click "Image" tab
                try:
                    page.get_by_role("tab", name=re.compile(r"^image$", re.IGNORECASE)).first.click(timeout=5000)
                    _jitter(500, 1000)
                except Exception:
                    try:
                        page.get_by_role("button", name=re.compile(r"^image$", re.IGNORECASE)).first.click(timeout=3000)
                        _jitter(500, 1000)
                    except Exception:
                        pass

                # Select model via dropdown or tabs
                try:
                    page.get_by_role("combobox").first.click(timeout=3000)
                    _jitter(300, 700)
                    page.get_by_role("option", name=re.compile(model_label, re.IGNORECASE)).first.click(timeout=3000)
                    _jitter(300, 600)
                except Exception:
                    try:
                        page.locator(f"text={model_label}").first.click(timeout=3000)
                        _jitter(300, 600)
                    except Exception:
                        pass

                # Set aspect ratio
                try:
                    page.get_by_role(
                        "button", name=re.compile(re.escape(aspect_ratio), re.IGNORECASE)
                    ).first.click(timeout=3000)
                    _jitter(300, 600)
                except Exception:
                    try:
                        page.locator(f"text={aspect_ratio}").first.click(timeout=2000)
                        _jitter(300, 600)
                    except Exception:
                        pass

                # Upload reference images
                for ref_path in reference_image_paths[:10]:
                    if not Path(ref_path).exists():
                        continue
                    try:
                        ref_btn = page.get_by_role(
                            "button", name=re.compile(r"add.?reference|upload.?image|reference", re.IGNORECASE)
                        ).first
                        ref_btn.click(timeout=3000)
                        _jitter(300, 700)
                        page.locator('input[type="file"]').last.set_input_files(ref_path)
                        _jitter(600, 1200)
                    except Exception:
                        try:
                            page.locator('input[type="file"]').last.set_input_files(ref_path)
                            _jitter(600, 1200)
                        except Exception:
                            pass

                # Set count (variations)
                if count > 1:
                    try:
                        count_input = page.locator(
                            'input[aria-label*="count" i], input[aria-label*="variation" i], input[name="count"]'
                        ).first
                        count_input.fill(str(count))
                        _jitter(300, 600)
                    except Exception:
                        try:
                            plus_btn = page.locator(
                                'button[aria-label*="increase" i], button[aria-label*="+" i]'
                            ).first
                            for _ in range(count - 1):
                                plus_btn.click()
                                _jitter(150, 300)
                        except Exception:
                            pass

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

                # Enter prompt
                try:
                    prompt_area = page.locator("textarea").first
                    prompt_area.click()
                    _jitter(200, 500)
                    prompt_area.press("Control+a")
                    prompt_area.press("Delete")
                    _jitter(100, 300)
                    for char in prompt:
                        prompt_area.type(char, delay=random.randint(40, 120))
                    _jitter(400, 900)
                except Exception:
                    try:
                        page.get_by_role("textbox").first.fill(prompt)
                        _jitter(400, 900)
                    except Exception:
                        pass

                # Click Generate
                try:
                    gen_btn = page.get_by_role(
                        "button", name=re.compile(r"generate|create", re.IGNORECASE)
                    ).first
                    gen_btn.hover()
                    _jitter(300, 600)
                    gen_btn.click(timeout=5000)
                except Exception:
                    page.keyboard.press("Enter")

                # Wait for images
                deadline = time.time() + self._MAX_WAIT
                while time.time() < deadline:
                    page.wait_for_timeout(4000)

                    # Check intercepted response URLs
                    if captured_image_urls:
                        break

                    # Check DOM for generated image elements
                    try:
                        img_urls: list[str] = page.evaluate("""() => {
                            const imgs = document.querySelectorAll('img[src*="googleusercontent"], img[src*="aisandbox"], img[src*="googleapis"]');
                            return Array.from(imgs).map(i => i.src).filter(s => s && s.startsWith('http'));
                        }""")
                        if img_urls:
                            for u in img_urls:
                                if u not in captured_image_urls:
                                    captured_image_urls.append(u)
                            break
                    except Exception:
                        pass

                    # Check page source for image URLs
                    try:
                        src = page.content()
                        for pattern in [
                            r'"fifeUrl"\s*:\s*"(https?://[^"]+)"',
                            r'"imageUrl"\s*:\s*"(https?://[^"]+)"',
                            r'(https://[a-z0-9\-]+\.googleusercontent\.com/[^"\s<>]+)',
                        ]:
                            for m in re.finditer(pattern, src):
                                url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
                                if url not in captured_image_urls:
                                    captured_image_urls.append(url)
                        if captured_image_urls:
                            break
                    except Exception:
                        pass

                # Download captured images
                headers = {"User-Agent": "Mozilla/5.0"}
                for i, img_url in enumerate(captured_image_urls[:count]):
                    dest = output_dir / f"{slug}_{i + 1}.jpg"
                    try:
                        resp = requests.get(img_url, headers=headers, timeout=60)
                        resp.raise_for_status()
                        dest.write_bytes(resp.content)
                        saved_paths.append(str(dest))
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

        return saved_paths
