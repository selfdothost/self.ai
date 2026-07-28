"""T-004: the reference mod's namespace registration and context-free emit path.

`register_ws` registers exactly one namespace, equal to the manifest's declared
`ws.namespace` (`/reference`), on core's existing Socket.IO server -- the `sio`
the loader passes in, not a mod-owned mount. The module exposes
`emit_state_update(user_id)`: R4's context-free emit, which pushes the shared
state to one user through the facade's `emit_to_user` (never raw `sio`
targeting, which cannot resolve a user to their sessions).

The live-boot proof -- a client authenticated as the user, subscribed to the
namespace over a real transport, actually receiving the event, and a client on a
different namespace not receiving it -- is Tier 2's T-013 against the running
mod. Here the namespace registration and the emit helper are proven directly:
the registration against a stub server, and the emit path with `emit_to_user`
patched, so what `emit_state_update` sends (user, event, payload, namespace) is
asserted exactly.

Cavekit: cavekit-mods-reference-implementation.md R4, R1 (criterion 4) -- T-004
"""

import asyncio
from pathlib import Path

import pytest

from mods.reference import reference_mod
from mods.reference.reference_mod import (
    REFERENCE_NAMESPACE,
    REFERENCE_STATE_EVENT,
    STATE,
    Mod,
    emit_state_update,
)
from selfai_ui.mods.validate import load_manifest

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "mods" / "reference" / "mod.yaml"
CORE = "0.5.0"


class _StubSio:
    """A minimal stand-in for core's Socket.IO server.

    Records the `sio.on(event, namespace=...)` registrations `register_ws` makes
    (in decorator form, the form the mod uses) and captures `sio.emit` calls the
    registered handler issues -- enough to assert the namespace and event without
    standing up a real transport (that is T-013's job).
    """

    def __init__(self) -> None:
        self.registered: list[tuple[str, str, object]] = []
        self.emitted: list[tuple[str, object, object, object]] = []

    def on(self, event, handler=None, namespace=None):
        def _record(h):
            self.registered.append((event, namespace, h))
            return h

        return _record if handler is None else _record(handler)

    async def emit(self, event, data, to=None, namespace=None):
        self.emitted.append((event, data, to, namespace))


# --- R1 criterion 4: the namespace equals the manifest's declared ws.namespace ---
@pytest.mark.tier0
def test_the_namespace_constant_equals_the_manifest_declared_namespace():
    """The string the mod registers on is the one the manifest declares.

    The loader installs auth on `manifest.ws.namespace` and refuses collisions by
    that same string; if the mod registered a different namespace the gate would
    guard one namespace and the handler would live on another. Pinning them equal
    is R1's fourth criterion for this hook.
    """
    manifest = load_manifest(MANIFEST_PATH, core_version=CORE)
    assert manifest.ws is not None
    assert reference_mod.REFERENCE_NAMESPACE == manifest.ws.namespace == "/reference"


# --- R4: register_ws registers the one namespace on the server it is given ---
@pytest.mark.tier0
def test_register_ws_registers_the_declared_namespace_on_the_passed_server():
    """`register_ws(sio)` attaches its handler to the `sio` it is handed, on the
    declared namespace -- not on a server of its own making."""
    stub = _StubSio()
    Mod().register_ws(stub)

    assert stub.registered, "register_ws must register at least one handler so the namespace is connectable"
    namespaces = {ns for _event, ns, _h in stub.registered}
    assert namespaces == {REFERENCE_NAMESPACE}, "every handler must live on the one declared namespace"


@pytest.mark.tier0
def test_the_subscribe_handler_pushes_the_current_snapshot_to_the_requesting_client():
    """The `"sync"` handler lets a freshly-subscribed client pull current state:
    it emits `STATE.snapshot()` back to that one session, on the mod's namespace,
    as the one event type."""
    stub = _StubSio()
    Mod().register_ws(stub)
    (_event, _ns, handler) = next(reg for reg in stub.registered if reg[0] == "sync")

    STATE.reset()
    STATE.record("task-1", "accepted")
    try:
        asyncio.run(handler("sidZ"))
    finally:
        STATE.reset()

    assert stub.emitted == [
        (
            REFERENCE_STATE_EVENT,
            {"latest": {"task_id": "task-1", "status": "accepted"}, "count": 1},
            "sidZ",
            REFERENCE_NAMESPACE,
        )
    ]


# --- R4: emit_state_update is the context-free push, via the facade emit path ---
@pytest.mark.tier0
def test_emit_state_update_targets_the_user_on_the_namespace_via_emit_to_user(monkeypatch):
    """The context-free path R4 requires: given only a user id, it pushes the
    snapshot as the one event type on the reference namespace, and it does so
    through `emit_to_user` -- the facade helper -- not raw `sio`."""
    calls = []

    async def _fake_emit(user_id, event, data, *, namespace=None):
        calls.append((user_id, event, data, namespace))

    # Patch the name the module resolves at call time, so this asserts the helper
    # routes through emit_to_user rather than reaching for sio directly.
    monkeypatch.setattr(reference_mod, "emit_to_user", _fake_emit)

    STATE.reset()
    STATE.record("task-42", "accepted")
    try:
        asyncio.run(emit_state_update("u7"))
    finally:
        STATE.reset()

    assert len(calls) == 1, "one user targeted, one emit"
    user_id, event, data, namespace = calls[0]
    assert user_id == "u7"
    assert event == REFERENCE_STATE_EVENT
    assert namespace == REFERENCE_NAMESPACE == "/reference"
    assert data == {"latest": {"task_id": "task-42", "status": "accepted"}, "count": 1}


@pytest.mark.tier0
def test_emit_state_update_is_the_real_facade_emit_to_user(monkeypatch):
    """The name the helper calls IS the facade's `emit_to_user`, so this exercises
    the exact path the mod would take at runtime -- not a look-alike."""
    from selfai_ui import modapi

    assert reference_mod.emit_to_user is modapi.emit_to_user
    assert "emit_to_user" in modapi.__all__
