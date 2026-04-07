"""Capture Google Flow API endpoint bằng cách intercept network traffic.

Chạy script này khi đang mở Opera với Google Flow để tự động detect endpoint:
  python tools/video/capture_flow_endpoint.py

Script sẽ:
1. Dùng mitmproxy (nếu có) hoặc Playwright để intercept network
2. Click Generate trong Google Flow
3. Print ra exact endpoint URL + request format
4. Save vào .env

Cách dùng thủ công (nhanh hơn):
1. Mở Opera, vào https://labs.google/fx/tools/flow
2. DevTools (F12) → Network tab → filter: aisandbox
3. Click Generate với 1 prompt ngắn
4. Tìm request có method POST và URL chứa "aisandbox-pa.googleapis.com"
5. Right-click → Copy as cURL
6. Paste vào terminal: curl ... (để verify)
7. Copy URL vào GOOGLE_FLOW_ENDPOINT trong .env
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main():
    print("=== Google Flow Endpoint Capture ===\n")

    # Check cookies
    cookies_raw = os.environ.get("GOOGLE_FLOW_COOKIES") or _load_env_var("GOOGLE_FLOW_COOKIES")
    if not cookies_raw:
        print("❌ GOOGLE_FLOW_COOKIES chưa set trong .env")
        sys.exit(1)

    try:
        cookies = json.loads(cookies_raw)
        print(f"✅ Cookies loaded: {len(cookies)} entries")
        sapisid = next((c["value"] for c in cookies if c["name"] == "SAPISID"), None)
        print(f"✅ SAPISID: {'found' if sapisid else '❌ NOT FOUND'}")
        psid = next((c["value"] for c in cookies if c["name"] == "__Secure-1PSID"), None)
        print(f"✅ __Secure-1PSID: {'found' if psid else 'not found (ok)'}")
    except Exception as e:
        print(f"❌ Cookie parse error: {e}")
        sys.exit(1)

    print("\n--- Thử direct API ---")
    from tools.video.google_flow_direct import GoogleFlowDirect, _extract_cookies, _build_headers, _GENERATE_ENDPOINT
    cookies_dict = _extract_cookies(cookies_raw)
    headers = _build_headers(cookies_dict)

    import requests

    # Test với Veo-3.1-fast-relaxed model
    test_model = "veo-3.1-fast-relaxed-generate-preview"
    endpoint = _GENERATE_ENDPOINT.format(model=test_model)
    print(f"\nEndpoint: {endpoint}")

    payload = {
        "instances": [{
            "prompt": "A simple test: blue sky with white clouds, 4K cinematic",
            "videoGenerationConfig": {
                "aspectRatio": "9:16",
                "durationSeconds": 4,
                "model": test_model,
                "sampleCount": 1,
            },
        }],
        "parameters": {
            "model": test_model,
            "aspectRatio": "9:16",
            "durationSeconds": 4,
        },
    }

    print(f"\nPOST {endpoint}")
    print(f"Headers: Authorization={headers.get('Authorization', 'none')[:30]}...")

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        print(f"\nStatus: {resp.status_code}")
        print(f"Response: {resp.text[:500]}")

        if resp.status_code == 200:
            print("\n✅ ENDPOINT CHÍNH XÁC! Direct API hoạt động.")
            return
        elif resp.status_code == 404:
            print("\n⚠️ Endpoint 404 — URL model path có thể khác.")
            print("Làm theo hướng dẫn thủ công ở đầu file này.")
        elif resp.status_code == 401:
            print("\n❌ Session expired — re-capture cookies từ DevTools.")
        elif resp.status_code == 403:
            print("\n⚠️ 403 Forbidden — cần reCAPTCHA token.")
            print("Set CAPSOLVER_API_KEY trong .env để auto-solve.")
        else:
            print(f"\n⚠️ Unexpected status {resp.status_code}")
    except Exception as e:
        print(f"\n❌ Request error: {e}")

    print("\n=== Hướng dẫn capture thủ công ===")
    print("1. Opera → https://labs.google/fx/tools/flow")
    print("2. F12 → Network → filter: aisandbox-pa")
    print("3. Nhập prompt ngắn → click Generate")
    print("4. Tìm POST request → copy URL")
    print("5. Thêm vào .env:")
    print("   GOOGLE_FLOW_ENDPOINT=<URL bạn vừa copy>")
    print("6. Chạy lại script này để verify")


def _load_env_var(key: str) -> str | None:
    env_file = Path(".env")
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line[len(key)+1:].strip()
    return None


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
