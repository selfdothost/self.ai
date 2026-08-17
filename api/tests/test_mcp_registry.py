"""Dynamic MCP backend registration (self.ai#25).

Registration decides where `/mcp/{name}` points, and the proxy forwards the
caller's own backend credential there. So the authority to register is the
authority to redirect other people's GitLab PATs, and most of what follows is
about who may do it, where they may point, and which names they may not take.

The shape of the threat is not hypothetical: self.ai#79 found the VRAM broker's
`/register` accepted any mesh ticket under any consumer_id, because a service
ticket proves possession of a shared secret rather than identity. These tests
pin the equivalent holes shut here.
"""

import pytest

from selfai_ui.models.mcp_backends import McpBackends, validate_name, validate_url
from selfai_ui.routers import mcp_proxy

GLAB_URL = "http://glab-mcp.crew-system.svc:8081/mcp"
ALLOWED = {"self-ai", "crew-system"}


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test starts with an empty table."""
    for row in McpBackends.get_all():
        McpBackends.delete(row.name)
    yield
    for row in McpBackends.get_all():
        McpBackends.delete(row.name)


@pytest.fixture
def registrar(monkeypatch, test_user):
    """Make the test user a permitted registrar."""
    monkeypatch.setattr(mcp_proxy, "MCP_REGISTRARS", {test_user["id"]})
    return test_user


# --- who may register ---------------------------------------------------------


@pytest.mark.tier0
def test_registration_is_closed_by_default(authenticated_user, monkeypatch):
    """Empty MCP_REGISTRARS means closed, not open. A surface that can redirect
    other users' credentials must not open itself because nobody configured it."""
    monkeypatch.setattr(mcp_proxy, "MCP_REGISTRARS", set())
    resp = authenticated_user.post("/api/v1/mcp/servers", json={"name": "glab", "url": GLAB_URL})
    assert resp.status_code == 403
    assert McpBackends.get_by_name("glab") is None


@pytest.mark.tier0
def test_a_non_registrar_is_refused(authenticated_user, monkeypatch):
    """Being a verified self.ai user is not enough — the allowlist is the gate."""
    monkeypatch.setattr(mcp_proxy, "MCP_REGISTRARS", {"some-other-user-id"})
    resp = authenticated_user.post("/api/v1/mcp/servers", json={"name": "glab", "url": GLAB_URL})
    assert resp.status_code == 403


@pytest.mark.tier0
def test_unauthenticated_registration_is_rejected(client):
    resp = client.post("/api/v1/mcp/servers", json={"name": "glab", "url": GLAB_URL})
    assert resp.status_code == 403


@pytest.mark.tier0
def test_a_registrar_can_register(authenticated_user, registrar):
    resp = authenticated_user.post("/api/v1/mcp/servers", json={"name": "glab", "url": GLAB_URL})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "glab"
    assert body["url"] == GLAB_URL
    assert body["owner_id"] == registrar["id"]


# --- where a backend may point ------------------------------------------------
#
# The load-bearing control: it holds even if a registrar credential leaks.


@pytest.mark.tier0
@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example.com/mcp",  # off-cluster host
        "https://glab-mcp.crew-system.svc:8081/mcp",  # https would smuggle a hostname past the check
        "http://glab-mcp.kube-system.svc:8081/mcp",  # real namespace, not allowlisted
        "http://10.43.0.5:8081/mcp",  # IP literal carries no namespace to check
        "http://glab-mcp:8081/mcp",  # bare service name, no namespace
        "http://glab-mcp.crew-system.svc:8081",  # no MCP path
    ],
)
def test_urls_outside_the_allowlist_are_refused(authenticated_user, registrar, url):
    resp = authenticated_user.post("/api/v1/mcp/servers", json={"name": "x", "url": url})
    assert resp.status_code == 400, f"{url} should not be registrable"
    assert McpBackends.get_by_name("x") is None


@pytest.mark.tier0
def test_the_cluster_local_suffix_is_accepted():
    """Kubernetes' fully-qualified form is the same address, so it must pass."""
    assert validate_url("http://glab-mcp.crew-system.svc.cluster.local:8081/mcp", ALLOWED) is None


@pytest.mark.tier0
def test_name_validation():
    assert validate_name("glab") is None
    assert validate_name("glab-mcp_2") is None
    assert validate_name("") is not None
    assert validate_name("Glab") is not None  # uppercase
    assert validate_name("-glab") is not None  # leading dash
    assert validate_name("glab/../etc") is not None  # path traversal in a path segment
    assert validate_name("a" * 64) is not None


# --- which names may be taken -------------------------------------------------


@pytest.mark.tier0
def test_a_gitops_name_cannot_be_registered(authenticated_user, registrar):
    """`echo` is declared in MCP_PROXY_BACKENDS. A registration must not be able
    to capture it — that would redirect an operator-owned backend."""
    resp = authenticated_user.post(
        "/api/v1/mcp/servers", json={"name": "echo", "url": GLAB_URL}
    )
    assert resp.status_code == 409
    assert "reserved" in resp.json()["detail"].lower()


@pytest.mark.tier0
def test_a_gitops_name_cannot_be_deleted(authenticated_user, registrar):
    resp = authenticated_user.delete("/api/v1/mcp/servers/echo")
    assert resp.status_code == 409


@pytest.mark.tier0
def test_gitops_wins_at_resolution_even_if_a_row_exists(registrar):
    """Belt and braces. Registration refuses a reserved name, but resolution
    checks GitOps first too — so a row inserted by any other route (a migration,
    a direct write) still cannot redirect `echo`."""
    McpBackends.upsert(
        type("F", (), {"name": "echo", "url": GLAB_URL})(), owner_id="x", owner_name="x"
    )
    assert mcp_proxy.resolve_backend("echo") == mcp_proxy._BACKENDS["echo"]
    assert mcp_proxy.resolve_backend("echo") != GLAB_URL


@pytest.mark.tier0
def test_another_users_backend_cannot_be_taken_or_deleted(
    authenticated_user, registrar, monkeypatch
):
    """The #79 hole: registering under a name someone else owns. The owner check
    is on a real user id, not a shared-secret ticket."""
    McpBackends.upsert(
        type("F", (), {"name": "theirs", "url": GLAB_URL})(),
        owner_id="someone-else",
        owner_name="Someone Else",
    )

    resp = authenticated_user.post(
        "/api/v1/mcp/servers",
        json={"name": "theirs", "url": "http://mcp-echo.self-ai.svc:8084/mcp"},
    )
    assert resp.status_code == 409
    # ...and the original is untouched.
    assert McpBackends.get_by_name("theirs").url == GLAB_URL

    resp = authenticated_user.delete("/api/v1/mcp/servers/theirs")
    assert resp.status_code == 409
    assert McpBackends.get_by_name("theirs") is not None


@pytest.mark.tier0
def test_an_owner_can_update_and_delete_their_own(authenticated_user, registrar):
    authenticated_user.post("/api/v1/mcp/servers", json={"name": "mine", "url": GLAB_URL})

    moved = "http://mcp-echo.self-ai.svc:8084/mcp"
    resp = authenticated_user.post("/api/v1/mcp/servers", json={"name": "mine", "url": moved})
    assert resp.status_code == 200
    assert McpBackends.get_by_name("mine").url == moved

    resp = authenticated_user.delete("/api/v1/mcp/servers/mine")
    assert resp.status_code == 200
    assert McpBackends.get_by_name("mine") is None


# --- it actually serves -------------------------------------------------------


@pytest.mark.tier0
def test_a_registered_backend_resolves_without_a_restart(authenticated_user, registrar):
    """The whole point of the registry. `_BACKENDS` is import-time and cannot
    change; resolution reads through to the table per request."""
    assert mcp_proxy.resolve_backend("glab") is None
    authenticated_user.post("/api/v1/mcp/servers", json={"name": "glab", "url": GLAB_URL})
    assert mcp_proxy.resolve_backend("glab") == GLAB_URL


@pytest.mark.tier0
def test_listing_separates_gitops_from_registered(authenticated_user, registrar):
    authenticated_user.post("/api/v1/mcp/servers", json={"name": "glab", "url": GLAB_URL})
    body = authenticated_user.get("/api/v1/mcp/servers").json()

    gitops = {e["name"] for e in body["gitops"]}
    registered = {e["name"] for e in body["registered"]}
    assert "echo" in gitops
    assert "glab" in registered
    assert all(e["reserved"] for e in body["gitops"])
    assert not any(e["reserved"] for e in body["registered"])


@pytest.mark.tier0
def test_listing_is_readable_by_any_verified_user(authenticated_user, monkeypatch):
    """Not registrar-gated: these names are already discoverable by anyone who
    can call /mcp/{name}, and no credential is stored to leak."""
    monkeypatch.setattr(mcp_proxy, "MCP_REGISTRARS", set())
    assert authenticated_user.get("/api/v1/mcp/servers").status_code == 200
