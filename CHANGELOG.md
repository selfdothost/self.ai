# Changelog

All notable changes to self.ai are documented here. This is a curated, human-readable summary of what shipped each week — not a raw commit or merge log — organized by Security, Added, Removed, and Fixed. self.ai publishes weekly on Tuesdays.

## self.ai — 2026-07-21

### Security
- Fixed a stored XSS vulnerability in file preview/inline-content serving — an uploaded file with a spoofed content-type could execute script when previewed. Hardened with strict server-side content-type detection (never trusts client-supplied metadata) plus CSP sandboxing on all file-content routes. (critical, self.ai#55)
- Outbound calls from the curator and eval workers (self.code-eval / self.language-eval) now authenticate with narrowly-scoped service tickets instead of broader shared credentials, reducing blast radius if a worker is ever compromised.

### Added
- New unauthenticated `/api/models/public` endpoint for read-only model-catalog access.
- Periodic model-integrity self-check — the platform now verifies loaded models on a schedule instead of only at load time.
- New local model presets available: Gemma 4, GLM-4.5-Air, Qwen3-Coder-Next.

### Removed
- Nothing removed this week.

### Fixed
- Reranker was returning near-zero garbage scores due to a missing classifier head.
- Model-list cache could be silently defeated, serving stale results.
- Cancelling a model pull could fail due to route-registration order.
- Knowledge-base uploads/deletes weren't always committing their version-control branch, risking inconsistent state.
- Async cleanup path for connection pooling used a blocking client, risking stalls under load.
- Dispatching an eval job to a different model without unloading the prior one could exhaust GPU memory.
- General GPU/CPU offload tuning across local models for stability under load, plus a memory-limit fix to prevent out-of-memory crashes.
