"""The supported import surface for mods.

A mod imports from `selfai_ui.modapi` and from nowhere else in this package.
Everything else under `selfai_ui` is internal and may be renamed, moved, or
rewritten without notice.

Why this exists
---------------
self.ai is mid-way through separating itself from its Open-WebUI origins. The
moment a real mod ships against core's internals, core stops being able to move
them -- the fork's inherited shape would be welded in place by things outside
this repository depending on it. Drawing the boundary before the first mod
exists is the cheapest this will ever be.

What is promised
----------------
The names in `__all__` below are the compatibility surface. Within a major
version of core they keep working. `min_core_version` in a mod's manifest is a
claim about *this* surface and nothing else -- core refuses at load time to run
a mod whose `min_core_version` it does not satisfy, rather than letting it load
and fail later somewhere unrelated.

Concretely: the running core version is `CORE_VERSION` (read from the single
declared location, `selfai_ui.env.VERSION`). A mod declaring a
`min_core_version` with the same major and a minor at or below the running
core's is supported. Names may be *added* to `__all__` in a minor release;
a name is only ever removed or renamed in a major one, and
`tests/test_mods_facade_boundary.py` fails the build if that rule is broken.

Adding to this surface is a decision, not a convenience. Every name here is a
promise that outlives the reason it was added.

What is NOT promised
--------------------
**This boundary is not enforced for third-party mods, and must not be presented
as if it were.** A mod is trusted code running in the API process with the API's
privileges; it can import whatever it likes and nothing at runtime stops it. The
automated check enumerates imports for mods *in this repository* only. For a mod
you did not write, the boundary is an install-time review obligation -- read its
imports before you enable it.

Cavekit: cavekit-mods-facade.md R1, R3 -- T-005, T-006
"""

from selfai_ui import config as _config
from selfai_ui.env import VERSION as _CORE_VERSION
from selfai_ui.internal.db import get_db
from selfai_ui.socket.main import emit_to_user, get_user_id_from_session_pool, sio
from selfai_ui.utils.access_control import has_permission
from selfai_ui.utils.auth import get_verified_user
from selfai_ui.utils.tools import get_tools_specs
from selfai_ui.utils.toolspec import ToolSpec as _ToolSpec


def permission_defaults() -> dict:
    """The live instance default permission tree, for a scope check off the request path.

    `has_permission(user_id, key, default_permissions)` resolves a scope against
    the caller's group permissions and then falls back to this tree. Its third
    argument defaults to `{}`, so a two-argument call **denies every scope an
    admin granted through instance defaults rather than through a group** -- it
    fails closed, which is the right direction but the wrong answer, and it does
    so silently.

    On an HTTP path a mod avoids that by reading
    `request.app.state.config.USER_PERMISSIONS`. A Socket.IO handler has no
    request, and before this the facade exposed no other source -- so a mod
    gating a namespace on instance-default permissions had to import
    `selfai_ui.config` directly, which is the internal import the facade exists
    to prevent. Found on `self.crew#141`, where the crew mod pinned exactly that
    import and flagged it.

    This returns the same object that request-scoped read returns: `main.py`
    assigns the module-level `USER_PERMISSIONS` onto `app.state.config`, so both
    names resolve to one `PersistentConfig`, and a grant is visible through
    either.

    Read on every call and never cached: an admin can grant a scope at any time,
    and a long-lived socket must not answer from a boot-time snapshot. Returns
    `{}` if the value is missing or not a mapping -- the same shape
    `has_permission` defaults to, so the failure mode is "denies".

    Typical use, off any request::

        if not has_permission(user_id, "mods.example.thing.read", permission_defaults()):
            ...
    """
    value = getattr(getattr(_config, "USER_PERMISSIONS", None), "value", None)
    return value if isinstance(value, dict) else {}

#: One model-callable tool specification: a validated, provider-neutral model.
#:
#: This was `dict[str, Any]` for as long as core's own spec was untyped. A mod's
#: specs must be indistinguishable from a user-authored toolkit's -- the model
#: must not be able to tell them apart -- so the facade tracked core rather than
#: inventing a stricter type of its own. Core's spec now *is* typed
#: (`selfai_ui.utils.toolspec.ToolSpec`), which is exactly the landing the alias
#: was a placeholder for: the name promised here is unchanged, so a mod picks the
#: real model up without editing its imports.
#:
#: The contract: `name` (required), `description`, `input_schema` -- a plain
#: JSON-Schema dict, defaulting to the empty object -- and optional `title` and
#: `output_schema`. Field names follow MCP, the superset of the provider
#: formats, in snake_case. `extra` is forbidden, so a misspelt key is a
#: validation error rather than a field that silently vanishes before the model
#: ever sees it. Rendering to a particular provider's wire format is an edge
#: concern; a mod returns the neutral model and does not pick a dialect.
#:
#: `selfai_ui.utils.toolspec` is deliberately import-light -- pydantic and
#: stdlib only, no DB layer and no FastAPI -- so this re-export costs nothing on
#: its own. That is a property of the model's module, not of this facade:
#: importing `modapi` already pulls FastAPI and SQLAlchemy in through `get_db`,
#: `get_verified_user`, `sio`, and `get_tools_specs`, and did so before
#: `ToolSpec` was typed. A mod that wants the spec type and nothing else can
#: import `selfai_ui.utils.toolspec` directly.
ToolSpec = _ToolSpec

#: The running core version, read from the single declared location
#: (`selfai_ui.env.VERSION`). This is the value a manifest's `min_core_version`
#: is compared against; the comparison itself lives with the manifest loader.
CORE_VERSION = _CORE_VERSION

#: The declared export list. Enumerable by an automated check -- this is the
#: contract, not an implicit "whatever happens to be importable".
#:
#: Keep this list minimal. Every name added here is a promise; a name removed
#: or renamed without a major-version increment is a broken one.
__all__ = [
    # Identity / auth -- usable as a route dependency by a mod router.
    "get_verified_user",
    # Permission checking -- the same checker core uses for enforcement, so a
    # mod's scope decisions cannot drift from core's.
    "has_permission",
    # The instance default permission tree `has_permission` falls back to.
    # Needed wherever there is no request to read it off app state -- a Socket.IO
    # handler, a watch loop -- because omitting it silently denies every
    # default-granted scope.
    "permission_defaults",
    # The authenticated user behind a Socket.IO connection. Core auth-gates every
    # mod namespace on connect and records the identity; this is how a mod's
    # handler reads it back, sid -> user id. Without it a namespace handler
    # cannot do an ownership check against the facade.
    "get_user_id_from_session_pool",
    # Database access.
    "get_db",
    # The event/emit path: core's Socket.IO server. Mods register a namespace
    # on it rather than mounting a websocket stack of their own.
    "sio",
    # Emit to a specific user from outside any request context, without knowing
    # session ids. Raw `sio` cannot target a user -- the user-to-session mapping
    # is internal -- so this is the supported way for a mod's watch loop to push
    # to a user.
    "emit_to_user",
    # Tool specifications, in the same shape core builds for user-authored
    # toolkits.
    "ToolSpec",
    "get_tools_specs",
    # Compatibility.
    "CORE_VERSION",
]
