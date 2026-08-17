"""MCP front-door proxy — relay Streamable-HTTP MCP traffic to backends.

self.ai#25. Exposes the yard's MCP servers (and, first, a dead-simple `echo`
backend) behind self.ai's auth at one external origin,
``<your-host>/mcp/{server}``. The proxy is a thin reverse
proxy: it does NOT speak MCP itself — it forwards POST/GET/DELETE on
``/mcp/{server}`` verbatim to that server's ``/mcp`` endpoint and streams the
SSE response straight back, preserving the headers the MCP Streamable-HTTP
transport requires (``Accept`` carrying both ``application/json`` and
``text/event-stream``, ``Mcp-Session-Id``, ``Mcp-Protocol-Version``,
``Last-Event-ID``).

Auth is self.ai's: the route is gated by ``get_verified_user``, the same
dependency ``/api/chat/completions`` uses, so a caller may present a session
cookie, ``Authorization: Bearer <jwt>``, or ``Authorization: Bearer sk-...``
API key. Only authenticated users reach the MCPs from outside the cluster.

That inbound credential is for self.ai and **stops here** — it is never forwarded
to a backend. A caller that also needs to authenticate to the backend itself
(``glab-mcp`` wants the caller's own GitLab PAT; ``mcp-mailbox`` wants HTTP Basic
with an IPA user/password) sends that second credential as
``X-Mcp-Authorization``, which the proxy renames to ``Authorization`` on the way
out. See ``BACKEND_AUTH_HEADER`` and ``_FORWARD_ALLOWLIST``.

Forwarding is an allowlist, not a denylist, precisely because the first cut of
this proxy forwarded ``Authorization`` and ``Cookie`` straight through — handing
every registered backend a working self.ai credential.

The backend table is data-driven (a name → URL map), so wiring the real
crew-system servers in, and later dynamic registration, is additive — adding an
entry, not rewriting this router. Phase 1 seeds only ``echo``.

Relay uses aiohttp (already an api-tier dependency — no httpx) and streams
chunk-by-chunk; the POST response is an SSE stream that must not be buffered.
"""

import json
import logging
import os
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from selfai_ui.env import MCP_REGISTRARS, MCP_REGISTRY_ALLOWED_NAMESPACES
from selfai_ui.models.mcp_backends import (
    McpBackendForm,
    McpBackendModel,
    McpBackends,
    validate_name,
    validate_url,
)
from selfai_ui.utils.auth import get_verified_user

log = logging.getLogger(__name__)

router = APIRouter()

# Hop-by-hop headers (RFC 7230 §6.1) plus `host`/`content-length`, which aiohttp
# sets itself from the relayed body. These are never forwarded in either
# direction.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

#: Client → backend forwarding is an ALLOWLIST, deliberately.
#:
#: This started as "forward everything that is not hop-by-hop", which leaked the
#: caller's self.ai credential to every backend: `Authorization: Bearer sk-...`
#: is not hop-by-hop, so it went straight through, and so did `Cookie` carrying a
#: session JWT. A denylist in a credential-forwarding proxy is one forgotten
#: header away from that same bug — `Cookie` is exactly the header a fix aimed at
#: `Authorization` would miss.
#:
#: An allowlist is affordable here because MCP Streamable-HTTP's header surface is
#: small and closed: content negotiation, the session/protocol triple, and enough
#: diagnostics for a backend to log something useful. Anything a future backend
#: genuinely needs is a deliberate line in this set, reviewed as such.
#:
#: `authorization` is NOT here and must never be. The only thing that may become
#: an outbound `Authorization` is BACKEND_AUTH_HEADER below.
_FORWARD_ALLOWLIST = frozenset(
    {
        # content negotiation — `accept` MUST carry both application/json and
        # text/event-stream or a spec-compliant backend answers 406
        "accept",
        "accept-encoding",
        "accept-language",
        "content-type",
        # the MCP Streamable-HTTP transport triple
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
        # diagnostics
        "user-agent",
    }
)

#: How a caller hands a credential to the BACKEND rather than to self.ai.
#:
#: The two credentials in play are for different parties and the wire has only
#: one `Authorization` header, so the caller's backend credential travels under
#: its own name and the proxy renames it on the way out:
#:
#:     Authorization:       Bearer sk-...      -> consumed by self.ai, dropped here
#:     X-Mcp-Authorization: Bearer glpat-...   -> becomes Authorization for the backend
#:
#: Verbatim, including the scheme, so `Basic dXNlcjpwYXNz` works as readily as
#: `Bearer` — mcp-mailbox wants HTTP Basic (an IPA user/password), glab-mcp wants
#: a Bearer PAT, and one mechanism covers both.
#:
#: self.ai stores nothing. The client already holds its own credential, and
#: keeping it that way preserves the property those servers were built for: they
#: hold no token and act as whichever persona called. A stored per-user vault
#: would make self.ai a custodian of other systems' secrets, with the rotation,
#: revocation and blast-radius consequences that implies.
BACKEND_AUTH_HEADER = "x-mcp-authorization"


def _load_backends() -> dict:
    """The GitOps name → URL map: the reserved, immutable set.

    Seeded with the dead-simple echo backend; overridable as JSON via the
    ``MCP_PROXY_BACKENDS`` env. A bad override is logged and ignored — the seed
    keeps the proxy useful, rather than booting with zero backends on a typo.

    These names are **reserved**: a dynamic registration can never shadow,
    update or delete one (see ``resolve_backend``). An operator who writes a
    name here owns it, and no registrar can take it.
    """
    seed = {"echo": "http://mcp-echo.self-ai.svc:8084/mcp"}
    raw = os.environ.get("MCP_PROXY_BACKENDS")
    if not raw:
        return seed
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError("expected a JSON object of {server_name: url}")
        return parsed
    except Exception:
        log.exception("MCP_PROXY_BACKENDS is set but not valid JSON {name: url}; using the echo seed")
        return seed


#: The GitOps set, read once at import — it cannot change without a restart
#: anyway, because the env cannot.
_BACKENDS = _load_backends()


def resolve_backend(server: str) -> Optional[str]:
    """The URL for ``server``, or None.

    GitOps first, then the registry. The order IS the reservation: a registered
    row named ``echo`` can never be reached, so shadowing an operator-owned name
    is impossible at resolution time as well as at registration time. Two
    independent places enforce it because this one decides where credentials go.

    Looked up per request rather than cached at import, which is the entire
    point of the registry — a backend registered a moment ago serves on the next
    request, with no pod restart.
    """
    if server in _BACKENDS:
        return _BACKENDS[server]
    row = McpBackends.get_by_name(server)
    return row.url if row else None


def _forward_headers(src) -> dict:
    """Client → backend headers: the allowlist, plus the renamed backend credential.

    Two properties this function exists to guarantee:

    1. **The caller's self.ai credential never leaves self.ai.** ``authorization``
       and ``cookie`` are absent from ``_FORWARD_ALLOWLIST``, so neither can
       survive into the outbound request no matter what the caller sends.
    2. **An outbound ``Authorization`` can only come from BACKEND_AUTH_HEADER.**
       It is set last and from one source, so there is no path by which the
       inbound value reappears.

    Everything else — a smuggled ``X-Selfai-Ticket``, a self-asserted
    ``X-Crew-Captain``, an ``X-Forwarded-For`` — is simply not in the allowlist
    and is dropped without needing to be enumerated. That is the point of
    allowlisting: the identity headers of backends we have not wired yet cannot
    be spoofed through this proxy, because nothing unrecognised passes at all.
    """
    out = {k: v for k, v in src.items() if k.lower() in _FORWARD_ALLOWLIST}

    # Starlette's Headers is case-insensitive, so this matches any casing the
    # client used. Set after the comprehension: the allowlist cannot have
    # produced an `authorization` key, so this is the only writer.
    backend_auth = src.get(BACKEND_AUTH_HEADER)
    if backend_auth:
        out["Authorization"] = backend_auth

    return out


def _relay_response_headers(upstream: "aiohttp.ClientResponse") -> dict:
    """Backend → client headers: relay the ones a client/MCP needs.

    ``content-type`` is set via ``StreamingResponse.media_type`` (avoids a
    duplicate), so it is excluded here; ``mcp-session-id`` and ``cache-control``
    are passed through explicitly. Hop-by-hop is dropped.
    """
    out = {}
    for k, v in upstream.headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk == "content-type":
            continue
        if lk in {"mcp-session-id", "cache-control"}:
            out[k] = v
    return out


@router.api_route("/mcp/{server}", methods=["POST", "GET", "DELETE"])
async def proxy_mcp(request: Request, server: str, user=Depends(get_verified_user)):
    """Relay one MCP Streamable-HTTP request to the named backend.

    Auth first (``get_verified_user`` — API key or JWT), then a straight
    method/body/header pass-through to the backend's ``/mcp`` and a streamed
    response back. The backend owns the session (it mints ``Mcp-Session-Id`` on
    ``initialize``); the proxy just carries it both ways.
    """
    backend_url = resolve_backend(server)
    if not backend_url:
        raise HTTPException(status_code=404, detail=f"unknown MCP server: {server}")

    body = await request.body()
    fwd_headers = _forward_headers(request.headers)

    # Acquire the upstream response here (so its status + headers are known
    # before StreamingResponse is constructed), then drain it inside the
    # generator — which closes the response and the session when the client
    # finishes or disconnects. The `async with` lives INSIDE the generator so its
    # lifetime is the stream's lifetime, not the route's.
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
    try:
        upstream = await session.request(request.method, backend_url, data=body, headers=fwd_headers)
    except aiohttp.ClientError as e:
        await session.close()
        log.warning("MCP proxy: backend %s unreachable at %s: %s", server, backend_url, e)
        raise HTTPException(status_code=502, detail=f"MCP backend {server} unreachable")

    async def relay():
        try:
            async for chunk in upstream.content.iter_any():
                yield chunk
        finally:
            # Closing the session releases its connector and the connection the
            # upstream response holds — the documented cleanup when the response
            # was acquired outside an `async with` (aiohttp 3.11 ClientResponse).
            await session.close()

    return StreamingResponse(
        relay(),
        status_code=upstream.status,
        headers=_relay_response_headers(upstream),
        media_type=upstream.headers.get("Content-Type"),
    )


####################
# Registry — dynamic registration (self.ai#25)
####################
#
# Phase 1 registration was an env edit plus a pod restart, which gets clumsy
# around the third backend. These three endpoints are the durable half.
#
# The authority to register is deliberately narrow, because it is the authority
# to decide where other people's credentials are sent: only a user listed in
# MCP_REGISTRARS, only to a URL in an allowlisted namespace, never over a name an
# operator has claimed in GitOps, and never over another registrar's name.


def _require_registrar(user) -> None:
    """403 unless this user may register.

    Empty MCP_REGISTRARS means registration is closed, and closed is the
    default. A surface that can redirect other users' GitLab PATs should not
    open itself because nobody configured it.
    """
    if not MCP_REGISTRARS:
        raise HTTPException(
            status_code=403,
            detail="MCP registration is not enabled (MCP_REGISTRARS is unset)",
        )
    if user.id not in MCP_REGISTRARS and (user.email or "") not in MCP_REGISTRARS:
        raise HTTPException(status_code=403, detail="not permitted to register MCP servers")


@router.get("/api/v1/mcp/servers")
async def list_mcp_servers(user=Depends(get_verified_user)):
    """Every backend the front door serves, and where each came from.

    Readable by any verified user, not just registrars: these names are already
    discoverable by anyone who can call ``/mcp/{name}``, and a caller needs to
    know what exists to use it. No credential is stored or returned — the proxy
    holds none.
    """
    registered = McpBackends.get_all()
    return {
        "gitops": [
            {"name": name, "url": url, "source": "gitops", "reserved": True}
            for name, url in sorted(_BACKENDS.items())
        ],
        "registered": [
            {
                "name": row.name,
                "url": row.url,
                "source": "registered",
                "reserved": False,
                "owner_id": row.owner_id,
                "owner_name": row.owner_name,
                "updated_at": row.updated_at,
            }
            for row in sorted(registered, key=lambda r: r.name)
        ],
    }


@router.post("/api/v1/mcp/servers", response_model=McpBackendModel)
async def register_mcp_server(form: McpBackendForm, user=Depends(get_verified_user)):
    """Register a backend, or update one this user already owns.

    Refuses, in order: a caller who is not a registrar (403); a malformed name
    (400); a URL that is not an in-cluster Service in an allowlisted namespace
    (400); a name reserved by GitOps (409); a name owned by someone else (409).

    Serving is immediate — ``resolve_backend`` reads through to the table per
    request, so there is no restart and no cache to invalidate.
    """
    _require_registrar(user)

    if err := validate_name(form.name):
        raise HTTPException(status_code=400, detail=err)

    if err := validate_url(form.url, MCP_REGISTRY_ALLOWED_NAMESPACES):
        raise HTTPException(status_code=400, detail=err)

    if form.name in _BACKENDS:
        raise HTTPException(
            status_code=409,
            detail=f"'{form.name}' is reserved by a GitOps entry and cannot be registered",
        )

    existing = McpBackends.get_by_name(form.name)
    if existing and existing.owner_id != user.id:
        # Deliberately does not say who owns it: the caller has no business
        # knowing, and the name itself is the only thing they needed to learn.
        raise HTTPException(status_code=409, detail=f"'{form.name}' is registered by another user")

    return McpBackends.upsert(form, owner_id=user.id, owner_name=user.name)


@router.delete("/api/v1/mcp/servers/{name}")
async def delete_mcp_server(name: str, user=Depends(get_verified_user)):
    """Remove a backend this user registered.

    A GitOps name is not deletable here at all — it does not live in this table,
    and removing it means editing the manifest that declares it.
    """
    _require_registrar(user)

    if name in _BACKENDS:
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' is a GitOps entry; remove it from MCP_PROXY_BACKENDS instead",
        )

    existing = McpBackends.get_by_name(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"unknown MCP server: {name}")
    if existing.owner_id != user.id:
        raise HTTPException(status_code=409, detail=f"'{name}' is registered by another user")

    McpBackends.delete(name)
    return {"deleted": name}
