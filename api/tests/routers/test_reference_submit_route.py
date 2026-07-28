"""T-A09: a direct backend trigger for the reference mod's `submit` tool.

Gap remediation for `cavekit-mods-frontend-client.md` R7 AC5 — the reference
mod's status view must trigger its `submit` action "through its backend
surface". Phase 1 built `submit` only as a model-callable TOOL dispatched
through core's chat/tool-calling path (`assemble_for_user` -> the `_scope_guard`
callable); the mod's only external route was `GET /reference/state`. T-A09 adds
one plain HTTP entry point — `POST /reference/submit` — that is a THIN WRAPPER
over the two functions that already exist: it calls the module-level
`submit_task()` to write the handle, then `emit_state_update(user.id)` so the
change streams live over the `/reference` namespace, and returns the
`{task_id, status}` handle for an immediate synchronous confirmation.

Five behaviours are proven, each through a real surface (never off `STATE`'s
internals):

  1. A scoped caller can POST and gets back a `{task_id, status}` handle.
  2. The write is observable afterward via `GET /reference/state` — proof the
     route actually called `submit_task()`, not a fabricated response.
  3. The write is observable LIVE via the `/reference` namespace over a real
     Socket.IO client — proof the route actually called `emit_state_update`
     (reusing the live-transport pattern from `test_reference_namespace_delivery.py`).
  4. An authenticated caller LACKING `mods.reference.use` is refused with 403 —
     a permissions denial, the HTTP analogue of the tool guard's `PermissionError`.
  5. An unauthenticated caller is refused (401).

The route is gated on the IDENTICAL scope the tool is gated on (`TOOL_SCOPE` ==
`mods.reference.use`), checked with the same `has_permission` core uses — not a
new scope, not `_scope_guard` (which wraps a TOOL callable for model dispatch).

Cavekit: cavekit-mods-frontend-client.md R7 (criterion 5) — T-A09.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket as _stdlib_socket
import sys
from types import ModuleType

import httpx
import pytest
import socketio as socketio_client
import uvicorn

# The scope the route must enforce, read from the real mod's own constant. The
# namespace/event constants are read off the running module object (`mod.*`) in
# the live-transport test, so they are not imported by name here.
from mods.reference.reference_mod import TOOL_SCOPE
from tests.conftest import _create_test_user

# `reference_booted_client` is provided by the tests.mods_reference_boot plugin
# (registered in tests/conftest.py); requesting it by name is how a test consumes
# it. Importing it would collide with the parameter name (ruff F811), so only the
# constant and the grant helper are imported.
from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone

# Generous ceilings so a slow CI box does not flake the live-transport test while
# still failing fast on genuine non-delivery (mirrors test_reference_namespace_delivery).
_STARTUP_TIMEOUT = 10.0
_DELIVERY_TIMEOUT = 5.0


@pytest.fixture
def scoped_boot(reference_booted_client):
    """`reference_booted_client`, with the instance permission defaults restored
    on teardown so a grant made here does not leak into another test.

    `USER_PERMISSIONS` is a `PersistentConfig` persisted to the DB, and boot's
    `seed_defaults` never overwrites an existing value — so a grant this test
    makes survives into the next test's boot and would flip that test's
    deny-by-default precondition. This snapshots the defaults right after boot
    through the admin endpoint and POSTs them back on teardown, the same way an
    admin revoke would. Restoring through the endpoint, not by mutating
    `USER_PERMISSIONS`, keeps this off the forbidden shortcut. (Same pattern as
    `test_reference_state_chain.py::scoped_boot`.)
    """
    boot = reference_booted_client
    admin = boot.as_admin()
    original = admin.get("/api/v1/users/default/permissions")
    assert original.status_code == 200, original.text
    snapshot = original.json()
    try:
        yield boot
    finally:
        boot.as_admin().post("/api/v1/users/default/permissions", json=snapshot)


# ---------------------------------------------------------------------------
# Live Socket.IO transport helpers (mirrors test_reference_namespace_delivery.py)
# ---------------------------------------------------------------------------


def _free_loopback_port() -> int:
    """Grab an ephemeral loopback port the OS is currently offering."""
    probe = _stdlib_socket.socket(_stdlib_socket.AF_INET, _stdlib_socket.SOCK_STREAM)
    probe.setsockopt(_stdlib_socket.SOL_SOCKET, _stdlib_socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _transports() -> list[str]:
    """Match the client's transport to the server's configured one."""
    from selfai_ui.env import ENABLE_WEBSOCKET_SUPPORT

    return ["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]


def _running_reference_module(boot) -> ModuleType:
    """The exact module object the loader imported for the booted mod, so the
    `STATE` / `emit_state_update` these assertions read are the SAME objects the
    running route handler mutates and pushes over — proving the route drove them,
    not a second copy of the module."""
    entrypoint = boot.app.state.MODS.loaded["reference"].entrypoint
    return sys.modules[type(entrypoint).__module__]


@contextlib.asynccontextmanager
async def _live_socket_server():
    """Serve core's real Socket.IO ASGI app on a loopback port for the test.

    Serves `selfai_ui.socket.main.app` — the `socketio.ASGIApp` wrapping the
    global `sio` the reference mod already registered its namespace on — with
    `lifespan="off"` (the mod boot already happened in the fixture; this server
    is only a transport in front of the live `sio`)."""
    from selfai_ui.socket.main import app as socket_asgi_app

    port = _free_loopback_port()
    config = uvicorn.Config(socket_asgi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        deadline = asyncio.get_event_loop().time() + _STARTUP_TIMEOUT
        while not server.started:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("uvicorn did not start in time")
            await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serve_task


# ---------------------------------------------------------------------------
# Behaviour 1: a scoped caller can POST and gets a {task_id, status} handle
# ---------------------------------------------------------------------------
@pytest.mark.tier1
def test_a_scoped_caller_can_post_submit_and_gets_a_handle(scoped_boot):
    """An authenticated caller HOLDING `mods.reference.use` can POST
    `/reference/submit` and receives the async-handle shape."""
    boot = scoped_boot
    admin = boot.as_admin()
    grant_reference_scope_for_everyone(admin)

    resp = admin.post("/reference/submit")
    assert resp.status_code == 200, resp.text
    handle = resp.json()
    assert set(handle) == {"task_id", "status"}, handle
    assert handle["status"] == "submitted" and handle["task_id"], handle


# ---------------------------------------------------------------------------
# Behaviour 2: the write is observable via GET /reference/state (submit_task ran)
# ---------------------------------------------------------------------------
@pytest.mark.tier1
def test_the_posted_submit_is_observable_via_the_state_route(scoped_boot):
    """Proof the route actually called `submit_task()`, not a fabricated
    response: the state route reflects exactly the handle the POST returned, and
    the recorded count advances by exactly one.

    `count` is read relative to a `before` snapshot because `STATE` is
    process-global across the session — the delta, plus `latest == handle`, is
    the load-bearing evidence."""
    boot = scoped_boot
    admin = boot.as_admin()
    grant_reference_scope_for_everyone(admin)

    before = admin.get("/reference/state")
    assert before.status_code == 200, before.text
    before_count = before.json()["count"]

    resp = admin.post("/reference/submit")
    assert resp.status_code == 200, resp.text
    handle = resp.json()

    after = admin.get("/reference/state")
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["latest"] == handle, "the state route must read back exactly the handle the POST wrote"
    assert body["count"] == before_count + 1, "one POST recorded exactly one handle"


# ---------------------------------------------------------------------------
# Behaviour 3: the write streams LIVE over the namespace (emit_state_update ran)
# ---------------------------------------------------------------------------
@pytest.mark.tier2
def test_the_posted_submit_streams_live_over_the_namespace(scoped_boot):
    """Proof the route actually called `emit_state_update`: a real Socket.IO
    client subscribed to `/reference` for the calling user receives the mod's one
    event carrying the snapshot the POST just wrote — over a live transport, with
    nothing about `emit_state_update`/`emit_to_user` mocked.

    The POST is driven into the ALREADY-BOOTED app via an in-process ASGI
    transport running on the SAME event loop as the live socket server, so the
    route's `await emit_state_update(user.id)` issues its `sio.emit` on the loop
    that owns the client's connection — exactly as
    `test_reference_namespace_delivery.py` calls `emit_state_update` directly in
    that loop, but here the call is made BY the route under test."""
    boot = scoped_boot
    grant_reference_scope_for_everyone(boot.as_admin())
    mod = _running_reference_module(boot)
    token = boot.admin["token"]

    async def _scenario():
        async with _live_socket_server() as base_url:
            client = socketio_client.AsyncClient(reconnection=False)
            received: asyncio.Queue = asyncio.Queue()

            @client.on(mod.REFERENCE_STATE_EVENT, namespace=mod.REFERENCE_NAMESPACE)
            async def _on_state(data):
                await received.put(data)

            await client.connect(
                base_url,
                auth={"token": token},
                namespaces=[mod.REFERENCE_NAMESPACE],
                socketio_path="ws/socket.io",
                transports=_transports(),
                wait_timeout=_STARTUP_TIMEOUT,
            )
            try:
                # Drive the real route into the booted app on THIS loop.
                transport = httpx.ASGITransport(app=boot.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                    resp = await http.post("/reference/submit", headers={"Authorization": f"Bearer {token}"})
                assert resp.status_code == 200, resp.text
                handle = resp.json()
                assert set(handle) == {"task_id", "status"}, handle

                # The subscribed client receives the mod's one event, carrying the
                # snapshot the route wrote — proving emit_state_update fired live.
                payload = await asyncio.wait_for(received.get(), timeout=_DELIVERY_TIMEOUT)
                assert payload == mod.STATE.snapshot()
                assert payload["latest"] == handle, "the live event streams exactly the handle the POST wrote"
            finally:
                await client.disconnect()

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Behaviour 4: a caller lacking the scope is refused (permissions denial, 403)
# ---------------------------------------------------------------------------
@pytest.mark.tier1
def test_a_caller_lacking_the_scope_is_refused(scoped_boot, db_session):
    """An authenticated, non-admin caller who does NOT hold `mods.reference.use`
    is refused with 403 — a permissions denial naming the scope, NOT a technical
    failure. This mirrors how Phase 1 proved the tool refuses an unscoped caller
    (`test_reference_scope_enforcement.py`, which raises `PermissionError` naming
    the scope); the HTTP analogue is a 403.

    Precondition asserted, not assumed: the instance default for the scope is
    deny-by-default, so a fresh user in no granting group genuinely lacks it. The
    `scoped_boot` fixture restores the defaults on teardown either way."""
    boot = scoped_boot

    # Precondition: the scope is seeded deny-by-default (read through the surface).
    perms = boot.as_admin().get("/api/v1/users/default/permissions")
    assert perms.status_code == 200, perms.text
    assert (
        perms.json().get("mods", {}).get(REFERENCE_MOD_ID, {}).get("use") is False
    ), "precondition: the reference scope must be deny-by-default"

    unscoped = _create_test_user(db_session, role="user")
    resp = boot.client.post("/reference/submit", headers={"Authorization": f"Bearer {unscoped['token']}"})
    assert resp.status_code == 403, resp.text
    assert TOOL_SCOPE in resp.json().get("detail", ""), "the denial must name the required scope"


# ---------------------------------------------------------------------------
# Behaviour 5: an unauthenticated caller is refused
# ---------------------------------------------------------------------------
@pytest.mark.tier1
def test_an_unauthenticated_caller_is_refused(reference_booted_client):
    """A tokenless POST to `/reference/submit` is refused by the same
    `get_verified_user` dependency `GET /reference/state` uses, before any scope
    check or state write. The refusal is the same one that dependency gives the
    read route for a missing credential — asserted here to be identical to what
    `GET /reference/state` returns unauthenticated, so the write route is no more
    permissive than the read route to an anonymous caller."""
    boot = reference_booted_client
    # No Authorization header set on the fresh client.
    read_unauth = boot.client.get("/reference/state")
    resp = boot.client.post("/reference/submit")
    assert resp.status_code in (401, 403), resp.text
    assert resp.status_code == read_unauth.status_code, (
        "the write route must refuse an anonymous caller exactly as the read route does; "
        f"read={read_unauth.status_code} write={resp.status_code}"
    )
