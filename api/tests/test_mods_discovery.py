"""Discovery: what is installed, intersected with what is enabled.

Cavekit: cavekit-mods-discovery.md R1, R3 — T-020, T-021, T-022
"""

from pathlib import Path

import pytest

from selfai_ui.mods.discovery import discover, scan

CORE = "0.5.0"


def _install(root, mod_id, *, body=None, name="mod.yaml"):
    """Write a mod directory containing a manifest. Returns the manifest path."""
    directory = root / mod_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        body
        if body is not None
        else (
            f"id: {mod_id}\nname: {mod_id.title()}\nversion: 0.1.0\n"
            f"entrypoint: {mod_id}_mod:Mod\nmin_core_version: 0.5.0\n"
        ),
        encoding="utf-8",
    )
    return path


# --- T-020 / T-021: installed x enabled ------------------------------------
@pytest.mark.tier0
def test_present_and_enabled_is_discovered(tmp_path):
    _install(tmp_path, "alpha")
    result = discover([tmp_path], ["alpha"], core_version=CORE)

    assert result.loaded_ids == ["alpha"]
    assert result.loaded["alpha"].entrypoint == "alpha_mod:Mod"
    assert result.errors == []


@pytest.mark.tier0
def test_present_but_not_enabled_loads_nothing_and_reports_nothing(tmp_path):
    """Somebody meant to switch it off. That is not a problem to report."""
    _install(tmp_path, "alpha")
    result = discover([tmp_path], [], core_version=CORE)

    assert result.loaded == {}
    assert result.errors == []
    assert result.warnings == []


@pytest.mark.tier0
def test_enabled_but_not_installed_is_an_error_that_does_not_stop_boot(tmp_path):
    """Always a mistake, so always reported — but never fatal."""
    result = discover([tmp_path], ["ghost"], core_version=CORE)

    assert result.loaded == {}
    assert len(result.errors) == 1
    assert "ghost" in result.errors[0]


@pytest.mark.tier0
def test_a_nonexistent_install_location_warns_and_does_not_stop_boot(tmp_path):
    _install(tmp_path, "alpha")
    result = discover([tmp_path, tmp_path / "absent"], ["alpha"], core_version=CORE)

    assert result.loaded_ids == ["alpha"], "a bad location must not lose a good mod"
    assert any("absent" in w for w in result.warnings)
    assert result.errors == []


@pytest.mark.tier0
def test_one_broken_mod_does_not_prevent_the_others_loading(tmp_path):
    """The whole point of collecting errors instead of raising them."""
    _install(tmp_path, "alpha")
    _install(tmp_path, "broken", body="id: [unclosed\n")
    _install(tmp_path, "gamma")

    result = discover([tmp_path], ["alpha", "broken", "gamma"], core_version=CORE)

    assert result.loaded_ids == ["alpha", "gamma"]
    assert len(result.errors) == 1 and "broken" in result.errors[0]


@pytest.mark.tier0
def test_a_mod_failing_semantic_validation_is_reported_not_raised(tmp_path):
    _install(
        tmp_path,
        "future",
        body=("id: future\nname: Future\nversion: 0.1.0\nentrypoint: f:Mod\nmin_core_version: 99.0.0\n"),
    )
    result = discover([tmp_path], ["future"], core_version=CORE)

    assert result.loaded == {}
    assert "future" in result.errors[0] and "99.0.0" in result.errors[0]


@pytest.mark.tier0
def test_directory_name_and_declared_id_must_agree(tmp_path):
    """Otherwise the enabled list means one thing and the scopes another."""
    _install(
        tmp_path,
        "alpha",
        body=("id: beta\nname: Beta\nversion: 0.1.0\nentrypoint: b:Mod\nmin_core_version: 0.5.0\n"),
    )
    result = discover([tmp_path], ["alpha"], core_version=CORE)

    assert result.loaded == {}
    assert "beta" in result.errors[0] and "alpha" in result.errors[0]


@pytest.mark.tier0
def test_a_directory_without_a_manifest_is_not_a_mod(tmp_path):
    (tmp_path / "notamod").mkdir()
    result = discover([tmp_path], ["notamod"], core_version=CORE)

    assert result.loaded == {}
    assert len(result.errors) == 1


@pytest.mark.tier0
def test_enabled_list_deduplicates(tmp_path):
    _install(tmp_path, "alpha")
    result = discover([tmp_path], ["alpha", "alpha"], core_version=CORE)

    assert result.loaded_ids == ["alpha"]
    assert result.errors == []


# --- T-022: duplicate id is a hard error -----------------------------------
@pytest.mark.tier0
def test_the_same_id_in_two_locations_loads_neither(tmp_path):
    """Not last-wins. Ambiguity resolved silently means running code nobody chose."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    path_one = _install(first, "clash")
    path_two = _install(second, "clash")

    result = discover([first, second], ["clash"], core_version=CORE)

    assert result.loaded == {}, "neither copy may load"
    assert len(result.errors) == 1
    message = result.errors[0]
    assert "clash" in message
    assert str(path_one) in message and str(path_two) in message, "the error must name both paths"


@pytest.mark.tier0
def test_a_duplicate_does_not_prevent_other_mods_loading(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    _install(first, "clash")
    _install(second, "clash")
    _install(first, "fine")

    result = discover([first, second], ["clash", "fine"], core_version=CORE)

    assert result.loaded_ids == ["fine"]
    assert len(result.errors) == 1


# --- T-A02: the mod's on-disk directory survives discovery (frontend-api R2) -
@pytest.mark.tier0
def test_the_loaded_manifest_retains_its_absolute_on_disk_directory(tmp_path):
    """R2 crit 1: after discovery the loaded record carries the absolute directory
    it loaded from -- the resolved parent of the `path` discover() selected."""
    path = _install(tmp_path, "alpha")
    result = discover([tmp_path], ["alpha"], core_version=CORE)

    directory = result.loaded["alpha"].directory
    assert directory is not None
    assert directory.is_absolute()
    assert directory == path.parent.resolve()


@pytest.mark.tier0
def test_the_loader_record_exposes_the_same_directory(tmp_path):
    """R2 crit 1: the record the registry and asset server read (LoadedMod)
    carries the directory too, via its manifest -- no full boot needed to show
    the field threads onto the loaded record."""
    from selfai_ui.mods.loader import LoadedMod

    _install(tmp_path, "alpha")
    manifest = discover([tmp_path], ["alpha"], core_version=CORE).loaded["alpha"]
    loaded = LoadedMod(manifest=manifest, entrypoint=object())
    assert loaded.directory == manifest.directory
    assert loaded.directory is not None


@pytest.mark.tier0
def test_a_multi_location_mod_is_still_a_hard_error_and_retains_no_directory(tmp_path):
    """R2 crit 2: the multi-location refusal (discovery R3) is unchanged -- the
    mod is not loaded, so nothing retains a directory for it."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    path_one = _install(first, "clash")
    path_two = _install(second, "clash")

    result = discover([first, second], ["clash"], core_version=CORE)

    assert "clash" not in result.loaded, "an ambiguous id loads neither copy"
    assert len(result.errors) == 1
    message = result.errors[0]
    assert "clash" in message
    assert str(path_one) in message and str(path_two) in message


@pytest.mark.tier0
def test_retaining_the_directory_adds_no_filesystem_walk(tmp_path, monkeypatch):
    """R2 crit 3: retention introduces no directory enumeration beyond the one
    scan() already performs -- iterdir is called once per install location,
    never once per loaded mod, no matter how many retained a directory."""
    _install(tmp_path, "alpha")
    _install(tmp_path, "beta")

    calls = {"iterdir": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self):
        calls["iterdir"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    result = discover([tmp_path], ["alpha", "beta"], core_version=CORE)

    assert result.loaded_ids == ["alpha", "beta"]
    assert result.loaded["alpha"].directory is not None
    assert result.loaded["beta"].directory is not None
    # One install location -> exactly one iterdir. If retention walked per mod,
    # this would be 1 + one-per-mod. It is not.
    assert calls["iterdir"] == 1


@pytest.mark.tier0
def test_scan_groups_by_directory_name_across_locations(tmp_path):
    """Grouping is by directory, because a manifest too broken to parse still
    occupies a slot that must not silently collide."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    _install(first, "clash", body="{{{ not yaml\n")
    _install(second, "clash")

    found, warnings = scan([first, second])

    assert list(found) == ["clash"]
    assert len(found["clash"]) == 2
    assert warnings == []
