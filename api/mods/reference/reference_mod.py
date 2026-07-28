"""The reference mod's entrypoint: one `Mod` object exposing all four hooks.

This is the scaffold (T-002). The four hooks exist with the exact signatures
`mods/loader.py:load_mod` calls them with, and they wire to the shared state
store (`state.STATE`) that makes R1's coherent chain possible -- but the hook
BODIES are deliberately minimal here. Later tasks flesh them out against the
real running app:

  * register_tools     -> T-003 (the async-handle tool, writes STATE)
  * register_ws        -> T-004 (one namespace streaming STATE via emit_to_user)
  * register_routers   -> T-005 (one route reading STATE back)
  * register_lifecycle -> T-006 (startup/shutdown + a context-free emit)

The one invariant this file must hold from the start: every application-package
import resolves to `selfai_ui.modapi` (or a submodule) and nothing else -- so
the facade-boundary check (R7, T-008) flips from vacuous to real and passes.
The scaffold imports nothing from the application package yet because the stubs
need nothing from it; each later task adds only facade names.

How the loader drives this object (`mods/loader.py`):
  * `resolve_entrypoint` instantiates `Mod` (it is a class) and requires at
    least one contract hook be callable.
  * `register_tools()` is called with no arguments and its result passed through
    `validate_mod_tools` (a list of `ModTool` or dicts; `[]` is valid).
  * `register_routers(staging)` is called with a staging `APIRouter`.
  * `register_lifecycle()` is called with no arguments; a dict with optional
    `startup`/`shutdown` callables is read from it.
  * `register_ws(sio)` is called with core's Socket.IO server, last of all.

Cavekit: cavekit-mods-reference-implementation.md R1 -- T-002
"""

from __future__ import annotations

import uuid

# `Request` is imported at module scope, not inside `register_routers`, on
# purpose: `from __future__ import annotations` makes every annotation a string,
# and FastAPI resolves an endpoint's annotations against its function
# `__globals__` (the module namespace) -- a name imported only inside the hook
# would be invisible there, and FastAPI would mistake `request: Request` for a
# query parameter. `fastapi` is a third-party dependency, not the application
# package, so this import does not touch the `selfai_ui.modapi` facade boundary.
from fastapi import Request

from selfai_ui.modapi import emit_to_user

from .state import STATE

#: The scope this mod's tool gates on. Must equal the single scope the manifest
#: declares (`mod.yaml`: `mods.reference.use`, rooted at
#: `scope_root_for("reference")`). Stated once here so the tool and any later
#: hook that references the scope share one source of truth.
TOOL_SCOPE = "mods.reference.use"

#: The JSON-schema shape of what the tool's handler returns: the async-handle
#: `{task_id, status}` pattern `self.crew#141` asked core to confirm support for.
#: This value carries straight through `ModTool.output_schema` ->
#: `ToolSpec.output_schema` at real assembly (proven end to end in T-011); here
#: it is declared correctly so that proof is possible.
TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": ["task_id", "status"],
}

#: The one Socket.IO namespace this mod registers, equal to the manifest's
#: declared `ws.namespace`. The loader installs the connect-auth gate on this
#: exact string before `register_ws` runs; the handler and the emit path below
#: must name the same namespace or they would talk past core's gate.
REFERENCE_NAMESPACE = "/reference"

#: The one event type R4 pins: every push over the namespace -- the on-subscribe
#: pull and the context-free `emit_state_update` -- carries this event and the
#: same `STATE.snapshot()` payload, so a client wires one listener.
REFERENCE_STATE_EVENT = "reference:state"


def submit_task() -> dict:
    """Accept a unit of work and hand back a handle to track it -- do not wait.

    This is the async-handle shape, not a synchronous long-running call: it mints
    a task id, records `{task_id, status="submitted"}` in the shared store, and
    returns that same handle immediately. It never blocks for the "work" to
    finish -- a real mod would enqueue the job and later push progress over its
    namespace (the follow-up path R4/T-004 exercises); the reference mod records
    the handle and returns.

    The handler is intentionally SYNC. Core's dispatch invokes a mod tool as
    `handler(**model_args)` inside `mods/tools.py:_scope_guard.guarded`, which
    awaits the result only `if inspect.isawaitable(result)`. A plain dict return
    is therefore the right shape: there is no external await to make, so making
    the handler a coroutine would add a suspension point that does nothing. The
    `__`-prefixed injected params never reach here (the guard strips them), so
    this takes no arguments.
    """
    task_id = uuid.uuid4().hex[:12]
    return STATE.record(task_id, "submitted")


class Mod:
    """The reference mod. One object, four hooks, one shared state store."""

    def register_tools(self) -> list:
        """Return this mod's model-callable tools: exactly one, the submit tool.

        Returned as a plain dict rather than a `ModTool` instance on purpose. The
        loader passes this through `validate_mod_tools`, which coerces a dict via
        `ModTool(**raw)` -- so a dict is a first-class, supported input. It is
        also the only facade-clean option: `ModTool` lives in
        `selfai_ui.mods.tools`, which is NOT part of the `selfai_ui.modapi`
        facade (R7), so importing it into this mod's source would break the
        import-boundary check. A dict keeps the mod's application-package imports
        at zero.

        The tool declares its required scope (`mods.reference.use`) and an
        `output_schema` for the `{task_id, status}` handle its handler returns.
        `parameters` is None: submit takes no model arguments.
        """
        return [
            {
                "name": "submit",
                "description": (
                    "Submit a unit of work to the reference mod and receive a "
                    "{task_id, status} handle to track it. Returns immediately; "
                    "the work is not awaited."
                ),
                "handler": submit_task,
                "scope": TOOL_SCOPE,
                "parameters": None,
                "output_schema": TOOL_OUTPUT_SCHEMA,
            }
        ]

    def register_routers(self, router) -> None:
        """Mount this mod's one route onto the staging router.

        The loader passes a staging `APIRouter` and later mounts it under the
        manifest's `api.prefix` (`/reference`), so the path declared here is
        relative to that prefix: `GET /state` on the router becomes
        `GET /reference/state` once mounted. `/state` (not the router root `/`)
        is chosen so the served path is `/reference/state` rather than the
        trailing-slash-ambiguous `/reference/` -- a named sub-path reads clearly
        and matches the `installed_mod` boot fixture's `/ping`-under-prefix
        convention (`tests/routers/test_mods_boot_integration.py`).

        The handler returns `STATE.snapshot()` -- the plain, JSON-serializable
        `{"latest": <handle|None>, "count": <int>}` dict the tool writes into via
        `STATE.record`. This is R1's read path: a client that never subscribed to
        the namespace can still observe the tool's latest recorded state change by
        requesting this route after the tool ran.

        The route requires an authenticated user (any verified user) via the
        facade's `get_verified_user`, matching core's general read-endpoint
        posture -- state is not served to anonymous callers. It is deliberately
        NOT gated on the mod's `mods.reference.use` scope: in this kit the scope
        gates the TOOL (R3), and R1 asks only that the route exist and be mounted,
        not that it be scope-restricted. The facade import is the sole
        application-package import this hook needs, keeping the mod inside the
        `selfai_ui.modapi` boundary (R7).

        A SECOND route -- `POST /submit`, landing at `/reference/submit` under the
        prefix -- is a direct backend trigger for the same action the `submit`
        TOOL performs (frontend-client R7 AC5, gap-remediation T-A09). Phase 1
        exposed `submit` only as a model-callable tool dispatched through core's
        chat/tool-calling path (`assemble_for_user` -> the `_scope_guard`
        callable); there was no plain HTTP entry point for it, so the reference
        mod's status view had no backend surface to POST to. This route is that
        entry point and nothing more: it is a THIN WRAPPER over the two functions
        that already exist and already work -- it calls the module-level
        `submit_task()` (the exact function the tool's handler calls) to write the
        handle, then `emit_state_update(user.id)` (the exact context-free push the
        lifecycle callback uses) so the change streams live over the `/reference`
        namespace to the caller, and returns the `{task_id, status}` handle
        `submit_task()` produced for an immediate synchronous confirmation. It
        does NOT re-implement the tool, add a second scope, or touch the
        model-dispatch path.

        Unlike `GET /state`, this route IS scope-gated -- on the IDENTICAL scope
        the tool is gated on (`TOOL_SCOPE` == `mods.reference.use`), because it
        performs the same privileged action. The gate is the facade's
        `has_permission` -- the same permission checker core's own routers use and
        the same one the tool's `_scope_guard` re-evaluates at call time -- read
        against the live instance defaults (`request.app.state.config.USER_PERMISSIONS`),
        exactly as `_scope_guard` reads them. A caller lacking the scope is refused
        with a 403 naming the scope (a permissions denial, the HTTP analogue of the
        tool guard's `PermissionError`), NOT a technical failure. There is no admin
        bypass, mirroring the tool guard, which grants the action to holders of the
        scope only. `has_permission` is the second and last facade import this hook
        needs; the mod stays inside the `selfai_ui.modapi` boundary (R7).
        """
        from fastapi import Depends, HTTPException, status

        from selfai_ui.modapi import get_verified_user, has_permission

        @router.get("/state")
        def read_state(user=Depends(get_verified_user)) -> dict:
            return STATE.snapshot()

        @router.post("/submit")
        async def submit(request: Request, user=Depends(get_verified_user)) -> dict:
            # Same scope the tool is gated on, checked the same way the tool's
            # `_scope_guard` checks it: `has_permission` against the live instance
            # permission defaults. No admin bypass -- the tool grants this action
            # to scope holders only, and this route must not be more permissive.
            defaults = request.app.state.config.USER_PERMISSIONS
            if not has_permission(user.id, TOOL_SCOPE, defaults):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"this action requires the '{TOOL_SCOPE}' permission",
                )

            # Call the existing functions directly; do not duplicate their logic.
            handle = submit_task()
            # Stream the write live over the namespace to the calling user (and any
            # other subscribed session of theirs), so a subscribed client observes
            # the change without polling -- the same context-free push the lifecycle
            # callback issues.
            await emit_state_update(user.id)
            # Immediate synchronous confirmation: the {task_id, status} handle.
            return handle

        @router.get("/config")
        def read_config(request: Request, user=Depends(get_verified_user)) -> dict:
            # The READ half of the config surface (Loader R8), and the reason this
            # route exists at all: nothing in the tree demonstrated how a mod gets
            # at a resolved `config:` value, so the surface could be -- and was --
            # inert at boot without a single test failing.
            #
            # The read path is `app.state.config.MOD_<ID>_<KEY>`: core attaches
            # each declared key as a PersistentConfig under the attribute name
            # `modconfig.env_var_for(id, key)` derives, and `AppConfig.__getattr__`
            # returns the resolved `.value`. A mod names that derived attribute; it
            # does NOT read the environment itself, which is the whole point of the
            # key being declared in the manifest.
            #
            # `getattr` with a default rather than a bare attribute access:
            # `AppConfig.__getattr__` raises KeyError for an unattached key, and a
            # mod reporting its own configuration should say "absent" plainly
            # rather than raise into the loader's error isolation.
            app_config = request.app.state.config
            greeting = getattr(app_config, "MOD_REFERENCE_GREETING", None)
            api_key = getattr(app_config, "MOD_REFERENCE_API_KEY", None)
            # The secret is reported as set-or-not and never echoed. `redacted()`
            # covers the admin surface; a mod's own route simply must not return
            # the value.
            return {"greeting": greeting, "api_key_set": bool(api_key)}

        return None

    def register_ws(self, sio) -> None:
        """Register this mod's one Socket.IO namespace on core's existing server.

        `sio` is core's server, passed in by the loader -- `load_mod` calls
        `register_ws(sio)` last of all, and does so AFTER it has already installed
        the connect-auth gate on this namespace via `install_namespace_auth`. So
        this hook never touches auth: a client presenting no valid token is
        refused by core's gate before any handler here runs, with no
        reference-mod code involved (R4). The namespace registered is exactly the
        manifest's declared `ws.namespace` (`REFERENCE_NAMESPACE`), on core's
        server -- not a mod-owned websocket mount.

        The one handler lets a freshly-subscribed client pull the current shared
        state: on `"sync"` the mod emits the snapshot back to that one client's
        session. The push R4 turns on -- a context-free emit to a user with no
        request, no chat, and no sid -- is the module-level `emit_state_update`
        below, which T-003's tool handler and T-006's lifecycle callback call to
        stream a state change out through the facade's `emit_to_user`.

        `sio` is used directly (it is the argument the loader passes) rather than
        importing `sio` from the facade a second time: the facade re-exports the
        same object, but taking the passed-in server keeps this hook honest about
        registering on core's own server, which `test_mods_ws.py` asserts.
        """

        @sio.on("sync", namespace=REFERENCE_NAMESPACE)
        async def _sync(sid):
            # A client that just subscribed asks for the current state; push the
            # snapshot to that one session. No user-id resolution is needed here
            # -- the sid is the connection. The cross-session, context-free push
            # a mod's backend code issues is emit_state_update, not this.
            await sio.emit(REFERENCE_STATE_EVENT, STATE.snapshot(), to=sid, namespace=REFERENCE_NAMESPACE)

        return None

    def register_lifecycle(self) -> dict:
        """Supply the mod's startup and shutdown callbacks.

        The loader (`mods/loader.py:load_mod`) reads this dict once per enabled
        mod at boot: it calls `startup()` exactly once inline -- so "runs once at
        boot" is the loader's guarantee, not something this mod must enforce --
        and stashes `shutdown` to run on orderly drain via `run_shutdown_hooks`.
        Both callbacks are invoked SYNCHRONOUSLY (`startup()` and `hook()` are
        never awaited), so both must be plain, non-coroutine functions.

        Startup guarantees a clean state store at boot and demonstrates R4's
        context-free emit path by calling the module-level `emit_state_update`
        (built for exactly this purpose, see its docstring) -- the same call
        `register_ws`'s on-subscribe pull and a real reconciler-watch loop would
        make, issued here from a callback that has no request, chat, or sid in
        scope, only a user id. Shutdown is deliberately non-destructive: R6
        requires the mod's persisted data to survive a restart, so drain only
        logs and never clears STATE.

        Cavekit: cavekit-mods-reference-implementation.md R1 (criterion 6),
        R4 (criterion 3).
        """
        import asyncio
        import logging

        log = logging.getLogger("selfai_ui.mods.reference")

        def _startup() -> None:
            """Run once at boot (loader-guaranteed). Reset the store so the mod
            always boots from clean state, then fire the context-free emit once to
            prove the path is reachable from a lifecycle callback.

            The emit is scheduled onto the app's running event loop rather than
            awaited: this callback is synchronous but `emit_state_update` is a
            coroutine, and at boot `load_mod` runs inside `lifespan`'s loop. With
            no running loop (e.g. a unit test calling this directly) there is
            nothing to schedule onto, so the emit is skipped -- the state reset,
            the observable part, still happens. At boot no user is connected yet,
            so the demonstration targets `None` and is the documented no-op; a
            real reconciler would pass the user id it is reacting for.

            Calls `emit_state_update`, not a second hand-rolled `emit_to_user`
            call: the on-subscribe pull in `register_ws` and this startup push
            must stream the identical event name and namespace, and pinning that
            in one function (rather than two call sites agreeing by convention)
            is what keeps them from drifting apart.
            """
            STATE.reset()
            log.info("reference mod started; state store reset at boot")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(emit_state_update(None))

        def _shutdown() -> None:
            """Run on orderly drain. Intentionally non-destructive: the mod holds
            no external resource to release, and R6 requires its data to survive a
            restart, so this must never clear STATE -- it only records the drain.
            """
            log.info("reference mod draining; state store left intact for restart")

        return {"startup": _startup, "shutdown": _shutdown}


async def emit_state_update(user_id: str) -> None:
    """Push the current shared state to one user over the reference namespace.

    This is R4's context-free emit path: it needs only a user id -- no request,
    no chat, no session id -- because the user-to-session mapping it depends on is
    reached through the facade's `emit_to_user`, never raw `sio` targeting (raw
    `sio` cannot resolve a user to their sessions; that mapping is internal). A
    user with no active session is a no-op, not an error -- that is
    `emit_to_user`'s own contract, not something re-implemented here.

    Exposed at module level, deliberately, so the two backend call sites that
    produce a state change can trigger the stream without holding a `Mod`
    instance and without a request context:

      * T-003's tool handler, after it records a `{task_id, status}` handle, may
        `await emit_state_update(user_id)` to stream the change it just wrote;
      * T-006's lifecycle callback issues the mod's context-free emit at boot the
        same way.

    Both call `emit_state_update(user_id)` and nothing else -- they do not import
    `emit_to_user`, pick an event name, or shape a payload; those are pinned here
    so the on-subscribe pull (`register_ws`'s `"sync"` handler) and this push stay
    one event type with one payload shape.
    """
    await emit_to_user(user_id, REFERENCE_STATE_EVENT, STATE.snapshot(), namespace=REFERENCE_NAMESPACE)


#: Re-exported so later hook tasks and tests share the one process-wide store
#: through the entrypoint module rather than reaching for `.state` directly.
#: `emit_state_update` (with the namespace/event constants it uses) is the
#: concrete call site T-003 and T-006 target to stream a state change.
__all__ = [
    "Mod",
    "STATE",
    "REFERENCE_NAMESPACE",
    "REFERENCE_STATE_EVENT",
    "emit_state_update",
]
