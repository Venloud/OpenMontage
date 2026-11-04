"""Shared local asset library (Venloud/vandam-assets): look here first, call stock APIs only for what is missing.

Opt-in: AVAILABLE only when VANDAM_ASSET_LIBRARY is set and vandam_assets is importable.
- action "find": best rights-cleared match for a scene, copied to output_path, use recorded.
- action "save": after a video, add a downloaded asset with its rights record so the next video finds it.
Only verified, commercially reusable licences cross projects (CC0, PDM, CC BY, Pexels, Pixabay, Mixkit, Coverr);
unknown / NC / ND / SA assets are never returned.
"""

from __future__ import annotations

import os
import shutil
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

PROJECT = "openmontage"
RIGHTS = ("creator", "source_url", "license", "license_url", "attribution", "license_key", "attribution_required")


def _library():
    root = os.environ.get("VANDAM_ASSET_LIBRARY")
    if not root:
        return None
    try:
        from vandam_assets import Library
    except ImportError:
        return None
    return Library(root)


class LibraryAsset(BaseTool):
    name = "library_asset"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "image_generation"
    provider = "local_library"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = []
    install_instructions = (
        "Disabled by default. Set VANDAM_ASSET_LIBRARY to the library folder (e.g. "
        "C:\\Users\\van02\\Desktop\\cody\\asset-library) and install vandam_assets "
        "(pip install -e <vandam-assets checkout>)."
    )
    agent_skills = []

    capabilities = ["search_image", "search_video", "search_audio", "stock_image", "stock_video", "reuse_asset"]
    supports = {"free_commercial_use": True, "attribution_metadata": True, "license_filter": True, "offline": True}
    best_for = ["try FIRST, before Pexels / Pixabay / Openverse: assets earlier videos already downloaded"]
    not_good_for = ["subjects no earlier video used"]

    input_schema = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["find", "save"]},
            "query": {"type": "string", "description": "find: scene description"},
            "kind": {"type": "string", "enum": ["image", "video", "audio"], "default": "image"},
            "output_path": {"type": "string", "description": "find: where to copy the match"},
            "video_id": {"type": "string", "description": "the video this asset is used in"},
            "exclude_ids": {"type": "array", "items": {"type": "string"}},
            "file": {"type": "string", "description": "save: the downloaded asset"},
            "record": {"type": "object", "description": "save: provider, source_url, license, creator, license_url, "
                                                      "attribution, title, tags"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=50, network_required=False)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["action", "query", "kind", "file"]
    side_effects = ["copies a file to output_path", "appends to the library index"]
    user_visible_verification = ["Check the reused asset fits the scene", "Keep the attribution line when it is CC BY"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if _library() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        lib = _library()
        if not lib:
            return ToolResult(success=False, error="Asset library is off. " + self.install_instructions)
        start = time.time()
        kind = inputs.get("kind", "image")
        try:
            if inputs["action"] == "save":
                rec = lib.add(Path(inputs["file"]), {"kind": kind, **(inputs.get("record") or {})}, PROJECT,
                              inputs.get("video_id"))
                return ToolResult(success=True, data={"id": rec["id"], "rights_status": rec["rights_status"],
                                                      **{k: rec.get(k) for k in RIGHTS}},
                                  duration_seconds=round(time.time() - start, 2))
            query = inputs.get("query", "")
            hits = [h for h in lib.find(query, kind, PROJECT, exclude_ids=set(inputs.get("exclude_ids") or []),
                                       cooldown_videos=int(os.environ.get("VANDAM_ASSET_COOLDOWN", "10")))
                    if h.get("rights_status") == "verified"]
            if not hits:
                return ToolResult(success=False, error=f"No library {kind} for: {query} (use a stock provider)")
            hit = hits[0]
            out = Path(inputs.get("output_path") or f"library_{hit['id'][:12]}{Path(hit['path']).suffix}")
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hit["path"], out)
            if inputs.get("video_id"):
                lib.record_use(hit["id"], PROJECT, inputs["video_id"])
        except (ValueError, KeyError, OSError) as e:
            return ToolResult(success=False, error=f"Asset library {inputs.get('action')} failed: {e}")
        return ToolResult(
            success=True,
            data={"provider": "local_library", "library_id": hit["id"], "original_provider": hit.get("provider"),
                  "query": query, "output": str(out), "match": hit["match"], **{k: hit.get(k) for k in RIGHTS},
                  "candidates": [{"id": h["id"], "match": h["match"]} for h in hits]},
            artifacts=[str(out)],
            cost_usd=0.0,
            duration_seconds=round(time.time() - start, 2),
        )
