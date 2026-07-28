"""The Phase 1 reference mod, proven through the real contract.

This file starts with the two Tier 0 facts and later tasks (T-003 onward) extend
it: T-001's real `mod.yaml` parses and passes semantic validation, and T-002's
entrypoint scaffold imports cleanly, instantiates, and exposes all four hooks
with the signatures the loader calls them with.

Cavekit: cavekit-mods-reference-implementation.md R1, R2, R5 -- T-001, T-002, T-003
"""

from pathlib import Path

import pytest

from selfai_ui.mods import naming
from selfai_ui.mods.validate import load_manifest

#: The reference mod lives permanently in the tree under api/mods/reference/.
REFERENCE_DIR = Path(__file__).resolve().parents[1] / "mods" / "reference"
MANIFEST_PATH = REFERENCE_DIR / "mod.yaml"

#: A core version the manifest's min_core_version (0.5.0) is satisfied by. Passed
#: explicitly rather than read from env so this test needs no running core.
CORE = "0.5.0"


# --- T-001: the manifest --------------------------------------------------
@pytest.mark.tier0
def test_reference_manifest_loads_and_validates():
    """The real mod.yaml on disk parses and passes full semantic validation."""
    manifest = load_manifest(MANIFEST_PATH, core_version=CORE)

    assert manifest.id == "reference"
    assert naming.is_valid_mod_id(manifest.id)
    assert manifest.min_core_version == "0.5.0"

    # One router prefix, one namespace.
    assert manifest.api is not None and manifest.api.prefix == "/reference"
    assert manifest.ws is not None and manifest.ws.namespace == "/reference"

    # Exactly one scope, rooted at this mod's own scope root.
    assert len(manifest.scopes) == 1
    (scope,) = manifest.scopes
    assert scope.id == "mods.reference.use"
    assert naming.is_scope_in_namespace(scope.id, manifest.id)

    # The one table's prefix is the derived value, never freely chosen (R5-1).
    assert manifest.db is not None
    assert manifest.db.table_prefix == naming.table_prefix_for("reference")
    assert manifest.db.table_prefix == "mod_reference_"


# --- T-A07: the reference mod declares a valid frontend block (frontend-api R6 AC1)
@pytest.mark.tier0
def test_reference_manifest_declares_a_valid_frontend_nav_block():
    """The real mod.yaml on disk declares a `frontend` block that passes full
    semantic validation (frontend-api R1 + R6 AC1): a view id/path, a nav label,
    an icon, an add_to_nav flag, and a gating scope rooted at `mods.reference`.

    The block loads through the SAME `load_manifest` path that runs at boot, so a
    malformed block would refuse the whole mod -- this asserts it does not.
    """
    manifest = load_manifest(MANIFEST_PATH, core_version=CORE)

    assert manifest.frontend is not None
    fe = manifest.frontend
    assert fe.view == "reference-home"
    assert fe.label == "Reference"
    assert fe.icon == "puzzle"
    assert fe.add_to_nav is True
    assert fe.bundle_url == "/static/mods/reference/index.html"

    # The gating scope is rooted at this mod's OWN namespace -- an ordinary scope
    # the mod already declares (`mods.reference.use`), no new gating mechanism.
    assert fe.scopes == ["mods.reference.use"]
    assert all(naming.is_scope_in_namespace(s, manifest.id) for s in fe.scopes)

    # `tag` is left undeclared, so it derives from the mod id in the single place
    # the rule lives -- resolving to "mod-reference".
    assert fe.tag is None
    assert manifest.custom_element_tag() == naming.custom_element_tag_for("reference")
    assert manifest.custom_element_tag() == "mod-reference"


@pytest.mark.tier0
def test_reference_entrypoint_string_points_at_the_scaffold():
    """The manifest's entrypoint resolves to the module:attribute T-002 built."""
    manifest = load_manifest(MANIFEST_PATH, core_version=CORE)
    assert manifest.entrypoint == "mods.reference.reference_mod:Mod"


# --- T-002: the entrypoint scaffold ---------------------------------------
@pytest.mark.tier0
def test_reference_entrypoint_exposes_all_four_hooks():
    """The Mod object imports cleanly and exposes the four contract hooks.

    Verified through the loader's own resolver so the shape the loader requires
    is the shape asserted, not a hand-read approximation of it.
    """
    from selfai_ui.mods.loader import CONTRACT_HOOKS, resolve_entrypoint

    manifest = load_manifest(MANIFEST_PATH, core_version=CORE)
    obj = resolve_entrypoint(manifest)

    for hook in CONTRACT_HOOKS:
        assert callable(getattr(obj, hook, None)), f"missing hook {hook!r}"

    # The hooks return the shapes the loader consumes: a list of tools, a dict
    # of lifecycle callbacks. register_routers/register_ws are called for side
    # effects and return None. register_tools now returns the one real tool
    # (T-003); register_lifecycle's populated shape (startup/shutdown callables)
    # is asserted in detail in the T-006 section below.
    tools = obj.register_tools()
    assert isinstance(tools, list) and len(tools) == 1
    assert isinstance(obj.register_lifecycle(), dict)


@pytest.mark.tier0
def test_reference_state_store_chains_write_to_read():
    """The shared store is the substrate for R1: write is observable on read.

    T-002 only builds the store; the tool/namespace/route wire to it later. This
    asserts the store itself supports the chain a later task depends on.
    """
    from mods.reference.reference_mod import STATE

    STATE.reset()
    assert STATE.latest() is None
    assert STATE.snapshot() == {"latest": None, "count": 0}

    written = STATE.record("task-1", "accepted")
    assert written == {"task_id": "task-1", "status": "accepted"}
    assert STATE.latest() == {"task_id": "task-1", "status": "accepted"}
    assert STATE.snapshot() == {"latest": {"task_id": "task-1", "status": "accepted"}, "count": 1}

    STATE.reset()


# --- T-003: the async-handle tool -----------------------------------------
def test_register_tools_returns_exactly_one_valid_tool():
    """register_tools yields one tool that passes registration-time validation.

    `validate_mod_tools` is what the loader runs on whatever register_tools
    returns; running it here proves the returned dict coerces to a `ModTool` and
    clears both gates that live in it -- the required-scope check and the
    `TOOL_NAME_PATTERN` name check. That is the unit-level stand-in for the real
    boot-path registration T-011 proves against the running instance.
    """
    from mods.reference.reference_mod import Mod
    from selfai_ui.mods.tools import TOOL_NAME_PATTERN, ModTool, validate_mod_tools

    declared = Mod().register_tools()
    assert isinstance(declared, list) and len(declared) == 1

    (tool,) = validate_mod_tools(declared)
    assert isinstance(tool, ModTool)

    # Name validates at registration rather than at a provider's wire (R2-1).
    assert TOOL_NAME_PATTERN.match(tool.name)

    # Gated on exactly the scope the manifest declares (R1-5).
    assert tool.scope == "mods.reference.use"

    # The async-handle output shape (R2-2): a {task_id, status} JSON schema.
    assert tool.output_schema == {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}},
        "required": ["task_id", "status"],
    }


def test_tool_handler_returns_a_handle_and_writes_state():
    """Invoking the handler returns a {task_id, status} handle and writes STATE.

    Proves the write-path wiring (tool -> shared store) the namespace (T-004) and
    the route (T-005) later read back, and that the returned value is the very
    handle recorded -- the async-handle shape, produced without blocking for
    external work. The full real-assembly proof is T-011's job.
    """
    from mods.reference.reference_mod import STATE, Mod
    from selfai_ui.mods.tools import validate_mod_tools

    STATE.reset()
    (tool,) = validate_mod_tools(Mod().register_tools())

    result = tool.handler()
    assert set(result) == {"task_id", "status"}
    assert isinstance(result["task_id"], str) and result["task_id"]
    assert result["status"] == "submitted"

    # The handler's return is exactly what it recorded (R1-7 substrate).
    assert STATE.latest() == result
    assert STATE.snapshot() == {"latest": result, "count": 1}

    STATE.reset()


# --- T-005: register_routers reads state back -----------------------------
@pytest.mark.tier0
def test_reference_route_returns_state_snapshot():
    """register_routers mounts one GET route that returns STATE.snapshot().

    Proven on a throwaway app holding only this router, mounted under the
    manifest's `/reference` prefix the way the loader mounts it -- the full
    live-boot proof through selfai_ui.main.app is T-010. The route's
    get_verified_user dependency is overridden so this asserts the route's read
    path, not core's auth wiring (that is the next test).
    """
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from mods.reference.reference_mod import STATE, Mod
    from selfai_ui.modapi import get_verified_user

    STATE.reset()

    router = APIRouter()
    Mod().register_routers(router)

    app = FastAPI()
    app.dependency_overrides[get_verified_user] = lambda: {"id": "test-user"}
    app.include_router(router, prefix="/reference")
    client = TestClient(app)

    # Before any tool ran: an empty snapshot, still served.
    resp = client.get("/reference/state")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"latest": None, "count": 0}

    # A tool-equivalent write, then the same route reads that state back -- the
    # R1 chain a never-subscribed client observes purely by requesting the route.
    STATE.record("task-42", "accepted")
    resp = client.get("/reference/state")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"latest": {"task_id": "task-42", "status": "accepted"}, "count": 1}

    STATE.reset()


@pytest.mark.tier0
def test_reference_route_requires_authentication():
    """The route depends on get_verified_user, so an anonymous request is refused.

    Not scope-gated -- the scope gates the tool (R3), not this route -- but not
    anonymous either. With no dependency override and no token, core's
    get_current_user rejects the request rather than serving state.
    """
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from mods.reference.reference_mod import Mod

    router = APIRouter()
    Mod().register_routers(router)

    app = FastAPI()
    app.include_router(router, prefix="/reference")
    client = TestClient(app)

    resp = client.get("/reference/state")
    assert resp.status_code in (401, 403), resp.text


# --- T-006: the lifecycle hooks (R1 criterion 6, R4 criterion 3) -----------
@pytest.mark.tier0
def test_register_lifecycle_returns_sync_startup_and_shutdown():
    """The shape the loader consumes: a dict with `startup` and `shutdown`.

    Both are called synchronously by the loader (`startup()` inline at boot,
    `shutdown()` via `run_shutdown_hooks`) and never awaited, so neither may be
    a coroutine function -- a coroutine would return an un-awaited coroutine and
    silently do nothing.
    """
    import asyncio

    from mods.reference.reference_mod import Mod

    hooks = Mod().register_lifecycle()
    assert set(hooks) == {"startup", "shutdown"}
    assert callable(hooks["startup"]) and callable(hooks["shutdown"])
    assert not asyncio.iscoroutinefunction(hooks["startup"])
    assert not asyncio.iscoroutinefunction(hooks["shutdown"])


@pytest.mark.tier0
def test_startup_resets_state_and_survives_no_running_loop():
    """Startup guarantees a clean store at boot (R1 criterion 6) and must not
    raise when called outside a running event loop -- the emit demonstration is
    simply skipped, the observable reset still happens."""
    from mods.reference.reference_mod import STATE, Mod

    STATE.record("stale", "accepted")
    assert STATE.latest() is not None

    Mod().register_lifecycle()["startup"]()  # no running loop here

    assert STATE.latest() is None
    assert STATE.snapshot() == {"latest": None, "count": 0}


@pytest.mark.tier0
def test_startup_issues_the_context_free_emit_through_the_facade():
    """R4 criterion 3: the emit is issued from a lifecycle callback -- no request,
    chat, or sid in scope -- via the facade's `emit_to_user`, targeting a user by
    id alone with the mod's one event type and namespace.

    Startup no longer calls `emit_to_user` directly -- it calls the module-level
    `emit_state_update`, the same call `register_ws`'s on-subscribe pull makes, so
    the two push sites cannot drift onto different event names (they briefly did,
    reconciled during the Tier 1 merge: see `impl-mods-reference-implementation.md`).
    `emit_to_user` is imported once at module load in `reference_mod`, not inside
    the hook, so the spy patches that module-level name directly -- patching
    `modapi.emit_to_user` would not reach it, since the binding was already
    resolved at import time.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from mods.reference import reference_mod
    from mods.reference.reference_mod import REFERENCE_NAMESPACE, REFERENCE_STATE_EVENT, STATE, Mod

    STATE.reset()
    spy = AsyncMock()

    async def scenario():
        with patch.object(reference_mod, "emit_to_user", spy):
            hooks = Mod().register_lifecycle()
            hooks["startup"]()  # schedules the emit onto THIS running loop
            await asyncio.sleep(0)  # let the scheduled task run

    asyncio.run(scenario())

    spy.assert_awaited_once()
    args, kwargs = spy.call_args
    assert args[0] is None  # user id: no user connected at boot -> the no-op case
    assert args[1] == REFERENCE_STATE_EVENT  # the one event type -- shared with register_ws
    assert args[2] == {"latest": None, "count": 0}  # the post-reset snapshot
    assert kwargs["namespace"] == REFERENCE_NAMESPACE  # the mod's declared namespace


@pytest.mark.tier0
def test_shutdown_is_non_destructive_state_survives_drain():
    """R6 requires the mod's data to survive a restart, so shutdown must never
    clear STATE -- an orderly drain leaves the last handle intact."""
    from mods.reference.reference_mod import STATE, Mod

    STATE.reset()
    STATE.record("task-9", "done")

    Mod().register_lifecycle()["shutdown"]()

    assert STATE.latest() == {"task_id": "task-9", "status": "done"}
    STATE.reset()
