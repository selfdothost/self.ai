"""Per-mod static asset serving: `/static/mods/<id>/<path>`.

The API's first path-traversal-safe subtree server (frontend-api R3). It serves a
mod's built frontend assets off the on-disk directory the loaded-mod record
retained (R2), under a per-mod URL prefix distinct from core's three static
mounts (`main.py:1718-1740`). The traversal guard is `mods.assets.resolve_mod_asset`
(`resolve()` + `is_relative_to()`); this module is the thin HTTP adapter around it.

Path shape mirrors core's `/static` and `/cache` mount naming, adding a `mods/<id>`
subtree per mod. Because the route names the literal `mods` segment, it never
shadows the core `/static` mount (`/static/swagger-ui/...` falls through to it);
it is registered before that mount so `/static/mods/...` resolves here.

Content-hashed, immutable assets: every response under this surface carries a
long-lived immutable `Cache-Control`. Asset filenames are content-hashed by the
mod's own build convention, so the bytes at a given URL never change -- the
always-fresh indirection is the per-mod manifest endpoint (R4/T-A05), not this
surface. Serving assets immutably is this surface's whole job.

Enablement (frontend-api R3 AC5 -- a disabled mod serves nothing) is T-A04's
extension point: `_loaded_mod` already returns `None` for any mod not in the
loaded set, and boot only loads enabled mods, so a not-loaded mod is already a
not-found here. T-A04 hardens that into the explicit clean-disable posture.

Cavekit: cavekit-mods-frontend-api.md R3 -- T-A03 (AC 1,2,3,4,6; AC5 is T-A04)
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from selfai_ui.mods.assets import AssetResolutionError, resolve_mod_asset

router = APIRouter()

#: Long-lived immutable cache directive applied to every served asset. Assets are
#: content-hashed by the mod's build convention, so the bytes at a URL are stable
#: for the URL's lifetime; the R4 manifest is what stays fresh.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _loaded_mod(request: Request, mod_id: str):
    """The loaded-mod record for `mod_id`, or `None`.

    Reads `app.state.MODS` -- the LoadResult `boot_mods` publishes. Only enabled
    mods that loaded cleanly appear in `.loaded`, so a disabled, failed, or
    unknown mod is `None` here and the caller returns a not-found. This is the
    single place T-A04 extends for the explicit disabled-mod zero-surface check.
    """
    load_result = getattr(request.app.state, "MODS", None)
    if load_result is None:
        return None
    return load_result.loaded.get(mod_id)


@router.get("/static/mods/{mod_id}/{asset_path:path}")
async def serve_mod_asset(request: Request, mod_id: str, asset_path: str):
    """Serve one asset file from a mod's own directory, traversal-safe.

    Every failure -- unknown/not-loaded mod, no retained directory, path escape,
    or missing file -- is the same 404, so the surface discloses nothing about
    which mods exist or what lies outside a mod's directory.
    """
    mod = _loaded_mod(request, mod_id)
    if mod is None:
        raise HTTPException(status_code=404, detail="Not Found")

    directory = mod.directory
    if directory is None:
        # A loaded mod with no retained directory cannot serve assets. Serving
        # only from the R2-retained directory (R3 AC6): there is no other source.
        raise HTTPException(status_code=404, detail="Not Found")

    try:
        file_path = resolve_mod_asset(directory, asset_path)
    except AssetResolutionError:
        raise HTTPException(status_code=404, detail="Not Found")

    return FileResponse(file_path, headers={"Cache-Control": IMMUTABLE_CACHE_CONTROL})
