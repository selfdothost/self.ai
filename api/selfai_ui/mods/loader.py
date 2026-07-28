"""Turning a validated, enabled manifest into a mounted mod.

The organising rule is **fail before you attach**. Everything that can fail --
entrypoint resolution, tool validation, router assembly onto a staging router,
lifecycle startup -- runs before anything touches the shared application. Only
once those succeed are the two irreversible steps taken, in the order that makes
their irreversibility safe: mount the router, then register the ws namespace
last, because a namespace cannot be cleanly un-registered from the shared
Socket.IO server. If ws registration itself raises, its partial state is rolled
back explicitly (the namespace handlers entry is deleted). A mod that fails
before the attach point leaves nothing behind.

The second rule is that no mod can take the instance down. Entrypoint
resolution, every registration hook, every lifecycle hook, and every request
into a mod route are individually contained. A mod failing is an error in the
log and one absent surface, never a boot failure and never a 500 for somebody
else's request.

Cavekit: cavekit-mods-loader.md R1-R8 -- T-025, T-027, T-029, T-031, T-033,
T-035, T-036, T-038
"""

import logging
import sys
from dataclasses import dataclass, field
from importlib import import_module

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException  # the BASE class -- fastapi.HTTPException subclasses it

from selfai_ui.mods.manifest import ModManifest
from selfai_ui.socket.main import install_namespace_auth, sio

log = logging.getLogger(__name__)

#: The hooks a mod may expose. All optional -- a mod contributing only tools is
#: as valid as one contributing only routes -- but an object exposing none of
#: them is not a mod, and saying so at load time beats a silent no-op.
CONTRACT_HOOKS = ("register_routers", "register_ws", "register_tools", "register_lifecycle")

LOG_NAMESPACE = "selfai_ui.mods"


class ModLoadError(Exception):
    """A mod could not be loaded. Never escapes the loader."""


def mod_logger(mod_id: str) -> logging.Logger:
    """The logger a mod's records are attributed to.

    Derived from the id so that loader-emitted records about a mod and
    mod-emitted records share a namespace, and two mods are always
    distinguishable. Core records never carry it.
    """
    return logging.getLogger(f"{LOG_NAMESPACE}.{mod_id}")


def _ensure_importable(directory) -> None:
    """Make a mod's own install directory importable, if it isn't already.

    Discovery finds a mod's `mod.yaml` by reading it directly off disk -- it
    never touches `sys.path`. But `resolve_entrypoint` below imports the mod's
    CODE with a bare `import_module(module_name)`, and Python's import system
    only ever consults `sys.path` (after the `sys.modules` cache). Without this,
    a mod dropped onto a volume via `MODS_EXTRA_DIRS` has a discoverable
    manifest but genuinely unimportable code -- the "drop a directory, flip
    `ENABLED_MODS`, no rebuild" story silently doesn't hold unless the deploy
    environment happens to also wire `PYTHONPATH`, which self.ai's own deploy
    manifests do not.

    Appended, not prepended: a mod's directory should be found for its OWN
    top-level module name, but should never be able to shadow something core
    or a dependency already resolves earlier on the path. Checked for prior
    presence so loading several mods across one boot, or in one test process,
    never grows `sys.path` with duplicate entries.
    """
    path_str = str(directory)
    if path_str not in sys.path:
        sys.path.append(path_str)


def resolve_entrypoint(manifest: ModManifest):
    """Resolve `module:attribute` to an object exposing the Mod contract.

    Checked *before* anything is invoked: an object that cannot satisfy the
    contract should be rejected while the consequence is still "this mod did
    not load" rather than a half-registered surface.
    """
    raw = manifest.entrypoint
    if ":" not in raw:
        raise ModLoadError(f"entrypoint {raw!r} must be of the form 'module:attribute'")

    module_name, _, attribute = raw.partition(":")

    # `manifest.directory` is `None` only for a manifest built directly (e.g. in
    # a test) rather than through discovery -- there is no on-disk location to
    # add, so importability is whatever the caller already arranged.
    if manifest.directory is not None:
        _ensure_importable(manifest.directory)

    try:
        module = import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - a mod's import may raise anything
        raise ModLoadError(f"cannot import {module_name!r}: {exc.__class__.__name__}: {exc}") from exc

    try:
        target = getattr(module, attribute)
    except AttributeError as exc:
        raise ModLoadError(f"{module_name!r} has no attribute {attribute!r}") from exc

    obj = target() if isinstance(target, type) else target

    if not any(callable(getattr(obj, hook, None)) for hook in CONTRACT_HOOKS):
        raise ModLoadError(
            f"{raw!r} exposes none of the mod contract hooks ({', '.join(CONTRACT_HOOKS)}); "
            f"it cannot contribute any surface"
        )

    return obj


def _normalise(prefix: str) -> str:
    return "/" + prefix.strip("/")


def prefix_conflict(candidate: str, existing: str) -> bool:
    """True when two route prefixes overlap.

    Overlap is not equality: `/crew` and `/crewing` are distinct, but `/crew`
    and `/crew/api` are not -- one shadows the other, and which one wins would
    depend on mount order. Ordering is never allowed to decide this, so both
    containment directions count as a conflict.
    """
    a, b = _normalise(candidate), _normalise(existing)
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def core_prefixes(app: FastAPI) -> list[str]:
    """Every path prefix core already serves, as mounted."""
    seen: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or not path.startswith("/"):
            continue
        seen.add("/" + path.strip("/").split("/")[0])
    return sorted(p for p in seen if p != "/")


@dataclass
class LoadedMod:
    """A mod that registered cleanly and is attached to the application."""

    manifest: ModManifest
    entrypoint: object
    router: APIRouter | None = None
    shutdown_hooks: list = field(default_factory=list)
    tools: list = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def directory(self):
        """The absolute on-disk directory this mod loaded from, or `None`.

        Carried on the manifest, which discovery stamped with the single install
        location it resolved (frontend-api R2). The per-mod asset and manifest
        surfaces (R3, R4) read it from the loaded record here.
        """
        return self.manifest.directory


@dataclass
class LoadResult:
    loaded: dict[str, LoadedMod] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def loaded_ids(self) -> list[str]:
        return sorted(self.loaded)


def isolating_route_class(mod_id: str) -> type[APIRoute]:
    """An APIRoute subclass that contains exceptions raised inside its handler.

    A custom route class is used rather than middleware because `APIRouter` has
    no middleware of its own -- Starlette middleware is application-scoped, so
    installing it per mod is not possible and attempting it silently does
    nothing. Overriding `get_route_handler` is the supported per-router hook.

    The response deliberately carries no exception text. A mod runs in-process
    with the API's privileges, so its tracebacks can name internal paths and
    state; the detail goes to that mod's log namespace where an operator can
    see it and a caller cannot.

    HTTPException and its kin are re-raised untouched: a mod returning 404 or
    403 deliberately is not a failure to contain.
    """
    logger = mod_logger(mod_id)

    class IsolatedRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def isolated(request: Request):
                try:
                    return await original(request)
                except (HTTPException, RequestValidationError):
                    raise
                except Exception:  # noqa: BLE001 - containment is the whole point
                    logger.exception("unhandled exception in mod route %s", request.url.path)
                    return JSONResponse(
                        status_code=500,
                        content={"detail": f"The '{mod_id}' mod failed to handle this request."},
                    )

            return isolated

    return IsolatedRoute


def load_mod(app: FastAPI, manifest: ModManifest, *, taken_prefixes: dict[str, str]) -> LoadedMod:
    """Resolve, register, and attach one mod. Raises ModLoadError on any failure.

    Nothing is attached to `app` until every hook has succeeded, so a failure
    here leaves the application exactly as it was.
    """
    logger = mod_logger(manifest.id)
    entrypoint = resolve_entrypoint(manifest)
    loaded = LoadedMod(manifest=manifest, entrypoint=entrypoint)

    # Tools are validated at registration: every declared tool must gate on a
    # scope, or the mod's tools are refused and the mod is disabled. This runs
    # before anything is attached, so a refusal leaves no partial surface.
    register_tools = getattr(entrypoint, "register_tools", None)
    if callable(register_tools):
        from selfai_ui.mods.tools import ModToolError, validate_mod_tools

        try:
            loaded.tools = validate_mod_tools(register_tools() or [])
        except ModToolError as exc:
            raise ModLoadError(str(exc)) from exc

    if manifest.api is not None:
        prefix = _normalise(manifest.api.prefix)
        for existing_prefix, owner in taken_prefixes.items():
            if prefix_conflict(prefix, existing_prefix):
                raise ModLoadError(
                    f"prefix {prefix!r} conflicts with {existing_prefix!r} (owned by {owner}); "
                    f"mount order must never decide which of two overlapping prefixes wins"
                )

        # The route class must be set at construction: routes capture it as
        # they are added, so installing isolation afterwards would miss every
        # route the mod already registered.
        staging = APIRouter(route_class=isolating_route_class(manifest.id))
        register_routers = getattr(entrypoint, "register_routers", None)
        if callable(register_routers):
            register_routers(staging)
        loaded.router = staging

    register_lifecycle = getattr(entrypoint, "register_lifecycle", None)
    if callable(register_lifecycle):
        hooks = register_lifecycle() or {}
        startup = hooks.get("startup") if isinstance(hooks, dict) else None
        shutdown = hooks.get("shutdown") if isinstance(hooks, dict) else None
        if callable(startup):
            startup()
        if callable(shutdown):
            loaded.shutdown_hooks.append(shutdown)

    # Mount the router FIRST, then register the ws namespace LAST. Registering a
    # namespace mutates the shared Socket.IO server and -- unlike a router --
    # cannot be un-done cleanly, so it must be the final mutation: nothing after
    # it may fail. (An earlier version ran include_router after register_ws,
    # which meant a router-mount failure could strand an orphaned namespace on
    # the shared server -- the "nothing after it can fail" claim was false.)
    if loaded.router is not None:
        app.include_router(loaded.router, prefix=_normalise(manifest.api.prefix), tags=[f"mod:{manifest.id}"])
        taken_prefixes[_normalise(manifest.api.prefix)] = f"mod {manifest.id!r}"

    # `sio.handlers` is the authoritative set of namespaces already registered
    # (core's default `/` plus any earlier mod's), so a collision -- including
    # with core's default namespace -- is refused before the hook runs and mount
    # order cannot decide the winner.
    register_ws = getattr(entrypoint, "register_ws", None)
    if callable(register_ws) and manifest.ws is not None:
        namespace = manifest.ws.namespace
        if namespace in sio.handlers:
            owner = "core" if namespace == "/" else "another mod"
            raise ModLoadError(
                f"ws namespace {namespace!r} is already registered (by {owner}); "
                f"mount order must never decide which of two mods owns a namespace"
            )
        # Auth-gate the namespace BEFORE the mod adds its own handlers, so the
        # namespace can never exist unauthenticated even briefly. If register_ws
        # then raises, roll the namespace back off the shared server -- socketio
        # has no unregister API, so we delete the handlers entry we created.
        install_namespace_auth(namespace)
        try:
            register_ws(sio)
        except Exception:
            sio.handlers.pop(namespace, None)
            raise

    logger.info("loaded")
    return loaded


def load_mods(app: FastAPI, manifests) -> LoadResult:
    """Load every discovered mod, containing each failure to its own mod."""
    result = LoadResult()
    taken: dict[str, str] = {p: "core" for p in core_prefixes(app)}

    for manifest in manifests:
        try:
            result.loaded[manifest.id] = load_mod(app, manifest, taken_prefixes=taken)
        except ModLoadError as exc:
            result.errors.append(f"mods: {manifest.id!r} disabled: {exc}")
        except Exception as exc:  # noqa: BLE001 - a mod may raise anything
            result.errors.append(f"mods: {manifest.id!r} disabled: {exc.__class__.__name__}: {exc}")

    for message in result.errors:
        log.error(message)

    return result


def boot_mods(app: FastAPI, install_dirs, enabled_ids, *, core_version: str) -> LoadResult:
    """Discover and load in one step -- everything a boot does, and only then.

    This exists so that "what happens at boot" has a single name. Enablement is
    a boot-time decision: the enabled list is read here, once. Editing that list
    afterwards changes what the *next* boot loads and has no effect on the
    running process -- no route appears, and no already-mounted route
    disappears. There is deliberately no way to ask this module to re-read the
    list, because a mod that could appear mid-flight would mean the surface an
    operator reviewed is not the surface that is serving.

    Cavekit: cavekit-mods-discovery.md R4 -- T-041; R2 scope seeding -- T-045;
    Loader R8 config attachment -- T-038.
    """
    from selfai_ui.mods.discovery import discover
    from selfai_ui.mods.modconfig import attach_mod_config
    from selfai_ui.mods.scopes import seed_defaults

    found = discover(install_dirs, enabled_ids, core_version=core_version)
    result = load_mods(app, [found.loaded[mod_id] for mod_id in found.loaded_ids])
    result.errors[:0] = found.errors

    config = getattr(app.state, "config", None)
    if config is not None:
        # Attach each loaded mod's declared configuration keys to `AppConfig`
        # (Loader R8). Without this the `config:` block of every manifest parsed,
        # validated, and then did nothing: `build_mod_config`/`attach_mod_config`
        # were unit-tested but called from nowhere, so a mod author following the
        # published contract got no value at runtime and fell back to reading raw
        # environment variables -- the exact "mod invents its own mechanism" the
        # requirement exists to prevent (found on self.crew#141's crew mod, which
        # reads CREW_RUNTIME_URL/CREW_RUNTIME_TOKEN directly for this reason).
        #
        # Same failure class as F-002 in the Phase 0 review: the unit was proven,
        # the assembly was never wired. The reference mod now declares a `config:`
        # block so this path is covered by a real boot, not only by unit tests.
        #
        # Contained per mod: a mod whose config cannot be attached is recorded and
        # skipped, never a boot failure, matching the isolation posture everywhere
        # else in this module. It attaches AFTER load, alongside scope seeding, so
        # a mod that failed to load contributes no config entries either.
        for mod in result.loaded.values():
            try:
                attach_mod_config(config, mod.manifest)
            except Exception as exc:  # noqa: BLE001 - config attachment may raise anything
                message = f"mods: {mod.id!r} configuration could not be attached: {exc.__class__.__name__}: {exc}"
                result.errors.append(message)
                log.error(message)

        # Seed each loaded mod's declared scopes deny-by-default into the instance
        # permission defaults, so they are grantable in the admin UI and enabling a
        # mod grants nothing to anyone. seed_defaults never overwrites an existing
        # value, so an admin's prior grant survives a restart. This is the boot half
        # of Permissions R2 -- without it a mod loads but its scopes are invisible.
        defaults = config.USER_PERMISSIONS or {}
        for mod in result.loaded.values():
            defaults = seed_defaults(defaults, mod.manifest)
        config.USER_PERMISSIONS = defaults

    # Publish the loaded set for the registry endpoint. Stored as the LoadResult
    # so the registry sees exactly what boot produced -- mods that failed to load
    # are not in `loaded` and therefore never appear in any response.
    app.state.MODS = result
    return result


def run_shutdown_hooks(result: LoadResult) -> None:
    """Drain every loaded mod. One hook raising must not skip the rest."""
    for mod in result.loaded.values():
        for hook in mod.shutdown_hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001
                mod_logger(mod.id).exception("shutdown hook raised")
