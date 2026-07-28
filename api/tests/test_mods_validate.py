"""Semantic manifest validation and fail-closed loading.

Cavekit: cavekit-mods-manifest.md R2, R3, R4, R5, R6; cavekit-mods-loader.md R8
Tasks: T-009, T-010, T-011, T-012, T-013, T-019
"""

import pytest

from selfai_ui.mods import naming
from selfai_ui.mods.manifest import ManifestError, ModManifest
from selfai_ui.mods.validate import core_satisfies, load_manifest, parse_version, validate_semantics

CORE = "0.5.0"

MINIMAL = {
    "id": "example",
    "name": "Example",
    "version": "0.1.0",
    "entrypoint": "example_mod:Mod",
    "min_core_version": "0.5.0",
}


def _manifest(**overrides):
    return ModManifest(**{**MINIMAL, **overrides})


def _yaml(tmp_path, body: str):
    path = tmp_path / "mod.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- T-009: id charset enforced during validation ---------------------------
@pytest.mark.tier0
@pytest.mark.parametrize("bad", ["has.dot", "has space", "has/slash", "Upper", "1leading"])
def test_unsafe_id_is_refused(bad):
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(_manifest(id=bad), core_version=CORE)
    assert "id" in str(excinfo.value)


@pytest.mark.tier0
def test_safe_id_passes():
    validate_semantics(_manifest(id="my-mod2"), core_version=CORE)


# --- T-010: scopes rooted at the mod's own namespace ------------------------
@pytest.mark.tier0
@pytest.mark.parametrize(
    "scope,why",
    [
        ("mods.other.thing.read", "another mod's root"),
        ("workspace.models", "a core namespace"),
        ("chat.delete", "a core namespace"),
        ("thing.read", "no mods. root at all"),
        ("mods.example", "the bare root is not a permission"),
        ("mods:example:thing", "non-dot separators"),
    ],
)
def test_scope_outside_the_mods_own_namespace_is_refused(scope, why):
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(
            _manifest(scopes=[{"id": scope, "desc": "x"}]),
            core_version=CORE,
        )
    assert "scope" in str(excinfo.value).lower(), why


@pytest.mark.tier0
def test_duplicate_scope_ids_are_refused():
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(
            _manifest(
                scopes=[
                    {"id": "mods.example.a.read", "desc": "x"},
                    {"id": "mods.example.a.read", "desc": "y"},
                ]
            ),
            core_version=CORE,
        )
    assert "duplicate" in str(excinfo.value)


@pytest.mark.tier0
def test_own_namespace_scopes_pass():
    validate_semantics(
        _manifest(
            scopes=[
                {"id": "mods.example.thing.read", "desc": "x"},
                {"id": "mods.example.thing.write", "desc": "y"},
            ]
        ),
        core_version=CORE,
    )


# --- T-011: table prefix matches the derivation -----------------------------
@pytest.mark.tier0
def test_table_prefix_must_match_the_derivation():
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(_manifest(db={"table_prefix": "whatever_"}), core_version=CORE)
    assert naming.table_prefix_for("example") in str(excinfo.value)


@pytest.mark.tier0
def test_derived_table_prefix_passes_and_no_db_block_is_fine():
    validate_semantics(_manifest(db={"table_prefix": "mod_example_"}), core_version=CORE)
    validate_semantics(_manifest(), core_version=CORE)


# --- T-012: min_core_version refused at load --------------------------------
@pytest.mark.tier0
@pytest.mark.parametrize(
    "required,running,satisfied",
    [
        ("0.5.0", "0.5.0", True),
        ("0.5.0", "0.5.1", True),
        ("0.5.0", "0.6.0", True),
        ("0.5", "0.5.0", True),
        ("0.5.0", "0.4.9", False),
        ("1.0.0", "0.9.9", False),
    ],
)
def test_core_version_comparison(required, running, satisfied):
    assert core_satisfies(required, running) is satisfied


@pytest.mark.tier0
def test_core_reports_a_real_version_not_the_00_fallback():
    """F-008 regression. The API image had no package.json on its version-read
    path, so VERSION fell to 0.0.0 and every mod's min_core_version (the
    contract examples use 0.5.0) was refused at the gate. VERSION must resolve
    to a real semver that satisfies the documented example."""
    from selfai_ui.env import VERSION

    assert VERSION != "0.0.0", "core VERSION fell back to the 0.0.0 placeholder — mods would all be refused"
    assert core_satisfies("0.5.0", VERSION), (
        f"core VERSION {VERSION!r} does not satisfy the contract's min_core_version example 0.5.0"
    )


@pytest.mark.tier0
def test_unsatisfied_min_core_version_names_all_three_facts():
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(_manifest(min_core_version="9.0.0"), core_version=CORE)
    message = str(excinfo.value)
    assert "example" in message and "9.0.0" in message and CORE in message


@pytest.mark.tier0
@pytest.mark.parametrize("bad", ["", "not-a-version", "1.x.0", None, "1..0"])
def test_unparseable_version_is_refused_rather_than_guessed(bad):
    with pytest.raises(ManifestError):
        parse_version(bad)


# --- T-013: fail-closed loading ---------------------------------------------
@pytest.mark.tier0
def test_valid_file_loads(tmp_path):
    path = _yaml(
        tmp_path,
        "id: example\nname: Example\nversion: 0.1.0\n"
        "entrypoint: example_mod:Mod\nmin_core_version: 0.5.0\n",
    )
    manifest = load_manifest(path, core_version=CORE)
    assert manifest.id == "example"


_COMPLETE = "id: example\nname: Example\nversion: 0.1.0\nentrypoint: example_mod:Mod\nmin_core_version: 0.5.0\n"


@pytest.mark.tier0
@pytest.mark.parametrize(
    "body,expect",
    [
        ("id: [unclosed\n", "not valid YAML"),
        ("", "is empty"),
        ("- a\n- b\n", "must be a mapping"),
        # Exactly one required field removed, so the reported field is
        # deterministic. Removing several would leave pydantic free to report
        # whichever it reaches first, which is what an earlier version of this
        # test got wrong.
        (_COMPLETE.replace("min_core_version: 0.5.0\n", ""), "min_core_version"),
        (_COMPLETE.replace("entrypoint: example_mod:Mod\n", ""), "entrypoint"),
        # Complete and valid, PLUS an unknown key -- so the unknown key is the
        # only thing wrong and is necessarily what gets reported.
        (_COMPLETE + "extra_key: x\n", "extra_key"),
    ],
)
def test_every_failure_mode_raises_manifest_error_naming_the_path(tmp_path, body, expect):
    path = _yaml(tmp_path, body)
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path, core_version=CORE)
    message = str(excinfo.value)
    assert str(path) in message, "the failure must name the file"
    assert expect in message


@pytest.mark.tier0
def test_the_failure_names_the_path_before_the_field(tmp_path):
    """An operator scanning a boot log wants to know *which mod* failed first."""
    path = _yaml(tmp_path, _COMPLETE.replace("min_core_version: 0.5.0\n", ""))
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path, core_version=CORE)

    message = str(excinfo.value)
    assert message.startswith(str(path)), message
    assert excinfo.value.field == "min_core_version"
    assert excinfo.value.path == path
    # The field must appear exactly once — the semantic path re-raises an error
    # that already carried a field, and must not double the prefix.
    assert message.count("min_core_version:") == 1, message


@pytest.mark.tier0
def test_a_missing_file_fails_closed_rather_than_raising_oserror(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "absent.yaml", core_version=CORE)


@pytest.mark.tier0
def test_semantic_failure_through_the_loader_also_names_the_path(tmp_path):
    path = _yaml(
        tmp_path,
        "id: example\nname: Example\nversion: 0.1.0\n"
        "entrypoint: example_mod:Mod\nmin_core_version: 9.9.9\n",
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path, core_version=CORE)
    assert str(path) in str(excinfo.value)


# --- T-A01: frontend nav surface validation (frontend-api R1) ---------------
def _frontend(**overrides):
    block = {
        "bundle_url": "/static/mods/example/index.html",
        "view": "example-home",
        "label": "Example",
        "icon": "puzzle",
        "add_to_nav": True,
        "scopes": ["mods.example.thing.read"],
    }
    block.update(overrides)
    return block


@pytest.mark.tier0
def test_frontend_gating_scope_in_own_namespace_validates():
    """R1 crit 2: a gating scope rooted at the mod's own mods.<id> validates."""
    validate_semantics(_manifest(frontend=_frontend()), core_version=CORE)


@pytest.mark.tier0
@pytest.mark.parametrize(
    "scope,why",
    [
        ("mods.other.view", "another mod's namespace"),
        ("workspace.models", "a core permission key"),
        ("chat.delete", "a core permission key"),
        ("thing.read", "no mods. root at all"),
        ("mods.example", "the bare root is not a permission"),
    ],
)
def test_frontend_gating_scope_outside_own_namespace_is_refused(scope, why):
    """R1 crit 2: a foreign root or a core key fails validation, via the SAME
    naming.is_scope_in_namespace helper the declared scopes use -- no new gating
    mechanism."""
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(_manifest(frontend=_frontend(scopes=[scope])), core_version=CORE)
    message = str(excinfo.value)
    assert "frontend" in message.lower() and "scope" in message.lower(), why


@pytest.mark.tier0
def test_a_frontend_block_naming_no_gating_scope_is_refused():
    """R1: the block must name one or more gating scopes; an empty list is a
    fail-closed refusal, distinct from an out-of-namespace refusal."""
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(_manifest(frontend=_frontend(scopes=[])), core_version=CORE)
    assert "gating scope" in str(excinfo.value)


@pytest.mark.tier0
def test_a_declared_invalid_custom_element_tag_is_refused():
    """R1 crit 4: a tag the block declares itself must be a real custom-element
    name (lowercase, opening with a letter, containing a hyphen)."""
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(_manifest(frontend=_frontend(tag="NoHyphen")), core_version=CORE)
    assert "tag" in str(excinfo.value)


@pytest.mark.tier0
def test_a_valid_declared_custom_element_tag_passes():
    validate_semantics(_manifest(frontend=_frontend(tag="example-view")), core_version=CORE)


@pytest.mark.tier0
def test_a_malformed_frontend_block_refuses_the_whole_mod_through_the_loader(tmp_path):
    """R1 crit 3: fail-closed through the real load path -- a frontend block
    missing a required sub-field refuses the mod, and the ManifestError names both
    the file and the offending sub-field. A refused mod contributes nothing."""
    body = _COMPLETE + (
        "frontend:\n"
        "  bundle_url: /static/mods/example/index.html\n"
        "  view: example-home\n"
        "  icon: puzzle\n"  # note: no `label`
        "  add_to_nav: true\n"
        "  scopes:\n    - mods.example.thing.read\n"
    )
    path = _yaml(tmp_path, body)
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path, core_version=CORE)
    message = str(excinfo.value)
    assert str(path) in message, "the failure must name the file"
    assert "label" in message


@pytest.mark.tier0
def test_frontend_missing_and_unknown_are_distinguishable_named_failures_through_loader(tmp_path):
    """R1 crit 3: through the loader, a missing-required and an unknown sub-field
    surface as distinct named failures (different field and different message)."""
    good = (
        "frontend:\n"
        "  bundle_url: /s\n  view: v\n  label: L\n  icon: i\n"
        "  add_to_nav: true\n  scopes:\n    - mods.example.thing.read\n"
    )
    missing_body = _COMPLETE + good.replace("  label: L\n", "")
    unknown_body = _COMPLETE + good + "  bogus: 1\n"

    da = tmp_path / "a"
    da.mkdir()
    db = tmp_path / "b"
    db.mkdir()
    with pytest.raises(ManifestError) as miss:
        load_manifest(_yaml(da, missing_body), core_version=CORE)
    with pytest.raises(ManifestError) as unknown:
        load_manifest(_yaml(db, unknown_body), core_version=CORE)

    assert miss.value.field == "frontend.label"
    assert unknown.value.field == "frontend.bogus"
    assert miss.value.field != unknown.value.field
    assert miss.value.raw != unknown.value.raw  # "Field required" vs "Extra inputs..."


# --- T-019: a secret must not carry an inline value -------------------------
@pytest.mark.tier0
def test_secret_config_key_with_an_inline_value_is_refused():
    with pytest.raises(ManifestError) as excinfo:
        validate_semantics(
            _manifest(config=[{"key": "api_key", "secret": True, "default": "hunter2"}]),
            core_version=CORE,
        )
    assert "secret" in str(excinfo.value)


@pytest.mark.tier0
def test_secret_without_a_value_and_non_secret_with_one_both_pass():
    validate_semantics(_manifest(config=[{"key": "api_key", "secret": True}]), core_version=CORE)
    validate_semantics(
        _manifest(config=[{"key": "endpoint", "default": "https://example.internal"}]),
        core_version=CORE,
    )


@pytest.mark.tier0
@pytest.mark.parametrize("mod_id", ["on", "off", "yes", "no", "true", "false"])
def test_a_yaml_truthy_id_is_diagnosed_not_left_as_input_should_be_a_valid_string(tmp_path, mod_id):
    """YAML 1.1 turns bare `on` into a boolean before validation ever sees it.

    The id charset permits these names, so a mod called `on` is legal — it just
    has to be quoted. Without this guard the author gets "Input should be a
    valid string" and no clue why, which is a genuinely bad afternoon.
    """
    path = _yaml(
        tmp_path,
        f"id: {mod_id}\nname: X\nversion: 0.1.0\nentrypoint: m:Mod\nmin_core_version: 0.5.0\n",
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path, core_version=CORE)

    message = str(excinfo.value)
    assert "boolean" in message, message
    assert "quote it" in message.lower(), message


@pytest.mark.tier0
def test_a_quoted_truthy_id_loads_fine(tmp_path):
    """The fix the error message tells the author to apply must actually work."""
    path = _yaml(
        tmp_path,
        'id: "on"\nname: X\nversion: 0.1.0\nentrypoint: m:Mod\nmin_core_version: 0.5.0\n',
    )
    assert load_manifest(path, core_version=CORE).id == "on"


@pytest.mark.tier0
@pytest.mark.parametrize("mod_id", ["y", "n"])
def test_bare_y_and_n_are_plain_strings_to_pyyaml(tmp_path, mod_id):
    """The YAML 1.1 spec lists y/n as booleans; PyYAML's resolver does not.

    Its bool pattern covers yes/no/on/off/true/false and stops there, so a mod
    called `y` loads unquoted. Pinned as a test because the difference between
    the spec and the implementation is exactly the kind of thing that changes
    under you on a library bump — if PyYAML ever adds them, this fails and
    points at the guard above rather than at a confused mod author.
    """
    path = _yaml(
        tmp_path,
        f"id: {mod_id}\nname: X\nversion: 0.1.0\nentrypoint: m:Mod\nmin_core_version: 0.5.0\n",
    )
    assert load_manifest(path, core_version=CORE).id == mod_id


# =============================================================================
# #70 -- a mod's install directory is not a web root.
# =============================================================================
@pytest.mark.tier0
def test_only_web_asset_file_types_are_servable(tmp_path):
    """The traversal guard answers "inside the directory", not "should be public".

    For a while nothing answered the second question: the surface served every
    regular file under a mod's directory, so a mod's Python source, its
    `mod.yaml`, its tests and its build inputs were all anonymously readable.
    Operators install a mod by dropping a directory onto a volume; nothing warned
    an author that everything dropped there becomes world-readable.
    """
    from selfai_ui.mods.assets import AssetResolutionError, resolve_mod_asset

    mod_dir = tmp_path / "widget"
    mod_dir.mkdir()
    for name in ("entry.a1b2c3.js", "styles.css", "icon.svg", "mod.yaml", "widget_mod.py", "deploy.sh", ".env"):
        (mod_dir / name).write_text("x", encoding="utf-8")

    for servable in ("entry.a1b2c3.js", "styles.css", "icon.svg"):
        assert resolve_mod_asset(mod_dir, servable).name == servable

    for private in ("mod.yaml", "widget_mod.py", "deploy.sh", ".env"):
        with pytest.raises(AssetResolutionError):
            resolve_mod_asset(mod_dir, private)


@pytest.mark.tier0
def test_the_asset_type_check_is_case_insensitive_and_reads_the_resolved_target(tmp_path):
    """A file is no less source code for being named `.PY`.

    The suffix is also checked on the RESOLVED path rather than the requested
    one, so a symlink with an innocent name cannot launder a private file: the
    refusal follows what the path actually points at.
    """
    from selfai_ui.mods.assets import AssetResolutionError, is_servable_asset, resolve_mod_asset

    assert is_servable_asset("bundle.JS") is True
    assert is_servable_asset("secrets.YAML") is False
    assert is_servable_asset("mod.PY") is False
    assert is_servable_asset("noextension") is False

    mod_dir = tmp_path / "widget"
    mod_dir.mkdir()
    (mod_dir / "secrets.yaml").write_text("token: hunter2", encoding="utf-8")
    (mod_dir / "logo.svg").symlink_to(mod_dir / "secrets.yaml")

    with pytest.raises(AssetResolutionError):
        resolve_mod_asset(mod_dir, "logo.svg")
