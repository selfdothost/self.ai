"""
T-222: Route auth coverage audit.

Asserts every registered route has an authentication dependency
except for an explicit allowlist of public endpoints.

Two defects made this pass while auditing almost nothing (self.ai#51):

1. **It only saw routes decorated directly on `app`.** On the pinned
   `fastapi==0.139.0`, `app.include_router(...)` registers a lazy
   `_IncludedRouter` wrapper rather than flattening its routes onto the
   application -- the mount prefix lives on `include_context.prefix` and the
   real routes on `original_router.routes`. `isinstance(route, APIRoute)` is
   False for every one of those, so the old flat loop skipped them all: 21
   routes audited out of 478 that exist. Essentially the whole authenticated
   API, plus every route a mod contributes (the loader mounts mod routers the
   same way), went unchecked. `_iter_api_routes` recurses instead.

2. **It recognised auth by function name, from a fixed list of four.** That
   list holds the user-auth dependencies and nothing else, so the service-
   ticket gate (`require_service_ticket`) -- whose closure is named
   `_dependency` -- looked like no gate at all. Fixing (1) alone would have
   reported three correctly-gated `/api/vram-leases/*` endpoints as
   unauthenticated: a false critical on the fixed audit's first run, which is
   how a correct change gets reverted. Auth dependencies now carry an explicit
   `__selfai_auth__` marker and this checks for it.
"""

import pytest
from fastapi.routing import APIRoute

# Endpoints that are intentionally public
PUBLIC_ALLOWLIST = {
    "/health",
    "/health/db",
    "/api/version",
    "/api/version/updates",
    "/api/changelog",
    "/api/config",
    # Free-tier model listing (self.ai#6) — deliberately public, response
    # is stripped to a minimal safe subset (see /api/models/public).
    "/api/models/public",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/manifest.json",
    "/opensearch.xml",
    # OAuth endpoints (callback routes) — public by design for OAuth flows
    "/oauth/{provider}/login",
    "/oauth/{provider}/callback",
    # Auth endpoints that must be public for signin/signup
    "/api/v1/auths/signin",
    "/api/v1/auths/signup",
    "/api/v1/auths/ldap",
    # Static assets and root
    "/",
    "/watch",
    # Signout is public by design (clears the session)
    "/api/v1/auths/signout",
    # Gravatar proxy — email is a weak secret, endpoint just proxies to
    # gravatar.com. Public access is acceptable.
    "/api/v1/utils/gravatar",
    # Provider-reachability stub returning {"status": True} and nothing else.
    # The sibling provider routers (/openai, /ollama, …) are exempted wholesale
    # via PROXIED_PREFIXES; /anthropic is deliberately NOT, because the rest of
    # that router is admin-gated and should stay audited. Allowlisting the one
    # public path keeps the other seven checked.
    "/anthropic/",
    # A mod's built frontend assets. Unauthenticated by design: the client loads
    # a mod bundle with dynamic import(), which cannot carry an Authorization
    # header. What it may serve is restricted instead — mods/assets.py refuses
    # anything that is not a web-asset file type, so a mod's source and config
    # are not reachable through it (#70).
    "/static/mods/{mod_id}/{asset_path:path}",
}

# Prefixes that are externally proxied services — auth handled differently
PROXIED_PREFIXES = (
    "/ollama",
    "/openai",
    "/llamolotl",
    "/curator",
    "/language-eval",
    "/code-eval",
    "/ws",
)


#: User-auth dependencies, identified by name. Kept as names because these are
#: plain module-level functions whose identity IS their name.
USER_AUTH_DEPENDENCIES = (
    "get_current_user",
    "get_verified_user",
    "get_admin_user",
    "get_current_user_by_api_key",
)


def _iter_api_routes(routes, prefix: str = ""):
    """Yield `(full_path, APIRoute)` for every real route, however mounted.

    A route reached through `include_router` is not an `APIRoute` on `app`; it
    is an `_IncludedRouter` holding the sub-router plus the prefix it was
    mounted at. Recursing through `original_router.routes` and accumulating
    `include_context.prefix` is what turns 21 visible routes into the 478 that
    actually serve. Nesting is handled by the recursion, so a router included
    into a router included into the app is reached too.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        sub_router = getattr(route, "original_router", None)
        if sub_router is None:
            continue  # a Mount, a WebSocket route, a bare Starlette Route
        context = getattr(route, "include_context", None)
        yield from _iter_api_routes(sub_router.routes, prefix + (getattr(context, "prefix", "") or ""))


def _route_has_auth_dep(route: APIRoute) -> bool:
    """Return True if any dependency in the route chain is an auth gate.

    Two ways to qualify, and the marker is the one that scales: a dependency
    built by a factory (`require_service_ticket`) has a closure name that says
    nothing about what it does, so it declares itself via `__selfai_auth__`
    instead of being recognised by accident.
    """
    if not hasattr(route, "dependant") or route.dependant is None:
        return False

    def _check(dependant) -> bool:
        call = dependant.call
        if call is not None:
            if getattr(call, "__selfai_auth__", None):
                return True
            if getattr(call, "__name__", "") in USER_AUTH_DEPENDENCIES:
                return True
        for sub in dependant.dependencies:
            if _check(sub):
                return True
        return False

    return _check(route.dependant)


@pytest.mark.tier0
@pytest.mark.security
def test_all_routes_have_auth_or_allowlisted(test_app):
    """Every route must have an auth dependency or be on the public allowlist."""
    missing_auth = []
    for path, route in _iter_api_routes(test_app.routes):
        if path in PUBLIC_ALLOWLIST:
            continue
        if any(path.startswith(prefix) for prefix in PROXIED_PREFIXES):
            continue
        if not _route_has_auth_dep(route):
            missing_auth.append(f"{sorted(route.methods)} {path}")

    assert (
        missing_auth == []
    ), "The following routes have no auth dependency and are not on the " "public allowlist:\n" + "\n".join(
        f"  - {r}" for r in missing_auth
    )


@pytest.mark.tier0
@pytest.mark.security
def test_route_inspection_does_not_hit_http(test_app):
    """Verify the audit uses route definitions, not HTTP requests."""
    # This test just confirms the fixture provides an inspectable app
    assert hasattr(test_app, "routes")
    assert len(test_app.routes) > 0


@pytest.mark.tier0
@pytest.mark.security
def test_the_audit_actually_reaches_included_routers(test_app):
    """The audit must inspect the mounted API, not just app-level decorators.

    This is the regression guard for self.ai#51. The old flat filter saw 21
    routes; the real surface is an order of magnitude larger, and everything it
    missed was the authenticated API. A bare count is deliberately not asserted
    (it moves every time a route is added) -- what is asserted is that recursion
    finds substantially more than the flat view, and that specific
    known-mounted paths are among them.
    """
    flat = [r for r in test_app.routes if isinstance(r, APIRoute)]
    recursed = list(_iter_api_routes(test_app.routes))

    assert len(recursed) > len(flat) * 5, (
        f"recursion found {len(recursed)} routes vs {len(flat)} flat -- if these are close, "
        f"_iter_api_routes has stopped descending into included routers and this audit is "
        f"back to inspecting almost nothing (self.ai#51)"
    )

    paths = {path for path, _ in recursed}
    for mounted in ("/api/v1/chats/", "/api/v1/models/", "/api/v1/auths/signin"):
        assert mounted in paths, f"{mounted} is include_router-mounted and must be audited"


@pytest.mark.tier0
@pytest.mark.security
def test_service_ticket_dependencies_count_as_auth(test_app):
    """A ticket-gated route is authenticated, and the audit must know it.

    Without this, fixing the recursion reports the three /api/vram-leases/*
    consumer endpoints as unauthenticated -- a false critical on correctly
    gated routes. Asserted against the real routes rather than a synthetic
    dependency, so it stays true only while those endpoints really are gated.
    """
    ticket_gated = [
        path
        for path, route in _iter_api_routes(test_app.routes)
        if path.startswith("/api/vram-leases/") and path.endswith(("register", "heartbeat", "request-lease"))
    ]
    assert len(ticket_gated) == 3, f"expected the three vram-lease consumer endpoints, found {ticket_gated}"

    for path, route in _iter_api_routes(test_app.routes):
        if path in ticket_gated:
            assert _route_has_auth_dep(route), (
                f"{path} is gated by require_service_ticket but the audit does not "
                f"recognise it as authenticated"
            )


@pytest.mark.tier0
@pytest.mark.security
def test_the_auth_check_still_rejects_a_genuinely_open_route():
    """A checker that cannot fail proves nothing.

    Guards the marker specifically: an arbitrary attribute must not satisfy it,
    or `__selfai_auth__` would degrade into "any dependency counts".
    """

    class _Dependant:
        def __init__(self, call, dependencies=()):
            self.call = call
            self.dependencies = list(dependencies)

    class _Route:
        def __init__(self, dependant):
            self.dependant = dependant

    def unguarded():
        pass

    assert _route_has_auth_dep(_Route(_Dependant(unguarded))) is False

    def ticketed():
        pass

    ticketed.__selfai_auth__ = "service_ticket"
    assert _route_has_auth_dep(_Route(_Dependant(unguarded, [_Dependant(ticketed)]))) is True

    def decoy():
        pass

    decoy.__selfai_auth__ = ""  # falsy marker must not qualify
    assert _route_has_auth_dep(_Route(_Dependant(decoy))) is False
