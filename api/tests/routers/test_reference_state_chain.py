"""T-010: R1's concrete state chain, proven through the real surfaces only.

R1's final acceptance criterion (criterion 7) is the one that turns the reference
mod from "four independent stubs" into one coherent unit: *invoking the tool
writes state; that same state is what the namespace streams to a subscribed
client; the route reads that state back. A client that never subscribed to the
namespace can still observe the state change by requesting the route after the
tool ran.*

This module proves that chain end to end against the REAL booted app (the
`reference_booted_client` fixture from `tests.mods_reference_boot`, which drives
`selfai_ui.main.app` through its actual lifespan with the real `reference` mod
installed and enabled). Every fact is read through one of the three real
surfaces — the assembled tool's `callable`, the namespace's registered handler,
and the HTTP route — never off the `STATE` store's internals or the `Mod`
object's attributes (the Verification Convention's forbidden shortcut for this
task). `STATE` is not imported here at all: the tool's own return value carries
the written handle forward as the comparison value.

How the tool is invoked — the most-real path reachable without standing up a full
chat completion. Core's chat path (`selfai_ui/utils/middleware.py:1106-1109`)
assembles a user's mod tools with `assemble_for_user(app.state.MODS, user,
defaults=USER_PERMISSIONS)` and later invokes each as an async
`callable(**model_args + __-prefixed extras)`; the `_scope_guard` wrapper
re-checks the scope and strips the `__`-prefixed params before calling the mod's
handler (`selfai_ui/mods/tools.py:173-181`). These tests call that exact function
against the booted mod's real `LoadedMod`, then invoke the assembled callable the
way dispatch does. That is the real assembled tool of the real loaded mod — not a
hand-built spec.

How the namespace is proven. This test suite never stands up a live Socket.IO
transport — that live-transport proof is T-013's job (see
`tests/test_reference_ws.py`'s docstring). Here the namespace is exercised through
the handler the mod actually registered on core's real `sio` server at boot,
reached by the same namespace/event dispatch keys core itself uses
(`sio.handlers["/reference"]["sync"]`), with `sio.emit` captured to observe what a
subscribed client would receive. The reference mod's tool write does NOT itself
push over the namespace (its `submit_task` handler records and returns; it does
not call `emit_state_update` — that context-free push is R4/T-004's surface). So
the namespace fact this task proves is the *pull-on-subscribe* path: the always-
live delivery `register_ws`'s docstring guarantees — a freshly-subscribed client
emits `"sync"` and receives the current snapshot, which is exactly the state the
tool wrote. Connecting AFTER the tool ran is what makes that snapshot reflect the
write.

Cavekit: cavekit-mods-reference-implementation.md R1 (criterion 7) — T-010.
"""

from __future__ import annotations

import asyncio
import types

import pytest

# Contract constants (the namespace string and the one event type) — identifiers
# used to key into the surfaces and to name the event, NOT a read of mod state.
from mods.reference.reference_mod import REFERENCE_NAMESPACE, REFERENCE_STATE_EVENT

# `reference_booted_client` is provided by the `tests.mods_reference_boot` plugin
# (registered in tests/conftest.py); requesting it by name is how a Tier-2 test
# consumes it. Importing it would collide with the parameter name (ruff F811), so
# only the module constants and the grant helper are imported.
from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone


@pytest.fixture
def scoped_boot(reference_booted_client):
    """`reference_booted_client`, with the instance permission defaults restored
    on teardown so a grant made here does not leak into another test.

    `USER_PERMISSIONS` is a `PersistentConfig` persisted to the DB, and boot's
    `seed_defaults` never overwrites an existing value (`mods/loader.py:315`) — so
    a grant this test makes survives into the next test's boot and would flip that
    test's deny-by-default precondition to True. This fixture snapshots the
    defaults right after boot (deny-by-default, before any grant here) through the
    admin endpoint and POSTs them back on teardown, restoring the seeded state the
    same way an admin revoke would (a sent `False` wins in `_merge_permissions`).
    Restoring through the endpoint, not by mutating `USER_PERMISSIONS`, keeps this
    off the forbidden shortcut.
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


def _caller(boot) -> types.SimpleNamespace:
    """A minimal user object for the assembly path.

    `assemble_for_user` / `_scope_guard` read only `user.id` (to resolve group
    permissions and to bind the guard's identity), so the fixture's admin id is
    all that is needed. This is the identity dispatch would carry into assembly.
    """
    return types.SimpleNamespace(
        id=boot.admin["id"],
        email=boot.admin.get("email"),
        role=boot.admin.get("role"),
    )


def _invoke_submit_through_dispatch(boot, user) -> dict:
    """Invoke the mod's `submit` tool the way core's chat dispatch does, and
    return the handle it produced.

    Real surface, not a hand-built spec: `assemble_for_user` builds the per-user
    tool set from the booted mod's actual `LoadedMod.tools`, scope-gated against
    the live instance permission defaults — the exact call
    `selfai_ui/utils/middleware.py` makes. The assembled entry's `callable` is the
    `_scope_guard`-wrapped async handler; dispatch invokes it as
    `callable(**args)` with the model's arguments plus `__`-prefixed extras the
    guard strips. `submit` takes no model arguments, so only the injected extras
    are passed here — proving the guard strips them and the handler still runs.
    """
    from selfai_ui.mods.tools import assemble_for_user

    defaults = boot.app.state.config.USER_PERMISSIONS
    pairs = assemble_for_user(boot.app.state.MODS, user, defaults=defaults)
    assembled = dict(pairs)
    assert "submit" in assembled, f"the scoped user's assembled tool set must include submit; got {list(assembled)}"

    callable_ = assembled["submit"]["callable"]
    # `__`-prefixed params are what dispatch injects and the guard strips; passing
    # them here exercises that stripping (submit_task takes no arguments).
    return asyncio.run(callable_(__user__={"id": user.id}, __request__=None))


def _pull_via_namespace(boot, monkeypatch, sid: str = "sid-subscriber") -> tuple:
    """Drive the namespace's on-subscribe pull through the handler the mod
    actually registered on core's `sio` at boot, and return the single
    `(event, data, to, namespace)` it emits to that session.

    Reaches `sio.handlers[REFERENCE_NAMESPACE]["sync"]` — the same dispatch keys
    core uses to route an incoming `"sync"` from a subscribed client — and
    captures `sio.emit` to observe exactly what that client would receive. The
    handler closes over core's own `sio`, so patching `sio.emit` on the shared
    module object is what the handler calls.
    """
    from selfai_ui.socket.main import sio

    handlers = sio.handlers.get(REFERENCE_NAMESPACE, {})
    assert "sync" in handlers, "register_ws must have registered a connectable 'sync' handler on the namespace"
    sync_handler = handlers["sync"]

    captured: list[tuple] = []

    async def _capture_emit(event, data, to=None, namespace=None):
        captured.append((event, data, to, namespace))

    monkeypatch.setattr(sio, "emit", _capture_emit)
    asyncio.run(sync_handler(sid))

    assert len(captured) == 1, f"one subscribe pull, one emit to the requesting session; got {captured!r}"
    return captured[0]


# --- R1 criterion 7, unsubscribed variant: tool write -> route read ----------
@pytest.mark.tier1
def test_the_route_reads_back_state_a_tool_wrote_without_any_subscription(scoped_boot):
    """A client that never touches the namespace still observes the tool's state
    change by reading the route after the tool ran.

    Three facts, each through its surface: (1) the route reports clean state at
    boot — evidence the lifecycle startup reset ran; (2) invoking the assembled
    tool returns a `{task_id, status="submitted"}` handle; (3) the route, read by
    an authenticated user who never subscribed, reflects exactly that handle.
    """
    boot = scoped_boot
    admin = boot.as_admin()
    grant_reference_scope_for_everyone(admin)

    before = admin.get("/reference/state")
    assert before.status_code == 200, before.text
    assert before.json() == {"latest": None, "count": 0}, "the route must report clean state at boot"

    handle = _invoke_submit_through_dispatch(boot, _caller(boot))
    assert set(handle) == {"task_id", "status"}, handle
    assert handle["status"] == "submitted" and handle["task_id"], handle

    after = admin.get("/reference/state")
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["latest"] == handle, "the route must read back exactly the handle the tool wrote"
    assert body["count"] == 1, "one tool call recorded one handle"


# --- R1 criterion 7, full chain: tool -> namespace -> route are one state ----
@pytest.mark.tier1
def test_the_namespace_streams_the_same_state_the_tool_wrote_and_the_route_reads_it_back(scoped_boot, monkeypatch):
    """The coherent chain across all three real surfaces at once.

    Invoke the tool through dispatch; then a client subscribing to the namespace
    (emitting `"sync"`) receives the current snapshot, and the route returns the
    same snapshot. The load-bearing assertion is that the handle the TOOL
    returned, the `latest` the NAMESPACE streamed, and the `latest` the ROUTE
    read are one and the same object — not three coincidentally-equal reads.
    """
    boot = scoped_boot
    admin = boot.as_admin()
    grant_reference_scope_for_everyone(admin)

    handle = _invoke_submit_through_dispatch(boot, _caller(boot))

    # Namespace surface: subscribe AFTER the tool ran; the on-subscribe pull
    # delivers the current state to the requesting session.
    event, data, to, namespace = _pull_via_namespace(boot, monkeypatch)
    assert event == REFERENCE_STATE_EVENT, "the namespace streams the one pinned event type"
    assert namespace == REFERENCE_NAMESPACE == "/reference"
    assert to == "sid-subscriber", "the snapshot is pushed to the requesting session only"
    assert data["latest"] == handle, "the namespace streams exactly the state the tool wrote"
    assert data["count"] == 1

    # Route surface: the same snapshot, read back by HTTP.
    route_body = admin.get("/reference/state").json()

    # The chain is coherent: one state, observed identically through all three.
    assert (
        handle == data["latest"] == route_body["latest"]
    ), "tool-written handle, namespace-streamed state, and route-read state must be identical"
    assert data["count"] == route_body["count"] == 1


# --- R1: the four hooks are wired as one unit; startup reset ran at boot ------
@pytest.mark.tier1
def test_the_startup_reset_ran_at_boot_and_the_four_hooks_are_wired_as_one_unit(scoped_boot):
    """Light-touch confirmation (T-002/T-006 own the rigorous proofs) that the
    booted mod presents all four hooks as one coherent unit, each verified
    through its real surface:

      * register_lifecycle — the route reports clean state at boot, evidence the
        startup callback reset the store. (The loader guarantees it runs exactly
        once per enabled mod; T-006 proves the count. Here the reset is the
        observable trace that the callback executed at this boot, even though the
        process-global store may have been written by a prior test.)
      * register_routers   — `GET /reference/state` serves under the mod's prefix.
      * register_ws        — the `/reference` namespace is registered on core's
        real `sio` with a connectable handler.
      * register_tools     — the scoped user's assembled tool set contains submit.
    """
    boot = scoped_boot
    admin = boot.as_admin()

    # register_lifecycle + register_routers: the route serves and shows the store
    # was reset clean at boot.
    resp = admin.get("/reference/state")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"latest": None, "count": 0}, "startup must reset the store clean at boot"

    # register_ws: the one namespace is on core's own server (not a mod mount).
    from selfai_ui.socket.main import sio

    assert REFERENCE_NAMESPACE in sio.handlers, "register_ws must register the namespace on core's sio"
    assert "sync" in sio.handlers[REFERENCE_NAMESPACE], "the namespace must carry a connectable handler"

    # register_tools: the tool is assembled and scope-gated (visible once granted).
    grant_reference_scope_for_everyone(admin)
    from selfai_ui.mods.tools import assemble_for_user

    pairs = dict(assemble_for_user(boot.app.state.MODS, _caller(boot), defaults=boot.app.state.config.USER_PERMISSIONS))
    assert "submit" in pairs, "register_tools must contribute the submit tool to the scoped user's set"
    assert pairs["submit"]["toolkit_id"] == f"mod:{REFERENCE_MOD_ID}", "the tool must be attributed to the mod"
