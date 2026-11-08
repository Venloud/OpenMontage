# Optional Openverse provider

## Status and scope

Review-only integration, 2026-11-08. Existing providers remain in place; no schedule, publishing mode, credentials or production workflow was changed. Openverse supplies **images and audio, not video footage**. It is a source search engine, not an image generator or a licensing guarantee.

Enable only for a reviewed job/environment: `OPENMONTAGE_OPENVERSE=1 (tool availability)`. Do not enable it globally as part of installation. Existing rendering paths remain authoritative; MPT's optional still top-up is an explicit opt-in and runs after stock providers, including script-order requests. Kindled audio is library preparation, not automatic searching during renders.

Native integration points: `tools/graphics/openverse_image.py; tools/audio/openverse_audio.py`.

## Rights and provenance

The shared `openverse_assets.py` client searches over narration/scene terms, accepts only CC0, Public Domain Mark or CC BY metadata, checks HTTPS source/asset URLs and matching license versions, and checks the original source page for the corresponding Creative Commons license link. Restricted NC/ND/SA and mature/sensitive entries are rejected. Individual adapters can be stricter: MPT and Kindled images use CC0/PDM, Kindled audio uses CC0, and Tory/Night Files audio use CC0/PDM. Existing providers have their own policies.

A matching license link is **corroboration**, not proof the page owner owns every item or that no personality, privacy, trademark or other rights apply. Review relevance and the original source before production approval. Failure to confirm the license rejects that candidate. Never silently reinterpret missing metadata as public domain.

The local `.license.json` record includes creator, title, source URL, asset URL, license/version/URL, attribution, source-check time, source-page hash and media SHA-256. CC BY requires carrying attribution into the published credits/description; downloading a sidecar does not automatically publish those credits. Keep sidecars and source manifests with the production. No real video was published to test this.

## Cache, limits and fallback

Set `OPENVERSE_CACHE_DIR` to a Cody media directory, for example `C:\Users\van02\Desktop\cody\asset-library\openverse`. Do not put media in Home Beacon. Without the override, the verified client uses this repository's `output/openverse`.

Search cache TTL: seven days. URL-identified downloads are reused after hash and decoding checks. Corrupt search JSON shapes and asset sidecars are rejected/reacquired. Asset size limits: 40 MiB images, 80 MiB audio; MIME/ffprobe/FFmpeg checks reject non-media. Bounded acquisition tries up to five candidates through `first()`; individual tools can return a failure so their caller selects an existing provider.

Anonymous API was tested successfully; observed limits were 20 requests/minute and 200/day. No OAuth, API key or paid service is required for this integration. HTTP 429 records a shared cache-root cooldown using Retry-After; it does not sleep forever or repeatedly consume quota. Source servers can separately block, throttle or remove files. The cooldown is local to a cache directory, not an account-wide distributed quota manager. Concurrent jobs should share the intended cache and avoid bursts. Search freshness is bounded by cache TTL; arbitrary retries/backoff do not grant extra quota.

## Verification

24 focused tests pass. Existing contracts/tools: 250 passed, 1 failed, 6 skipped. The vbee TTS-provider catalogue expectation failure was reproduced in the original checkout before this resume.

Real public image + audio search/acquisition, original-page license-link corroboration, hash reuse and a 1.5-second FFmpeg MP4 encode/decode passed for this repository's verified client. Evidence: `C:\Users\van02\Desktop\cody\openverse-verification\OpenMontage\verification.json` and `openverse-smoke.mp4`. These live client checks are not a claim that every full end-to-end production render or editorial choice passed.

Offline regression tests mock HTTP and exercise adapter opt-in/failure behavior; no posting credentials or paid generation. The new offline-only CI workflow (where added) has no schedule or publishing permission. MPT's existing CI already discovers its new tests. Run `python tools/verify_openverse.py --output <Cody-directory>` explicitly for live acquisition; do not add live network tests to ordinary CI.

Disable by removing the opt-in variables or returning the existing provider setting to its prior value. Cached assets/records are retained; no cleanup or deletion is required. Review the existing draft PR before merging. No production integration is complete until owner approval, actual CI review and the appropriate project-level acceptance checks.

Official references: https://api.openverse.org/v1/ ; https://docs.openverse.org/ ; https://openverse.org/ .
