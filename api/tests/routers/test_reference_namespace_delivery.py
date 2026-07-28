"""R4 (T-013): the reference mod's namespace delivers over a LIVE connection.

This is the Tier-2 proof R4 explicitly defers to from T-004. `test_mods_ws.py`
already covers `emit_to_user` / `emit_state_update` in isolation with the socket
pools and `sio.emit` mocked -- but a mock cannot prove that an event actually
crosses a real Socket.IO transport to a real subscribed client. R4's last two
criteria demand exactly that: "proven against the running reference mod (its
namespace connected and its emit issued live), not only by exercising
`emit_to_user` in isolation."

So these tests do the thing the unit tests could not:

  1. Boot the REAL app with the REAL `reference` mod (`reference_booted_client`,
     T-009), so the `/reference` namespace is registered on core's shared `sio`
     and core's connect-auth gate is installed on it by the loader.
  2. Serve core's own Socket.IO ASGI app (`selfai_ui.socket.main.app`, the same
     `socketio.ASGIApp(sio, ...)` the deployed process mounts at `/ws`) on a real
     loopback port via uvicorn, IN the test's event loop. We serve that ASGI app
     directly rather than re-running the FastAPI lifespan, because the fixture has
     already booted the mod onto the process-global `sio`; a second lifespan would
     double-register the namespace. The transport is real either way.
  3. Connect a real `socketio.AsyncClient` over that transport and assert delivery.

Nothing here mocks or patches `emit_to_user` or `emit_state_update` (the
Verification Convention's forbidden shortcut for this task): every emit is issued
by the running mod's own module-level `emit_state_update`, or by the mod's
on-subscribe `"sync"` handler, over the live namespace.

Cavekit: cavekit-mods-reference-implementation.md R4 -- T-013
"""

from __future__ import annotations

import asyncio
import contextlib
import socket as _stdlib_socket
import sys
from types import ModuleType

import pytest
import socketio as socketio_client
import uvicorn

# The event loop startup of a uvicorn server plus a Socket.IO handshake is not
# instantaneous; give generous ceilings so a slow CI box does not flake, while
# still failing fast on a genuine non-delivery.
_STARTUP_TIMEOUT = 10.0
_DELIVERY_TIMEOUT = 5.0
_NON_DELIVERY_WINDOW = 1.5


def _free_loopback_port() -> int:
    """Grab an ephemeral loopback port the OS is currently offering.

    Bind-then-release: there is a small race between release and uvicorn's
    re-bind, but on loopback with SO_REUSEADDR it is not observed to matter for a
    short-lived test server.
    """
    probe = _stdlib_socket.socket(_stdlib_socket.AF_INET, _stdlib_socket.SOCK_STREAM)
    probe.setsockopt(_stdlib_socket.SOL_SOCKET, _stdlib_socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _running_reference_module(boot) -> ModuleType:
    """Return the exact module object the loader imported for the booted mod.

    Resolved from the loaded entrypoint's own `__module__` (not by a fresh import)
    so the `emit_state_update` / `submit_task` / `STATE` these tests drive are the
    SAME objects the running mod's namespace handler and lifecycle callback use --
    proving live delivery against the running mod, not a second copy of the module.
    """
    entrypoint = boot.app.state.MODS.loaded["reference"].entrypoint
    return sys.modules[type(entrypoint).__module__]


@contextlib.asynccontextmanager
async def _live_socket_server():
    """Serve core's real Socket.IO ASGI app on a loopback port for the test.

    Yields the base URL a `socketio` client connects to. We serve
    `selfai_ui.socket.main.app` -- the `socketio.ASGIApp` wrapping the global
    `sio` the reference mod has already registered its namespace on -- with
    `lifespan="off"` so uvicorn does not attempt to re-run any app lifespan (the
    mod boot already happened in the fixture; this server is only a transport in
    front of the live `sio`).
    """
    from selfai_ui.socket.main import app as socket_asgi_app

    port = _free_loopback_port()
    config = uvicorn.Config(
        socket_asgi_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        # uvicorn flips `started` True once the socket is accepting.
        deadline = asyncio.get_event_loop().time() + _STARTUP_TIMEOUT
        while not server.started:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("uvicorn did not start in time")
            await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serve_task


def _transports() -> list[str]:
    """Match the client's transport to the server's configured one.

    `selfai_ui.socket.main.sio` is built with a single transport chosen by
    `ENABLE_WEBSOCKET_SUPPORT` (websocket vs polling); a client offering the other
    would fail the handshake, so mirror the server's choice.
    """
    from selfai_ui.env import ENABLE_WEBSOCKET_SUPPORT

    return ["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_authenticated_subscriber_receives_the_live_context_free_emit(reference_booted_client):
    """R4-2,3,7 (and R4-1): a subscribed, authenticated client receives the mod's
    one event when the mod's OWN no-request-context backend code emits it live.

    The emit is `emit_state_update(user_id)` -- the module-level function the
    lifecycle callback and a reconciler-watch loop call, holding only a user id,
    no request/chat/sid. We call it directly here "as the reconciler would" (WHO
    calls it is T-006's concern; THIS task proves DELIVERY works when it is
    called). A real state change is produced first via the mod's own
    `submit_task` so the payload the client receives is a genuine snapshot of a
    write, not an empty store.
    """
    boot = reference_booted_client
    mod = _running_reference_module(boot)

    async def _scenario():
        async with _live_socket_server() as base_url:
            client = socketio_client.AsyncClient(reconnection=False)
            received: asyncio.Queue = asyncio.Queue()

            @client.on(mod.REFERENCE_STATE_EVENT, namespace=mod.REFERENCE_NAMESPACE)
            async def _on_state(data):
                await received.put(data)

            await client.connect(
                base_url,
                auth={"token": boot.admin["token"]},
                namespaces=[mod.REFERENCE_NAMESPACE],
                socketio_path="ws/socket.io",
                transports=_transports(),
                wait_timeout=_STARTUP_TIMEOUT,
            )
            try:
                # Produce a real state change through the mod's own tool handler,
                # then issue the mod's live context-free emit for the admin user.
                handle = mod.submit_task()
                await mod.emit_state_update(boot.admin["id"])

                payload = await asyncio.wait_for(received.get(), timeout=_DELIVERY_TIMEOUT)

                # It is the mod's one event type carrying its snapshot shape, and
                # it reflects the write we just made (proving a live end-to-end
                # path, not a stale/empty push).
                assert payload == mod.STATE.snapshot()
                assert payload["latest"] == handle
                assert payload["count"] >= 1

                # The on-subscribe "sync" pull delivers the same event type too,
                # so a freshly-subscribed client can bootstrap current state.
                await client.emit("sync", namespace=mod.REFERENCE_NAMESPACE)
                synced = await asyncio.wait_for(received.get(), timeout=_DELIVERY_TIMEOUT)
                assert synced == mod.STATE.snapshot()
            finally:
                await client.disconnect()

    asyncio.run(_scenario())


@pytest.mark.tier2
def test_a_client_on_a_different_namespace_does_not_receive_the_event(reference_booted_client):
    """R4-4: an emit over `/reference` reaches ONLY the `/reference` subscription.

    Same authenticated user, two live connections: one subscribed to the mod's
    namespace, one to core's default namespace. `emit_state_update` targets the
    user by id (so BOTH of their sessions are in `USER_POOL`), but scopes the emit
    to `namespace="/reference"` -- so the default-namespace session must receive
    nothing, proving namespace isolation is real over the wire, not just in
    `emit_to_user`'s signature.
    """
    boot = reference_booted_client
    mod = _running_reference_module(boot)

    async def _scenario():
        async with _live_socket_server() as base_url:
            transports = _transports()

            ns_client = socketio_client.AsyncClient(reconnection=False)
            ns_received: asyncio.Queue = asyncio.Queue()

            @ns_client.on(mod.REFERENCE_STATE_EVENT, namespace=mod.REFERENCE_NAMESPACE)
            async def _on_ns(data):
                await ns_received.put(data)

            default_client = socketio_client.AsyncClient(reconnection=False)
            default_received: asyncio.Queue = asyncio.Queue()

            # Listen for the reference event on the DEFAULT namespace; it must
            # never arrive there.
            @default_client.on(mod.REFERENCE_STATE_EVENT)
            async def _on_default(data):
                await default_received.put(data)

            await ns_client.connect(
                base_url,
                auth={"token": boot.admin["token"]},
                namespaces=[mod.REFERENCE_NAMESPACE],
                socketio_path="ws/socket.io",
                transports=transports,
                wait_timeout=_STARTUP_TIMEOUT,
            )
            await default_client.connect(
                base_url,
                auth={"token": boot.admin["token"]},
                namespaces=["/"],
                socketio_path="ws/socket.io",
                transports=transports,
                wait_timeout=_STARTUP_TIMEOUT,
            )
            try:
                mod.submit_task()
                await mod.emit_state_update(boot.admin["id"])

                # The reference-namespace client gets it...
                got = await asyncio.wait_for(ns_received.get(), timeout=_DELIVERY_TIMEOUT)
                assert got == mod.STATE.snapshot()

                # ...and the default-namespace client does NOT, within a window
                # comfortably longer than the delivery that already happened.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(default_received.get(), timeout=_NON_DELIVERY_WINDOW)
            finally:
                await ns_client.disconnect()
                await default_client.disconnect()

    asyncio.run(_scenario())


@pytest.mark.tier2
def test_an_unauthenticated_connect_to_the_namespace_is_refused(reference_booted_client):
    """R4-5: a tokenless connect to `/reference` is refused by core's own
    connect-time auth, with no reference-mod code involved.

    Provenance first: the connect gate on `/reference` is core's
    `install_namespace_auth._connect` (module `selfai_ui.socket.main`), installed
    by the loader; the reference mod's `register_ws` contributes only a `"sync"`
    handler, no `connect` of its own. So whatever refuses the anonymous client is
    core code, not the mod.

    Refusal, proven live: core's `sio` is built with `always_connect=True`, so a
    refused client lingers at the Engine.IO transport level (it is not the mod's
    concern, and core's DEFAULT namespace behaves identically) -- the meaningful,
    observable refusal is that core establishes NO authenticated session for it
    and drops it from the namespace. We assert exactly that against the running
    server: after the anonymous connect, core's `SESSION_POOL` has no entry for it
    and a namespace broadcast reaches the authenticated subscriber but never the
    anonymous one. The authenticated client is the control: its session IS
    recorded and it DOES receive the broadcast, pinning the token as the thing
    core gated on.
    """
    boot = reference_booted_client
    mod = _running_reference_module(boot)
    namespace = mod.REFERENCE_NAMESPACE

    async def _scenario():
        from selfai_ui.socket.main import SESSION_POOL, sio

        # (a) Provenance: the gate is core's, and the mod added no connect handler.
        ns_handlers = sio.handlers.get(namespace, {})
        connect_handler = ns_handlers.get("connect")
        assert connect_handler is not None, "core must install a connect gate on the mod namespace"
        assert (
            connect_handler.__module__ == "selfai_ui.socket.main"
        ), "the connect gate must be core's own, not reference-mod code"
        assert "sync" in ns_handlers, "the reference mod's own ws handler is present alongside core's gate"

        async with _live_socket_server() as base_url:
            transports = _transports()

            authed = socketio_client.AsyncClient(reconnection=False)
            authed_got: asyncio.Queue = asyncio.Queue()

            @authed.on(mod.REFERENCE_STATE_EVENT, namespace=namespace)
            async def _on_authed(data):
                await authed_got.put(data)

            anon = socketio_client.AsyncClient(reconnection=False)
            anon_got: asyncio.Queue = asyncio.Queue()

            @anon.on(mod.REFERENCE_STATE_EVENT, namespace=namespace)
            async def _on_anon(data):
                await anon_got.put(data)

            await authed.connect(
                base_url,
                auth={"token": boot.admin["token"]},
                namespaces=[namespace],
                socketio_path="ws/socket.io",
                transports=transports,
                wait_timeout=_STARTUP_TIMEOUT,
            )
            await anon.connect(
                base_url,
                auth=None,  # no token at all
                namespaces=[namespace],
                socketio_path="ws/socket.io",
                transports=transports,
                wait_timeout=_STARTUP_TIMEOUT,
            )
            try:
                # (b) Core recorded a session for the authenticated connect only.
                authed_sid = authed.namespaces[namespace]
                anon_sid = anon.namespaces.get(namespace)
                assert (
                    await SESSION_POOL.aget(authed_sid) is not None
                ), "core must record a session for the authenticated connect"
                if anon_sid is not None:
                    assert (
                        await SESSION_POOL.aget(anon_sid) is None
                    ), "core must NOT record a session for a tokenless connect"

                # (c) A namespace broadcast reaches the authed subscriber but not
                #     the refused anonymous one (core dropped it from the manager).
                await sio.emit(mod.REFERENCE_STATE_EVENT, mod.STATE.snapshot(), namespace=namespace)

                await asyncio.wait_for(authed_got.get(), timeout=_DELIVERY_TIMEOUT)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(anon_got.get(), timeout=_NON_DELIVERY_WINDOW)
            finally:
                await authed.disconnect()
                if anon.connected:
                    await anon.disconnect()

    asyncio.run(_scenario())


@pytest.mark.tier2
def test_emitting_to_a_user_with_no_active_session_is_a_live_noop(reference_booted_client):
    """R4-6: issuing the mod's emit for a user with no connected session is a
    no-op, not an error -- proven live, with the namespace actually connected.

    A real subscriber (admin) is connected so the namespace and server are live;
    we then fire `emit_state_update` for a DIFFERENT user id that has no session.
    The call must complete without raising, and the connected subscriber must not
    receive the stray emit (it was addressed to someone else).
    """
    boot = reference_booted_client
    mod = _running_reference_module(boot)

    async def _scenario():
        async with _live_socket_server() as base_url:
            client = socketio_client.AsyncClient(reconnection=False)
            received: asyncio.Queue = asyncio.Queue()

            @client.on(mod.REFERENCE_STATE_EVENT, namespace=mod.REFERENCE_NAMESPACE)
            async def _on_state(data):
                await received.put(data)

            await client.connect(
                base_url,
                auth={"token": boot.admin["token"]},
                namespaces=[mod.REFERENCE_NAMESPACE],
                socketio_path="ws/socket.io",
                transports=_transports(),
                wait_timeout=_STARTUP_TIMEOUT,
            )
            try:
                # No exception for an offline / unknown user.
                result = await mod.emit_state_update("user-with-no-session-xyz")
                assert result is None

                # And the connected subscriber, being a different user, gets
                # nothing from that stray emit.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(received.get(), timeout=_NON_DELIVERY_WINDOW)
            finally:
                await client.disconnect()

    asyncio.run(_scenario())
