"""Backup archive construction and the self.corpus store behind it (self.ai#93).

Two things here are deliberate and easy to undo by accident:

1. **Rows are dumped generically off the SQLAlchemy tables, not through the
   model accessor classes.** The accessors reshape, filter by user, and in one
   case re-own what they touch (`Chats.import_chat` always re-owns to the
   importer, which is exactly what makes today's "Export All Chats" not a
   backup). Walking `__table__.columns` preserves ids, ownership and
   `access_control` verbatim, which is the whole requirement.

2. **Nothing here is best-effort.** Every self.corpus path already in the
   codebase swallows and logs — correct for mirroring Knowledge Base files,
   where a corpus outage must not break the KB feature. It is exactly wrong
   here: a swallowed upload produces a job marked `completed` with nothing
   behind it, which is the worst failure a backup tool can have. Errors raise
   and fail the job.

Config is **not** a scope. `GET /configs/export` returns live secrets
unredacted (self.ai#95), including the very self.corpus credentials this module
writes with, so config stays out of archives until #95's gating and redaction
land.
"""

import json
import logging
import os
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


# Bumped when the on-disk archive layout changes in a way a reader must know
# about. Restore refuses a version it does not recognise.
BACKUP_FORMAT_VERSION = 1

# self.corpus's ACL grant for self.ai-public is scoped to
# arn:lakefs:fs:::repository/selfai-* — a repo missing this prefix 401s on
# create/delete regardless of credential validity (self.corpus#3). Same
# constraint as utils/self_corpus.SELFAI_REPO_PREFIX.
SELFAI_BACKUP_REPO = "selfai-backups"
BACKUP_BRANCH = "main"


class BackupError(Exception):
    """Fatal to the job. Never swallowed."""


####################
# Scope registry
####################


@dataclass
class Scope:
    """One selectable unit of a backup.

    `tables` are dotted paths to the SQLAlchemy model class, imported lazily so
    this module does not drag the whole model layer in at import time. Order
    matters on restore — parents before children.
    """

    name: str
    description: str
    tables: list[str] = field(default_factory=list)
    # Whether rows in this scope reference blobs on the storage provider.
    carries_files: bool = False


SCOPES: dict[str, Scope] = {
    "users": Scope(
        name="users",
        description="User accounts and groups. Auth rows are included — restoring users restores their logins.",
        tables=["selfai_ui.models.users.User", "selfai_ui.models.auths.Auth", "selfai_ui.models.groups.Group"],
    ),
    "chats": Scope(
        name="chats",
        description="Chats, folders, tags, channels and messages.",
        tables=[
            "selfai_ui.models.folders.Folder",
            "selfai_ui.models.chats.Chat",
            "selfai_ui.models.tags.Tag",
            "selfai_ui.models.channels.Channel",
            "selfai_ui.models.messages.Message",
            "selfai_ui.models.messages.MessageReaction",
        ],
    ),
    "knowledge": Scope(
        name="knowledge",
        description="Knowledge Bases and their linked files, including the file contents.",
        tables=["selfai_ui.models.knowledge.Knowledge", "selfai_ui.models.knowledge.KnowledgeFile"],
        carries_files=True,
    ),
    "files": Scope(
        name="files",
        description="Uploaded file rows and their contents (chat attachments and Knowledge Base sources).",
        tables=["selfai_ui.models.files.File"],
        carries_files=True,
    ),
    "prompts": Scope(
        name="prompts",
        description="Saved prompts.",
        tables=["selfai_ui.models.prompts.Prompt"],
    ),
    "models": Scope(
        name="models",
        description="Model definitions and their presets.",
        tables=["selfai_ui.models.models.Model"],
    ),
    "tools": Scope(
        name="tools",
        description="Tools and functions.",
        tables=["selfai_ui.models.tools.Tool", "selfai_ui.models.functions.Function"],
    ),
    "voices": Scope(
        name="voices",
        description="Workspace voices and their sample files.",
        tables=["selfai_ui.models.voices.Voice", "selfai_ui.models.voices.VoiceFile"],
        carries_files=True,
    ),
    "memories": Scope(
        name="memories",
        description="Per-user memories.",
        tables=["selfai_ui.models.memories.Memory"],
    ),
    "evaluations": Scope(
        name="evaluations",
        description="Feedback rows and evaluation job history.",
        tables=["selfai_ui.models.feedbacks.Feedback", "selfai_ui.models.eval_jobs.EvalJob"],
    ),
}


def resolve_scopes(requested: Optional[list[str]]) -> list[str]:
    """Resolve a scope selection to a concrete, ordered list.

    An empty/omitted selection means "everything the registry knows about",
    resolved *now* rather than stored as a wildcard — an archive must record
    what it actually took, not a token that means something different when the
    registry grows a scope later.
    """
    if not requested:
        return list(SCOPES.keys())
    unknown = [s for s in requested if s not in SCOPES]
    if unknown:
        raise BackupError(f"unknown scope(s): {sorted(unknown)}. known: {sorted(SCOPES)}")
    # Preserve registry order so restore ordering is deterministic.
    return [s for s in SCOPES if s in requested]


def _import_model(dotted: str):
    module_path, _, class_name = dotted.rpartition(".")
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


####################
# Schema revision
####################


def current_schema_revision() -> Optional[str]:
    """The Alembic revision the live database is actually at.

    Read back out of the database rather than inferred, the same posture
    `config._assert_at_head` takes at boot — an archive stamped with what we
    believe the schema to be is worth nothing on restore.
    """
    from alembic.runtime.migration import MigrationContext

    from selfai_ui.internal.db import engine

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def script_heads() -> set:
    """Revision head(s) this pod's migration directory knows about."""
    from alembic.script import ScriptDirectory

    from selfai_ui.config import _alembic_config

    return set(ScriptDirectory.from_config(_alembic_config()).get_heads())


####################
# Archive construction
####################


def _serialise_row(model, row) -> dict:
    out = {}
    for column in model.__table__.columns:
        value = getattr(row, column.name, None)
        if isinstance(value, (bytes, bytearray)):
            # No column is bytes today; encode rather than crash if one appears.
            value = value.decode("utf-8", errors="replace")
        out[column.name] = value
    return out


def _dump_tables(db, scope: Scope, tmpdir: str) -> dict:
    """Write one JSONL file per table. Returns {table_name: row_count}."""
    counts = {}
    tables_dir = os.path.join(tmpdir, "tables")
    os.makedirs(tables_dir, exist_ok=True)

    for dotted in scope.tables:
        try:
            model = _import_model(dotted)
        except (ImportError, AttributeError) as e:
            raise BackupError(f"scope {scope.name!r}: cannot import {dotted}: {e}") from e

        table_name = model.__tablename__
        path = os.path.join(tables_dir, f"{table_name}.jsonl")
        count = 0
        with open(path, "w", encoding="utf-8") as fh:
            for row in db.query(model).all():
                fh.write(json.dumps(_serialise_row(model, row), default=str, ensure_ascii=False))
                fh.write("\n")
                count += 1
        counts[table_name] = count
        log.info(f"backup: dumped {count} row(s) from {table_name}")

    return counts


def _copy_files(db, tmpdir: str) -> dict:
    """Copy every referenced blob into the archive.

    Rows without files restore dangling references and look successful doing
    it, which is worse than failing — so a file row whose blob cannot be read
    is recorded in `missing` and surfaced, not silently skipped.

    Rows with no `path` at all are a different case and are counted as
    `inline`: their content lives in the `data` column, so the table dump
    already carries it and there is no blob to copy. That count exists because
    the first production run reported `copied: 1` against 36 file rows and
    `missing: 0`, which reads as "35 files were lost" until you go and check.
    Nothing was lost — but a backup tool whose own numbers do not reconcile is
    asking to be distrusted at exactly the wrong moment.
    """
    import shutil

    from selfai_ui.models.files import File
    from selfai_ui.storage.provider import Storage

    files_dir = os.path.join(tmpdir, "files")
    os.makedirs(files_dir, exist_ok=True)

    copied, total_bytes, missing, inline = 0, 0, [], 0
    for row in db.query(File).all():
        if not row.path:
            inline += 1
            continue
        try:
            local_path = Storage.get_file(row.path)
            dest_dir = os.path.join(files_dir, row.id)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(row.path))
            shutil.copyfile(local_path, dest)
            total_bytes += os.path.getsize(dest)
            copied += 1
        except Exception as e:
            log.warning(f"backup: file {row.id} ({row.path}) unreadable: {e}")
            missing.append({"id": row.id, "path": row.path, "error": str(e)})

    log.info(f"backup: {copied} blob(s) copied, {inline} row(s) carry inline data, {len(missing)} unreadable")
    return {"copied": copied, "bytes": total_bytes, "inline": inline, "missing": missing}


def build_archive(scopes: list[str], created_by: str, dest_path: str) -> dict:
    """Build a .tar.gz archive at `dest_path`. Returns the manifest.

    Layout:
        manifest.json
        tables/<table_name>.jsonl
        files/<file_id>/<filename>
    """
    from selfai_ui.internal.db import get_db

    revision = current_schema_revision()
    if revision is None:
        raise BackupError("database reports no Alembic revision; refusing to take an unstamped backup")

    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": int(time.time()),
        "created_by": created_by,
        "schema_revision": revision,
        "scopes": scopes,
        "tables": {},
        "files": {"copied": 0, "bytes": 0, "inline": 0, "missing": []},
    }

    with tempfile.TemporaryDirectory(prefix="selfai-backup-") as tmpdir:
        with get_db() as db:
            wants_files = False
            for scope_name in scopes:
                scope = SCOPES[scope_name]
                manifest["tables"].update(_dump_tables(db, scope, tmpdir))
                wants_files = wants_files or scope.carries_files

            if wants_files:
                manifest["files"] = _copy_files(db, tmpdir)

        with open(os.path.join(tmpdir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        with tarfile.open(dest_path, "w:gz") as tar:
            for entry in sorted(os.listdir(tmpdir)):
                tar.add(os.path.join(tmpdir, entry), arcname=entry)

    manifest["archive_bytes"] = os.path.getsize(dest_path)
    return manifest


def read_manifest(archive_path: str) -> dict:
    """Read manifest.json out of an archive without extracting the rest."""
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember("manifest.json")
        except KeyError as e:
            raise BackupError("archive has no manifest.json — not a self.ai backup") from e
        fh = tar.extractfile(member)
        if fh is None:
            raise BackupError("archive manifest.json is not a regular file")
        manifest = json.loads(fh.read().decode("utf-8"))

    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"archive format_version {manifest.get('format_version')!r} is not supported "
            f"by this build (expects {BACKUP_FORMAT_VERSION})"
        )
    return manifest


####################
# self.corpus store — strict, never best-effort
####################


def _corpus_settings(app_state) -> tuple[str, str, str]:
    cfg = app_state.config
    if not getattr(cfg, "ENABLE_SELF_CORPUS", False):
        raise BackupError("ENABLE_SELF_CORPUS is False — no archive store is configured")
    endpoint = (cfg.SELF_CORPUS_LAKEFS_ENDPOINT or "").rstrip("/")
    key = cfg.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID or ""
    secret = cfg.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY or ""
    if not (endpoint and key and secret):
        raise BackupError("self.corpus endpoint/credentials are incomplete")
    return endpoint, key, secret


def _boto_client(endpoint: str, key: str, secret: str):
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=BotoConfig(s3={"addressing_style": "path"}),
    )


async def ensure_backup_repo(app_state) -> None:
    """Create `selfai-backups` if it does not exist. Idempotent."""
    from selfai_ui.utils.self_corpus import SelfCorpusError, create_repository

    endpoint, key, secret = _corpus_settings(app_state)
    try:
        await create_repository(
            endpoint=endpoint,
            access_key_id=key,
            secret_access_key=secret,
            repo_id=SELFAI_BACKUP_REPO,
            default_branch=BACKUP_BRANCH,
        )
        log.info(f"backup: created self.corpus repo {SELFAI_BACKUP_REPO}")
    except SelfCorpusError as e:
        # Already-exists is the steady state, not a failure. Anything else is.
        if "409" in str(e) or "already exists" in str(e).lower():
            return
        raise BackupError(f"could not ensure backup repo {SELFAI_BACKUP_REPO}: {e}") from e


def put_archive(app_state, local_path: str, object_key: str) -> str:
    """Upload an archive and commit it. Returns the LakeFS commit id.

    The S3 gateway only *stages* an object on the branch; without the commit
    the archive is unaddressable by ref and silently losable. Both steps must
    succeed or the job fails.
    """
    endpoint, key, secret = _corpus_settings(app_state)

    client = _boto_client(endpoint, key, secret)
    try:
        client.upload_file(local_path, SELFAI_BACKUP_REPO, f"{BACKUP_BRANCH}/{object_key}")
    except Exception as e:
        raise BackupError(f"archive upload to {SELFAI_BACKUP_REPO} failed: {e}") from e

    try:
        import lakefs
        from lakefs.client import Client as LakeFSClient

        lakefs_client = LakeFSClient(host=endpoint, username=key, password=secret)
        branch = lakefs.Repository(SELFAI_BACKUP_REPO, client=lakefs_client).branch(BACKUP_BRANCH)
        ref = branch.commit(message=f"backup {object_key}")
        log.info(f"backup: committed {object_key} as {ref.id}")
        return ref.id
    except Exception as e:
        raise BackupError(f"archive committed upload failed for {object_key}: {e}") from e


def open_archive_stream(app_state, object_key: str):
    """Return a streaming body for a stored archive."""
    endpoint, key, secret = _corpus_settings(app_state)
    client = _boto_client(endpoint, key, secret)
    try:
        response = client.get_object(Bucket=SELFAI_BACKUP_REPO, Key=f"{BACKUP_BRANCH}/{object_key}")
        return response["Body"]
    except Exception as e:
        raise BackupError(f"could not read archive {object_key}: {e}") from e


def download_archive(app_state, object_key: str, dest_path: str) -> str:
    """Fetch a stored archive to a local path (used by restore)."""
    endpoint, key, secret = _corpus_settings(app_state)
    client = _boto_client(endpoint, key, secret)
    try:
        client.download_file(SELFAI_BACKUP_REPO, f"{BACKUP_BRANCH}/{object_key}", dest_path)
        return dest_path
    except Exception as e:
        raise BackupError(f"could not download archive {object_key}: {e}") from e


####################
# Job runner
####################


def _progress_callback(job_id: str) -> Callable[[dict], None]:
    from selfai_ui.models.backup_jobs import BackupJobs

    def update(progress: dict) -> None:
        BackupJobs.update_job(job_id, progress=progress)

    return update


async def run_backup_job(app_state, job_id: str) -> None:
    """Execute a backup job end to end. Errors mark the job failed, loudly."""
    import asyncio

    from selfai_ui.models.backup_jobs import BackupJobs

    job = BackupJobs.get_job_by_id(job_id)
    if not job:
        log.error(f"backup: job {job_id} vanished before it started")
        return

    BackupJobs.update_job(job_id, status="running")
    tmp_path = None
    try:
        await ensure_backup_repo(app_state)

        fd, tmp_path = tempfile.mkstemp(prefix=f"selfai-backup-{job_id}-", suffix=".tar.gz")
        os.close(fd)

        # The dump is synchronous and can be long; keep the event loop free.
        manifest = await asyncio.to_thread(build_archive, job.scopes or [], job.user_id, tmp_path)
        _progress_callback(job_id)({"tables": manifest["tables"], "files": manifest["files"]})

        object_key = f"{job.created_at}-{job_id}.tar.gz"
        commit_id = await asyncio.to_thread(put_archive, app_state, tmp_path, object_key)

        BackupJobs.update_job(
            job_id,
            status="completed",
            archive_repo=SELFAI_BACKUP_REPO,
            archive_path=object_key,
            archive_commit=commit_id,
            archive_bytes=manifest.get("archive_bytes"),
            schema_revision=manifest.get("schema_revision"),
        )
        log.info(f"backup: job {job_id} completed ({manifest.get('archive_bytes')} bytes, commit {commit_id})")
    except Exception as e:
        log.error(f"backup: job {job_id} failed: {e}", exc_info=True)
        BackupJobs.update_job(job_id, status="failed", error_message=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as e:
                log.warning(f"backup: could not remove temp archive {tmp_path}: {e}")


async def fail_interrupted_jobs() -> None:
    """Mark jobs left `pending`/`running` by a restart as failed.

    A backup job holds no checkpoint, so there is nothing to resume. Leaving
    the row on `running` would show a spinner forever, which reads as "still
    working" rather than "this never finished".
    """
    from selfai_ui.models.backup_jobs import BackupJobs

    for job in BackupJobs.get_interrupted_jobs():
        BackupJobs.update_job(
            job.id,
            status="failed",
            error_message="interrupted by a server restart; backups are not resumable — start a new one",
        )
        log.warning(f"backup: failed interrupted job {job.id} on startup")
