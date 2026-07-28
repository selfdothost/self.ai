"""GET /api/v1/mods/enabled -- the mod registry.

A thin adapter: authenticate the caller, read the loaded mods off app state,
evaluate visibility through the registry module, return it. All of the
interesting rules live in `selfai_ui.mods.registry`.

Cavekit: cavekit-mods-registry.md R1 -- T-050
"""

from fastapi import APIRouter, Depends, Request

from selfai_ui.mods.registry import visible_mods
from selfai_ui.utils.access_control import has_permission
from selfai_ui.utils.auth import get_admin_user, get_verified_user

router = APIRouter()


def _loaded_manifests(request: Request):
    load_result = getattr(request.app.state, "MODS", None)
    if load_result is None:
        return []
    # LoadResult.loaded is dict[id -> LoadedMod]; callers want manifests.
    return [mod.manifest for mod in load_result.loaded.values()]


@router.get("/enabled")
async def list_enabled_mods(request: Request, user=Depends(get_verified_user)):
    """The single source of truth a client consumes to decide what mod surfaces
    to load, filtered by the calling user's scopes."""
    defaults = request.app.state.config.USER_PERMISSIONS or {}

    return visible_mods(
        user,
        _loaded_manifests(request),
        has_permission=has_permission,
        defaults=defaults,
    )


@router.get("/scopes")
async def list_mod_scopes(request: Request, user=Depends(get_admin_user)):
    """Every declared scope of every loaded mod, with its description,
    regardless of current grant state.

    `GET /enabled` deliberately reports only scopes the caller already holds
    (self.ai#69's premise: an admin needs to see and grant a mod's *ungranted*
    scopes too, not just confirm ones already held). This is the admin-only
    surface for that -- consumed by the permissions editor to render one
    toggle per declared scope, labelled with `desc`.
    """
    return [
        {
            "id": manifest.id,
            "name": manifest.name,
            "scopes": [{"id": scope.id, "desc": scope.desc} for scope in manifest.scopes],
        }
        for manifest in _loaded_manifests(request)
        if manifest.scopes
    ]
