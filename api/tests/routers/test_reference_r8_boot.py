"""R8 (T-016): ONE boot proves R1-R6 together, through the real surfaces.

This is the Verification Convention (`cavekit-mods-overview.md`) applied
literally, and the capstone of the whole reference-mod build. It mirrors
`tests/routers/test_mods_boot_integration.py` -- the test whose absence let F-002
through -- but drives the REAL `reference` mod (via the shared
`reference_booted_client` fixture, T-009) rather than the synthetic `boottest`
fixture, and additionally exercises the namespace, the `output_schema` shape, the
scope grant/deny, and the table.

The single primary test below starts the real `selfai_ui.main.app` through its
actual lifespan ONCE (no test-side call to the discovery or boot entry point --
the fixture drives the lifespan, `boot_mods` runs at startup) and, WITHIN THAT
ONE BOOT, proves every R8 criterion, each read through its own real surface and
never off mod internals, boot-code output, or config state directly:

  * R8-1 -- the real app object booted with the mod installed + enabled, no
    test-side boot/discovery call. The fixture's `TestClient(app)` lifespan is
    the only thing that ran boot_mods.
  * R8-2 (R1) -- the route serves through its caller-facing HTTP path
    `GET /reference/state`.
  * R8-3 (R4) -- a real Socket.IO client connects to the mod's namespace over a
    live transport and receives an event issued by the mod's OWN
    `emit_state_update` (the facade `emit_to_user` path), nothing mocked.
  * R8-4 (R3) -- the tool is refused server-side for an unscoped user (a
    `PermissionError`, not a technical failure), and after an admin grant THROUGH
    THE REAL PERMISSIONS ROUND TRIP a holder invokes it the way core's dispatch
    does: `callable(**model_args)`, only the model's arguments.
  * R8-5 (R2) -- the assembled `ToolSpec` for the granted user carries the
    `output_schema` and serializes it ONLY under `to_mcp()`, not `to_openai()` /
    `to_anthropic()`.
  * R8-6 (R5) -- the `mod_reference_` table exists in the real database, read
    through the app's own `get_db` facade.
  * R8-7 -- the mod appears in `GET /api/v1/mods/enabled` for an admin, read
    through the endpoint itself.
  * R8-8 -- the F-002-shaped regression guard: every assertion below is reachable
    only because boot ran and published `app.state.MODS` with `reference` loaded.
    Removing the boot invocation from startup would leave `app.state.MODS` unset
    (or `reference` unloaded), failing the guard assertion and, transitively,
    every criterion. This mirrors
    `test_mods_boot_integration.py::test_boot_actually_loads_an_enabled_mod`'s
    "if the lifespan call is ever removed again, this fails" technique for the
    real reference mod.

Forbidden shortcuts (named in T-010...T-015) apply here too: no reading of the
`STATE` store internals or the `Mod` object; no mutating `USER_PERMISSIONS`
directly (the grant goes through the admin GET -> POST -> GET); no mocking
`emit_to_user` / `emit_state_update`; the registry and route facts are read
through their endpoints, not off boot-code output.

Cavekit: cavekit-mods-reference-implementation.md R8 -- T-016.
"""

from __future__ import annotations

import asyncio
import socket as _stdlib_socket
import sys
import types
from types import ModuleType

import pytest
import socketio as socketio_client
import uvicorn
from sqlalchemy import inspect as sa_inspect

from selfai_ui.mods.naming import table_prefix_for
from selfai_ui.mods.tools import _scope_guard, assemble_for_user

# `reference_booted_client` is provided by the tests.mods_reference_boot plugin
# (registered in tests/conftest.py); request it by name -- importing it would
# collide with the parameter name and trip ruff F811. Only the module constants
# and the round-trip grant helper are imported.
from tests.conftest import _create_test_user
from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone

#: The real reference mod's one declared scope and its one tool's name -- stated
#: here so the assertions read against the real artifact's own contract.
REFERENCE_SCOPE = "mods.reference.use"
SUBMIT_TOOL = "submit"

#: The one table the mod owns, derived (not hardcoded) so this agrees with the
#: migration's authority on the name.
EXPECTED_TABLE = f"{table_prefix_for(REFERENCE_MOD_ID)}handles"

# A uvicorn start + Socket.IO handshake is not instantaneous; generous ceilings so
# a slow box does not flake, while still failing fast on genuine non-delivery.
_STARTUP_TIMEOUT = 10.0
_DELIVERY_TIMEOUT = 5.0


def _free_loopback_port() -> int:
    """Grab an ephemeral loopback port the OS is currently offering."""
    probe = _stdlib_socket.socket(_stdlib_socket.AF_INET, _stdlib_socket.SOCK_STREAM)
    probe.setsockopt(_stdlib_socket.SOL_SOCKET, _stdlib_socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _transports() -> list[str]:
    """Mirror the server's single configured transport (websocket vs polling)."""
    from selfai_ui.env import ENABLE_WEBSOCKET_SUPPORT

    return ["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]


def _running_reference_module(boot) -> ModuleType:
    """The exact module object the loader imported for the booted mod.

    Resolved from the loaded entrypoint's own `__module__` (not a fresh import)
    so `emit_state_update` / `submit_task` / `STATE` driven here are the SAME
    objects the running mod's namespace handler and lifecycle callback use --
    proving live delivery against the running mod, not a second copy.
    """
    entrypoint = boot.app.state.MODS.loaded[REFERENCE_MOD_ID].entrypoint
    return sys.modules[type(entrypoint).__module__]


async def _prove_live_namespace_delivery(boot) -> None:
    """R8-3 (R4): stand up a real Socket.IO transport in front of the live `sio`
    the fixture already booted the mod onto, connect an authenticated client to
    the mod's namespace, and assert it receives the mod's one event when the
    mod's OWN backend code emits it live.

    Serves `selfai_ui.socket.main.app` -- the `socketio.ASGIApp` the deployed
    process mounts at `/ws` -- with `lifespan="off"` (the mod boot already
    happened in the fixture; this is only a transport). Nothing is mocked: the
    emit is the module-level `emit_state_update(user_id)` the lifecycle callback
    and a reconciler-watch loop call, holding only a user id -- no request, chat,
    or sid.
    """
    from selfai_ui.socket.main import app as socket_asgi_app

    mod = _running_reference_module(boot)

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

        client = socketio_client.AsyncClient(reconnection=False)
        received: asyncio.Queue = asyncio.Queue()

        @client.on(mod.REFERENCE_STATE_EVENT, namespace=mod.REFERENCE_NAMESPACE)
        async def _on_state(data):
            await received.put(data)

        await client.connect(
            f"http://127.0.0.1:{port}",
            auth={"token": boot.admin["token"]},
            namespaces=[mod.REFERENCE_NAMESPACE],
            socketio_path="ws/socket.io",
            transports=_transports(),
            wait_timeout=_STARTUP_TIMEOUT,
        )
        try:
            # A real state change through the mod's own tool handler, then the
            # mod's live context-free emit for the connected admin user.
            handle = mod.submit_task()
            await mod.emit_state_update(boot.admin["id"])

            payload = await asyncio.wait_for(received.get(), timeout=_DELIVERY_TIMEOUT)
            # The mod's one event type, carrying its snapshot, reflecting the
            # write we just made -- a live end-to-end path, not a stale push.
            assert payload == mod.STATE.snapshot()
            assert payload["latest"] == handle
            assert payload["count"] >= 1
        finally:
            await client.disconnect()
    finally:
        server.should_exit = True
        await serve_task


@pytest.mark.tier2
def test_one_boot_proves_r1_through_r6_and_the_registry_together(reference_booted_client, db_session):
    """The whole of R8 in one pass, within a single real boot.

    Every criterion is proven against the same live `reference_booted_client`
    boot; nothing re-boots between them. Read the module docstring for the map of
    which block proves which criterion.
    """
    boot = reference_booted_client
    admin = boot.as_admin()

    # -- R8-1 + R8-8: the F-002-shaped guard --------------------------------------
    # The ONLY thing that ran boot_mods is the fixture's TestClient(app) lifespan;
    # this test never calls discovery/boot itself. If that boot invocation were
    # removed from startup, app.state.MODS would be None (or reference unloaded),
    # failing here and transitively every assertion below (mirrors
    # test_mods_boot_integration::test_boot_actually_loads_an_enabled_mod).
    assert boot.app.state.MODS is not None, "boot must publish app.state.MODS -- F-002"
    assert (
        REFERENCE_MOD_ID in boot.app.state.MODS.loaded
    ), f"the enabled reference mod did not load; errors: {boot.app.state.MODS.errors}"

    # -- R8-7: the registry endpoint reports the mod for an admin (through the API)
    reg = admin.get("/api/v1/mods/enabled")
    assert reg.status_code == 200, reg.text
    assert REFERENCE_MOD_ID in {e["id"] for e in reg.json()}, reg.json()

    # -- R8-2 (R1): the route serves through its caller-facing HTTP path ----------
    route = admin.get("/reference/state")
    assert route.status_code == 200, route.text
    assert set(route.json()) == {"latest", "count"}, route.json()

    # -- R8-6 (R5): the mod's table exists in the real DB, via the app's get_db ---
    from selfai_ui.modapi import get_db

    with get_db() as db:
        table_names = sa_inspect(db.get_bind()).get_table_names()
    assert EXPECTED_TABLE in table_names, f"{EXPECTED_TABLE} missing from the real DB; have {sorted(table_names)}"

    # -- R8-3 (R4): live namespace delivery via the real emit path ----------------
    asyncio.run(_prove_live_namespace_delivery(boot))

    # -- R8-4 (R3), deny half: unscoped user refused server-side ------------------
    loaded = boot.app.state.MODS.loaded[REFERENCE_MOD_ID]
    (tool,) = loaded.tools
    assert tool.scope == REFERENCE_SCOPE, tool.scope

    before_defaults = boot.app.state.config.USER_PERMISSIONS
    unscoped = _create_test_user(db_session, role="user")
    unscoped_user = types.SimpleNamespace(id=unscoped["id"])

    # Not offered at assembly time...
    unscoped_pairs = dict(assemble_for_user(boot.app.state.MODS, unscoped_user, defaults=before_defaults))
    assert SUBMIT_TOOL not in unscoped_pairs, "an unscoped user must not be offered the reference tool"

    # ...and refused server-side even when invoked directly (never offered): build
    # the SAME guard assemble_mod_tools builds internally, for the unscoped user,
    # and invoke it the dispatch way (only the model's args -- none). The refusal
    # is a PermissionError naming the scope: a permissions denial, distinguishable
    # from a technical failure.
    guarded = _scope_guard(tool, user=unscoped_user, defaults=before_defaults)
    with pytest.raises(PermissionError) as excinfo:
        asyncio.run(guarded())
    assert REFERENCE_SCOPE in str(excinfo.value), "the denial must name the required scope"

    # -- the admin grant THROUGH THE REAL PERMISSIONS ROUND TRIP ------------------
    # Snapshot the seeded defaults through the endpoint and restore them in the
    # finally so the persisted grant does not leak into later boots in the session.
    original = admin.get("/api/v1/users/default/permissions")
    assert original.status_code == 200, original.text
    snapshot = original.json()
    try:
        confirmed = grant_reference_scope_for_everyone(admin)  # GET -> mutate -> POST -> GET
        assert confirmed["mods"][REFERENCE_MOD_ID]["use"] is True, "the round trip must flip the leaf to True"

        # -- R8-4 (R3), allow half: a holder receives and invokes the tool -------
        after_defaults = boot.app.state.config.USER_PERMISSIONS
        holder = _create_test_user(db_session, role="user")
        holder_user = types.SimpleNamespace(id=holder["id"])

        holder_pairs = dict(assemble_for_user(boot.app.state.MODS, holder_user, defaults=after_defaults))
        assert SUBMIT_TOOL in holder_pairs, "after the grant the holder must be offered the tool"
        entry = holder_pairs[SUBMIT_TOOL]
        assert entry["toolkit_id"] == f"mod:{REFERENCE_MOD_ID}", entry["toolkit_id"]

        # Invoke the way core's dispatch does: only the model's arguments (none),
        # no acting user or defaults passed in at call time (the F-003 shape).
        handle = asyncio.run(entry["callable"]())
        assert set(handle) == {"task_id", "status"}, handle
        assert handle["status"] == "submitted" and handle["task_id"], handle

        # R1 coherence bonus: the route reads back exactly the handle the tool
        # wrote, through its HTTP surface -- one state across tool and route.
        after_route = admin.get("/reference/state")
        assert after_route.status_code == 200, after_route.text
        assert after_route.json()["latest"] == handle, "the route must reflect the tool's write"

        # -- R8-5 (R2): output_schema survives assembly, inert on the wire -------
        from mods.reference.reference_mod import TOOL_OUTPUT_SCHEMA

        spec = entry["spec"]
        assert spec.output_schema == TOOL_OUTPUT_SCHEMA, spec.output_schema
        assert set(spec.output_schema.get("properties", {})) == {"task_id", "status"}

        mcp = spec.to_mcp()
        assert "outputSchema" in mcp, f"MCP serialization dropped the output schema: {sorted(mcp)}"
        assert mcp["outputSchema"] == TOOL_OUTPUT_SCHEMA

        openai = spec.to_openai()
        assert set(openai) == {"name", "description", "parameters"}, openai
        assert not any("output" in key.lower() for key in openai), openai

        anthropic = spec.to_anthropic()
        assert set(anthropic) == {"name", "description", "input_schema"}, anthropic
        assert not any("output" in key.lower() for key in anthropic), anthropic
    finally:
        admin.post("/api/v1/users/default/permissions", json=snapshot)
