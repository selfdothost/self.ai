"""Tier 0 of the mods build site: naming rules, manifest schema, facade surface.

Cavekit: cavekit-mods-manifest.md R1/R2/R6, cavekit-mods-facade.md R1/R3
Tasks: T-003, T-004, T-005, T-006
"""

import pytest
from pydantic import ValidationError

from selfai_ui.mods import naming
from selfai_ui.mods.manifest import ModManifest


# --- T-003: naming rules, single source ------------------------------------
@pytest.mark.tier0
@pytest.mark.parametrize(
    "mod_id,reason",
    [
        ("has.dot", "a dot would traverse the scope namespace"),
        ("has space", "whitespace is ambiguous when serialized"),
        ("has/slash", "a path separator would traverse the install dir"),
        ("has\\backslash", "a path separator would traverse the install dir"),
        ("", "empty is not a namespace"),
        ("Upper", "case folds in some table backends; two ids could collide"),
        ("1leading", "must open with a letter"),
        (None, "not a string at all"),
    ],
)
def test_rejected_mod_ids(mod_id, reason):
    assert not naming.is_valid_mod_id(mod_id), reason


@pytest.mark.tier0
@pytest.mark.parametrize("mod_id", ["crew", "a", "my-mod", "my_mod2", "a1"])
def test_accepted_mod_ids(mod_id):
    assert naming.is_valid_mod_id(mod_id)


@pytest.mark.tier0
def test_both_namespaces_derive_from_the_same_id():
    """The scope root and the table prefix are derived, never restated."""
    assert naming.scope_root_for("crew") == "mods.crew"
    assert naming.table_prefix_for("crew") == "mod_crew_"


@pytest.mark.tier0
def test_scope_must_sit_strictly_beneath_the_mod_root():
    assert naming.is_scope_in_namespace("mods.crew.session.connect", "crew")
    # The root names the branch, not a permission.
    assert not naming.is_scope_in_namespace("mods.crew", "crew")
    # Prefix-adjacent must not pass: `mods.crewx` is a different mod.
    assert not naming.is_scope_in_namespace("mods.crewx.thing", "crew")
    # A core permission key is not claimable by a mod.
    assert not naming.is_scope_in_namespace("workspace.models", "crew")
    assert not naming.is_scope_in_namespace(None, "crew")


# --- T-004: manifest schema -------------------------------------------------
MINIMAL = {
    "id": "example",
    "name": "Example",
    "version": "0.1.0",
    "entrypoint": "example_mod:Mod",
    "min_core_version": "0.5.0",
}


@pytest.mark.tier0
def test_minimal_manifest_validates():
    manifest = ModManifest(**MINIMAL)
    assert manifest.id == "example"
    # A mod contributing no surfaces at all is structurally valid.
    assert manifest.api is None and manifest.ws is None
    assert manifest.scopes == [] and manifest.config == []


@pytest.mark.tier0
def test_full_manifest_validates():
    manifest = ModManifest(
        **MINIMAL,
        api={"prefix": "/example"},
        ws={"namespace": "/example"},
        frontend={
            "bundle_url": "/static/mods/example/index.html",
            "view": "example-home",
            "label": "Example",
            "icon": "puzzle",
            "add_to_nav": True,
            "scopes": ["mods.example.thing.read"],
        },
        db={"table_prefix": "mod_example_"},
        scopes=[{"id": "mods.example.thing.read", "desc": "View things"}],
        config=[{"key": "endpoint_url", "desc": "Where to reach it"}],
    )
    assert manifest.api.prefix == "/example"
    assert manifest.scopes[0].id == "mods.example.thing.read"


@pytest.mark.tier0
@pytest.mark.parametrize("missing", sorted(MINIMAL))
def test_every_required_field_is_required(missing):
    payload = {k: v for k, v in MINIMAL.items() if k != missing}
    with pytest.raises(ValidationError) as excinfo:
        ModManifest(**payload)
    assert missing in str(excinfo.value)


@pytest.mark.tier0
def test_unknown_top_level_key_is_a_named_failure():
    """Fail-closed: a typo is a refusal, not a silently ignored setting."""
    with pytest.raises(ValidationError) as excinfo:
        ModManifest(**MINIMAL, prefx="/typo")
    assert "prefx" in str(excinfo.value)


@pytest.mark.tier0
def test_unknown_key_inside_a_block_is_a_named_failure():
    with pytest.raises(ValidationError) as excinfo:
        ModManifest(**MINIMAL, api={"prefix": "/example", "prefx": "/typo"})
    assert "prefx" in str(excinfo.value)


@pytest.mark.tier0
def test_config_entry_carries_a_secret_marker():
    """Loader R8's 'no inline secret value' check depends on this marker."""
    manifest = ModManifest(**MINIMAL, config=[{"key": "api_key", "secret": True}])
    entry = manifest.config[0]
    assert entry.secret is True
    # Non-secret is the default, so an author must opt in rather than remember.
    assert ModManifest(**MINIMAL, config=[{"key": "url"}]).config[0].secret is False


# --- T-A01: the frontend block declares a nav surface (frontend-api R1) ------
FRONTEND = {
    "bundle_url": "/static/mods/example/index.html",
    "view": "example-home",
    "label": "Example",
    "icon": "puzzle",
    "add_to_nav": True,
    "scopes": ["mods.example.thing.read"],
}


@pytest.mark.tier0
def test_frontend_nav_block_validates_and_exposes_its_fields():
    """R1 crit 1: a view id/path, a nav label, an icon, and an addToNav flag all
    validate and are readable."""
    fe = ModManifest(**MINIMAL, frontend=FRONTEND).frontend
    assert fe.view == "example-home"
    assert fe.label == "Example"
    assert fe.icon == "puzzle"
    assert fe.add_to_nav is True
    # bundle_url is retained unchanged from Phase 0.
    assert fe.bundle_url == "/static/mods/example/index.html"


@pytest.mark.tier0
def test_a_mod_with_no_frontend_block_validates_and_contributes_no_nav_surface():
    """R1 crit 5: the frontend block is optional; its absence is not a failure and
    yields no nav surface (no resolved tag, no frontend)."""
    manifest = ModManifest(**MINIMAL)
    assert manifest.frontend is None
    assert manifest.custom_element_tag() is None


@pytest.mark.tier0
@pytest.mark.parametrize("missing", sorted(k for k in FRONTEND))
def test_a_frontend_block_missing_a_required_subfield_refuses_the_mod(missing):
    """R1 crit 3 (structural): a missing required sub-field is a named failure that
    refuses the mod -- every nav sub-field and the retained bundle_url is required."""
    block = {k: v for k, v in FRONTEND.items() if k != missing}
    with pytest.raises(ValidationError) as excinfo:
        ModManifest(**MINIMAL, frontend=block)
    assert missing in str(excinfo.value)


@pytest.mark.tier0
def test_missing_and_unknown_frontend_subfields_are_distinguishable_named_failures():
    """R1 crit 3: the two failure modes carry different, named pydantic error
    types -- 'you forgot a field' is distinguishable from 'you added an unknown
    one'. Both refuse the mod (fail-closed)."""
    with pytest.raises(ValidationError) as miss:
        ModManifest(**MINIMAL, frontend={k: v for k, v in FRONTEND.items() if k != "label"})
    with pytest.raises(ValidationError) as unknown:
        ModManifest(**MINIMAL, frontend={**FRONTEND, "bogus": 1})

    miss_types = {e["type"] for e in miss.value.errors()}
    unknown_types = {e["type"] for e in unknown.value.errors()}
    assert "missing" in miss_types
    assert "extra_forbidden" in unknown_types
    assert miss_types != unknown_types
    # The unknown key is named, not silently dropped.
    assert any(e["loc"][-1] == "bogus" for e in unknown.value.errors())


@pytest.mark.tier0
def test_custom_element_tag_derives_from_the_id_by_a_single_source_rule():
    """R1 crit 4: with no declared tag, the tag derives from the id, and the
    derivation lives in exactly one place -- naming.custom_element_tag_for --
    which ModManifest.custom_element_tag reads from."""
    manifest = ModManifest(**MINIMAL, frontend=FRONTEND)  # no `tag` declared
    assert manifest.custom_element_tag() == naming.custom_element_tag_for("example")
    assert manifest.custom_element_tag() == "mod-example"


@pytest.mark.tier0
def test_a_declared_custom_element_tag_overrides_the_derivation():
    """R1 crit 4: a block may declare its own tag; the single resolver returns it."""
    manifest = ModManifest(**MINIMAL, frontend={**FRONTEND, "tag": "example-view"})
    assert manifest.custom_element_tag() == "example-view"


@pytest.mark.tier0
def test_the_tag_derivation_rule_folds_underscores_and_yields_a_valid_name():
    """R1 crit 4: the single-source rule produces a real custom-element name (a
    mod id may carry an underscore, which a custom-element name may not)."""
    assert naming.custom_element_tag_for("my_mod") == "mod-my-mod"
    assert naming.is_valid_custom_element_tag("mod-my-mod")
    assert not naming.is_valid_custom_element_tag("nohyphen")
    assert not naming.is_valid_custom_element_tag("Mod-Upper")


# --- T-005 / T-006: the facade surface --------------------------------------
@pytest.mark.tier0
def test_facade_export_list_is_declared_and_enumerable():
    """The contract is `__all__`, not 'whatever happens to be importable'."""
    from selfai_ui import modapi

    assert isinstance(modapi.__all__, list) and modapi.__all__

    expected = {
        "get_verified_user",  # mount a route
        "has_permission",  # check a scope
        "get_db",  # read the database
        "sio",  # emit an event
        "ToolSpec",  # return a tool spec
        "get_tools_specs",
        "CORE_VERSION",
    }
    assert expected <= set(modapi.__all__)


@pytest.mark.tier0
def test_every_declared_export_actually_resolves():
    """A name promised in `__all__` but absent is a broken promise on day one."""
    from selfai_ui import modapi

    missing = [name for name in modapi.__all__ if not hasattr(modapi, name)]
    assert not missing, f"declared in __all__ but not present: {missing}"


@pytest.mark.tier0
def test_core_version_reads_from_the_single_declared_location():
    """T-006: one source for the version `min_core_version` is compared against."""
    from selfai_ui import modapi
    from selfai_ui.env import VERSION

    assert modapi.CORE_VERSION == VERSION
    assert isinstance(modapi.CORE_VERSION, str) and modapi.CORE_VERSION


@pytest.mark.tier0
def test_facade_documents_that_the_boundary_is_unenforced_for_third_parties():
    """cavekit-mods-facade.md R2: this must not be presented as enforced."""
    from selfai_ui import modapi

    doc = (modapi.__doc__ or "").lower()
    assert "not enforced for third-party" in doc
