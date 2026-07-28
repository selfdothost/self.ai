"""T-013: VRAM lease broker router — auth split + reconcile-is-only-clear.

Two auth stories, verified distinctly:
  * consumer endpoints (register / heartbeat / request-lease) are gated by an
    inbound service ticket (X-Selfai-Ticket), NOT an admin user — a peer service
    calling in is not a human admin;
  * operator endpoints (list / capacity / reconcile) are gated by get_admin_user.

The load-bearing acceptance criterion here is R1-AC7: ``POST /{consumer_id}/
reconcile`` is admin-only and is the ONLY path that clears a stale/leftover
hold — nothing else frees it automatically.

conftest sets SERVICE_AUTH_SECRET to a test value at import, so a ticket minted
here with aud="self.ai" validates against the router's inbound check. NOTE: not
run locally (this bastion has no app venv); written for CI.
"""

import pytest

from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

GIB = 1024**3


def _svc(scope, audience="self.ai"):
    """Inbound service-ticket header for the given scope (aud = self.ai)."""
    return {TICKET_HEADER: mint_service_ticket(audience, scope)}


def _register(client, consumer_id, capacity, held=0, priority=0):
    return client.post(
        "/api/vram-leases/register",
        json={
            "consumer_id": consumer_id,
            "total_capacity_bytes": capacity,
            "held_bytes": held,
            "priority": priority,
        },
        headers=_svc("vram:report"),
    )


# ---------------------------------------------------------------------------
# Consumer-facing: service-ticket gated (NOT admin)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_register_with_service_ticket(client, db_session):
    resp = _register(client, "self.llamolotl", 24 * GIB)
    assert resp.status_code == 200
    body = resp.json()
    assert body["consumer_id"] == "self.llamolotl"
    assert body["total_capacity_bytes"] == 24 * GIB


@pytest.mark.tier0
def test_register_is_idempotent(client, db_session):
    _register(client, "self.llamolotl", 24 * GIB, held=1 * GIB)
    # Re-register updates in place, no duplicate.
    resp = _register(client, "self.llamolotl", 24 * GIB, held=2 * GIB)
    assert resp.status_code == 200
    assert resp.json()["held_bytes"] == 2 * GIB


@pytest.mark.tier0
def test_register_without_ticket_rejected(client):
    """No X-Selfai-Ticket -> 401, before any DB access."""
    resp = client.post(
        "/api/vram-leases/register",
        json={"consumer_id": "x", "total_capacity_bytes": GIB},
    )
    assert resp.status_code == 401


@pytest.mark.tier0
def test_register_with_wrong_scope_rejected(client):
    """A valid ticket lacking vram:report -> 403 (scope enforced)."""
    resp = client.post(
        "/api/vram-leases/register",
        json={"consumer_id": "x", "total_capacity_bytes": GIB},
        headers=_svc("vram:lease"),  # wrong scope for register
    )
    assert resp.status_code == 403


@pytest.mark.tier0
def test_register_with_bad_audience_rejected(client):
    """A ticket minted for another audience (e.g. self.llamolotl) must not be
    accepted by self.ai's inbound check."""
    resp = client.post(
        "/api/vram-leases/register",
        json={"consumer_id": "x", "total_capacity_bytes": GIB},
        headers=_svc("vram:report", audience="self.llamolotl"),
    )
    assert resp.status_code == 401


@pytest.mark.tier0
def test_heartbeat_updates_held(client, db_session):
    _register(client, "self.llamolotl", 24 * GIB, held=1 * GIB)
    resp = client.post(
        "/api/vram-leases/heartbeat",
        json={"consumer_id": "self.llamolotl", "held_bytes": 3 * GIB},
        headers=_svc("vram:report"),
    )
    assert resp.status_code == 200
    assert resp.json()["held_bytes"] == 3 * GIB


@pytest.mark.tier0
def test_heartbeat_unregistered_404(client, db_session):
    resp = client.post(
        "/api/vram-leases/heartbeat",
        json={"consumer_id": "ghost", "held_bytes": GIB},
        headers=_svc("vram:report"),
    )
    assert resp.status_code == 404


@pytest.mark.tier0
def test_request_lease_free_path_granted(client, db_session):
    """Registered requester, capacity free -> granted, hold recorded."""
    _register(client, "self.llamolotl", 24 * GIB, held=0)
    resp = client.post(
        "/api/vram-leases/request-lease",
        json={"consumer_id": "self.llamolotl", "amount_bytes": 4 * GIB, "priority": 0},
        headers=_svc("vram:lease"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["granted_bytes"] == 4 * GIB
    assert body["held_bytes"] == 4 * GIB


@pytest.mark.tier0
def test_request_lease_priority_from_registry_not_body(client, db_session):
    """#4 trust boundary: the requester's reclamation priority is taken from its
    REGISTRATION, not the (spoofable) request body. Register at priority 7, ask at
    priority 99 -> the grant records 7, so a caller cannot elevate its own
    reclamation power by lying in the request."""
    _register(client, "self.llamolotl", 24 * GIB, held=0, priority=7)
    resp = client.post(
        "/api/vram-leases/request-lease",
        json={"consumer_id": "self.llamolotl", "amount_bytes": 4 * GIB, "priority": 99},
        headers=_svc("vram:lease"),
    )
    assert resp.status_code == 200
    # The granted lease carries the REGISTERED priority (7), not the body's 99.
    assert resp.json()["priority"] == 7


@pytest.mark.tier0
def test_request_lease_over_capacity_denied_409(client, db_session):
    """Over free+releasable -> 409 with the structured denial body."""
    _register(client, "self.llamolotl", 24 * GIB, held=0)
    resp = client.post(
        "/api/vram-leases/request-lease",
        json={"consumer_id": "self.llamolotl", "amount_bytes": 99 * GIB, "priority": 0},
        headers=_svc("vram:lease"),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["requested_bytes"] == 99 * GIB
    assert "free_bytes" in detail and "freeable_bytes" in detail


@pytest.mark.tier0
def test_request_lease_unregistered_requester_400(client, db_session):
    resp = client.post(
        "/api/vram-leases/request-lease",
        json={"consumer_id": "ghost", "amount_bytes": GIB, "priority": 0},
        headers=_svc("vram:lease"),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Operator-facing: admin gated (like routers/windows.py)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_list_consumers_admin(client, authenticated_admin):
    _register(client, "self.llamolotl", 24 * GIB, held=2 * GIB)
    resp = authenticated_admin.get("/api/vram-leases")
    assert resp.status_code == 200
    rows = resp.json()
    llamolotl = [r for r in rows if r["consumer_id"] == "self.llamolotl"]
    assert len(llamolotl) == 1
    # effective_state surfaced on every row
    assert "effective_state" in llamolotl[0]


@pytest.mark.tier0
def test_capacity_admin(client, authenticated_admin):
    _register(client, "self.llamolotl", 24 * GIB, held=2 * GIB)
    resp = authenticated_admin.get("/api/vram-leases/capacity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_capacity_bytes"] == 24 * GIB
    assert body["total_held_bytes"] == 2 * GIB
    assert body["free_bytes"] == 22 * GIB


@pytest.mark.tier0
def test_list_forbidden_for_non_admin(authenticated_user):
    resp = authenticated_user.get("/api/vram-leases")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# R1-AC7: reconcile is admin-only AND the only path that clears a hold
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_reconcile_requires_admin(client, authenticated_user, db_session):
    """A non-admin cannot clear a hold — reconcile is an operator action."""
    _register(client, "self.llamolotl", 24 * GIB, held=8 * GIB)
    resp = authenticated_user.post("/api/vram-leases/self.llamolotl/reconcile")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_reconcile_clears_hold_admin(client, authenticated_admin):
    """Admin reconcile is the ONLY path that clears the leftover hold."""
    _register(client, "self.llamolotl", 24 * GIB, held=8 * GIB)

    # No consumer endpoint frees the hold — heartbeat only self-reports, it does
    # not clear a leftover lease. Confirm the hold is still there beforehand.
    pre = authenticated_admin.get("/api/vram-leases/capacity").json()
    assert pre["total_held_bytes"] == 8 * GIB

    resp = authenticated_admin.post("/api/vram-leases/self.llamolotl/reconcile")
    assert resp.status_code == 200
    assert resp.json()["held_bytes"] == 0

    post = authenticated_admin.get("/api/vram-leases/capacity").json()
    assert post["total_held_bytes"] == 0


@pytest.mark.tier0
def test_reconcile_unregistered_404(authenticated_admin):
    resp = authenticated_admin.post("/api/vram-leases/ghost/reconcile")
    assert resp.status_code == 404
