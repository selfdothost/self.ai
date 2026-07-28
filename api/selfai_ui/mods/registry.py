"""The registry response: which enabled mods a caller may see, and what they hold.

Everything here is pure -- it takes a user, the loaded manifests, and the
permission checker as arguments. No imports of the app, no request, no app.state.
That keeps the filtering logic testable against fixtures and lets the router be a
thin adapter.

The two rules that make this a registry rather than a listing:

* **Visibility is scope-based.** An admin sees every enabled, loaded mod,
  because an admin manages mods and hiding one would make the admin surface lie
  about instance state. A non-admin sees a mod only if they hold at least one of
  its declared scopes; an unheld mod is absent entirely, never flagged or nulled.
  This is the registry-response guarantee. It says nothing about the route
  surface -- a mod's routes are mounted for everyone, so probing a guessed prefix
  still discloses existence. That is an accepted Phase 0 limitation, recorded in
  the kit, not something this module claims to fix.

* **Granted scopes are reported through the same checker that enforces them.**
  The set on an entry is derived by evaluating each declared scope with
  `has_permission`, so the report cannot drift from what is actually enforced.
  A scope the caller does not hold is absent from the list, not reported as
  `False`, because the interesting fact is what you *can* do.

Cavekit: cavekit-mods-registry.md R1-R5 -- T-050..T-057
"""

from collections.abc import Callable

from selfai_ui.mods.manifest import ModManifest


def granted_scopes_for(
    manifest: ModManifest, user_id: str, *, has_permission: Callable, defaults: dict
) -> list[str]:
    """The declared scopes this caller holds, in declaration order.

    Evaluated through `has_permission` -- the same function enforcement uses --
    so the report cannot disagree with what is enforced. Only scopes rooted at
    this mod's own namespace can appear, because the manifest validator already
    refused anything else; this function does not re-check that.
    """
    return [scope.id for scope in manifest.scopes if has_permission(user_id, scope.id, defaults)]


def holds_any_scope(
    manifest: ModManifest, user_id: str, *, has_permission: Callable, defaults: dict
) -> bool:
    return any(has_permission(user_id, scope.id, defaults) for scope in manifest.scopes)


def entry_for(manifest: ModManifest, held_scopes: list[str]) -> dict:
    """One registry entry. Carries no configuration and no secrets.

    The frontend fields appear only when the mod declares a `frontend` block --
    they are absent otherwise, exactly as `bundle_url` was in Phase 0. The mod's
    id and name are always present.

    `bundle_url` is reported **unchanged** from Phase 0 (`cavekit-mods-registry.md`
    R4) -- a loading address. Alongside it now ride the declarative nav fields
    (`cavekit-mods-frontend-api.md` R5): `view`, `label`, `icon`, and
    `add_to_nav`, so the client can render nav without a second request. This is
    purely additive: it removes nothing, repurposes nothing, and adds no gating.
    The whole entry rides `visible_mods`' existing scope filter unchanged -- an
    unseen mod contributes no entry and therefore no nav fields.
    """
    entry = {"id": manifest.id, "name": manifest.name, "scopes": held_scopes}
    if manifest.frontend is not None:
        entry["bundle_url"] = manifest.frontend.bundle_url
        entry["view"] = manifest.frontend.view
        entry["label"] = manifest.frontend.label
        entry["icon"] = manifest.frontend.icon
        entry["add_to_nav"] = manifest.frontend.add_to_nav
    return entry


def visible_mods(
    user, loaded_manifests, *, has_permission: Callable, defaults: dict
) -> list[dict]:
    """The registry response for one caller.

    Admins see every loaded mod. A non-admin sees a mod only if they hold at
    least one of its declared scopes. A mod that declares no scopes is therefore
    admin-visible only for non-admins -- a defensible reading of "holding >= 1
    scope yields an entry", and a case that does not arise until a real mod
    exists to test it against.
    """
    entries: list[dict] = []
    for manifest in loaded_manifests:
        held = granted_scopes_for(manifest, user.id, has_permission=has_permission, defaults=defaults)
        if user.role == "admin" or held:
            entries.append(entry_for(manifest, held))
    return entries
