"""
Mint short-lived, scoped service tickets (JWTs) for self.ai's internal
service-to-service calls — self.ai#25's proposal: self.ai is the yard's
internal ticket-granting service for its own backend mesh (Kerberos-shaped,
not Kerberos-literal).

Backends wired to validate these, in rollout order (see
context/kits/cavekit-service-mesh-ticket-auth.md R3):
  1. self.llamolotl (self.llamolotl#12) — selfai_ui/routers/llamolotl.py
     and selfai_ui/routers/training.py.
  2. self.curator (self.curator#5 / self.ai#25) — selfai_ui/routers/curator.py
     and selfai_ui/utils/gpu_queue.py's curator dispatch/sync/finalize calls.
  3. self.speak (audience "self.speak") — selfai_ui/routers/audio.py's TTS
     synthesis forward; and self.transcribe (audience "self.transcribe") — the
     same file's STT transcription forward (self.ai#63). Both engines validate a
     single audio scope on their serving endpoint and fail-closed with 401 when
     the secret is unset, so their Deployments must also wire SERVICE_AUTH_SECRET.
self.code-eval and self.language-eval remain explicit follow-up scope once this
leg proves out; they are NOT wired in this pass.

Ticket shape: a signed HS256 JWT with `iss` (SERVICE_AUTH_ISSUER, "self.ai"),
`aud` (the target backend, e.g. "self.llamolotl"), `scope` (space-separated
capability list, JWT-conventional), `iat`/`exp` (minutes-lived — self.ai
mints one right before making the call, never a standing credential), and a
`jti` for traceability. The signing secret is shared out-of-band with each
backend via the selfai-service-auth ExternalSecret (see
manifests/external-secrets/service-auth.yaml) — not negotiated or exchanged
over the wire.

Scope taxonomy for the self.llamolotl audience (mirror of the validating
side in self.llamolotl's api/auth.py — keep both lists in sync):
  models:read, models:pull, models:delete, models:write,
  system:read, system:write, system:restart,
  jobs:read, jobs:create, jobs:write,
  pipeline:read, pipeline:write

Scope taxonomy for the self.curator audience (mirror of the validating side
in self.curator's api/auth.py — keep both lists in sync):
  jobs:read, jobs:create, jobs:write,
  data:read, data:write,
  stages:read, stages:write

Scope taxonomy for the audio audiences (mirror of the validating sides in
self.speak's and self.transcribe's api/auth.py — each is deliberately a single
scope on its one serving endpoint, not an unused hierarchy):
  self.speak:      audio:synthesize   (POST /v1/audio/speech)
  self.transcribe: audio:transcribe   (POST /v1/audio/transcriptions)

Inbound validation (self.ai as callee)
--------------------------------------
Everything above is self.ai *minting* tickets to call a backend. The GPU VRAM
lease broker (cavekit-gpu-lease-broker) is the first place self.ai is itself the
*callee* of a service-to-service call: a consumer (self.llamolotl) calls in to
register / heartbeat / request a lease. ``verify_service_ticket`` /
``require_service_ticket`` are the symmetric inverse of ``mint_service_ticket``
— they validate an inbound ``X-Selfai-Ticket`` against the SAME shared
``SERVICE_AUTH_SECRET`` (HS256), checking the ticket's ``aud`` equals self.ai's
own ``SERVICE_AUTH_AUDIENCE`` and that it carries the required scope. This is
the same shape every other mesh backend already runs against tickets self.ai
mints; it is NOT a new auth scheme, just the previously-absent inbound leg.

Scope taxonomy for self.ai's own inbound audience (VRAM lease broker):
  vram:report   self-report of held VRAM (POST /register, POST /heartbeat)
  vram:lease    request a VRAM allocation   (POST /request-lease)

Note the consumer side that MINTS these inbound tickets (self.llamolotl minting
aud="self.ai") is its own not-yet-written lease-client kit; until it lands this
inbound leg cannot be exercised end-to-end (only unit-tested against a
locally-minted ticket).
"""

import logging
import time
import uuid
from typing import Iterable, Optional, Union

import jwt
from fastapi import Header, HTTPException

from selfai_ui.env import (
    SERVICE_AUTH_AUDIENCE,
    SERVICE_AUTH_ISSUER,
    SERVICE_AUTH_SECRET,
)

log = logging.getLogger(__name__)

SERVICE_AUTH_ALGORITHM = "HS256"
TICKET_HEADER = "X-Selfai-Ticket"

# A few minutes is plenty: self.ai mints a ticket immediately before making
# the call it's needed for, so the window only has to cover request latency
# plus a small margin — not "however long the operation might run" (e.g. a
# model pull can take minutes, but that's the backend's job to track once
# accepted; the ticket only gates admission to the endpoint).
DEFAULT_TICKET_TTL_SECONDS = 120


class ServiceAuthNotConfigured(RuntimeError):
    """Raised at mint time (not import time) so self.ai can boot fine on
    deployments that don't yet call any ticket-gated backend."""


def mint_service_ticket(
    audience: str,
    scope: Union[str, Iterable[str]],
    ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
) -> str:
    """Mint a short-lived signed JWT scoped to `audience` + `scope`.

    `scope` may be a single scope string ("models:pull") or an iterable of
    scopes (["models:read", "models:pull"]); on the wire it's always a
    single space-separated string, per JWT convention.

    Raises ServiceAuthNotConfigured if SERVICE_AUTH_SECRET is unset — callers
    should let this propagate (it surfaces as a clear 5xx to the admin) rather
    than silently calling the backend unauthenticated.
    """
    if not SERVICE_AUTH_SECRET:
        raise ServiceAuthNotConfigured(
            "SERVICE_AUTH_SECRET is not configured — cannot mint a service "
            "ticket. Set it via the selfai-service-auth ExternalSecret (or "
            "the SERVICE_AUTH_SECRET env var for local dev)."
        )

    scope_str = scope if isinstance(scope, str) else " ".join(scope)

    now = int(time.time())
    payload = {
        "iss": SERVICE_AUTH_ISSUER,
        "aud": audience,
        "scope": scope_str,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, SERVICE_AUTH_SECRET, algorithm=SERVICE_AUTH_ALGORITHM)


def service_ticket_header(
    audience: str,
    scope: Union[str, Iterable[str]],
    ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
) -> dict:
    """Convenience wrapper: returns a one-entry headers dict ready to merge
    into an outgoing request's headers, e.g.:

        headers = {"Content-Type": "application/json", **service_ticket_header(...)}
    """
    return {TICKET_HEADER: mint_service_ticket(audience, scope, ttl_seconds)}


####################
# Inbound validation — self.ai as callee (symmetric inverse of the above)
####################


def verify_service_ticket(
    ticket: Optional[str],
    required_scopes: Union[str, Iterable[str]],
    audience: str = SERVICE_AUTH_AUDIENCE,
) -> dict:
    """Validate an inbound service ticket and return its decoded claims.

    The symmetric inverse of :func:`mint_service_ticket`: verifies the HS256
    signature with the shared ``SERVICE_AUTH_SECRET``, that ``aud`` matches
    self.ai's ``audience``, and that every scope in ``required_scopes`` is
    present in the ticket's space-separated ``scope`` claim.

    Fail-closed: raises :class:`fastapi.HTTPException` on any problem —
      * 503 if ``SERVICE_AUTH_SECRET`` is unset (cannot validate → never allow
        an unauthenticated caller through);
      * 401 if the ticket is missing, malformed, expired, or the audience is
        wrong (an invalid or absent credential);
      * 403 if the ticket is valid but lacks a required scope.

    ``iss`` is intentionally not pinned: unlike the outbound direction (where
    backends may confirm ``iss == "self.ai"``), several distinct consumers may
    legitimately call in, so the security boundary is the shared secret + the
    audience + the scope, not the caller's self-declared issuer.
    """
    if not SERVICE_AUTH_SECRET:
        # No shared secret configured — we cannot validate, so we must not admit
        # the caller. Fail closed (never fall back to "allow").
        raise HTTPException(
            status_code=503,
            detail="service ticket auth is not configured (SERVICE_AUTH_SECRET unset)",
        )
    if not ticket:
        raise HTTPException(status_code=401, detail="missing service ticket")

    try:
        claims = jwt.decode(
            ticket,
            SERVICE_AUTH_SECRET,
            algorithms=[SERVICE_AUTH_ALGORITHM],
            audience=audience,
        )
    except jwt.InvalidTokenError as e:
        # Bad signature / expired / wrong audience / malformed — all "invalid
        # credential", never a silent pass.
        log.warning(f"service ticket rejected: {e!r}")
        raise HTTPException(status_code=401, detail="invalid service ticket")

    required = {required_scopes} if isinstance(required_scopes, str) else set(required_scopes)
    granted = set(str(claims.get("scope", "")).split())
    if not required.issubset(granted):
        raise HTTPException(
            status_code=403,
            detail=f"service ticket missing required scope(s): {sorted(required - granted)}",
        )
    return claims


def require_service_ticket(*required_scopes: str):
    """FastAPI dependency factory gating an endpoint on a valid inbound service
    ticket carrying all of ``required_scopes``.

    Usage mirrors ``Depends(get_admin_user)`` for the admin surface::

        @router.post("/register")
        async def register(form: ..., _svc=Depends(require_service_ticket("vram:report"))):
            ...

    The returned dependency reads the ``X-Selfai-Ticket`` header and delegates
    to :func:`verify_service_ticket`; it returns the decoded claims (so a
    handler that wants the caller's scopes/jti can accept them)."""

    async def _dependency(
        ticket: Optional[str] = Header(None, alias=TICKET_HEADER),
    ) -> dict:
        return verify_service_ticket(ticket, required_scopes)

    # Marked so the route auth audit can recognise this as a real auth gate.
    #
    # The audit historically identified auth by the dependency's ``__name__``
    # against a fixed list of four user-auth functions. A closure produced by a
    # factory is named ``_dependency``, which that list will never match and
    # which says nothing to a reader either -- so a ticket-gated endpoint reads
    # to the audit exactly like an ungated one. That was invisible for as long
    # as the audit could not see ``include_router``-mounted routes at all
    # (#51); the moment it could, these endpoints would have been reported as
    # missing auth, which is a false critical on a correctly-gated route.
    #
    # An explicit marker rather than a name: the identity of an auth dependency
    # should be something it declares, not something inferred from what it
    # happens to be called.
    _dependency.__selfai_auth__ = "service_ticket"

    return _dependency
