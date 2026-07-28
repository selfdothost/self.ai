"""Mod scopes through the unchanged checker, and per-mod configuration.

Cavekit: cavekit-mods-permissions.md R1, R2; cavekit-mods-loader.md R8
Tasks: T-038, T-039, T-043, T-044, T-045
"""

import pytest

from selfai_ui.mods.manifest import ModManifest
from selfai_ui.mods.modconfig import build_mod_config, config_path_for, env_var_for, redacted
from selfai_ui.mods.scopes import seed_defaults, strip_mod_scopes
from selfai_ui.utils.access_control import has_permission

BASE = {"name": "Example", "version": "0.1.0", "entrypoint": "m:Mod", "min_core_version": "0.5.0"}


def _manifest(mod_id="example", **extra):
    return ModManifest(**{**BASE, "id": mod_id, **extra})


# --- T-043 / T-044: enforcement through the unchanged checker --------------
@pytest.mark.tier1
def test_a_mod_scope_and_a_core_scope_go_through_the_same_function(test_user):
    """Not an equivalent function. The same one, with no mods-specific branch."""
    defaults = {
        "workspace": {"models": True},
        "mods": {"crew": {"session": {"connect": True, "drive": False}}},
    }

    assert has_permission(test_user["id"], "workspace.models", defaults) is True
    assert has_permission(test_user["id"], "mods.crew.session.connect", defaults) is True
    assert has_permission(test_user["id"], "mods.crew.session.drive", defaults) is False


@pytest.mark.tier1
def test_a_missing_entry_at_any_level_denies(test_user):
    defaults = {"mods": {"crew": {"session": {"connect": True}}}}

    # Missing leaf, missing branch, and a wholly unknown mod all deny.
    assert not has_permission(test_user["id"], "mods.crew.session.absent", defaults)
    assert not has_permission(test_user["id"], "mods.crew.absent.thing", defaults)
    assert not has_permission(test_user["id"], "mods.other.session.connect", defaults)


@pytest.mark.tier1
def test_a_mod_scope_cannot_reach_a_core_permission(test_user):
    """`mods.` rooting means a mod's tree and core's tree cannot overlap."""
    defaults = {"workspace": {"models": True}, "mods": {"crew": {}}}
    assert not has_permission(test_user["id"], "mods.crew.workspace.models", defaults)


# --- T-045: deny-by-default seeding ----------------------------------------
@pytest.mark.tier0
def test_enabling_a_mod_grants_nothing():
    manifest = _manifest(
        "crew",
        scopes=[
            {"id": "mods.crew.session.connect", "desc": "a"},
            {"id": "mods.crew.session.drive", "desc": "b"},
        ],
    )

    seeded = seed_defaults({"workspace": {"models": True}}, manifest)

    assert seeded["mods"]["crew"]["session"] == {"connect": False, "drive": False}
    assert seeded["workspace"]["models"] is True, "core defaults must be untouched"


@pytest.mark.tier0
def test_seeding_never_overwrites_an_existing_grant():
    """Re-enabling must not silently revoke a deliberate admin grant."""
    manifest = _manifest("crew", scopes=[{"id": "mods.crew.session.connect", "desc": "a"}])
    existing = {"mods": {"crew": {"session": {"connect": True}}}}

    assert seed_defaults(existing, manifest)["mods"]["crew"]["session"]["connect"] is True


@pytest.mark.tier0
def test_seeding_does_not_mutate_the_input():
    manifest = _manifest("crew", scopes=[{"id": "mods.crew.a.b", "desc": "x"}])
    original = {"workspace": {"models": True}}
    seed_defaults(original, manifest)
    assert "mods" not in original


@pytest.mark.tier0
def test_two_mods_seed_side_by_side_without_collision():
    a = _manifest("alpha", scopes=[{"id": "mods.alpha.thing", "desc": "x"}])
    b = _manifest("beta", scopes=[{"id": "mods.beta.thing", "desc": "y"}])

    seeded = seed_defaults(seed_defaults({}, a), b)

    assert seeded["mods"]["alpha"]["thing"] is False
    assert seeded["mods"]["beta"]["thing"] is False


@pytest.mark.tier0
def test_stripping_one_mods_scopes_leaves_the_others():
    seeded = {"mods": {"alpha": {"x": True}, "beta": {"y": True}}, "workspace": {"models": True}}
    stripped = strip_mod_scopes(seeded, "alpha")

    assert "alpha" not in stripped["mods"]
    assert stripped["mods"]["beta"]["y"] is True
    assert stripped["workspace"]["models"] is True
    assert "alpha" in seeded["mods"], "the input must not be mutated"


# --- T-038 / T-039: per-mod configuration ----------------------------------
@pytest.mark.tier0
def test_config_is_namespaced_so_two_mods_cannot_collide():
    assert config_path_for("alpha", "api_key") != config_path_for("beta", "api_key")
    assert config_path_for("alpha", "api_key") == "mods.alpha.config.api_key"
    assert env_var_for("alpha", "api_key") == "MOD_ALPHA_API_KEY"
    assert env_var_for("my-mod", "api_key") == "MOD_MY_MOD_API_KEY"


@pytest.mark.tier0
def test_a_declared_key_becomes_a_persistent_config_entry():
    """Core's existing mechanism, not a parallel one — AppConfig accepts
    nothing else, which is exactly why this is the shape used."""
    built = build_mod_config(_manifest("alpha", config=[{"key": "endpoint", "default": "https://x.internal"}]))

    assert list(built) == ["endpoint"]
    assert built["endpoint"].value == "https://x.internal"
    assert built["endpoint"].config_path == "mods.alpha.config.endpoint"


@pytest.mark.tier0
def test_a_mod_declaring_no_config_yields_nothing():
    assert build_mod_config(_manifest("alpha")) == {}


@pytest.mark.tier0
def test_an_operator_set_value_wins_over_the_declared_default(monkeypatch):
    monkeypatch.setenv("MOD_ALPHA_ENDPOINT", "https://operator.internal")
    built = build_mod_config(_manifest("alpha", config=[{"key": "endpoint", "default": "https://default.internal"}]))
    assert built["endpoint"].value == "https://operator.internal"


@pytest.mark.tier0
def test_a_secret_takes_its_value_only_from_the_deployments_secret_path(monkeypatch):
    """Never the manifest: it is a file in an image, read by anyone who can
    read the image, and it lands in version control."""
    manifest = _manifest("alpha", config=[{"key": "api_key", "secret": True}])

    monkeypatch.delenv("MOD_ALPHA_API_KEY", raising=False)
    assert build_mod_config(manifest)["api_key"].value is None

    monkeypatch.setenv("MOD_ALPHA_API_KEY", "from-the-vault")
    assert build_mod_config(manifest)["api_key"].value == "from-the-vault"


@pytest.mark.tier0
def test_two_mods_with_the_same_key_name_do_not_read_each_others_values(monkeypatch):
    monkeypatch.setenv("MOD_ALPHA_API_KEY", "alpha-value")
    monkeypatch.setenv("MOD_BETA_API_KEY", "beta-value")

    alpha = build_mod_config(_manifest("alpha", config=[{"key": "api_key"}]))
    beta = build_mod_config(_manifest("beta", config=[{"key": "api_key"}]))

    assert alpha["api_key"].value == "alpha-value"
    assert beta["api_key"].value == "beta-value"


@pytest.mark.tier0
def test_a_secret_is_masked_for_display_but_its_existence_is_not_hidden():
    manifest = _manifest("alpha", config=[{"key": "api_key", "secret": True}, {"key": "endpoint"}])
    shown = redacted(manifest, {"api_key": "hunter2", "endpoint": "https://x.internal"})

    assert shown["api_key"] == "********"
    assert shown["endpoint"] == "https://x.internal"
    assert "api_key" in shown, "an operator must still see the key exists"


@pytest.mark.tier0
def test_an_unset_secret_is_not_masked_into_looking_set():
    manifest = _manifest("alpha", config=[{"key": "api_key", "secret": True}])
    assert redacted(manifest, {"api_key": None})["api_key"] is None


@pytest.mark.tier0
def test_an_unnamespaced_env_var_does_not_satisfy_a_mods_lookup(monkeypatch):
    """A stray MOD_API_KEY must not be picked up as alpha's api_key."""
    monkeypatch.delenv("MOD_ALPHA_API_KEY", raising=False)
    monkeypatch.setenv("MOD_API_KEY", "not-for-alpha")

    built = build_mod_config(_manifest("alpha", config=[{"key": "api_key", "secret": True}]))
    assert built["api_key"].value is None


@pytest.mark.tier0
def test_the_scopes_module_offers_no_route_enforcement_helper():
    """Enforcement is `has_permission(uid, scope, defaults)` through the facade.

    `require_scope()` and `check_scope()` used to live here and were removed:
    nothing ever called either, and `require_scope` refused every caller (its
    dependency took an un-annotated `user=None`, which FastAPI binds as a query
    parameter instead of injecting the `get_user` it was handed and never called;
    it also passed `{}` for the defaults, the F-005 bug). Anything in this module
    is outside the `selfai_ui.modapi` facade, so a helper here is one no mod may
    legally import -- re-adding one invites exactly the internal import the
    facade exists to prevent.

    Pinned as a test because the names are the obvious thing to reach for, and
    the broken version failed closed, which is the hardest kind of wrong to
    notice.
    """
    from selfai_ui.mods import scopes

    for name in ("require_scope", "check_scope"):
        assert not hasattr(scopes, name), (
            f"{name} was re-added to mods/scopes.py; enforcement belongs at the call site "
            f"via modapi's has_permission + permission_defaults -- see the module docstring"
        )
