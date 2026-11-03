"""Openly licensed image search and download from Openverse (no key, opt-in)."""

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


class OpenverseImage(BaseTool):
    name = "openverse_image"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "image_generation"
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

    capabilities = ["search_image", "download_image", "stock_image"]
    supports = {"free_commercial_use": True, "attribution_metadata": True, "license_filter": True}
    best_for = [
        "historical prints, museum and archive images",
        "CC0 / public-domain stills",
        "B-roll stills with recorded attribution",
    ]
    not_good_for = ["video footage (Openverse has none)", "offline use"]

    input_schema = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "Narration or scene description"},
            "orientation": {"type": "string", "enum": ["tall", "wide", "square"]},
            "per_page": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20},
            "pick": {"type": "integer", "default": 0, "description": "Index of the result to download"},
            "exclude_ids": {"type": "array", "items": {"type": "string"}, "description": "Openverse ids already used"},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True)
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["query", "orientation", "pick"]
    side_effects = ["writes image file to output_path", "calls Openverse API"]
    user_visible_verification = ["Check the image fits the scene", "Keep the attribution line when the license is CC BY"]

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
        if inputs.get("orientation"):
            params["aspect_ratio"] = inputs["orientation"]
        try:
            hits = ov.search("images", params)
        except Exception as e:
            return ToolResult(success=False, error=f"Openverse image search failed: {e}")
        exclude = set(inputs.get("exclude_ids") or [])
        hits = [h for h in hits if h.get("id") not in exclude]
        if not hits:
            return ToolResult(success=False, error=f"No openly licensed image found on Openverse for: {query}")
        hit = hits[min(int(inputs.get("pick", 0)), len(hits) - 1)]
        output = Path(inputs.get("output_path") or f"openverse_{hit['id']}.{(hit.get('filetype') or 'jpg').lower()}")
        candidates = [ov.rights(h) for h in hits]
        try:
            ov.download(hit["url"], output, "image")
        except Exception as e:
            return ToolResult(success=False, error=f"Openverse download failed: {e}", data={"candidates": candidates})
        return ToolResult(
            success=True,
            data={"provider": "openverse", "query": query, "output": str(output), **ov.rights(hit),
                  "width": hit.get("width"), "height": hit.get("height"),
                  "results_returned": len(hits), "candidates": candidates},
            artifacts=[str(output)],
            cost_usd=0.0,
            duration_seconds=round(time.time() - start, 2),
        )
