"""Admin backup surface (self.ai#93).

Replaces the three controls that used to sit under Admin > Settings > Database,
none of which was a backup:

- `GET /utils/db/download` 400s on anything but SQLite, so it has never once
  succeeded against this deployment's Postgres.
- `GET /configs/export` returns live secrets unredacted (self.ai#95).
- `GET /chats/all/db` materialises every chat for every user with no paging
  (`Chats.get_chats()` accepts skip/limit and ignores both) and does not round
  trip — `POST /chats/import` takes one chat and re-owns it to the importer.

Restore is not in this cut. It lands next, replace-only into an empty scope,
with the archive's stamped Alembic revision honoured: equal restores directly,
older migrates forward through a staging schema, newer or unknown refuses.
"""

import asyncio
import logging
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.backup_jobs import BackupJobForm, BackupJobs, BackupJobWithUser
from selfai_ui.models.users import Users
from selfai_ui.utils.auth import get_admin_user
from selfai_ui.utils.backup import (
    SCOPES,
    SELFAI_BACKUP_REPO,
    BackupError,
    current_schema_revision,
    download_archive,
    ensure_backup_repo,
    open_archive_stream,
    put_archive,
    read_manifest,
    resolve_scopes,
    run_backup_job,
    script_heads,
)
from selfai_ui.utils.restore import preview_archive, run_restore_job

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

router = APIRouter()


############################
# Scopes
############################


@router.get("/scopes")
async def get_backup_scopes(user=Depends(get_admin_user)):
    """What can go in an archive, and what each part means.

    Config is absent by design — see self.ai#95.
    """
    return {
        "scopes": [
            {
                "name": scope.name,
                "description": scope.description,
                "carries_files": scope.carries_files,
            }
            for scope in SCOPES.values()
        ]
    }


############################
# Schema revision
############################


@router.get("/schema")
async def get_schema_revision(user=Depends(get_admin_user)):
    """Current Alembic revision vs. the head(s) this build knows about.

    Surfaced because with strict boot migrations (#82) a mismatch is the
    difference between a serving pod and a CrashLoop, and there has been no way
    to see it from the UI. Also what the Database page needs (#94).
    """
    try:
        current = current_schema_revision()
        heads = sorted(script_heads())
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e

    return {"current": current, "heads": heads, "at_head": current in heads}


############################
# Jobs
############################


@router.get("/", response_model=list[BackupJobWithUser])
async def list_backup_jobs(user=Depends(get_admin_user)):
    return BackupJobs.get_all_jobs()


@router.post("/", response_model=BackupJobWithUser)
async def create_backup_job(request: Request, form_data: BackupJobForm, user=Depends(get_admin_user)):
    try:
        scopes = resolve_scopes(form_data.scopes)
        revision = current_schema_revision()
    except BackupError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e

    job = BackupJobs.insert_new_job(
        user_id=user.id,
        scopes=scopes,
        kind="backup",
        schema_revision=revision,
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not create backup job",
        )

    asyncio.create_task(run_backup_job(request.app.state, job.id))

    return BackupJobWithUser.model_validate({**job.model_dump(), "user": user.model_dump()})


@router.get("/{job_id}", response_model=BackupJobWithUser)
async def get_backup_job(job_id: str, user=Depends(get_admin_user)):
    job = BackupJobs.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backup job not found")

    owner = Users.get_user_by_id(job.user_id)
    return BackupJobWithUser.model_validate({**job.model_dump(), "user": owner.model_dump() if owner else None})


@router.get("/{job_id}/download")
async def download_backup_archive(request: Request, job_id: str, user=Depends(get_admin_user)):
    """Stream a completed archive back to the admin.

    Deviation worth naming: the decision record says "fetched by signed URL".
    This streams through the API instead, because presigned GETs against
    lakeFS's S3 gateway are unproven here and a signed URL that silently does
    not work is worse than a stream that does. It also keeps authorisation in
    one place. Swapping to presign later is a change to this handler alone.
    """
    job = BackupJobs.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backup job not found")
    if job.status != "completed" or not job.archive_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"backup job is {job.status}, not completed",
        )

    try:
        body = open_archive_stream(request.app.state, job.archive_path)
    except BackupError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e

    return StreamingResponse(
        body.iter_chunks(chunk_size=1024 * 1024),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{job.archive_path}"'},
    )


############################
# Restore
############################


class RestoreForm(BaseModel):
    # Object key of an archive already in the backup repo, as reported by a
    # completed backup job's `archive_path` or returned from /restore/upload.
    archive_path: str


@router.post("/restore/upload")
async def upload_restore_archive(request: Request, file: UploadFile = File(...), user=Depends(get_admin_user)):
    """Accept an archive from elsewhere and park it in the backup repo.

    Restoring from another instance's archive needs the file to outlive the
    request — the restore runs as a background job. Rather than inventing a
    second storage location, uploads land in the same repo under `uploads/`,
    and everything downstream then works in terms of an object key regardless
    of where the archive came from.
    """
    handle, tmp_path = tempfile.mkstemp(prefix="selfai-restore-upload-", suffix=".tar.gz")
    os.close(handle)
    try:
        with open(tmp_path, "wb") as sink:
            while chunk := await file.read(1024 * 1024):
                sink.write(chunk)

        # Validate before storing — an unreadable archive should fail here,
        # not halfway through a background job.
        manifest = read_manifest(tmp_path)

        object_key = f"uploads/{uuid.uuid4()}.tar.gz"
        await ensure_backup_repo(request.app.state)
        commit_id = await asyncio.to_thread(put_archive, request.app.state, tmp_path, object_key)
    except BackupError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return {
        "archive_path": object_key,
        "archive_commit": commit_id,
        "schema_revision": manifest.get("schema_revision"),
        "scopes": manifest.get("scopes", []),
    }


@router.post("/restore/preview")
async def preview_restore(request: Request, form_data: RestoreForm, user=Depends(get_admin_user)):
    """Report what an archive would do, without touching anything.

    Restore is replace-only and destructive by definition, so nothing runs
    until an admin has seen the plan: the revision gap and how it would be
    handled, what is in the archive, and what is currently occupying the
    target scopes.
    """
    handle, tmp_path = tempfile.mkstemp(prefix="selfai-restore-preview-", suffix=".tar.gz")
    os.close(handle)
    try:
        await asyncio.to_thread(download_archive, request.app.state, form_data.archive_path, tmp_path)
        return await asyncio.to_thread(preview_archive, tmp_path, user.id)
    except BackupError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post("/restore", response_model=BackupJobWithUser)
async def create_restore_job(request: Request, form_data: RestoreForm, user=Depends(get_admin_user)):
    """Start a restore.

    Re-runs the preview checks server-side rather than trusting that the
    caller looked at one — the UI showing a green preview is not evidence the
    database is still empty by the time this arrives.
    """
    handle, tmp_path = tempfile.mkstemp(prefix="selfai-restore-check-", suffix=".tar.gz")
    os.close(handle)
    try:
        await asyncio.to_thread(download_archive, request.app.state, form_data.archive_path, tmp_path)
        preview = await asyncio.to_thread(preview_archive, tmp_path, user.id)
        manifest = await asyncio.to_thread(read_manifest, tmp_path)
    except BackupError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not preview["can_restore"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "restore refused", "blocked_by": preview["blocked_by"]},
        )

    job = BackupJobs.insert_new_job(
        user_id=user.id,
        scopes=manifest.get("scopes") or [],
        kind="restore",
        schema_revision=manifest.get("schema_revision"),
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not create restore job",
        )

    BackupJobs.update_job(job.id, archive_repo=SELFAI_BACKUP_REPO, archive_path=form_data.archive_path)
    asyncio.create_task(run_restore_job(request.app.state, job.id))

    refreshed = BackupJobs.get_job_by_id(job.id)
    return BackupJobWithUser.model_validate({**refreshed.model_dump(), "user": user.model_dump()})


@router.delete("/{job_id}", response_model=bool)
async def delete_backup_job(job_id: str, user=Depends(get_admin_user)):
    """Forget a job row.

    Deliberately does not delete the archive from self.corpus. `fs:DeleteObject`
    is not something this feature asks for — a backup store the writer cannot
    delete from is a feature, and archives are retained by the repo's own
    policy, not by whether a row still points at them.
    """
    job = BackupJobs.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backup job not found")
    if job.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="backup job is still running",
        )
    return BackupJobs.delete_job_by_id(job_id)
