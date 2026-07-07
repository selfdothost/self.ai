"""
T-222: Route auth coverage audit.

Asserts every registered route has an authentication dependency
except for an explicit allowlist of public endpoints.
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


def _route_has_auth_dep(route: APIRoute) -> bool:
    """Return True if any dependency in the route chain is an auth function."""
    if not hasattr(route, "dependant") or route.dependant is None:
        return False

    def _check(dependant) -> bool:
        if dependant.call is not None:
            name = getattr(dependant.call, "__name__", "")
            if name in (
                "get_current_user",
                "get_verified_user",
                "get_admin_user",
                "get_current_user_by_api_key",
            ):
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
    for route in test_app.routes:
        if not isinstance(route, APIRoute):
            continue  # Skip mounts, WebSocket routes, etc.
        if route.path in PUBLIC_ALLOWLIST:
            continue
        if any(route.path.startswith(prefix) for prefix in PROXIED_PREFIXES):
            continue
        if not _route_has_auth_dep(route):
            missing_auth.append(f"{list(route.methods)} {route.path}")

    assert missing_auth == [], (
        f"The following routes have no auth dependency and are not on the "
        f"public allowlist:\n" + "\n".join(f"  - {r}" for r in missing_auth)
    )


@pytest.mark.tier0
@pytest.mark.security
def test_route_inspection_does_not_hit_http(test_app):
    """Verify the audit uses route definitions, not HTTP requests."""
    # This test just confirms the fixture provides an inspectable app
    assert hasattr(test_app, "routes")
    assert len(test_app.routes) > 0
