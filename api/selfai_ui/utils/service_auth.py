"""
Mint short-lived, scoped service tickets (JWTs) for self.ai's internal
service-to-service calls — self.ai#25's proposal: self.ai is the yard's
internal ticket-granting service for its own backend mesh (Kerberos-shaped,
not Kerberos-literal).

First backend wired to validate these: self.llamolotl (self.llamolotl#12) —
see selfai_ui/routers/llamolotl.py and selfai_ui/routers/training.py for the
call sites. self.curator, self.code-eval, self.language-eval, and
self.transcribe are explicit follow-up scope once this pattern proves
out; they are NOT wired in this pass.

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
"""

import logging
import time
import uuid
from typing import Iterable, Union

import jwt

from selfai_ui.env import SERVICE_AUTH_ISSUER, SERVICE_AUTH_SECRET

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
