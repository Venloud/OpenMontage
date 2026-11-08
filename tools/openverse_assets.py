"""Optional Openverse acquisition. Images/audio only; no publishing side effects.

Kept identical in the four independent video repositories (no cross-repo import).
Search metadata is not rights clearance: download requires the original source page
to contain the matching Creative Commons license URL. Unverifiable assets fail closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

API = "https://api.openverse.org/v1"
UA = "VenloudVideo/1.0 (Openverse optional media provider)"
LICENSES = {"cc0": "publicdomain/zero/1.0", "pdm": "publicdomain/mark/1.0", "by": "licenses/by/"}


def cache_root() -> Path:
    return Path(os.getenv("OPENVERSE_CACHE_DIR", Path(__file__).resolve().parents[1] / "output/openverse"))


def _url(value: str) -> bool:
    p = urlsplit(value or "")
    return p.scheme == "https" and bool(p.hostname) and not p.username and not p.password


def rights(hit: dict, allowed=("cc0", "pdm", "by")) -> dict:
    lic = str(hit.get("license") or "").lower()
    url = str(hit.get("license_url") or "")
    p = urlsplit(url)
    if (lic not in allowed or lic not in LICENSES or p.hostname != "creativecommons.org"
            or LICENSES[lic] not in p.path or not _url(url)
            or not _url(hit.get("foreign_landing_url", "")) or not _url(hit.get("url", ""))
            or not hit.get("id") or hit.get("mature") or hit.get("sensitive")):
        raise ValueError("Openverse asset has unsupported or incomplete rights metadata")
    if lic == "by" and not hit.get("creator"):
        raise ValueError("CC BY asset has no creator")
    attribution = hit.get("attribution") or (
        f'{hit.get("title") or "Untitled"} by {hit.get("creator") or "Unknown"}, '
        f'{lic.upper()} {hit.get("license_version") or ""}, {url}, {hit["foreign_landing_url"]}')
    return {"provider": "openverse", "openverse_id": hit["id"], "title": hit.get("title"),
            "creator": hit.get("creator"), "source_url": hit["foreign_landing_url"],
            "asset_url": hit["url"], "license": lic, "license_url": url,
            "license_version": hit.get("license_version"), "attribution": attribution,
            "attribution_required": lic == "by", "commercial_use": True, "modification": True}


def search(media: str, query: str, allowed=("cc0", "pdm", "by")) -> list[dict]:
    if media not in ("images", "audio"):
        raise ValueError("Openverse supports images and audio, not video")
    params = {"q": query.strip()[:200], "license": ",".join(sorted(allowed)), "mature": "false", "page_size": 20}
    if not params["q"]:
        return []
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([media, params], sort_keys=True).encode()).hexdigest()
    cache = root / (key + ".json")
    data = None
    try:
        if time.time() - cache.stat().st_mtime < 604800:
            data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if data is None:
        cooldown = root / "rate-limit.json"
        try:
            if json.loads(cooldown.read_text())["until"] > time.time():
                raise RuntimeError("Openverse rate limit cooldown; use another provider")
        except (OSError, ValueError, KeyError):
            pass
        r = requests.get(f"{API}/{media}/", params=params, headers={"User-Agent": UA}, timeout=30)
        if r.status_code == 429:
            try:
                delay = max(60, float(r.headers.get("Retry-After", 60)))
            except ValueError:
                delay = 60
            cooldown.write_text(json.dumps({"until": time.time() + delay}))
            raise RuntimeError("Openverse rate limit; use cached or existing providers")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data.get("results"), list):
            raise ValueError("Openverse response has no results list")
        cache.write_text(json.dumps(data), encoding="utf-8")
    out, seen = [], set()
    for h in data.get("results", []):
        try:
            rights(h, allowed)
        except (ValueError, TypeError):
            continue
        if h["url"] not in seen:
            seen.add(h["url"])
            out.append(h)
    return out


def _decode(path: Path, media: str) -> None:
    # Probe the stream and actually decode, not just trust MIME or filename.
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                       check=True, capture_output=True, text=True, timeout=30)
    kind = "video" if media == "images" else "audio"
    if not any(s.get("codec_type") == kind for s in json.loads(r.stdout)["streams"]):
        raise ValueError("Downloaded asset has no expected media stream")
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", "0.1", "-f", "null", "-"],
                   check=True, capture_output=True, timeout=30)


def acquire(hit: dict, media: str, allowed=("cc0", "pdm", "by")) -> tuple[Path, dict]:
    if media not in ("images", "audio"):
        raise ValueError("Unsupported media type")
    record = rights(hit, allowed)
    root = cache_root() / "assets"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(hit["url"].encode()).hexdigest()
    path, side = root / key, root / (key + ".license.json")
    if path.exists() and side.exists():
        saved = json.loads(side.read_text(encoding="utf-8"))
        if (saved.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
                and saved.get("license_url") == record["license_url"] and saved.get("source_verified")):
            _decode(path, media)
            return path, saved
    page = requests.get(record["source_url"], headers={"User-Agent": UA}, timeout=30)
    page.raise_for_status()
    # Match the exact license, allowing HTTP links and a missing final slash.
    needle = record["license_url"].split("://", 1)[1].rstrip("/").lower()
    if needle not in page.text.lower().replace("\\/", "/"):
        raise ValueError("Original source does not confirm the indexed license; review required")
    r = requests.get(hit["url"], headers={"User-Agent": UA}, timeout=60, stream=True)
    r.raise_for_status()
    limit = (40 if media == "images" else 80) * 1024 * 1024
    chunks, size = [], 0
    try:
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > limit:
                raise ValueError("Openverse asset exceeds download size limit")
            chunks.append(chunk)
    finally:
        r.close()
    if not size:
        raise ValueError("Empty Openverse asset")
    temp = path.with_suffix(".part")
    try:
        temp.write_bytes(b"".join(chunks))
        _decode(temp, media)
        record.update(source_verified=True, source_checked_at=time.time(),
                      source_page_sha256=hashlib.sha256(page.content).hexdigest(),
                      sha256=hashlib.sha256(temp.read_bytes()).hexdigest())
        temp.replace(path)
        side.write_text(json.dumps(record, indent=2), encoding="utf-8")
    finally:
        temp.unlink(missing_ok=True)
    return path, record


def first(media: str, query: str, allowed=("cc0", "pdm", "by")) -> tuple[Path, dict] | None:
    """Bounded candidate fallback. Callers preserve their existing provider chain."""
    for hit in search(media, query, allowed)[:5]:
        try:
            return acquire(hit, media, allowed)
        except (requests.RequestException, ValueError, OSError, subprocess.SubprocessError):
            continue
    return None
