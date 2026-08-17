"""Phase 0 of the Tokenization Studio programme: `workspace` -> `studio` (self.ai#134).

The failure this file exists to prevent is SILENT. `has_permission` denies on any
missing level of the dotted hierarchy, so the moment the call sites ask for
`studio.models`, a group whose stored blob still says `workspace` misses at the
first level and falls through to defaults that are all `False` -- with no
`USER_PERMISSIONS_*` env under `manifests/` to override them. No exception, no
log line: the navigation just stops rendering for every non-admin at once.

Three layers are asserted here, and they are deliberately redundant:

* the Alembic revision rekeys stored blobs (driven through the REAL runner
  against a throwaway SQLite DB, observing rows rather than reading the
  revision's source)
* the transitional dual-read in `has_permission` covers any group the migration
  missed, which is what makes the two halves safe to land in either order
* an end-to-end guard proving an UNMIGRATED group still reaches every Studio
  section -- the treasuremap's lockout scenario, stated as a test

Modelled on `test_crew_mod_migration.py` for the Alembic harness.
"""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

import selfai_ui.env
from selfai_ui.config import USER_PERMISSIONS
from selfai_ui.utils.access_control import get_permissions, has_permission

_API_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _API_ROOT / "selfai_ui" / "alembic.ini"
_MIGRATIONS = _API_ROOT / "selfai_ui" / "migrations"

#: This revision, and the revision immediately before it. Downgrading TO the
#: parent is what "undo the rekey" means; named rather than `-1` so a later
#: revision inserted on top does not silently change what is being tested.
_REVISION = "3f7eff4c5814"
_REVISION_PARENT = "d7e8f9a0b1c2"

#: The six children the rename carries across. `evaluations` is deliberately
#: ABSENT: it is enforced but missing from the default blob (self.ai#133), and
#: this rename must not quietly grant it.
_STUDIO_CHILDREN = {"models", "knowledge", "voices", "prompts", "training", "tools"}

#: Children added AFTER the rename, by later work. Each is a new grant with its
#: own env entry, defaulting False -- which is the thing that keeps it out of
#: self.ai#133's shape, where a key is enforced with no default and no env and
#: is therefore ungrantable. The rename's own guarantee is about the six above:
#: it carried them across and loosened nothing. A later addition is allowed to
#: exist and is still held to defaulting False.
#:
#: `publish` (self.ai#131 R6) gates merging a line's adapters into a new base
#: GGUF. `tokenization` (Phase 2, Decision 11) admits an artist to
#: /studio/tokenization AND to queueing tokenization jobs from it. Neither
#: implies the other, nor either of studio.training.
_STUDIO_CHILDREN_ADDED_LATER = {"publish", "tokenization"}


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_groups(db_path: Path, groups: list[tuple[str, object]]) -> None:
    """Insert group rows with the given raw `permissions` payloads."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            for gid, blob in groups:
                conn.execute(
                    sa.text(
                        'INSERT INTO "group" (id, user_id, name, description, permissions, '
                        "created_at, updated_at) VALUES (:i, :u, :n, :d, :p, :c, :ua)"
                    ),
                    {
                        "i": gid,
                        "u": "seed-user",
                        "n": gid,
                        "d": "",
                        "p": None if blob is None else json.dumps(blob),
                        "c": 0,
                        "ua": 0,
                    },
                )
    finally:
        engine.dispose()


def _read_blobs(db_path: Path) -> dict:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text('SELECT id, permissions FROM "group"')).fetchall()
        out = {}
        for gid, raw in rows:
            out[gid] = None if raw is None else json.loads(raw)
        return out
    finally:
        engine.dispose()


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    """A throwaway DB upgraded to this revision's PARENT, ready to be seeded."""
    db_path = tmp_path / "studio.db"
    monkeypatch.setattr(selfai_ui.env, "DATABASE_URL", f"sqlite:///{db_path}")
    command.upgrade(_cfg(db_path), _REVISION_PARENT)
    return db_path


# --------------------------------------------------------------------------
# T-001: the default blob
# --------------------------------------------------------------------------


def test_default_blob_is_keyed_studio_and_carries_every_expected_child():
    blob = USER_PERMISSIONS.value
    assert "studio" in blob, "the default permission blob must be keyed `studio`"
    assert "workspace" not in blob, "the dead `workspace` key must be gone from defaults"
    # The six the rename carried must all still be here, and nothing may appear
    # that is not either one of them or a deliberate later addition -- an
    # unlisted key means someone added a permission without saying so here.
    assert _STUDIO_CHILDREN <= set(blob["studio"])
    assert set(blob["studio"]) - _STUDIO_CHILDREN == _STUDIO_CHILDREN_ADDED_LATER
    # the rename loosens nothing, and neither does anything added since
    assert all(v is False for v in blob["studio"].values())


def test_every_studio_child_has_an_env_entry():
    """self.ai#133's shape: a key enforced with no default and no env is
    ungrantable by any admin action, and fails silently."""
    import selfai_ui.config as config

    for child in set(USER_PERMISSIONS.value["studio"]):
        assert hasattr(config, f"USER_PERMISSIONS_STUDIO_{child.upper()}_ACCESS"), (
            f"studio.{child} has no USER_PERMISSIONS_STUDIO_{child.upper()}_ACCESS entry, so no admin "
            f"can grant it without hand-editing a group blob (self.ai#133)"
        )


def test_tokenization_is_declared_in_the_defaults_not_only_enforced():
    """A permission missing from the default blob is invisible to the CLIENT
    even for a group that holds it, because `get_permissions` builds its object
    from these defaults -- not just `has_permission`. That is the live defect
    `evaluations` still has (self.ai#133); `tokenization` must not repeat it."""
    blob = USER_PERMISSIONS.value
    assert "tokenization" in blob["studio"]
    assert blob["studio"]["tokenization"] is False


def test_default_blob_leaves_chat_and_features_alone():
    blob = USER_PERMISSIONS.value
    assert set(blob["chat"]) == {"file_upload", "delete", "edit", "temporary"}
    assert set(blob["features"]) == {"web_browsing"}


# --------------------------------------------------------------------------
# T-002: the migration
# --------------------------------------------------------------------------


def test_upgrade_rekeys_workspace_to_studio(migrated_db):
    _seed_groups(
        migrated_db,
        [("g-ws", {"workspace": {"models": True, "tools": False}, "chat": {"delete": True}})],
    )

    command.upgrade(_cfg(migrated_db), _REVISION)

    blob = _read_blobs(migrated_db)["g-ws"]
    assert "workspace" not in blob
    # values preserved EXACTLY, including the explicit False
    assert blob["studio"] == {"models": True, "tools": False}
    # siblings untouched
    assert blob["chat"] == {"delete": True}


def test_upgrade_adds_no_absent_child(migrated_db):
    """Notably not `evaluations` -- see self.ai#133. Granting it here would be
    this revision handing out a permission nobody asked it to hand out."""
    _seed_groups(migrated_db, [("g-min", {"workspace": {"models": True}})])

    command.upgrade(_cfg(migrated_db), _REVISION)

    assert _read_blobs(migrated_db)["g-min"]["studio"] == {"models": True}


def test_upgrade_keeps_unrecognised_children(migrated_db):
    """An admin may hold keys this code does not know about."""
    _seed_groups(migrated_db, [("g-odd", {"workspace": {"models": True, "future_thing": False}})])

    command.upgrade(_cfg(migrated_db), _REVISION)

    assert _read_blobs(migrated_db)["g-odd"]["studio"] == {"models": True, "future_thing": False}


def test_upgrade_prefers_studio_and_does_not_merge_when_both_present(migrated_db):
    """The both-keys rule. A per-child merge would resurrect `tools`, which an
    admin turned OFF under the new name after it was on under the old one."""
    _seed_groups(
        migrated_db,
        [("g-both", {"workspace": {"models": True, "tools": True}, "studio": {"tools": False}})],
    )

    command.upgrade(_cfg(migrated_db), _REVISION)

    blob = _read_blobs(migrated_db)["g-both"]
    assert "workspace" not in blob
    assert blob["studio"] == {"tools": False}, "studio wins whole; the two are not merged"


@pytest.mark.parametrize(
    "gid,blob",
    [
        ("g-null", None),
        ("g-empty", {}),
        ("g-studio-only", {"studio": {"models": True}}),
        ("g-neither", {"chat": {"delete": True}, "features": {"web_browsing": True}}),
        ("g-mods", {"mods": {"crew": {"read": True}}}),
    ],
)
def test_upgrade_is_a_noop_for_blobs_with_nothing_to_move(migrated_db, gid, blob):
    _seed_groups(migrated_db, [(gid, blob)])

    command.upgrade(_cfg(migrated_db), _REVISION)

    assert _read_blobs(migrated_db)[gid] == blob


def test_downgrade_is_a_real_inverse(migrated_db):
    _seed_groups(migrated_db, [("g-ws", {"workspace": {"models": True}, "chat": {"delete": True}})])

    command.upgrade(_cfg(migrated_db), _REVISION)
    command.downgrade(_cfg(migrated_db), _REVISION_PARENT)

    blob = _read_blobs(migrated_db)["g-ws"]
    assert "studio" not in blob
    assert blob["workspace"] == {"models": True}
    assert blob["chat"] == {"delete": True}


def test_upgrade_is_idempotent_across_a_down_up_cycle(migrated_db):
    seed = {"workspace": {"models": True, "tools": False}, "chat": {"delete": True}}
    _seed_groups(migrated_db, [("g-ws", seed)])

    command.upgrade(_cfg(migrated_db), _REVISION)
    once = _read_blobs(migrated_db)["g-ws"]
    command.downgrade(_cfg(migrated_db), _REVISION_PARENT)
    command.upgrade(_cfg(migrated_db), _REVISION)

    assert _read_blobs(migrated_db)["g-ws"] == once


# --------------------------------------------------------------------------
# T-003 / T-006: the dual-read, and the lockout guard
# --------------------------------------------------------------------------


class _Group:
    def __init__(self, gid, permissions):
        self.id = gid
        self.permissions = permissions


@pytest.fixture
def as_groups(monkeypatch):
    """Drive has_permission against an arbitrary set of group blobs."""

    def _install(*blobs):
        groups = [_Group(f"g{i}", b) for i, b in enumerate(blobs)]
        monkeypatch.setattr(
            "selfai_ui.utils.access_control.Groups.get_groups_by_member_id",
            lambda _user_id: groups,
        )

    return _install


_ALL_FALSE = {"studio": {k: False for k in _STUDIO_CHILDREN}}


def test_unmigrated_group_still_reaches_every_studio_section(as_groups):
    """THE LOCKOUT GUARD (T-006). This is the treasuremap's silent failure,
    written as a test: a group whose blob predates the rekey must keep working."""
    as_groups({"workspace": {k: True for k in _STUDIO_CHILDREN}})

    for child in sorted(_STUDIO_CHILDREN):
        assert has_permission("u", f"studio.{child}", _ALL_FALSE) is True, child


def test_migrated_group_resolves_without_the_fallback(as_groups):
    as_groups({"studio": {"models": True}})
    assert has_permission("u", "studio.models", _ALL_FALSE) is True


def test_explicit_false_under_studio_is_not_overridden_by_workspace(as_groups):
    """The case that distinguishes a MISSING key from a FALSY one. The fallback
    must fire only on absence; otherwise an admin's deliberate revocation is
    undone by the stale half of a half-migrated blob."""
    as_groups({"studio": {"tools": False}, "workspace": {"tools": True}})
    assert has_permission("u", "studio.tools", _ALL_FALSE) is False


def test_fallback_does_not_apply_to_other_top_level_groups(as_groups):
    """Scoped to the first segment and to the one literal. A `chat.*` or
    `mods.*` traversal must be unchanged, and nothing outside the renamed group
    may be turned from a deny into an allow."""
    as_groups({"workspace": {"models": True}, "chat": {"delete": False}})
    assert has_permission("u", "chat.delete", {"chat": {"delete": False}}) is False
    assert has_permission("u", "mods.crew.read", {}) is False
    # a non-studio first segment never consults `workspace`
    assert has_permission("u", "features.web_browsing", {}) is False


def test_user_in_no_group_falls_back_to_the_default_blob(as_groups):
    as_groups()
    assert has_permission("u", "studio.models", {"studio": {"models": True}}) is True
    assert has_permission("u", "studio.models", _ALL_FALSE) is False


def test_neither_key_present_denies(as_groups):
    as_groups({"chat": {"delete": True}})
    assert has_permission("u", "studio.models", _ALL_FALSE) is False


# --------------------------------------------------------------------------
# get_permissions -- the SECOND traversal, the one the client reads
# --------------------------------------------------------------------------
# `has_permission` is the server-side gate. `get_permissions` builds the object
# returned by routers/auths.py on signin, signup and session, which self.chat
# stores as `$user.permissions` and gates every Studio nav entry on. Fixing only
# the first leaves the API permitting calls the UI never offers a way to make --
# the same silent lockout, relocated to a worse place to find it.


def test_get_permissions_folds_an_unmigrated_group_onto_studio(as_groups):
    """THE CLIENT-SIDE LOCKOUT GUARD, the mirror of
    test_unmigrated_group_still_reaches_every_studio_section.

    Without the fold, combine_permissions merges the group's blob over defaults
    that now carry a full `studio` block of False, so both keys survive and the
    all-False one is the name the client looks up."""
    as_groups({"workspace": {k: True for k in _STUDIO_CHILDREN}})

    resolved = get_permissions("u", _ALL_FALSE)

    for child in sorted(_STUDIO_CHILDREN):
        assert resolved["studio"][child] is True, child


def test_get_permissions_does_not_hand_the_client_the_stale_key(as_groups):
    """The stale name must not survive into the response at all. Two keys
    describing the same grant is how the halves drift apart again later."""
    as_groups({"workspace": {"models": True}})

    resolved = get_permissions("u", _ALL_FALSE)

    assert "workspace" not in resolved
    assert resolved["studio"]["models"] is True


def test_get_permissions_prefers_studio_when_a_blob_carries_both(as_groups):
    """Same rule as the Alembic revision and has_permission's fallback: studio
    wins, workspace is dropped. An admin's deliberate False is not resurrected
    by the stale half of a half-migrated blob."""
    as_groups({"studio": {"tools": False}, "workspace": {"tools": True}})

    resolved = get_permissions("u", _ALL_FALSE)

    assert resolved["studio"]["tools"] is False
    assert "workspace" not in resolved


def test_get_permissions_leaves_other_top_level_groups_alone(as_groups):
    """Scoped to the one renamed group. chat/features/mods pass through with
    the most-permissive merge they have always had."""
    as_groups({"chat": {"delete": True}, "mods": {"crew": {"read": True}}})

    resolved = get_permissions("u", {"studio": {"models": False}, "chat": {"delete": False}})

    assert resolved["chat"]["delete"] is True
    assert resolved["mods"]["crew"]["read"] is True
    assert resolved["studio"]["models"] is False


def test_get_permissions_still_takes_the_most_permissive_across_groups(as_groups):
    """The fold must not disturb the multi-group merge: one group's True still
    wins over another's False, whichever key name each of them uses."""
    as_groups(
        {"workspace": {"models": True, "tools": False}},
        {"studio": {"tools": True}},
    )

    resolved = get_permissions("u", _ALL_FALSE)

    assert resolved["studio"]["models"] is True
    assert resolved["studio"]["tools"] is True
    assert "workspace" not in resolved


def test_get_permissions_is_unchanged_for_a_fully_migrated_group(as_groups):
    """No fold, no stale key, no surprises -- the steady state after the
    migration has run everywhere and before the fallback is removed."""
    as_groups({"studio": {"models": True}})

    resolved = get_permissions("u", _ALL_FALSE)

    assert resolved["studio"]["models"] is True
    assert "workspace" not in resolved
