"""The facade's boundary checks.

These run in CI as part of `test:api`, which is a build-failing job -- that is
the "wired into CI" half of cavekit-mods-facade.md R2.

Cavekit: cavekit-mods-facade.md R2, R3; cavekit-mods-manifest.md R7;
cavekit-toolspec-model.md R1
Tasks: T-014, T-015, T-016, T-018; T-006
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from selfai_ui import modapi

API_ROOT = Path(__file__).resolve().parents[1]
APP_PACKAGE = "selfai_ui"
FACADE_MODULE = "selfai_ui.modapi"

#: Where installed mods live. Empty during Phase 0 -- the reference mod is
#: Phase 1 -- which is exactly why the check is built now: it must exist
#: BEFORE the first mod, or the first mod gets written against internals and
#: the boundary is lost before it is ever enforced.
MODS_DIR = API_ROOT / "mods"

#: Snapshot of the facade's declared exports. A name disappearing from
#: `__all__` is a broken compatibility promise, so it must be a deliberate act
#: with a major-version bump, not a quiet edit.
EXPORT_SNAPSHOT = Path(__file__).parent / "data" / "modapi_exports.json"


def _application_imports(source: str) -> list[str]:
    """Every `selfai_ui.*` module a source file imports, statically."""
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.split(".")[0] == APP_PACKAGE)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports have no module of ours to resolve; level>0 means
            # intra-mod, which is fine and not an application-package import.
            if node.level == 0 and node.module and node.module.split(".")[0] == APP_PACKAGE:
                found.append(node.module)
    return found


def _violations(source: str) -> list[str]:
    """Application-package imports that are not the facade."""
    return [
        module
        for module in _application_imports(source)
        if module != FACADE_MODULE and not module.startswith(FACADE_MODULE + ".")
    ]


# --- T-014: import boundary ------------------------------------------------
@pytest.mark.tier0
def test_no_mod_in_this_repository_imports_outside_the_facade():
    """Passes vacuously in Phase 0. That is deliberate, not a reason to skip it.

    The check has to exist before the first mod does. Its first non-vacuous run
    is against the Phase 1 reference mod.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(MODS_DIR.rglob("*.py")) if MODS_DIR.is_dir() else []:
        bad = _violations(path.read_text(encoding="utf-8"))
        if bad:
            offenders[str(path.relative_to(API_ROOT))] = bad

    assert not offenders, f"mods importing outside the facade: {offenders}"


@pytest.mark.tier0
def test_the_boundary_check_catches_a_violating_mod():
    """The negative case: a check that cannot fail proves nothing."""
    violating = "from selfai_ui.utils.access_control import has_permission\n"
    assert _violations(violating) == ["selfai_ui.utils.access_control"]

    aliased = "import selfai_ui.socket.main as internals\n"
    assert _violations(aliased) == ["selfai_ui.socket.main"]

    permitted = "from selfai_ui.modapi import has_permission, sio\n"
    assert _violations(permitted) == []


@pytest.mark.tier0
def test_dynamic_import_evasion_is_documented_as_undetected():
    """R2 AC5 offers a choice: detect dynamic imports, or say plainly you don't.

    This check is static, so `importlib.import_module("selfai_ui.socket.main")`
    slips past it. Saying so is the honest half of the requirement -- a mod is
    trusted code and could bypass the check regardless.
    """
    evasion = 'import importlib\nm = importlib.import_module("selfai_ui.socket.main")\n'
    assert _violations(evasion) == [], "expected the static check to miss this"

    doc = (modapi.__doc__ or "").lower()
    assert "not enforced for third-party" in doc


# --- T-015: export-list stability ------------------------------------------
@pytest.mark.tier0
def test_no_declared_export_disappears_without_a_major_bump():
    """A name promised in `__all__` and then removed is a broken promise.

    The snapshot is the previous surface. Adding is free; removing or renaming
    requires bumping the recorded major version in the same commit, which makes
    the break deliberate and reviewable rather than incidental.
    """
    if not EXPORT_SNAPSHOT.exists():
        pytest.skip("no export snapshot recorded yet")

    recorded = json.loads(EXPORT_SNAPSHOT.read_text(encoding="utf-8"))
    previous = set(recorded["exports"])
    current = set(modapi.__all__)

    removed = sorted(previous - current)
    if not removed:
        return

    previous_major = int(str(recorded["core_version"]).split(".")[0])
    current_major = int(str(modapi.CORE_VERSION).split(".")[0])
    assert current_major > previous_major, (
        f"facade exports removed without a major-version increment: {removed} "
        f"(core {recorded['core_version']} -> {modapi.CORE_VERSION})"
    )


@pytest.mark.tier0
def test_export_snapshot_is_current():
    """The snapshot must track additions, or it silently stops protecting."""
    if not EXPORT_SNAPSHOT.exists():
        pytest.skip("no export snapshot recorded yet")

    recorded = set(json.loads(EXPORT_SNAPSHOT.read_text(encoding="utf-8"))["exports"])
    added = sorted(set(modapi.__all__) - recorded)
    assert not added, f"facade gained exports not in the snapshot: {added} — update {EXPORT_SNAPSHOT.name}"


# --- T-016: facade-only smoke ----------------------------------------------
@pytest.mark.tier0
def test_the_five_things_a_mod_must_do_need_only_the_facade():
    """R1 AC6: mount a route, check a scope, read the db, emit, return a spec.

    Asserted against the facade's own surface rather than by writing a mod,
    because there are no mods in Phase 0. The fixture mod that exercises this
    for real is Phase 1.
    """
    from selfai_ui import modapi as f

    # mount a route: usable as a FastAPI dependency
    assert callable(f.get_verified_user)
    # check a scope
    assert callable(f.has_permission)
    # read from the database
    assert callable(f.get_db)
    # emit an event
    assert hasattr(f.sio, "emit")
    # return a tool spec
    assert callable(f.get_tools_specs) and f.ToolSpec is not None


@pytest.mark.tier0
def test_the_facade_exports_the_real_toolspec_model_not_a_dict_alias():
    """cavekit-toolspec-model.md R1 AC5 -- T-006.

    `ToolSpec` was `dict[str, Any]` here while core's spec was untyped. Asserting
    only that it is "not None" would keep passing if it silently reverted to an
    alias, so this pins the identity: the facade's name must BE the model core
    itself validates against, or a mod and core disagree about the same word.
    """
    from selfai_ui import modapi as f
    from selfai_ui.utils.toolspec import ToolSpec as CoreToolSpec

    assert f.ToolSpec is CoreToolSpec
    assert isinstance(f.ToolSpec, type) and issubclass(f.ToolSpec, BaseModel)
    assert f.ToolSpec is not dict and f.ToolSpec is not dict[str, Any]

    # Constructed through the facade, it validates rather than accepting anything.
    spec = f.ToolSpec(name="get_weather")
    assert spec.name == "get_weather"
    assert spec.input_schema == {"type": "object", "properties": {}}

    with pytest.raises(ValidationError):
        f.ToolSpec()  # `name` is required
    with pytest.raises(ValidationError):
        f.ToolSpec(name="get_weather", parameters={})  # extra keys are forbidden

    # The promise is a type change, not a surface change.
    assert "ToolSpec" in modapi.__all__


@pytest.mark.tier0
def test_the_facade_itself_is_the_only_application_import_a_mod_needs():
    """A fixture source importing only the facade has zero violations."""
    fixture = (
        "from selfai_ui.modapi import (\n"
        "    CORE_VERSION, ToolSpec, get_db, get_tools_specs,\n"
        "    get_verified_user, has_permission, sio,\n"
        ")\n"
    )
    assert _violations(fixture) == []


# --- T-018: manifest parser dependencies are declared ----------------------
@pytest.mark.tier0
def test_every_third_party_import_of_the_manifest_layer_is_declared():
    """R7: parsing must not rest on a transitively-resolved package.

    yaml resolved only transitively until it was pinned; this keeps it that
    way for anything the manifest layer grows later.
    """
    requirements = (API_ROOT / "requirements-api.txt").read_text(encoding="utf-8").lower()
    declared = {line.split("=")[0].split(">")[0].split("<")[0].strip() for line in requirements.splitlines()}

    # Ask the interpreter what the standard library is rather than keeping a
    # hand-written list. An earlier version enumerated the modules the manifest
    # layer happened to import that day, and broke the moment it imported one
    # more stdlib module — a maintenance trap that reports a false violation.
    stdlib_or_local = set(sys.stdlib_module_names) | {APP_PACKAGE}

    # Import name != distribution name for some packages. Mapped explicitly so
    # the check is a real lookup rather than an accidental substring match
    # ("yaml" happens to appear inside "pyyaml").
    distribution_of = {"yaml": "pyyaml"}

    for module_file in sorted((API_ROOT / APP_PACKAGE / "mods").glob("*.py")):
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in stdlib_or_local:
                    continue
                distribution = distribution_of.get(name, name)
                assert distribution in declared, (
                    f"{module_file.name} imports {name!r} (distribution {distribution!r}), "
                    f"which is not declared in requirements-api.txt — it would resolve "
                    f"only transitively"
                )


# --- the off-request scope-check surface -----------------------------------
@pytest.mark.tier0
def test_the_facade_exposes_the_instance_permission_defaults():
    """A mod must be able to check a scope where there is no request.

    `has_permission`'s third argument defaults to `{}`, so a two-argument call
    denies every scope granted through instance defaults rather than through a
    group -- silently, and in the "fails closed" direction that looks like a
    correct refusal. On an HTTP path a mod reads
    `request.app.state.config.USER_PERMISSIONS`; a Socket.IO handler has no
    request and, before this export, had no facade-sanctioned source at all.
    `self.crew#141`'s crew mod pinned `selfai_ui.config` directly for exactly
    this and flagged the import as a gap rather than a shrug.
    """
    from selfai_ui import config as config_module
    from selfai_ui import modapi as f

    assert callable(f.permission_defaults)
    assert isinstance(f.permission_defaults(), dict)

    # It reads the LIVE value, never a cached one: a grant made after import --
    # by an admin at any point in the process's life -- must be visible.
    original = config_module.USER_PERMISSIONS.value
    try:
        config_module.USER_PERMISSIONS.value = {"mods": {"facadecheck": {"use": True}}}
        assert f.permission_defaults() == {"mods": {"facadecheck": {"use": True}}}
        assert f.has_permission("nobody", "mods.facadecheck.use", f.permission_defaults()) is True
        # The two-argument call is the trap this export exists to remove.
        assert f.has_permission("nobody", "mods.facadecheck.use") is False
    finally:
        config_module.USER_PERMISSIONS.value = original


@pytest.mark.tier0
def test_permission_defaults_is_the_same_tree_the_request_path_reads():
    """The off-request read and the request-scoped read must not diverge.

    `main.py` assigns the module-level `USER_PERMISSIONS` onto
    `app.state.config`, so both resolve to one `PersistentConfig`. If that ever
    stopped being true, a mod's socket handler and its route handler would
    disagree about the same grant -- which is worse than either being wrong.
    """
    from selfai_ui import modapi as f
    from selfai_ui.main import app

    assert f.permission_defaults() == (app.state.config.USER_PERMISSIONS or {})


@pytest.mark.tier0
def test_the_facade_exposes_socket_connect_identity():
    """`register_ws` handlers need the authenticated user behind a sid.

    Core auth-gates every mod namespace on connect and records the identity, but
    the resolver lived only in `selfai_ui.socket.main` -- so a namespace handler
    could not do an ownership check without an internal import. Pinned to the
    real function, not merely to "something callable": a re-export that drifted
    to a lookalike would silently answer from a different pool.
    """
    from selfai_ui import modapi as f
    from selfai_ui.socket.main import get_user_id_from_session_pool as core_resolver

    assert f.get_user_id_from_session_pool is core_resolver
    assert "get_user_id_from_session_pool" in f.__all__
