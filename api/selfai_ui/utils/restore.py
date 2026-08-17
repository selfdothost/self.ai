"""Restore from a backup archive (self.ai#93).

Three rules, all deliberate:

1. **Replace-only, into an empty scope.** If the target scope already holds
   rows, restore refuses. Merge needs per-entity collision rules — chats,
   users and knowledge each want a different answer — and doubles the test
   surface, so it is deferred to its own issue. Merge-only was rejected
   outright: a non-destructive restore into a dirty instance produces a
   half-state that reports success, which is the worst failure mode a backup
   tool can have.

2. **Ids and ownership survive verbatim.** Rows go back exactly as they came
   out. This is the specific thing `POST /chats/import` gets wrong today — it
   re-owns every chat to whoever uploaded it, so restoring an all-users export
   would collapse the whole yard onto one account.

3. **The archive's schema revision is honoured, not assumed.**
   - equal to head → load directly.
   - older than head → load into a throwaway staging schema at the archive's
     own revision, run `alembic upgrade head` against it so the chain's data
     migrations transform the archived rows, then copy the migrated rows into
     the live database.
   - newer than head, or a revision this build has never heard of → refuse.
     Never downgrade.

   Migrating forward is the expensive option and was chosen over
   refuse-unless-exact deliberately: the schema moves often, and a backup that
   stops being restorable a fortnight after it was taken is not a backup.
"""

import json
import logging
import os
import tarfile
import tempfile
import uuid
from contextlib import contextmanager

from sqlalchemy import text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.backup import (
    SCOPES,
    BackupError,
    current_schema_revision,
    read_manifest,
    script_heads,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


class RestoreRefused(BackupError):
    """The restore cannot proceed and no data was touched."""


####################
# Compatibility
####################


def known_revisions() -> set:
    """Every revision this build's migration directory contains."""
    from alembic.script import ScriptDirectory

    from selfai_ui.config import _alembic_config

    script = ScriptDirectory.from_config(_alembic_config())
    return {revision.revision for revision in script.walk_revisions()}


def assess_compatibility(archive_revision: str) -> dict:
    """Decide what, if anything, this archive needs before it can be loaded."""
    current = current_schema_revision()
    heads = script_heads()
    known = known_revisions()

    if archive_revision is None:
        return {
            "plan": "refuse",
            "reason": "archive carries no schema revision stamp",
            "archive_revision": None,
            "current_revision": current,
        }

    if archive_revision == current:
        plan, reason = "direct", "archive matches the running schema revision"
    elif archive_revision not in known:
        # Either a newer build produced it, or it came from a fork. Both mean
        # this pod cannot reason about the gap.
        plan, reason = (
            "refuse",
            (
                f"archive revision {archive_revision!r} is unknown to this build — it was most "
                "likely taken by a newer version. Upgrade before restoring; never downgrade."
            ),
        )
    elif current is not None and _is_ancestor(archive_revision, current):
        plan, reason = (
            "migrate_forward",
            f"archive is older than the running schema; it will be migrated {archive_revision} -> head",
        )
    else:
        plan, reason = (
            "refuse",
            (
                f"archive revision {archive_revision!r} is not an ancestor of the running revision "
                f"{current!r} — restoring it would mean downgrading, which is never done."
            ),
        )

    return {
        "plan": plan,
        "reason": reason,
        "archive_revision": archive_revision,
        "current_revision": current,
        "heads": sorted(heads),
    }


def _is_ancestor(candidate: str, descendant: str) -> bool:
    """True when `candidate` appears in `descendant`'s ancestry."""
    from alembic.script import ScriptDirectory

    from selfai_ui.config import _alembic_config

    script = ScriptDirectory.from_config(_alembic_config())
    for revision in script.walk_revisions("base", descendant):
        if revision.revision == candidate:
            return True
    return False


####################
# Emptiness
####################


def _quote(identifier: str) -> str:
    """Quote a table/column name for interpolation into raw SQL.

    Not optional: the `users` scope includes the `group` table, and `group` is
    a reserved word in both SQLite and Postgres, so an unquoted
    `SELECT count(*) FROM group` is a syntax error. Names come from the model
    metadata and the archive, never from a request, but the double-quote check
    keeps a hand-edited archive from injecting through a column name.
    """
    if '"' in identifier:
        raise RestoreRefused(f"refusing to use an identifier containing a quote: {identifier!r}")
    return f'"{identifier}"'


def _tables_for_scopes(scopes: list) -> list:
    from selfai_ui.utils.backup import _import_model

    tables = []
    for scope_name in scopes:
        for dotted in SCOPES[scope_name].tables:
            model = _import_model(dotted)
            if model.__tablename__ not in tables:
                tables.append(model.__tablename__)
    return tables


def scope_occupancy(scopes: list, acting_user_id: str = None) -> dict:
    """Row counts per table for the scopes being restored.

    `user` and `auth` are counted excluding the admin performing the restore.
    There is no such thing as a genuinely empty user table on a running
    instance — somebody has to be authenticated to ask for the restore — so
    treating their own row as occupancy would make the users scope
    permanently unrestorable.
    """
    from selfai_ui.internal.db import get_db

    counts = {}
    with get_db() as db:
        for table in _tables_for_scopes(scopes):
            if table in {"user", "auth"} and acting_user_id:
                statement = text(f"SELECT count(*) FROM {_quote(table)} WHERE id != :uid")  # noqa: S608
                counts[table] = db.execute(statement, {"uid": acting_user_id}).scalar()
            else:
                counts[table] = db.execute(text(f"SELECT count(*) FROM {_quote(table)}")).scalar()  # noqa: S608
    return counts


def assert_scopes_empty(scopes: list, acting_user_id: str = None) -> dict:
    counts = scope_occupancy(scopes, acting_user_id)
    occupied = {table: n for table, n in counts.items() if n}
    if occupied:
        raise RestoreRefused(
            "restore is replace-only and the target is not empty: "
            + ", ".join(f"{table} has {n} row(s)" for table, n in sorted(occupied.items()))
            + ". Clear these first, or restore into a fresh instance."
        )
    return counts


####################
# Staging database
####################


@contextmanager
def staging_database(job_id: str):
    """A throwaway database the archive can be migrated forward inside.

    SQLite gets a temp file; Postgres gets a schema that is dropped afterwards.
    Both are torn down even when the restore fails — a staging schema left
    behind would be invisible clutter carrying a full copy of the data.
    """
    from sqlalchemy import create_engine

    from selfai_ui.env import DATABASE_URL

    suffix = uuid.UUID(job_id).hex[:12] if _is_uuid(job_id) else "".join(c for c in job_id if c.isalnum())[:12]

    if DATABASE_URL.startswith("sqlite"):
        handle, path = tempfile.mkstemp(prefix=f"selfai-restore-{suffix}-", suffix=".db")
        os.close(handle)
        engine = create_engine(f"sqlite:///{path}")
        try:
            yield engine, None
        finally:
            engine.dispose()
            if os.path.exists(path):
                os.unlink(path)
        return

    schema = f"selfai_restore_{suffix}"
    admin_engine = create_engine(DATABASE_URL)
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
    admin_engine.dispose()

    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine, schema
    finally:
        engine.dispose()
        cleanup_engine = create_engine(DATABASE_URL)
        try:
            with cleanup_engine.connect() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                connection.commit()
        finally:
            cleanup_engine.dispose()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def migrate_staging(engine, target: str) -> None:
    """Run the migration chain against a staging engine, up to `target`."""
    from alembic import command

    from selfai_ui.config import _alembic_config

    cfg = _alembic_config()
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, target)
        connection.commit()


####################
# Loading
####################


def _extract(archive_path: str, dest_dir: str) -> None:
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            # Refuse anything that would write outside dest_dir. An archive is
            # attacker-controlled input the moment restore accepts uploads.
            target = os.path.realpath(os.path.join(dest_dir, member.name))
            if not target.startswith(os.path.realpath(dest_dir) + os.sep):
                raise RestoreRefused(f"archive contains an unsafe path: {member.name!r}")
            if member.issym() or member.islnk():
                raise RestoreRefused(f"archive contains a link, which is not allowed: {member.name!r}")
        tar.extractall(dest_dir)  # noqa: S202 — every member validated above


def _iter_rows(tables_dir: str, table: str):
    path = os.path.join(tables_dir, f"{table}.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _load_rows_into(engine_or_db, table: str, rows: list, json_columns: set) -> int:
    """Insert rows into `table` with their columns exactly as archived."""
    if not rows:
        return 0

    inserted = 0
    for row in rows:
        columns = list(row.keys())
        placeholders = ", ".join(f":{c}" for c in columns)
        column_list = ", ".join(_quote(c) for c in columns)
        params = {}
        for column, value in row.items():
            # JSON columns round-trip as dicts/lists; the driver wants the
            # serialised form for a Text-backed JSONField.
            if column in json_columns and isinstance(value, (dict, list)):
                params[column] = json.dumps(value)
            else:
                params[column] = value
        statement = text(f"INSERT INTO {_quote(table)} ({column_list}) VALUES ({placeholders})")  # noqa: S608
        engine_or_db.execute(statement, params)
        inserted += 1
    return inserted


def _json_columns_for(table: str) -> set:
    """Column names on `table` that hold JSON, so values are re-serialised."""
    from sqlalchemy import JSON

    from selfai_ui.internal.db import Base, JSONField

    for mapper in Base.registry.mappers:
        model = mapper.class_
        if getattr(model, "__tablename__", None) == table:
            return {column.name for column in model.__table__.columns if isinstance(column.type, (JSON, JSONField))}
    return set()


def _split_stored_path(stored_path: str):
    """Recover the (subdirectory, filename) a stored path was written with.

    For local storage `File.path` is the full filesystem path, and the
    subdirectory is load-bearing — Knowledge Base files live under a directory
    named for their KB id, and that is also what routes them to the right
    self.corpus repo. Dropping it would restore every blob into the top level
    and quietly break KB file resolution.
    """
    from selfai_ui.config import UPLOAD_DIR

    upload_root = str(UPLOAD_DIR)
    normalised = os.path.normpath(stored_path)
    if normalised.startswith(os.path.normpath(upload_root) + os.sep):
        relative = os.path.relpath(normalised, upload_root)
        directory = os.path.dirname(relative)
        return (directory or None), os.path.basename(relative)
    # An s3:// key or anything else we did not write: keep the last segment.
    return None, os.path.basename(normalised.rstrip("/"))


def _restore_files(files_dir: str) -> dict:
    """Put archived blobs back where the file rows expect them.

    Goes through `Storage.upload_file` rather than writing to disk directly, so
    a deployment with an S3 provider configured gets its objects put back too.
    """
    from selfai_ui.internal.db import get_db
    from selfai_ui.models.files import File
    from selfai_ui.storage.provider import Storage

    if not os.path.isdir(files_dir):
        return {"restored": 0, "missing": []}

    restored, missing = 0, []
    with get_db() as db:
        for row in db.query(File).all():
            if not row.path:
                continue
            source_dir = os.path.join(files_dir, row.id)
            if not os.path.isdir(source_dir):
                missing.append(row.id)
                continue
            entries = sorted(os.listdir(source_dir))
            if not entries:
                missing.append(row.id)
                continue

            subdirectory, filename = _split_stored_path(row.path)
            source = os.path.join(source_dir, entries[0])
            try:
                with open(source, "rb") as handle:
                    Storage.upload_file(handle, filename, subdirectory)
                restored += 1
            except Exception as e:
                log.warning(f"restore: could not restore blob for file {row.id}: {e}")
                missing.append(row.id)

    if missing:
        # Rows whose blob did not come back are dangling references. Surfaced
        # in the job report rather than swallowed — a restore that silently
        # leaves broken files looks successful and is not.
        log.warning(f"restore: {len(missing)} file blob(s) could not be restored")
    return {"restored": restored, "missing": missing}


####################
# Preview
####################


def preview_archive(archive_path: str, acting_user_id: str = None) -> dict:
    """Everything an admin needs to decide, without changing anything."""
    manifest = read_manifest(archive_path)
    compatibility = assess_compatibility(manifest.get("schema_revision"))

    scopes = manifest.get("scopes") or []
    unknown = [s for s in scopes if s not in SCOPES]
    known_scopes = [s for s in scopes if s in SCOPES]

    occupancy = scope_occupancy(known_scopes, acting_user_id) if known_scopes else {}
    blocking = {table: n for table, n in occupancy.items() if n}

    return {
        "manifest": {
            "format_version": manifest.get("format_version"),
            "created_at": manifest.get("created_at"),
            "created_by": manifest.get("created_by"),
            "schema_revision": manifest.get("schema_revision"),
            "scopes": scopes,
            "tables": manifest.get("tables", {}),
            "files": {
                "copied": manifest.get("files", {}).get("copied", 0),
                # Rows whose content lives in the `data` column rather than on
                # disk — carried by the table dump, with no blob to restore.
                "inline": manifest.get("files", {}).get("inline", 0),
                "missing": len(manifest.get("files", {}).get("missing", [])),
            },
        },
        "compatibility": compatibility,
        "unknown_scopes": unknown,
        "target_occupancy": occupancy,
        "can_restore": compatibility["plan"] != "refuse" and not blocking and not unknown,
        "blocked_by": (
            [f"{table} has {n} row(s)" for table, n in sorted(blocking.items())]
            + ([f"unknown scope(s): {unknown}"] if unknown else [])
            + ([compatibility["reason"]] if compatibility["plan"] == "refuse" else [])
        ),
    }


####################
# The restore itself
####################


def run_restore(archive_path: str, acting_user_id: str, progress=None) -> dict:
    """Load an archive into this instance. Raises on refusal; never partial."""
    manifest = read_manifest(archive_path)
    scopes = manifest.get("scopes") or []

    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise RestoreRefused(f"archive contains scope(s) this build does not know: {sorted(unknown)}")

    compatibility = assess_compatibility(manifest.get("schema_revision"))
    if compatibility["plan"] == "refuse":
        raise RestoreRefused(compatibility["reason"])

    assert_scopes_empty(scopes, acting_user_id)

    tables = _tables_for_scopes(scopes)
    report = {"plan": compatibility["plan"], "tables": {}, "skipped": {}, "files": {}}

    with tempfile.TemporaryDirectory(prefix="selfai-restore-") as workdir:
        _extract(archive_path, workdir)
        tables_dir = os.path.join(workdir, "tables")

        if compatibility["plan"] == "migrate_forward":
            rows_by_table = _migrate_archive_forward(manifest["schema_revision"], tables_dir, tables, progress)
        else:
            rows_by_table = {table: list(_iter_rows(tables_dir, table)) for table in tables}

        _write_rows_into_live(rows_by_table, tables, acting_user_id, report, progress)

        report["files"] = _restore_files(os.path.join(workdir, "files"))

    return report


def _migrate_archive_forward(archive_revision: str, tables_dir: str, tables: list, progress) -> dict:
    """Stage the archive at its own revision, migrate to head, read it back.

    This is what makes an old archive restorable at all: the chain's own data
    migrations run over the archived rows, so the result is what those rows
    would have become had they been in the database the whole time — rather
    than the restore re-implementing each migration's intent by hand.
    """
    log.info(f"restore: staging archive at {archive_revision} to migrate forward")
    staged = {}

    with staging_database(str(uuid.uuid4())) as (engine, schema):
        migrate_staging(engine, archive_revision)

        with engine.begin() as connection:
            for table in tables:
                rows = list(_iter_rows(tables_dir, table))
                if not rows:
                    continue
                _load_rows_into(connection, table, rows, _json_columns_for(table))
                log.info(f"restore: staged {len(rows)} row(s) into {table}")

        if progress:
            progress({"stage": "staged", "revision": archive_revision})

        migrate_staging(engine, "head")
        log.info("restore: staging migrated to head")

        with engine.connect() as connection:
            for table in tables:
                try:
                    result = connection.execute(text(f"SELECT * FROM {_quote(table)}"))  # noqa: S608
                except Exception as e:
                    # A table the archive's revision had may not survive to
                    # head; that is the migration's decision, not an error.
                    log.info(f"restore: {table} not present after migrating forward ({e})")
                    continue
                staged[table] = [dict(row._mapping) for row in result]

    return staged


def _write_rows_into_live(rows_by_table: dict, tables: list, acting_user_id: str, report: dict, progress) -> None:
    """Insert into the live database, parents before children, in one go."""
    from selfai_ui.internal.db import get_db

    with get_db() as db:
        try:
            for table in tables:
                rows = rows_by_table.get(table) or []
                if not rows:
                    report["tables"][table] = 0
                    continue

                skipped = 0
                if table in {"user", "auth"} and acting_user_id:
                    before = len(rows)
                    rows = [r for r in rows if r.get("id") != acting_user_id]
                    skipped = before - len(rows)
                    if skipped:
                        # The only row this can be is the admin running the
                        # restore. Overwriting their own credentials mid-restore
                        # is how you lock yourself out of the instance.
                        report["skipped"][table] = skipped
                        log.warning(
                            f"restore: kept the acting admin's own {table} row, "
                            f"skipped {skipped} archived row(s) with the same id"
                        )

                inserted = _load_rows_into(db, table, rows, _json_columns_for(table))
                report["tables"][table] = inserted
                log.info(f"restore: loaded {inserted} row(s) into {table}")
                if progress:
                    progress({"stage": "loading", "tables": dict(report["tables"])})

            db.commit()
        except Exception:
            db.rollback()
            raise


####################
# Job runner
####################


async def run_restore_job(app_state, job_id: str) -> None:
    """Execute a restore job. Errors mark it failed, loudly."""
    import asyncio

    from selfai_ui.models.backup_jobs import BackupJobs
    from selfai_ui.utils.backup import download_archive

    job = BackupJobs.get_job_by_id(job_id)
    if not job:
        log.error(f"restore: job {job_id} vanished before it started")
        return

    BackupJobs.update_job(job_id, status="running")
    tmp_path = None
    try:
        handle, tmp_path = tempfile.mkstemp(prefix=f"selfai-restore-{job_id}-", suffix=".tar.gz")
        os.close(handle)
        await asyncio.to_thread(download_archive, app_state, job.archive_path, tmp_path)

        def progress(payload):
            BackupJobs.update_job(job_id, progress=payload)

        report = await asyncio.to_thread(run_restore, tmp_path, job.user_id, progress)

        BackupJobs.update_job(job_id, status="completed", progress=report)
        log.info(f"restore: job {job_id} completed ({report['plan']})")
    except Exception as e:
        log.error(f"restore: job {job_id} failed: {e}", exc_info=True)
        BackupJobs.update_job(job_id, status="failed", error_message=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as e:
                log.warning(f"restore: could not remove temp archive {tmp_path}: {e}")
