"""Model weights in self.corpus — self.ai#141.

Admin-only, and a separate router from ``routers/model_versions.py`` for the
same reason that one is separate from ``routers/models.py``: a weights repo is
not a version line. Versions are a history of what was produced; this is the
bytes of one model and where they came from.

Every route refuses with a specific 503 when ``ENABLE_SELF_CORPUS`` is off,
matching ``routers/model_versions.py`` — it is a supported configuration and
the default for a fresh install, so these must neither 500 nor return an empty
success that reads as "nothing to do".
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.models import Models
from selfai_ui.utils.auth import get_admin_user
from selfai_ui.utils.model_weights import (
    WeightsIngestError,
    WeightsIngestResult,
    WeightsProvenance,
    backfill_missing_model_repos,
    check_catalogue_links,
    download_to_repo,
    hf_source_from_model_row,
    read_corpus_weights,
)
from selfai_ui.utils.self_corpus import repo_id_for_model

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()

CORPUS_DISABLED_DETAIL = (
    "Model weight versioning requires self.corpus (ENABLE_SELF_CORPUS is False). "
    "Models continue to serve from the models PVC without corpus provenance."
)


def require_corpus(request: Request) -> None:
    """Refuse rather than 500 or no-op when self.corpus is switched off."""
    if not request.app.state.config.ENABLE_SELF_CORPUS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=CORPUS_DISABLED_DETAIL,
        )


class WeightsIngestForm(BaseModel):
    model_id: str
    # Both optional: omitted, they are read off the pull lineage
    # `register_model()` already wrote onto the row, so ingesting the weights
    # of an already-pulled model is `{"model_id": ...}` and nothing else.
    hf_repo: Optional[str] = None
    filenames: Optional[list[str]] = None
    revision: str = "main"
    provenance: Optional[WeightsProvenance] = None


@router.post("/ingest", response_model=WeightsIngestResult, dependencies=[Depends(require_corpus)])
async def ingest_weights(request: Request, form_data: WeightsIngestForm, user=Depends(get_admin_user)):
    """Pull weights from HuggingFace straight into the model's corpus repo.

    Synchronous on purpose for now: the caller learns the commit id, and a
    fire-and-forget task would have to invent its own way to report a failure
    that the atomicity guarantee already makes safe to surface directly.
    """
    hf_repo, filenames = form_data.hf_repo, form_data.filenames
    if not hf_repo or not filenames:
        row_repo, row_files = hf_source_from_model_row(form_data.model_id)
        hf_repo = hf_repo or row_repo
        filenames = filenames or row_files
    if not hf_repo or not filenames:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"model {form_data.model_id} carries no HuggingFace pull lineage; "
                "supply hf_repo and filenames explicitly"
            ),
        )

    try:
        return await download_to_repo(
            request.app.state,
            model_id=form_data.model_id,
            hf_repo=hf_repo,
            filenames=filenames,
            revision=form_data.revision,
            provenance=form_data.provenance,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except WeightsIngestError as e:
        # 502, not 500: the failure is upstream (HuggingFace or self.corpus),
        # and nothing partial is visible on the repo's default branch.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e


@router.get("/catalogue-check", dependencies=[Depends(require_corpus)])
async def catalogue_check(request: Request, user=Depends(get_admin_user)):
    """Rows pointing at missing repos, and repos no row points at."""
    return await check_catalogue_links(request.app.state)


class WeightsBackfillForm(BaseModel):
    limit: Optional[int] = None


@router.post("/backfill", dependencies=[Depends(require_corpus)])
async def backfill_weights_repos(
    request: Request,
    form_data: Optional[WeightsBackfillForm] = None,
    user=Depends(get_admin_user),
):
    """Create repos and two-way links for models that have none.

    Does not upload weights — core has no filesystem access to `/models`. See
    ``utils/model_weights.backfill_missing_model_repos``.
    """
    return await backfill_missing_model_repos(request.app.state, limit=(form_data.limit if form_data else None))


@router.get("/{model_id:path}", dependencies=[Depends(require_corpus)])
async def get_model_weights_link(request: Request, model_id: str, user=Depends(get_admin_user)):
    """What a single row says about its weights, and what it would say.

    `expected_repo` is returned even when the row has no pointer, so an
    operator can tell "never ingested" from "pointer lost" without guessing at
    the naming rule.
    """
    model = Models.get_model_by_id(model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"model {model_id} not found")
    return {
        "model_id": model_id,
        "expected_repo": repo_id_for_model(model_id),
        "corpus_weights": read_corpus_weights(model.meta.model_dump() if model.meta else {}),
    }
