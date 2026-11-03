"""Openly licensed audio (music, SFX) search and download from Openverse (no key, opt-in)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools import openverse_common as ov
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


class OpenverseAudio(BaseTool):
    name = "openverse_audio"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "music_search"
    provider = "openverse"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "No API key needed. Disabled by default: set OPENMONTAGE_OPENVERSE=1 to enable.\n"
        "  Anonymous limits: 20 requests/min, 200/day. https://api.openverse.org/v1/"
    )
    agent_skills = []

    capabilities = ["search_music", "search_sfx", "download_audio"]
    supports = {"free_commercial_use": True, "attribution_metadata": True, "license_filter": True}
    best_for = [
        "CC0 / CC BY sound effects and ambience",
        "Freesound and Free Music Archive tracks with recorded attribution",
    ]
    not_good_for = ["guaranteed broadcast-quality music", "offline use"]

    input_schema = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "Narration, scene or sound description"},
            "category": {"type": "string", "enum": ["music", "sound_effect", "podcast", "audiobook"]},
            "max_seconds": {"type": "number", "description": "Drop results longer than this"},
            "per_page": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20},
            "pick": {"type": "integer", "default": 0, "description": "Index of the result to download"},
            "exclude_ids": {"type": "array", "items": {"type": "string"}, "description": "Openverse ids already used"},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True)
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["query", "category", "pick"]
    side_effects = ["writes audio file to output_path", "calls Openverse API"]
    user_visible_verification = ["Listen before use", "Keep the attribution line when the license is CC BY"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if ov.enabled() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not ov.enabled():
            return ToolResult(success=False, error="Openverse is disabled. " + self.install_instructions)
        start = time.time()
        query = inputs["query"]
        params: dict[str, Any] = {"q": query[:200], "page_size": inputs.get("per_page", 10)}
        if inputs.get("category"):
            params["category"] = inputs["category"]
        try:
            hits = ov.search("audio", params)
            if not hits and "category" in params:  # most Openverse audio has no category set
                hits = ov.search("audio", {k: v for k, v in params.items() if k != "category"})
        except Exception as e:
            return ToolResult(success=False, error=f"Openverse audio search failed: {e}")
        if inputs.get("max_seconds"):
            hits = [h for h in hits if (h.get("duration") or 0) <= inputs["max_seconds"] * 1000]
        exclude = set(inputs.get("exclude_ids") or [])
        hits = [h for h in hits if h.get("id") not in exclude]
        if not hits:
            return ToolResult(success=False, error=f"No openly licensed audio found on Openverse for: {query}")
        hit = hits[min(int(inputs.get("pick", 0)), len(hits) - 1)]
        output = Path(inputs.get("output_path") or f"openverse_{hit['id']}.{(hit.get('filetype') or 'mp3').lower()}")
        candidates = [ov.rights(h) for h in hits]
        try:
            ov.download(hit["url"], output, "audio")
        except Exception as e:
            return ToolResult(success=False, error=f"Openverse download failed: {e}", data={"candidates": candidates})
        return ToolResult(
            success=True,
            data={"provider": "openverse", "query": query, "output": str(output), **ov.rights(hit),
                  "duration_seconds": (hit.get("duration") or 0) / 1000,
                  "results_returned": len(hits), "candidates": candidates},
            artifacts=[str(output)],
            cost_usd=0.0,
            duration_seconds=round(time.time() - start, 2),
        )
