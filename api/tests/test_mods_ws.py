"""The websocket surface: connect-auth refusal, namespace registration, per-user emit.

Core's connect handler refuses a connection carrying no valid token. A mod's
namespace does NOT inherit that gate automatically -- a namespace with no connect
handler is accepted unauthenticated (F-004) -- so the loader installs the same
gate on each mod namespace via install_namespace_auth. These tests exercise the
handler and the loader directly rather than standing up a real Socket.IO
transport.

Cavekit: cavekit-mods-surfaces.md R1; cavekit-mods-facade.md R1;
cavekit-mods-discovery.md R2 -- T-058, T-059, T-060, T-062, T-070, T-071
"""

import sys
import types

import pytest
from fastapi import FastAPI

from selfai_ui.mods.loader import load_mods
from selfai_ui.mods.manifest import ModManifest

BASE = {"name": "Crew", "version": "0.1.0", "entrypoint": "crew_mod:Mod", "min_core_version": "0.5.0"}


def _manifest(mod_id="crew", namespace=None, entrypoint=None):
    data = {**BASE, "id": mod_id, "name": mod_id.title()}
    if entrypoint is not None:
        data["entrypoint"] = entrypoint
    if namespace is not None:
        data["ws"] = {"namespace": namespace}
    return ModManifest(**data)


@pytest.fixture
def modmod(request):
    name = f"_wsmod_{request.node.name[:40]}"
    module = types.ModuleType(name)
    sys.modules[name] = module
    yield name, module
    sys.modules.pop(name, None)


# --- T-059 / T-062: core refuses a connection with no valid token -----------
@pytest.mark.tier1
def test_connect_is_refused_without_a_token(monkeypatch):
    import asyncio

    async def _run():
        from selfai_ui.socket import main as socket_main

        # Refusal is now a raise carrying a coded reason (self.ai#81) rather than
        # a bare `return False`, so the client is told why it was dropped.
        with pytest.raises(socket_main.SocketConnectionRefused) as exc:
            await socket_main.connect("sid1", {}, None)
        assert exc.value.error_args["data"]["code"] == "auth_required"


    asyncio.run(_run())

@pytest.mark.tier1
def test_connect_is_refused_with_an_invalid_token(monkeypatch):
    import asyncio

    async def _run():
        from selfai_ui.socket import main as socket_main

        monkeypatch.setattr(socket_main, "decode_token", lambda t: None)
        with pytest.raises(socket_main.SocketConnectionRefused) as exc:
            await socket_main.connect("sid2", {}, {"token": "garbage"})
        assert exc.value.error_args["data"]["code"] == "invalid_token"


    asyncio.run(_run())

@pytest.mark.tier1
def test_connect_is_accepted_with_a_valid_token(monkeypatch):
    import asyncio

    async def _run():
        from selfai_ui.socket import main as socket_main

        user = types.SimpleNamespace(id="u1", model_dump=lambda: {"id": "u1"})
        monkeypatch.setattr(socket_main, "decode_token", lambda t: {"id": "u1"})
        monkeypatch.setattr(socket_main.Users, "get_user_by_id", staticmethod(lambda uid: user))

        recorded = {}

        class _Pool:
            async def aset(self, k, v):
                recorded[k] = v

            async def aget(self, k, default=None):
                return recorded.get(k, default)

            async def akeys(self):
                return list(recorded)

        monkeypatch.setattr(socket_main, "SESSION_POOL", _Pool())
        monkeypatch.setattr(socket_main, "USER_POOL", _Pool())
        async def _models():
            return []

        monkeypatch.setattr(socket_main, "get_models_in_use", _models)

        async def _noop_emit(*a, **k):
            return None

        monkeypatch.setattr(socket_main.sio, "emit", _noop_emit)

        result = await socket_main.connect("sid3", {}, {"token": "good"})
        assert result is True
        assert recorded.get("sid3") == {"id": "u1"}


    asyncio.run(_run())

# --- T-071: emit_to_user ----------------------------------------------------
@pytest.mark.tier1
def test_emit_to_user_reaches_every_session_of_that_user(monkeypatch):
    import asyncio

    async def _run():
        from selfai_ui.socket import main as socket_main

        class _Pool:
            async def aget(self, k, default=None):
                return {"u1": ["sidA", "sidB"]}.get(k, default)

        emitted = []

        async def _emit(event, data, to=None, namespace=None):
            emitted.append((event, data, to, namespace))

        monkeypatch.setattr(socket_main, "USER_POOL", _Pool())
        monkeypatch.setattr(socket_main.sio, "emit", _emit)

        await socket_main.emit_to_user("u1", "crew:status", {"state": "running"}, namespace="/crew")

        assert len(emitted) == 2, "one emit per active session"
        assert {e[2] for e in emitted} == {"sidA", "sidB"}
        assert all(e[0] == "crew:status" and e[3] == "/crew" for e in emitted)


    asyncio.run(_run())

@pytest.mark.tier1
def test_emit_to_an_offline_user_is_a_noop(monkeypatch):
    import asyncio

    async def _run():
        from selfai_ui.socket import main as socket_main

        class _Empty:
            async def aget(self, k, default=None):
                return default

        called = []

        async def _emit(*a, **k):
            called.append(a)

        monkeypatch.setattr(socket_main, "USER_POOL", _Empty())
        monkeypatch.setattr(socket_main.sio, "emit", _emit)

        await socket_main.emit_to_user("ghost", "crew:status", {})
        assert called == [], "a user with no active session must not raise or emit"


    asyncio.run(_run())

@pytest.mark.tier0
def test_emit_to_user_is_on_the_facade():
    from selfai_ui import modapi

    assert "emit_to_user" in modapi.__all__
    assert callable(modapi.emit_to_user)


# --- T-058: register_ws registers a namespace on core's server --------------
@pytest.mark.tier0
def test_register_ws_is_invoked_with_cores_sio(modmod):
    name, module = modmod
    received = {}

    class Mod:
        def register_ws(self, sio):
            received["sio"] = sio

        def register_tools(self):
            return []

    module.Mod = Mod()

    app = FastAPI()
    load_mods(app, [_manifest("crew", namespace="/crew", entrypoint=f"{name}:Mod")])

    from selfai_ui.socket.main import sio as core_sio

    assert received.get("sio") is core_sio, "the mod must receive core's server, not a copy"


@pytest.mark.tier0
def test_a_mod_with_no_ws_block_registers_no_namespace(modmod):
    name, module = modmod
    called = []

    class Mod:
        def register_ws(self, sio):
            called.append(True)

    module.Mod = Mod()

    app = FastAPI()
    # No ws block in the manifest, so register_ws must not be called.
    load_mods(app, [_manifest("crew", namespace=None, entrypoint=f"{name}:Mod")])
    assert called == []


# --- T-060: namespace collision fails to load -------------------------------
@pytest.mark.tier0
def test_a_namespace_colliding_with_core_default_is_refused(modmod):
    name, module = modmod

    class Mod:
        def register_ws(self, sio):
            pass

    module.Mod = Mod()

    app = FastAPI()
    result = load_mods(app, [_manifest("crew", namespace="/", entrypoint=f"{name}:Mod")])
    assert result.loaded == {}
    assert "already registered" in result.errors[0] and "core" in result.errors[0]


@pytest.mark.tier0
def test_two_mods_claiming_one_namespace_load_only_the_first(modmod):
    name, module = modmod

    class Mod:
        def __init__(self, mid):
            self.mid = mid

        def register_ws(self, sio):
            # Actually register so the second collides on sio.handlers.
            @sio.on("ping", namespace="/shared")
            def _p(sid):
                pass

    module.A = Mod("a")
    module.B = Mod("b")

    app = FastAPI()
    a = ModManifest(**{**BASE, "id": "a", "name": "A", "entrypoint": f"{name}:A", "ws": {"namespace": "/shared"}})
    b = ModManifest(**{**BASE, "id": "b", "name": "B", "entrypoint": f"{name}:B", "ws": {"namespace": "/shared"}})

    try:
        result = load_mods(app, [a, b])
        assert result.loaded_ids == ["a"]
        assert len(result.errors) == 1 and "b" in result.errors[0]
    finally:
        # Clean the namespace we registered so the test is repeatable.
        from selfai_ui.socket.main import sio as core_sio

        core_sio.handlers.pop("/shared", None)


# --- T-070: a disabled mod contributes no namespace and no tools ------------
@pytest.mark.tier0
def test_a_disabled_mod_registers_no_namespace_and_no_tools(modmod):
    """Disabled means not loaded means register_ws / register_tools never run.
    A namespace that was never registered is not connectable, and tools that
    were never registered are absent from any assembled tool set."""
    name, module = modmod
    calls = []

    class Mod:
        def register_ws(self, sio):
            calls.append("ws")

            @sio.on("ping", namespace="/secret")
            def _p(sid):
                pass

        def register_tools(self):
            calls.append("tools")
            return [{"name": "leak", "description": "x", "handler": lambda **k: 1, "scope": "mods.secret.x"}]

    module.Mod = Mod()

    # Present on disk but NOT in the enabled list -> never loaded.
    import tempfile
    from pathlib import Path

    from selfai_ui.mods.discovery import discover
    from selfai_ui.socket.main import sio as core_sio

    root = Path(tempfile.mkdtemp())
    (root / "secret").mkdir()
    (root / "secret" / "mod.yaml").write_text(
        f"id: secret\nname: Secret\nversion: 0.1.0\n"
        f"entrypoint: {name}:Mod\nmin_core_version: 0.5.0\n"
        f"ws:\n  namespace: /secret\n",
        encoding="utf-8",
    )

    result = discover([root], [], core_version="0.5.0")
    assert result.loaded == {}
    assert calls == [], "a disabled mod's registration hooks must never run"
    assert "/secret" not in core_sio.handlers, "no namespace for a disabled mod"


# --- T-061 / T-062: a handler observes core's identity ----------------------
@pytest.mark.tier1
def test_a_namespace_handler_sees_the_identity_core_recorded(monkeypatch):
    """A mod handler reads the user from core's SESSION_POOL by sid -- the same
    identity core resolved at connect. It does not decode a token itself."""
    import asyncio

    async def _run():
        from selfai_ui.socket import main as socket_main

        class _Pool:
            async def aget(self, k, default=None):
                return {"sidX": {"id": "u9"}}.get(k, default)

        monkeypatch.setattr(socket_main, "SESSION_POOL", _Pool())

        # This is the accessor a mod handler would use via the facade path.
        observed = await socket_main.get_user_id_from_session_pool("sidX")
        assert observed == "u9", "the handler sees exactly the id core recorded"

        # An sid core never authenticated has no identity to observe.
        assert await socket_main.get_user_id_from_session_pool("never") is None

    asyncio.run(_run())


# --- F-004: a mod namespace is auth-gated, not accepted unauthenticated -----
@pytest.mark.tier1
def test_a_registered_mod_namespace_refuses_an_unauthenticated_connect(modmod):
    """The core gate does NOT auto-extend to a mod namespace. The loader must
    install one, so an anonymous client is refused on the mod's namespace too."""
    import asyncio

    name, module = modmod

    class Mod:
        def register_ws(self, sio):
            @sio.on("ping", namespace="/authns")
            def _p(sid):
                pass

    module.Mod = Mod()

    app = FastAPI()
    result = load_mods(app, [_manifest("authmod", namespace="/authns", entrypoint=f"{name}:Mod")])
    assert result.loaded_ids == ["authmod"], result.errors

    from selfai_ui.socket.main import sio as core_sio

    try:
        # A connect handler is registered on the mod's namespace...
        assert "/authns" in core_sio.handlers
        connect = core_sio.handlers["/authns"].get("connect")
        assert connect is not None, "the loader must install an auth connect handler"

        # ...and it refuses a credential-less connect exactly like core's
        # default -- same raise, same coded reason.
        from selfai_ui.socket.main import SocketConnectionRefused

        with pytest.raises(SocketConnectionRefused) as exc:
            asyncio.run(connect("sid1", {}, None))
        assert exc.value.error_args["data"]["code"] == "auth_required"
    finally:
        core_sio.handlers.pop("/authns", None)


# --- F-006: a ws mod that raises in register_ws leaves no orphan namespace ---
@pytest.mark.tier0
def test_register_ws_raising_rolls_back_the_namespace(modmod):
    name, module = modmod

    class Mod:
        def register_ws(self, sio):
            # Register something, THEN fail -- the orphan window F-006 described.
            @sio.on("ping", namespace="/orphan")
            def _p(sid):
                pass

            raise RuntimeError("boom after registering")

    module.Mod = Mod()

    from selfai_ui.socket.main import sio as core_sio

    app = FastAPI()
    result = load_mods(app, [_manifest("orphanmod", namespace="/orphan", entrypoint=f"{name}:Mod")])

    try:
        assert result.loaded == {}, "the failing mod must not load"
        assert "orphan" in result.errors[0] and "boom" in result.errors[0]
        assert "/orphan" not in core_sio.handlers, "a failed ws registration must not orphan a namespace"
    finally:
        core_sio.handlers.pop("/orphan", None)


# --- F-007: the isolating route re-raises the starlette base HTTPException ---
@pytest.mark.tier0
def test_isolating_route_reraises_starlette_httpexception_end_to_end(modmod):
    from fastapi.testclient import TestClient
    from starlette.exceptions import HTTPException as StarletteHTTPException

    name, module = modmod

    class Mod:
        def register_routers(self, router):
            @router.get("/missing")
            def _h():
                raise StarletteHTTPException(status_code=404, detail="deliberate")

    module.Mod = Mod()

    app = FastAPI()
    m = ModManifest(**{**BASE, "id": "errmod", "name": "Err", "entrypoint": f"{name}:Mod", "api": {"prefix": "/err"}})
    load_mods(app, [m])

    resp = TestClient(app).get("/err/missing")
    assert resp.status_code == 404, "a deliberate starlette 404 must not become a 500"
    assert resp.json()["detail"] == "deliberate"
