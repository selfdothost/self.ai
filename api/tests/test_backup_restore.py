"""Restore from a backup archive (self.ai#93).

The load-bearing behaviours here are the refusals. A backup tool that restores
when it should have stopped is worse than one that never restores at all, so
most of these assert that something is *refused* and that nothing was written.
"""

import json
import os
import tarfile
import tempfile

import pytest

from selfai_ui.utils.backup import BACKUP_FORMAT_VERSION, current_schema_revision
from selfai_ui.utils.restore import (
    RestoreRefused,
    _split_stored_path,
    assert_scopes_empty,
    assess_compatibility,
    known_revisions,
    preview_archive,
    run_restore,
)

pytestmark = pytest.mark.tier0


def _build_archive(tmpdir, *, revision, scopes, tables=None, files=None):
    root = os.path.join(tmpdir, "src")
    os.makedirs(os.path.join(root, "tables"), exist_ok=True)

    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": 1_760_000_000,
        "created_by": "archived-admin",
        "schema_revision": revision,
        "scopes": scopes,
        "tables": {name: len(rows) for name, rows in (tables or {}).items()},
        "files": {"copied": 0, "bytes": 0, "missing": []},
    }
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    for name, rows in (tables or {}).items():
        with open(os.path.join(root, "tables", f"{name}.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    if files:
        for file_id, (filename, content) in files.items():
            target = os.path.join(root, "files", file_id)
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, filename), "w", encoding="utf-8") as fh:
                fh.write(content)

    archive = os.path.join(tmpdir, "archive.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        for entry in sorted(os.listdir(root)):
            tar.add(os.path.join(root, entry), arcname=entry)
    return archive


####################
# Compatibility
####################


def test_matching_revision_restores_directly():
    assessment = assess_compatibility(current_schema_revision())
    assert assessment["plan"] == "direct"


def test_unknown_revision_is_refused_never_guessed():
    """An archive from a newer build. This pod cannot reason about the gap, so
    it must not try."""
    assessment = assess_compatibility("ffffffffffff")
    assert assessment["plan"] == "refuse"
    assert "unknown to this build" in assessment["reason"]


def test_missing_revision_stamp_is_refused():
    assessment = assess_compatibility(None)
    assert assessment["plan"] == "refuse"


def test_older_revision_is_migrated_forward_not_refused():
    """The whole reason migrate-forward was chosen over refuse-unless-exact:
    the schema moves often and a backup must outlive the next migration."""
    revisions = known_revisions()
    current = current_schema_revision()
    assert current in revisions

    # The initial revision is an ancestor of everything.
    assessment = assess_compatibility("7e5b5dc7342b")
    assert assessment["plan"] == "migrate_forward", assessment
    assert assessment["archive_revision"] == "7e5b5dc7342b"


####################
# Replace-only
####################


def test_restore_refuses_when_the_target_scope_is_occupied(monkeypatch):
    import selfai_ui.utils.restore as restore_module

    monkeypatch.setattr(restore_module, "scope_occupancy", lambda scopes, uid=None: {"prompt": 3})
    with pytest.raises(RestoreRefused) as exc:
        restore_module.assert_scopes_empty(["prompts"], "admin-1")
    assert "prompt has 3 row(s)" in str(exc.value)
    assert "replace-only" in str(exc.value)


def test_empty_scope_passes_the_check():
    # `prompts` is untouched by the fixture database.
    assert assert_scopes_empty(["prompts"], "admin-1") == {"prompt": 0}


def test_occupancy_ignores_the_acting_admin_for_user_tables():
    """There is no such thing as a genuinely empty user table on a running
    instance — somebody has to be authenticated to ask for the restore."""
    from selfai_ui.internal.db import get_db
    from selfai_ui.models.users import User
    from selfai_ui.utils.restore import scope_occupancy

    with get_db() as db:
        db.execute(
            User.__table__.insert().values(
                id="acting-admin",
                name="Admin",
                email="admin@example.test",
                role="admin",
                profile_image_url="",
                created_at=1,
                updated_at=1,
                last_active_at=1,
            )
        )
        db.commit()

    try:
        assert scope_occupancy(["users"], "acting-admin")["user"] == 0
        assert scope_occupancy(["users"], None)["user"] == 1
    finally:
        with get_db() as db:
            db.execute(User.__table__.delete().where(User.id == "acting-admin"))
            db.commit()


####################
# Round trip
####################


def test_round_trip_preserves_id_and_owner():
    """The specific thing /chats/import gets wrong: it re-owns every chat to
    the importer. A restore must not."""
    from selfai_ui.internal.db import get_db
    from selfai_ui.models.prompts import Prompt

    rows = [
        {
            "command": "/greet",
            "user_id": "someone-else-entirely",
            "title": "Greet",
            "content": "hello",
            "timestamp": 1234,
            "access_control": None,
        }
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _build_archive(
            tmpdir,
            revision=current_schema_revision(),
            scopes=["prompts"],
            tables={"prompt": rows},
        )
        report = run_restore(archive, acting_user_id="admin-doing-the-restore")

    assert report["plan"] == "direct"
    assert report["tables"]["prompt"] == 1

    try:
        with get_db() as db:
            restored = db.query(Prompt).filter_by(command="/greet").first()
            assert restored is not None
            assert restored.user_id == "someone-else-entirely", "restore re-owned the row"
            assert restored.title == "Greet"
    finally:
        with get_db() as db:
            db.execute(Prompt.__table__.delete().where(Prompt.command == "/greet"))
            db.commit()


def test_json_columns_survive_the_round_trip():
    from selfai_ui.internal.db import get_db
    from selfai_ui.models.prompts import Prompt

    rows = [
        {
            "command": "/scoped",
            "user_id": "owner-1",
            "title": "Scoped",
            "content": "x",
            "timestamp": 1,
            "access_control": {"read": {"group_ids": ["g1"]}},
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _build_archive(
            tmpdir, revision=current_schema_revision(), scopes=["prompts"], tables={"prompt": rows}
        )
        run_restore(archive, acting_user_id="admin-1")

    try:
        with get_db() as db:
            restored = db.query(Prompt).filter_by(command="/scoped").first()
            assert restored.access_control == {"read": {"group_ids": ["g1"]}}
    finally:
        with get_db() as db:
            db.execute(Prompt.__table__.delete().where(Prompt.command == "/scoped"))
            db.commit()


@pytest.mark.slow
def test_migrate_forward_restores_an_archive_from_the_initial_revision():
    """The expensive path, exercised for real rather than asserted about.

    At `7e5b5dc7342b` (init) the `prompt` table has five columns and no
    `access_control` — that arrives later, in `922e7a387820_add_group_table`.
    So this archive genuinely does not fit the current schema. The staging
    database is created at the archive's own revision, the rows are loaded
    there, `alembic upgrade head` runs over them, and the migrated rows are
    copied into the live database.

    That is the difference between a backup that survives a schema change and
    one that expires the next time a migration lands.
    """
    from selfai_ui.internal.db import get_db
    from selfai_ui.models.prompts import Prompt

    rows = [
        {
            "command": "/ancient",
            "user_id": "owner-from-the-past",
            "title": "Ancient",
            "content": "written before access_control existed",
            "timestamp": 1,
        }
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _build_archive(
            tmpdir,
            revision="7e5b5dc7342b",
            scopes=["prompts"],
            tables={"prompt": rows},
        )
        report = run_restore(archive, acting_user_id="admin-1")

    assert report["plan"] == "migrate_forward"
    assert report["tables"]["prompt"] == 1

    try:
        with get_db() as db:
            restored = db.query(Prompt).filter_by(command="/ancient").first()
            assert restored is not None, "row did not survive the forward migration"
            assert restored.user_id == "owner-from-the-past"
            assert restored.content == "written before access_control existed"
            # The column the archive never had, supplied by the chain itself.
            assert restored.access_control is None
    finally:
        with get_db() as db:
            db.execute(Prompt.__table__.delete().where(Prompt.command == "/ancient"))
            db.commit()


####################
# Preview
####################


def test_preview_reports_refusal_without_touching_anything():
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _build_archive(tmpdir, revision="ffffffffffff", scopes=["prompts"], tables={"prompt": []})
        preview = preview_archive(archive, acting_user_id="admin-1")

    assert preview["can_restore"] is False
    assert preview["compatibility"]["plan"] == "refuse"
    assert preview["blocked_by"]


def test_preview_reports_a_restorable_archive_as_restorable():
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _build_archive(tmpdir, revision=current_schema_revision(), scopes=["prompts"], tables={"prompt": []})
        preview = preview_archive(archive, acting_user_id="admin-1")

    assert preview["can_restore"] is True
    assert preview["compatibility"]["plan"] == "direct"
    assert preview["manifest"]["created_by"] == "archived-admin"


def test_archive_with_an_unknown_scope_is_refused():
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _build_archive(tmpdir, revision=current_schema_revision(), scopes=["not_a_scope"])
        preview = preview_archive(archive, acting_user_id="admin-1")
        assert preview["can_restore"] is False
        assert preview["unknown_scopes"] == ["not_a_scope"]

        with pytest.raises(RestoreRefused):
            run_restore(archive, acting_user_id="admin-1")


####################
# Archive safety
####################


def test_path_traversal_in_an_archive_is_refused():
    """Restore accepts uploads, so an archive is attacker-controlled input."""
    with tempfile.TemporaryDirectory() as tmpdir:
        payload = os.path.join(tmpdir, "evil.txt")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write("pwned")

        archive = os.path.join(tmpdir, "evil.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            manifest = os.path.join(tmpdir, "manifest.json")
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "format_version": BACKUP_FORMAT_VERSION,
                        "schema_revision": current_schema_revision(),
                        "scopes": ["prompts"],
                    },
                    fh,
                )
            tar.add(manifest, arcname="manifest.json")
            tar.add(payload, arcname="../../escaped.txt")

        with pytest.raises(RestoreRefused) as exc:
            run_restore(archive, acting_user_id="admin-1")
    assert "unsafe path" in str(exc.value)


####################
# Blob paths
####################


def test_stored_path_split_preserves_the_kb_subdirectory():
    """KB files live under a directory named for their KB id, and that is also
    what routes them to the right self.corpus repo."""
    from selfai_ui.config import UPLOAD_DIR

    subdirectory, filename = _split_stored_path(f"{UPLOAD_DIR}/kb-1234/report.pdf")
    assert subdirectory == "kb-1234"
    assert filename == "report.pdf"


def test_stored_path_split_handles_a_top_level_file():
    from selfai_ui.config import UPLOAD_DIR

    subdirectory, filename = _split_stored_path(f"{UPLOAD_DIR}/attachment.png")
    assert subdirectory is None
    assert filename == "attachment.png"


def test_stored_path_split_tolerates_a_foreign_path():
    subdirectory, filename = _split_stored_path("s3://some-bucket/some/key.bin")
    assert subdirectory is None
    assert filename == "key.bin"
