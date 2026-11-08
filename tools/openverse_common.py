"""Shared Openverse client for openverse_image and openverse_audio (no API key).

Anonymous limits: 20 requests/min, 200/day. Only Public Domain Mark, CC0 and CC BY
results are kept: commercial use and modification allowed, attribution may be required.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

API = "https://api.openverse.org/v1"
UA = "OpenMontage/0.1 (https://github.com/Venloud/OpenMontage)"
ALLOWED_LICENSES = {"pdm", "cc0", "by"}
LICENSE_NAMES = {"pdm": "Public Domain Mark", "cc0": "CC0", "by": "CC BY"}
ENABLE_ENV = "OPENMONTAGE_OPENVERSE"


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def _cache_dir() -> Path:
    return Path(os.environ.get("OPENVERSE_CACHE_DIR", Path.home() / ".cache" / "openmontage" / "openverse"))


def search(media: str, params: dict[str, Any], cache_days: float = 7) -> list[dict]:
    """Search /images/ or /audio/ and return only allowed, non-mature results."""
    from tools import openverse_assets

    try:
        return openverse_assets.search(media, params.get("q", ""), filters=params)
    except RuntimeError as exc:
        raise RuntimeError(f"rate_limit: {exc}") from exc


def rights(hit: dict) -> dict:
    lic = str(hit.get("license") or "").lower()
    return {
        "openverse_id": hit.get("id"),
        "title": hit.get("title"),
        "creator": hit.get("creator"),
        "creator_url": hit.get("creator_url"),
        "source": hit.get("source"),
        "source_url": hit.get("foreign_landing_url") or hit.get("url"),
        "license": f"{LICENSE_NAMES[lic]} {hit.get('license_version') or ''}".strip(),
        "license_url": hit.get("license_url"),
        "attribution": hit.get("attribution"),
        "attribution_required": lic == "by",
        "commercial_use": True,
        "modification": True,
    }


def download(url: str, output: Path, expect: str) -> Path:
    """Download and reject anything whose Content-Type is not image/* or audio/*."""
    import requests

    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if not ctype.startswith(expect + "/") or not r.content:
        raise ValueError(f"download is not {expect} (Content-Type {ctype!r}, {len(r.content)} bytes)")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(r.content)
    return output
