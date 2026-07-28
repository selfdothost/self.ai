"""Per-mod frontend manifest: `GET /api/v1/mods/{mod_id}/frontend-manifest`.

The small, always-fresh indirection that resolves a mod id to its CURRENT
content-hashed bundle entry URL (frontend-api R4). This is the versioned-manifest
half of the "immutable bundle filename + no-cache pointer" pattern: T-A03's asset
surface serves the hashed bundle immutably, and THIS endpoint is the one thing
that must stay fresh so a client picks up a mod update -- without a self.chat
redeploy -- by re-fetching this pointer on each view-entry
(`cavekit-mods-frontend-client.md` R2).

It is a THIRD, purpose-built object -- distinct from `mod.yaml` and from the
registry response (`GET /api/v1/mods/enabled`, `cavekit-mods-registry.md` R1). Its
payload names only what a loader needs to resolve and mount the bundle -- the mod
id, its current bundle URL, the custom-element tag, and a status -- and carries no
configuration values and no secrets (R4 AC4).

Freshness: every response carries `no-cache`/revalidating headers (R4 AC2), and
the bundle filename is discovered by reading the mod's directory anew on each
request (`discover_bundle_entry`), so a build that swaps the hashed filename is
observed on the next fetch (R4 AC3).

Zero surface when disabled: a mod not in the loaded set (disabled, failed, or
unknown) returns the same not-found as an unrouted path (R4 AC5), matching the
asset surface and `cavekit-mods-discovery.md` R2 -- the endpoint discloses nothing
about a mod it did not load.

Named no-bundle condition: a mod that IS loaded and DOES declare a `frontend`
block but ships no built entry yet is a distinguishable, named state
(`status: "no_bundle"`, `bundle_url: null`) -- never a 200 pointing at a missing
file (R4 AC6). The reference mod is exactly this until T-C08 compiles its bundle.

Cavekit: cavekit-mods-frontend-api.md R4 -- T-A05
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from selfai_ui.mods.assets import discover_bundle_entry
from selfai_ui.utils.auth import get_verified_user

router = APIRouter()

#: Revalidating cache directive on every manifest response. The bundle it points
#: at is immutable and cached long-lived (T-A03); this pointer is the ONLY thing
#: that must never be served stale, so a client re-fetch after a mod update
#: observes the new bundle URL rather than a cached old one (R4 AC2).
NO_CACHE_CONTROL = "no-cache, no-store, must-revalidate"


def _loaded_mod(request: Request, mod_id: str):
    """The loaded-mod record for `mod_id`, or `None`.

    Reads `app.state.MODS` -- the LoadResult `boot_mods` publishes. Only enabled
    mods that loaded cleanly appear in `.loaded`, so a disabled, failed, or
    unknown mod is `None` here and the caller returns a zero-surface not-found.
    Mirrors the identical helper in `routers.mod_assets`; kept local so this
    endpoint depends on nothing of T-A03's internals.
    """
    load_result = getattr(request.app.state, "MODS", None)
    if load_result is None:
        return None
    return load_result.loaded.get(mod_id)


@router.get("/api/v1/mods/{mod_id}/frontend-manifest")
async def serve_frontend_manifest(request: Request, mod_id: str, user=Depends(get_verified_user)):
    """Resolve a mod to its current bundle URL, always fresh.

    Returns a 404 identical to an unrouted path for a mod that is not loaded
    (disabled/unknown) or that declares no `frontend` surface -- zero surface,
    disclosing nothing. For a loaded frontend mod, returns a 200 whose payload
    either names the current content-hashed bundle URL (`status: "ok"`) or, when
    no built entry exists yet, reports the named `no_bundle` condition with a null
    bundle URL -- never a URL to a missing file.

    **Requires an authenticated user** (#70). This shipped anonymous, which made
    it a 404-vs-200 oracle over any mod id: an unauthenticated caller could
    enumerate which mods an instance runs, their custom-element tags, and their
    current bundle hashes. That is a broader disclosure than the accepted Phase 0
    limitation `product-docs/mods.md` records -- that one is about a caller who
    already guessed a route prefix, whereas this answered for free.

    No client change was needed: `self.chat`'s loader already sends
    `Authorization: Bearer` on this fetch (`src/lib/mods/loader.ts`, called with
    the stored token from the mod view). The BUNDLE it points at is fetched by
    dynamic `import()`, which cannot carry a header -- so the asset surface stays
    open by necessity and is narrowed a different way, by restricting which file
    types it will serve at all (`mods/assets.py`).

    Scope filtering -- returning 404 for a mod the caller holds no scope on, the
    way `GET /api/v1/mods/enabled` filters -- is the consistent end state but is a
    behaviour change for the client loader, so it is deliberately NOT done here.
    """
    mod = _loaded_mod(request, mod_id)
    if mod is None:
        raise HTTPException(status_code=404, detail="Not Found")

    manifest = mod.manifest
    frontend = getattr(manifest, "frontend", None)
    if frontend is None:
        # Loaded, but declares no frontend surface -- there is no manifest to
        # resolve. Same zero-surface not-found as a disabled/unknown mod.
        raise HTTPException(status_code=404, detail="Not Found")

    directory = mod.directory
    entry = discover_bundle_entry(directory) if directory is not None else None

    payload = {
        "mod_id": mod_id,
        "tag": manifest.custom_element_tag(),
    }
    if entry is None:
        # Distinguishable, NAMED condition (R4 AC6): the mod declares a frontend
        # but ships no built assets yet. Never a 200 pointing at a missing file --
        # the bundle URL is explicitly null and the status names why.
        payload["status"] = "no_bundle"
        payload["bundle_url"] = None
    else:
        payload["status"] = "ok"
        payload["bundle_url"] = f"/static/mods/{mod_id}/{entry}"

    return JSONResponse(payload, headers={"Cache-Control": NO_CACHE_CONTROL})
