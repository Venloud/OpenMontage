# ECC validation, 2026-10-25 (America/New_York)

Base revision: `ad544e130c64d4ebf9545d25fa10567eb441c64c`. Integration branch: `integration/ecc-20261025`.

Shared source: ECC v2.2.3, `c05b2d6614f62f6db0047669aa4eefb223d478f9`, MIT. Canonical source and integrity lock live in the sibling private Vandam checkout at `integrations/ecc/`.

Installed project adapters: `ecc-remotion-video-creation`, `ecc-agent-harness-construction`, `ecc-security-review`.

**Observed:** real `.agents/skills` and `.claude/skills` links resolved to canonical instructions; source integrity and 3 task-routing/source-loading probes passed. Claude `/ecc-check` definition is discoverable as a file; native Claude execution on the owner computer remains unverified. Existing skills were inventoried and no same-harness name conflicts found.

**Project verification:** 226 passed, 6 skipped, 1 failed: test_registry_catalog_views expects provider set without vbee. Same isolated failure reproduced on original checkout. Integration is blocked for full regression readiness.

**Dependencies:** missing executable probes: none among explicitly declared executables. Node does not establish browser/Remotion readiness; runtime-specific tests above are the only claims. Python 3 is required by the shared helper.

**Unavailable/adapted skills:** missing upstream names are explicitly mapped in the canonical registry: agent-security-hardening → security-review, mcp-dependency-review → mcp-server-patterns, docs-governance → living-docs-governance where selected. Unified memory/learning use manual Vandam handoffs/proposals, not automatic recording. No unspecified agent-eval runtime is installed.

**Readiness:** reviewable isolated repository integration; keep draft pending independent review and target-environment acceptance. No automatic merge, deployment, publishing, trading or paid model calls. Application source, runtime configuration, workflows, secrets and approved assets are preserved.

Detailed evidence, commands, attempts and sanitized logs: `claudevandam/integrations/ecc/evidence/verification.json`, `discovery.json` and `logs/` on the same integration branch. Commit and PR identifiers are recorded in Vandam's dated integration handoff. Reproduce with the commands in the shared integration README.
