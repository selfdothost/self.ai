"""Mod scopes: registration, deny-by-default seeding, and enforcement.

The whole design rests on one property of core's existing checker:
`has_permission(user_id, key, defaults)` splits a dotted key and walks a nested
boolean dict of the user's group permissions, falling back to instance
defaults. A scope named `mods.crew.session.connect` is therefore evaluated by
exactly the same code path as `studio.models`, with **no change to that
function and no mods-specific branch anywhere in it**.

That is not a convenience. If mod scopes needed their own resolver, they would
be a second permission concept that could drift from the first -- granting
correctly in one and not the other. Being literally the same object is what
makes "indistinguishable from a built-in permission" true rather than aspirational.

How a mod enforces a scope
--------------------------
It calls the checker, through the facade, with the instance defaults. There is
deliberately no enforcement helper in this module: everything here is reachable
only via `selfai_ui.mods.*`, which is outside `selfai_ui.modapi` -- so a helper
living here would be one a mod could not legally import, and providing it would
invite the internal import the facade exists to prevent.

    from selfai_ui.modapi import get_verified_user, has_permission, permission_defaults

    @router.get("/thing")
    def thing(request: Request, user=Depends(get_verified_user)):
        # On a route, either source works -- they are the same object.
        defaults = request.app.state.config.USER_PERMISSIONS
        if not has_permission(user.id, "mods.example.thing.read", defaults):
            raise HTTPException(status_code=403, detail="...")

Off any request -- a Socket.IO handler, a watch loop -- `permission_defaults()`
is the supported source for that third argument. Always pass it: it defaults to
`{}`, and omitting it denies every scope granted through instance defaults
rather than through a group, silently and in the direction that reads as a
correct refusal.

(This module previously carried `require_scope()` and `check_scope()` for this.
Neither was ever called by core, the reference mod, or the crew mod, and
`require_scope` did not work: its returned dependency took an un-annotated
`user=None`, which FastAPI binds as a query parameter rather than injecting the
`get_user` it was handed and never called, so it refused every caller; it also
passed `{}` for the defaults -- the same bug fixed in `tools.py` as F-005.
Removed rather than repaired: a broken helper no mod may import is a trap, and
the two-line call above is what both real mods already do.)

Cavekit: cavekit-mods-permissions.md R1, R2 -- T-043, T-045
"""

import logging

from selfai_ui.mods.manifest import ModManifest
from selfai_ui.mods.naming import MOD_SCOPE_ROOT

log = logging.getLogger(__name__)


def scope_segments(scope_id: str) -> list[str]:
    """The dotted key split the way the checker splits it."""
    return scope_id.split(".")


def seed_defaults(defaults: dict, manifest: ModManifest) -> dict:
    """Return `defaults` with this mod's scopes present and **denied**.

    Enabling a mod grants nothing to anyone. The scopes become *grantable* --
    they exist in the tree so an admin can see and toggle them -- but every one
    starts false. An operator installing a mod is not thereby handing its
    capabilities to every user on the instance.

    Existing values are never overwritten: re-enabling a mod must not silently
    revoke a grant an admin made deliberately, which is the same reasoning that
    keeps grants intact across a disable.
    """
    updated = dict(defaults)

    for scope in manifest.scopes:
        node = updated
        segments = scope_segments(scope.id)
        for segment in segments[:-1]:
            existing = node.get(segment)
            node[segment] = dict(existing) if isinstance(existing, dict) else {}
            node = node[segment]

        leaf = segments[-1]
        if leaf not in node:
            node[leaf] = False

    return updated


def declared_scope_ids(manifests) -> list[str]:
    """Every scope declared by the given mods, sorted."""
    return sorted(scope.id for manifest in manifests for scope in manifest.scopes)


def strip_mod_scopes(defaults: dict, mod_id: str) -> dict:
    """Remove one mod's scope subtree from a defaults object.

    Used when presenting the *runtime* default set for a mod that is no longer
    enabled. It does not touch persisted grants -- a disabled mod's grants stay
    exactly where they are, so re-enabling does not silently drop them.
    """
    updated = dict(defaults)
    root = updated.get(MOD_SCOPE_ROOT)
    if isinstance(root, dict) and mod_id in root:
        root = dict(root)
        root.pop(mod_id, None)
        updated[MOD_SCOPE_ROOT] = root
    return updated
