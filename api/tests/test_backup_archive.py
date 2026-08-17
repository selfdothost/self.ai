"""Backup archive construction (self.ai#93).

These cover the parts that are cheap to get subtly wrong and expensive to
discover on a restore: scope resolution, the revision stamp, the archive
layout, and — the specific thing today's export path gets wrong — that ids and
ownership survive the round trip through the archive verbatim.
"""

import json
import os
import tarfile
import tempfile

import pytest

from selfai_ui.utils.backup import (
    BACKUP_FORMAT_VERSION,
    SCOPES,
    SELFAI_BACKUP_REPO,
    BackupError,
    read_manifest,
    resolve_scopes,
)

pytestmark = pytest.mark.tier0


####################
# Scope resolution
####################


def test_empty_selection_resolves_to_every_scope():
    assert resolve_scopes(None) == list(SCOPES.keys())
    assert resolve_scopes([]) == list(SCOPES.keys())


def test_selection_is_returned_in_registry_order_not_caller_order():
    # Restore ordering must be deterministic regardless of how the UI sends it.
    ordered = resolve_scopes(["prompts", "chats"])
    assert ordered == [s for s in SCOPES if s in {"prompts", "chats"}]
    assert ordered.index("chats") < ordered.index("prompts")


def test_unknown_scope_is_rejected_rather_than_silently_dropped():
    with pytest.raises(BackupError) as exc:
        resolve_scopes(["chats", "not_a_scope"])
    assert "not_a_scope" in str(exc.value)


def test_config_is_not_an_offerable_scope():
    # self.ai#95: /configs/export returns live secrets unredacted, including
    # the self.corpus credentials the backup writes with. Config stays out of
    # archives until that is gated and redacted.
    assert "config" not in SCOPES


def test_backup_repo_carries_the_selfai_prefix():
    # self.corpus#3's ACL grant is scoped to repository/selfai-* — a repo
    # missing the prefix 401s on create regardless of credential validity.
    assert SELFAI_BACKUP_REPO.startswith("selfai-")


####################
# Manifest / archive layout
####################


def _write_archive(tmpdir, manifest, tables=None):
    """Build a minimal archive by hand, the way build_archive lays one out."""
    root = os.path.join(tmpdir, "src")
    os.makedirs(os.path.join(root, "tables"), exist_ok=True)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    for name, rows in (tables or {}).items():
        with open(os.path.join(root, "tables", f"{name}.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    archive = os.path.join(tmpdir, "archive.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        for entry in sorted(os.listdir(root)):
            tar.add(os.path.join(root, entry), arcname=entry)
    return archive


def test_read_manifest_returns_the_stamp():
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "schema_revision": "f3a7c2d1e8b9",
        "scopes": ["prompts"],
        "tables": {"prompt": 2},
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _write_archive(tmpdir, manifest)
        got = read_manifest(archive)
    assert got["schema_revision"] == "f3a7c2d1e8b9"
    assert got["scopes"] == ["prompts"]


def test_archive_without_a_manifest_is_refused():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = os.path.join(tmpdir, "src")
        os.makedirs(root)
        with open(os.path.join(root, "stray.txt"), "w", encoding="utf-8") as fh:
            fh.write("not a backup")
        archive = os.path.join(tmpdir, "archive.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(os.path.join(root, "stray.txt"), arcname="stray.txt")

        with pytest.raises(BackupError) as exc:
            read_manifest(archive)
    assert "manifest.json" in str(exc.value)


def test_unknown_format_version_is_refused():
    manifest = {"format_version": BACKUP_FORMAT_VERSION + 99, "schema_revision": "abc"}
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = _write_archive(tmpdir, manifest)
        with pytest.raises(BackupError) as exc:
            read_manifest(archive)
    assert "format_version" in str(exc.value)


####################
# The thing today's export gets wrong
####################


def test_serialised_rows_preserve_id_and_owner_verbatim():
    """`POST /chats/import` re-owns every chat to the importer, which is why
    restoring an all-users export would collapse the yard onto one account.
    The archive must carry user_id and id through untouched."""
    from selfai_ui.models.prompts import Prompt
    from selfai_ui.utils.backup import _serialise_row

    row = Prompt(
        command="/greet",
        user_id="owner-not-importer",
        title="Greet",
        content="hello",
        timestamp=1234,
    )
    serialised = _serialise_row(Prompt, row)

    assert serialised["user_id"] == "owner-not-importer"
    assert serialised["command"] == "/greet"
    # Every declared column is present, so a restore has the full row.
    assert set(serialised) == {c.name for c in Prompt.__table__.columns}


def test_serialiser_covers_every_column_of_a_json_bearing_table():
    from selfai_ui.models.chats import Chat
    from selfai_ui.utils.backup import _serialise_row

    row = Chat(
        id="chat-1",
        user_id="owner-1",
        title="t",
        chat={"messages": []},
        meta={"tags": ["a"]},
        created_at=1,
        updated_at=2,
    )
    serialised = _serialise_row(Chat, row)

    assert serialised["id"] == "chat-1"
    assert serialised["user_id"] == "owner-1"
    assert serialised["meta"] == {"tags": ["a"]}
    assert set(serialised) == {c.name for c in Chat.__table__.columns}


####################
# Scope registry integrity
####################


def test_every_scope_table_path_is_importable():
    """A typo'd dotted path would only surface mid-backup, after the job has
    already reported itself running."""
    from selfai_ui.utils.backup import _import_model

    for scope in SCOPES.values():
        for dotted in scope.tables:
            model = _import_model(dotted)
            assert hasattr(model, "__tablename__"), dotted


def test_scopes_declaring_files_are_the_ones_that_reference_blobs():
    carriers = {name for name, scope in SCOPES.items() if scope.carries_files}
    assert carriers == {"knowledge", "files", "voices"}


####################
# File accounting
####################


def test_file_report_accounts_for_every_row(monkeypatch, tmp_path):
    """The counts must reconcile against the number of file rows.

    The first production backup reported `copied: 1` against 36 file rows with
    `missing: 0`, which reads as 35 files lost. Nothing was lost — 35 rows keep
    their content in the `data` column and have no blob — but a backup tool
    whose own numbers do not add up will be distrusted at exactly the moment it
    matters.
    """
    from selfai_ui.utils import backup as backup_module

    class Row:
        def __init__(self, id, path):
            self.id = id
            self.path = path

    rows = [Row("inline-1", None), Row("inline-2", ""), Row("on-disk", "/data/uploads/x.txt")]

    class Query:
        def all(self):
            return rows

    class DB:
        def query(self, _model):
            return Query()

    source = tmp_path / "x.txt"
    source.write_text("blob")
    monkeypatch.setattr(backup_module, "_import_model", lambda dotted: object, raising=False)

    from selfai_ui.storage.provider import Storage

    # `Storage` is a module-level instance, not the class — the patched
    # attribute is looked up on the instance and so takes no `self`.
    monkeypatch.setattr(Storage, "get_file", lambda p: str(source))

    report = backup_module._copy_files(DB(), str(tmp_path))

    assert report["copied"] == 1
    assert report["inline"] == 2
    assert report["missing"] == []
    assert report["copied"] + report["inline"] + len(report["missing"]) == len(rows)
