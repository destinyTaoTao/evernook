# Trusted TypeScript transcription SDK (#183)

- Add a trusted host-only TypeScript client that invokes the existing fixed transcription CLI operations. Pass material via stdin, require an absolute interpreter/config/access path and an independent service, never expose this as an MCP tool.
- Bound child lifetime and output, suppress stderr, sanitize errors and document uncertain submission outcomes after interruption. Preserve the service's source, authorization, encryption and privacy checks.
- Exercise real isolated service with synthetic ordinary and private transcription, idempotency, source withdrawal and no-vault rejection. Run TypeScript checks and installed-package smoke, then update the transcription contract and issue with measured limits.
