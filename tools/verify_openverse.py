"""Explicit local live smoke test. No credentials, generation, publishing or scheduling.

Run with --output pointing to a Cody media directory. Uses the repo's real client.
"""
import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-query", default="Marcus Aurelius")
    parser.add_argument("--audio-query", default="wind howling")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = next(p for p in [root / "pipeline/openverse_assets.py", root / "kindled_iron/openverse_assets.py",
                            root / "tools/openverse_assets.py"] if p.exists())
    spec = importlib.util.spec_from_file_location("openverse_live_client", path)
    ov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ov)
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["OPENVERSE_CACHE_DIR"] = str(args.output / "cache")
    report, assets = {}, {}
    for media, query in [("images", args.image_query), ("audio", args.audio_query)]:
        row = {"query": query, "verified": False, "rejections": []}
        try:
            hits = ov.search(media, query, ("cc0", "pdm"))
            row["results"] = len(hits)
            for h in hits[:5]:
                try:
                    asset, record = ov.acquire(h, media, ("cc0", "pdm"))
                    again, _ = ov.acquire(h, media, ("cc0", "pdm"))
                    assert asset == again
                    assets[media] = asset
                    row.update(verified=True, path=str(asset), rights=record, cached_reuse=True)
                    break
                except Exception as exc:
                    row["rejections"].append({"id": h["id"], "reason": str(exc)[:240]})
        except Exception as exc:
            row["error"] = str(exc)[:240]
        report[media] = row
        print(media, "PASS" if row["verified"] else "FAIL", flush=True)
    if len(assets) == 2:
        out = args.output / "openverse-smoke.mp4"
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(assets["images"]),
                            "-i", str(assets["audio"]), "-t", "1.5", "-vf",
                            "scale=320:240:force_original_aspect_ratio=decrease,pad=320:240:(ow-iw)/2:(oh-ih)/2",
                            "-r", "12", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", str(out)], check=True, timeout=60, capture_output=True)
            ov._decode(out, "images")
            ov._decode(out, "audio")
            report["render"] = {"verified": True, "path": str(out)}
        except Exception as exc:
            report["render"] = {"verified": False, "error": str(exc)[:240]}
    (args.output / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if all(report.get(k, {}).get("verified") for k in ("images", "audio", "render")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
