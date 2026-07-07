# Security Hardening Roadmap

This document tracks security improvements for the Self.AI project. The codebase is currently in **development/testing** — these items should be addressed before production deployment.

## Critical (Addressed in Initial Setup)

- [x] Remove `.env` files from git history
- [x] Create `.env.example` with placeholder values
- [x] Document that operators must set `WEBUI_SECRET_KEY` before production

## High Priority (Before Early Production)

- [ ] **CORS Configuration** — Restrict to specific origins instead of `*`
  - File: `selfai_ui/main.py:909-913`
  - Current: `allow_origins=["*"]`, `allow_credentials=True`
  - Fix: Set to actual domain(s) in env var

- [ ] **Unprotected Utility Endpoints** — Add auth checks
  - File: `selfai_ui/routers/utils.py`
  - Routes: `/gravatar`, `/code/format`, `/markdown`, `/pdf`
  - Add: `Depends(get_verified_user)` parameter

- [ ] **Default KB Access Control** — Make private by default
  - File: `selfai_ui/utils/access_control.py:85`
  - Current: `access_control=None` means public read
  - Fix: Change default to `{"write": {}, "read": {"user_ids": [owner]}}`

- [ ] **Security Headers** — Add middleware
  - File: `selfai_ui/main.py`
  - Add headers:
    - `X-Content-Type-Options: nosniff`
    - `X-Frame-Options: DENY`
    - `Strict-Transport-Security: max-age=31536000`
    - `Content-Security-Policy: default-src 'self'`

- [ ] **Error Handling** — Stop leaking exceptions to clients
  - File: Multiple in `selfai_ui/main.py` (lines 1179, 1210, 1245, etc.)
  - Change: `detail=str(e)` → `detail="Internal error"` (log full error server-side)

- [ ] **Session Cookies** — Enable secure flags in production
  - File: `selfai_ui/env.py:365-373`
  - Set in production:
    - `WEBUI_SESSION_COOKIE_SECURE=true`
    - `WEBUI_SESSION_COOKIE_SAME_SITE=strict`

## Medium Priority (Hardening Phase)

- [ ] **Rate Limiting** — Protect against brute force and DoS
  - Add package: `slowapi`
  - Endpoints to protect:
    - `/auth/signin` — max 5 attempts per 15 min
    - `/auth/signup` — max 3 per hour
    - `/api/completions`, `/api/chat/completions` — global rate limit
  - File: `selfai_ui/main.py`

- [ ] **Input Validation** — Path traversal protection
  - File: `selfai_ui/storage/provider.py:178-209` (`move_file()`)
  - Validate: `new_subdirectory` matches `^[a-zA-Z0-9_-]+$`
  - File: `selfai_ui/routers/knowledge.py:47-54`
  - Use: `pathlib.Path.resolve()` and ensure within `UPLOAD_DIR`

- [ ] **SQL Injection** — Validate tag IDs
  - File: `selfai_ui/models/chats.py:546-550`
  - Tag extraction: Add regex validation `^[a-zA-Z0-9_-]+$`
  - File: `selfai_ui/test/util/abstract_integration_test.py:160`
  - Replace raw SQL table drops with parameterized queries

- [ ] **File Upload Security** — Type validation & size limits
  - File: `selfai_ui/routers/files.py:42-99`
  - Add: MIME type whitelist (allow: pdf, txt, docx, json, jsonl)
  - Add: Global file size limit env var (default 100MB)
  - Add: Scan files with antivirus/malware detector if available

- [ ] **WebSocket Authentication** — Require auth on all handlers
  - File: `selfai_ui/socket/main.py:129, 248, 166-196`
  - Handlers: `usage()`, `user-list()`, `user-join()`
  - Add: Token validation on connect and periodic re-auth
  - Add: Proper channel authorization checks

- [ ] **Token Management** — Expiration and revocation
  - File: `selfai_ui/utils/auth.py:38-77` (eval tokens)
  - Add: TTL-based expiration (suggest 1 hour)
  - Add: Token revocation list/blacklist on logout
  - File: `selfai_ui/utils/auth.py:104-109` (decode errors)
  - Change: Silent `except Exception: return None` → log security events

- [ ] **Knowledge Base Write Access** — Fix permission check
  - File: `selfai_ui/routers/knowledge.py:234-253, 358-375`
  - Add: `and not has_access(user.id, "write", knowledge.access_control)` check
  - Allow: Users with explicit write permissions to modify shared KBs

- [ ] **S3 Path Validation** — Prevent arbitrary S3 object access
  - File: `selfai_ui/storage/provider.py:73-84`
  - Validate: S3 paths are within allowed bucket/prefix

- [ ] **SSRF Prevention** — Validate curator URLs
  - File: `selfai_ui/routers/curator.py:155-188`
  - Add: Private IP range filtering (reject 127.0.0.1/8, 10.0.0.0/8, etc.)
  - Add: URL scheme validation (https only in production)

- [ ] **Docker Security** — Remove unnecessary mounts & run as non-root
  - File: `docker-compose.yml`
  - Remove: Docker socket mounts from `selfUI` (line 117) and `traefik` (line 246) unless necessary
  - Dockerfile: Run containers as non-root user (UID > 1000)

- [ ] **Remove Debug Logging** — Set production log level
  - File: `self.UI/.env` (production copy)
  - Change: `GLOBAL_LOG_LEVEL=DEBUG` → `GLOBAL_LOG_LEVEL=INFO`
  - Remove: `DEBUG=1` from service definitions

- [ ] **Container Isolation** — CORS on internal services
  - File: `docker-compose.yml` (service definitions)
  - Kokoro TTS: Change `CORS_ORIGINS=["*"]` → specific allowed origins

## Low Priority / Upstream Issues

- [ ] **Tool/Function Execution Sandboxing**
  - File: `selfai_ui/utils/plugin.py:101, 145`
  - Current: `exec()` on arbitrary Python code (by design — Open WebUI feature)
  - Option A: Run tools in restricted Python environment (RestrictedPython)
  - Option B: Run in separate sandboxed container
  - Option C: Accept risk, add admin-only gating + audit logging

- [ ] **JWT Signing Algorithm**
  - File: `selfai_ui/utils/auth.py:125` (HS256 with shared secret)
  - Consider: Migrate to RS256 (asymmetric) for better token security

- [ ] **API Key System Hardening**
  - File: `selfai_ui/utils/auth.py:164`
  - Current: Prefix-based validation (`startswith("sk-")`)
  - Improve: Add rate limiting, rotation policy, expiration

---

## Implementation Notes

### Testing Environment (Current)

These issues are acceptable for a testing/development environment:
- Weak default credentials (operators should change before any external access)
- Debug logging enabled (easier troubleshooting)
- CORS `*` (limited to internal access)
- Running as root (isolated Docker environment)
- No rate limiting (single-user/controlled testing)

### Before Internal Network Exposure

Before exposing this application to an internal network:
1. Rotate all default credentials
2. Enable session cookie security flags
3. Add auth to utility endpoints
4. Implement rate limiting
5. Fix CORS to specific origins
6. Add security headers

### Before Production Deployment

Before any production use, address **all High and Medium priority items** above.

---

## Audit History

- **2026-04-06**: Initial security audit — identified 27 findings across 4 categories
  - CRITICAL: 5 items (1 addressed, 4 design decisions accepted for dev)
  - HIGH: 9 items (environment-specific, not addressed for dev)
  - MEDIUM: 13 items (captured as hardening roadmap)
  - Reference: See audit report in project docs

---

## Questions or Updates?

If you discover new security issues or have hardening suggestions, update this file with:
- Date discovered
- Affected file/component
- Severity (Critical/High/Medium/Low)
- Brief description and remediation
