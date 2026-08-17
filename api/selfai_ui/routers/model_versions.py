"""Model lines and versions — self.ai#131.

A standalone router rather than routes bolted onto ``routers/models.py``: a
line is not a model, and the two surfaces have different lifecycles. Access
follows ``routers/models.py``'s pattern exactly — admin bypasses, then the
owner, then ``has_access`` against the line's own ``access_control``.

Every route refuses with a specific 503 when ``ENABLE_SELF_CORPUS`` is off. It
is a supported configuration and the default for a fresh install, so these must
neither 500 nor quietly return an empty success (kit R10).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.model_versions import (
    ModelLineForm,
    ModelLineModel,
    ModelLines,
    ModelVersionError,
    ModelVersionModel,
    ModelVersions,
    PublishJobForm,
    PublishJobModel,
    PublishJobs,
)
from selfai_ui.utils.access_control import has_access, has_permission
from selfai_ui.utils.auth import get_verified_user
from selfai_ui.utils.model_versions import (
    ProvenanceBroken,
    ResolvedVersion,
    VersionUnresolvable,
    create_publish_job,
    current_base_and_adapters,
    resolve_version,
    revert_line,
    walk_provenance,
)
from selfai_ui.utils.self_corpus import repo_id_for_model_line

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()

CORPUS_DISABLED_DETAIL = (
    "Model versioning requires self.corpus (ENABLE_SELF_CORPUS is False). "
    "Models continue to serve without version history."
)


class ModelLineWithHistory(ModelLineModel):
    versions: list[ModelVersionModel] = []


class SetCurrentVersionForm(BaseModel):
    version_id: str


def require_corpus(request: Request) -> None:
    """Refuse rather than 500 or no-op when versioning is switched off."""
    if not request.app.state.config.ENABLE_SELF_CORPUS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=CORPUS_DISABLED_DETAIL,
        )


def _readable(line: ModelLineModel, user) -> bool:
    return user.role == "admin" or line.user_id == user.id or has_access(user.id, "read", line.access_control)


def _writable(line: ModelLineModel, user) -> bool:
    return user.role == "admin" or line.user_id == user.id or has_access(user.id, "write", line.access_control)


def _line_or_404(line_id: str) -> ModelLineModel:
    line = ModelLines.get_line_by_id(line_id)
    if line is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return line


###########################
# Lines
###########################


@router.get("/", response_model=list[ModelLineModel], dependencies=[Depends(require_corpus)])
async def get_model_lines(user=Depends(get_verified_user)):
    return [line for line in ModelLines.get_lines() if _readable(line, user)]


@router.post("/create", response_model=ModelLineModel, dependencies=[Depends(require_corpus)])
async def create_model_line(
    request: Request,
    form_data: ModelLineForm,
    user=Depends(get_verified_user),
):
    if user.role != "admin" and not has_permission(
        user.id, "studio.models", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    line = ModelLines.insert_new_line(user.id, form_data)
    if line is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error creating model line"),
        )

    # The repo id is derived from the line's own id, so it cannot be chosen and
    # cannot collide with a Knowledge Base's. Callers may still pass one
    # explicitly (an ingest attaching to an existing repo); only the derived
    # default is filled in here.
    if not line.corpus_repo:
        ModelLines.update_corpus_repo(line.id, repo_id_for_model_line(line.id))
        line = ModelLines.get_line_by_id(line.id)
    return line


# Note: a query parameter rather than a path param, matching routers/models.py
# — ids may contain '/'.
@router.get("/line", response_model=ModelLineWithHistory, dependencies=[Depends(require_corpus)])
async def get_model_line_by_id(id: str, user=Depends(get_verified_user)):
    line = _line_or_404(id)
    if not _readable(line, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    return ModelLineWithHistory(
        **line.model_dump(),
        versions=ModelVersions.get_versions_by_line(line.id),
    )


@router.post("/line/current-version", response_model=ModelLineModel, dependencies=[Depends(require_corpus)])
async def set_current_version(
    id: str,
    form_data: SetCurrentVersionForm,
    user=Depends(get_verified_user),
):
    """Move a line to one of its versions. Rebuilds nothing (kit R8)."""
    line = _line_or_404(id)
    if not _writable(line, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    try:
        return revert_line(line.id, form_data.version_id, moved_by=user.id)
    except ModelVersionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.delete("/line/delete", response_model=bool, dependencies=[Depends(require_corpus)])
async def delete_model_line(id: str, user=Depends(get_verified_user)):
    line = _line_or_404(id)
    if not _writable(line, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    try:
        return ModelLines.delete_line_by_id(line.id)
    except ModelVersionError as e:
        # A line with versions is refused, not cascaded — the commit-backed
        # provenance is the point of the record.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


###########################
# Versions
###########################


@router.get("/version", response_model=ModelVersionModel, dependencies=[Depends(require_corpus)])
async def get_version_by_id(id: str, user=Depends(get_verified_user)):
    version = ModelVersions.get_version_by_id(id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    if not _readable(_line_or_404(version.line_id), user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)
    return version


@router.get(
    "/version/provenance",
    response_model=list[ModelVersionModel],
    dependencies=[Depends(require_corpus)],
)
async def get_version_provenance(id: str, user=Depends(get_verified_user)):
    """Walk a version back to its line's first version, newest first."""
    version = ModelVersions.get_version_by_id(id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    if not _readable(_line_or_404(version.line_id), user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    try:
        return walk_provenance(id)
    except ProvenanceBroken as e:
        # 409, not 404: the version exists — its history is the thing that is
        # wrong, and the message names which link.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


@router.get("/version/resolve", response_model=ResolvedVersion, dependencies=[Depends(require_corpus)])
async def resolve_version_by_id(id: str, user=Depends(get_verified_user)) -> Optional[ResolvedVersion]:
    """What this version serves as. A lookup, never a rebuild."""
    version = ModelVersions.get_version_by_id(id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    if not _readable(_line_or_404(version.line_id), user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    try:
        return resolve_version(id)
    except VersionUnresolvable as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


###########################
# Publish (R6) — the expensive tier
###########################


class PublishPreview(BaseModel):
    """What a publish would do, without doing it."""

    base_version_id: str
    adapter_version_ids: list[str]
    publishable: bool


@router.get("/line/publish/preview", response_model=PublishPreview, dependencies=[Depends(require_corpus)])
async def preview_publish(id: str, user=Depends(get_verified_user)):
    """Several GB of work is worth being able to look at first."""
    line = _line_or_404(id)
    if not _readable(line, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    try:
        base, adapters = current_base_and_adapters(line.id)
    except ModelVersionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return PublishPreview(
        base_version_id=base.id,
        adapter_version_ids=[adapter.id for adapter in adapters],
        publishable=bool(adapters),
    )


@router.post("/line/publish", response_model=PublishJobModel, dependencies=[Depends(require_corpus)])
async def publish_model_line(
    request: Request,
    id: str,
    form_data: PublishJobForm,
    user=Depends(get_verified_user),
):
    """Merge a line's accumulated adapters into its base as a new base version.

    Gated on `studio.publish`, which is deliberately NOT implied by
    `studio.training`: fitting an adapter costs an adapter, publishing costs
    several GB and a GPU window (kit R6).
    """
    line = _line_or_404(id)
    if not _writable(line, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    if user.role != "admin" and not has_permission(
        user.id, "studio.publish", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    try:
        # Enqueued, not performed. A merge holds the fp16 base on the GPU
        # (self.ai#136), so it waits for a window and a VRAM lease like every
        # other GPU consumer. Poll /line/publish-jobs for what happened.
        return await create_publish_job(request.app.state, line.id, user_id=user.id, form_data=form_data)
    except ModelVersionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


@router.get("/line/publish-jobs", response_model=list[PublishJobModel], dependencies=[Depends(require_corpus)])
async def get_publish_jobs(id: str, user=Depends(get_verified_user)):
    line = _line_or_404(id)
    if not _readable(line, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)
    return PublishJobs.get_jobs_by_line(line.id)
