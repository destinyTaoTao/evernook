# Provider Identity Implementation Plan

**Goal:** Expose safe, bounded identities for the provider currently bound to the service, with a compatible dsh display.

**Architecture:** Extend the existing authorized management status with a separate identity map. Only recognized built-in backends with a valid, unchanged startup configuration yield an identity; return fixed provider/model labels and a digest, never configuration values or filesystem paths. The client treats absent or malformed data as unknown. This is a diagnostic, not a model admission or an egress grant.

**Tech Stack:** Python service/management, existing dsh client, unittest and Node component tests.

**Spec:** GitHub #207; provider boundaries and drift are defined in `docs/contracts/semantic-v1.md`, `extraction-v1.md`, and `reflection-providers-v1.md`.

## Constraints and review focus

- Preserve the existing status fields, authorization, no-cloud-fallback and daily-use gates.
- Never expose endpoint, path, environment variable, key, source or ordinary content in identity fields.
- No identity from missing, invalid, replaced, drifted or unrecognized providers; no client model selection.
- Test configured and absent providers, drift, insufficient scope, no secrets, old service and malformed client response.
- A fixed local model label or processor digest identifies configuration, not health, installed assets, external model revision or quality.

## Steps

1. Add failing synthetic service tests for safe identities and drift with real service and authorization.
2. Implement bounded recognized-backend identities in `src/evernook/service.py` and pass source tests.
3. Add and implement dsh rendering/fallback component tests in `plugins/dsh/test/network.test.mjs` and `plugins/dsh/client.js`.
4. Update contracts and version only as needed; run affected source/installed tests, type/build checks and diff review.
5. Commit the single ticket, push to main and update #207, #124 and #114 with exact validation limits.
