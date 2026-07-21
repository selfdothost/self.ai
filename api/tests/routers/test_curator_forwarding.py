"""
T-R03 (part 3): Curator proxy forwarding.

Curator router uses aiohttp for verify + job submission upstream calls.

self.ai#25 / self.curator#5: every proxied call below /curator (other than
/verify's own /health probe, deliberately unticketed to match self.llamolotl's
pattern) must mint and forward an X-Selfai-Ticket service ticket scoped to
the endpoint it's calling — see api/selfai_ui/routers/curator.py and
context/kits/cavekit-service-mesh-ticket-auth.md.
"""

import jwt
import pytest
from aioresponses import CallbackResult

from tests.mocks.external_services import aioresponses_strict


@pytest.mark.tier1
def test_curator_verify_success(authenticated_admin):
    """POST /curator/verify returns the upstream /health body on 200."""
    target = "http://self-curator:8000"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/health",
            status=200,
            payload={"status": "healthy", "version": "1.0"},
        )
        resp = authenticated_admin.post("/curator/verify", json={"url": target})
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.tier1
def test_curator_verify_upstream_error_becomes_500(authenticated_admin):
    target = "http://self-curator:8000"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/health",
            status=503,
            payload={"error": "service down"},
        )
        resp = authenticated_admin.post("/curator/verify", json={"url": target})
    assert resp.status_code == 500


@pytest.mark.tier1
def test_curator_verify_strips_trailing_slash(authenticated_admin):
    """Router strips trailing slash before probing /health.

    Also asserts the captured request went to {url}/health (no double
    slashes), verifying the forwarding path is correct.
    """
    from aioresponses import CallbackResult

    target = "http://self-curator:8000"
    captured_urls = []

    def capture(url, **kwargs):
        captured_urls.append(str(url))
        return CallbackResult(status=200, payload={"ok": True})

    with aioresponses_strict() as m:
        m.get(f"{target}/health", callback=capture)
        resp = authenticated_admin.post("/curator/verify", json={"url": target + "/"})
    assert resp.status_code == 200
    assert len(captured_urls) == 1
    # URL should NOT contain double slash after host
    assert "//health" not in captured_urls[0].replace("://", ":")
    assert captured_urls[0].endswith("/health")


@pytest.mark.tier1
def test_curator_config_update_persists(authenticated_admin):
    """Config update round-trip using current config shape succeeds."""
    current = authenticated_admin.get("/curator/config").json()
    resp = authenticated_admin.post("/curator/config/update", json=current)
    assert resp.status_code == 200, f"Config round-trip returned {resp.status_code}: {resp.text[:200]}"


##########################################
# self.ai#25 / self.curator#5: service-ticket forwarding
##########################################

@pytest.fixture
def curator_target(authenticated_admin):
    """Resolve the live app's actual configured curator base URL rather than
    assuming config.py's default — the proxy endpoints below (unlike
    /verify, which takes an explicit `url` form field) always call out to
    whatever CURATOR_BASE_URLS[0] currently is."""
    cfg = authenticated_admin.get("/curator/config").json()
    return cfg["CURATOR_BASE_URLS"][0].rstrip("/")


def _capture_ticket_claims(captured: dict):
    """aioresponses callback: decode the forwarded X-Selfai-Ticket (using the
    same SERVICE_AUTH_SECRET conftest.py configures) and stash its claims."""

    def _cb(url, **kwargs):
        headers = kwargs.get("headers") or {}
        ticket = headers.get("X-Selfai-Ticket")
        captured["ticket_present"] = ticket is not None
        if ticket:
            captured["claims"] = jwt.decode(
                ticket,
                "test-service-auth-secret-not-for-production",
                algorithms=["HS256"],
                audience="self.curator",
            )
        return CallbackResult(status=200, payload=captured.pop("payload", {}))

    return _cb


@pytest.mark.tier1
def test_curator_list_jobs_forwards_ticket_with_jobs_read_scope(authenticated_admin, curator_target):
    captured = {"payload": []}
    with aioresponses_strict() as m:
        m.get(f"{curator_target}/api/jobs", callback=_capture_ticket_claims(captured))
        resp = authenticated_admin.get("/curator/api/jobs")
    assert resp.status_code == 200
    assert captured["ticket_present"] is True
    assert captured["claims"]["aud"] == "self.curator"
    assert captured["claims"]["iss"] == "self.ai"
    assert "jobs:read" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_curator_create_job_forwards_ticket_with_jobs_create_scope(authenticated_admin, curator_target):
    captured = {"payload": {"job_id": "abc123"}}
    with aioresponses_strict() as m:
        m.post(f"{curator_target}/api/jobs", callback=_capture_ticket_claims(captured))
        resp = authenticated_admin.post(
            "/curator/api/jobs",
            json={"name": "x", "input_path": "/x", "output_path": "/y", "stages": []},
        )
    assert resp.status_code == 200
    assert captured["ticket_present"] is True
    assert "jobs:create" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_curator_create_custom_stage_forwards_ticket_with_stages_write_scope(authenticated_admin, curator_target):
    """The highest-severity proxied endpoint (self.ai#14's custom-stage RCE
    surface, self.curator's create_custom_stage) must forward a
    stages:write-scoped ticket, not go out unauthenticated."""
    captured = {"payload": {"id": "stage-1"}}
    with aioresponses_strict() as m:
        m.post(f"{curator_target}/api/text/custom/stages", callback=_capture_ticket_claims(captured))
        resp = authenticated_admin.post(
            "/curator/api/text/custom/stages",
            json={"name": "x", "category": "text", "code": "pass"},
        )
    assert resp.status_code == 200
    assert captured["ticket_present"] is True
    assert "stages:write" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_curator_delete_custom_stage_forwards_ticket_with_stages_write_scope(authenticated_admin, curator_target):
    captured = {"payload": {"status": "deleted"}}
    with aioresponses_strict() as m:
        m.delete(f"{curator_target}/api/text/custom/stages/xyz", callback=_capture_ticket_claims(captured))
        resp = authenticated_admin.delete("/curator/api/text/custom/stages/xyz")
    assert resp.status_code == 200
    assert captured["ticket_present"] is True
    assert "stages:write" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_curator_upload_data_endpoint_not_verify_stays_unticketed(authenticated_admin):
    """/curator/verify's own /health probe is deliberately unticketed
    (matches self.llamolotl's /health being open on both sides) — confirm
    no X-Selfai-Ticket header is sent for it specifically."""
    target = "http://self-curator:8000"
    captured_headers = {}

    def capture(url, **kwargs):
        captured_headers.update(kwargs.get("headers") or {})
        return CallbackResult(status=200, payload={"status": "ok"})

    with aioresponses_strict() as m:
        m.get(f"{target}/health", callback=capture)
        resp = authenticated_admin.post("/curator/verify", json={"url": target})
    assert resp.status_code == 200
    assert "X-Selfai-Ticket" not in captured_headers
