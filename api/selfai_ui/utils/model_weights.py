"""Model weights in self.corpus — self.ai#141.

Wires the artifact kind `self_corpus.py` has named in its own docstring since
it was written ("One repo per artifact (KB/Dataset/Course/**Model**)") and
never had: a repo per model, a pull that lands weights in it as one motion, and
a catalogue link readable from either end.

**Why this exists — provenance, and explicitly not dedup.** Naming it here so
nobody builds on the wrong expectation: LakeFS will not dedup a model against
its quants. Quantization rewrites every tensor, so the byte streams share
nothing and content-addressed dedup finds no overlap; the same holds for a
distilled model against its teacher. What genuinely dedups is identical
re-uploads, unchanged shards between versions, and shared tokenizer/config
files — real, but small next to the weights. What is actually worth having is
provenance ("these weights came from teacher X at commit A over corpus B"),
atomic commits across multi-GB file sets, and reproducibility. See
gitlab-profile `context/treasuremaps/2026-08-13-crew-model-card-lifecycle.md`
D12.

**What this is not.** The models PVC stays the serving path. llamolotl owns
`/models` and llama-server loads from it; nothing here changes that. This is
versioning and provenance alongside it, so a model whose repo does not exist
serves exactly as it did before.

**Relationship to `utils/model_versions.py`.** That module records *manifests*
— core cannot stage bytes it does not hold, so a line's history describes
artifacts on llamolotl's disk rather than containing them (its own DUCT TAPE
note says so). This module is the case where core *can* hold the bytes,
because they are in flight: an HTTP download passes through this process, so it
can be pumped into self.corpus without ever landing here. The two are
complementary and deliberately not merged — a line is a history of versions, a
weights repo is one model's bytes.
"""

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import aiohttp
from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.self_corpus import (
    WEIGHTS_TRANSFER_TIMEOUT,
    SelfCorpusError,
    commit_branch,
    create_branch,
    create_repository,
    delete_branch,
    is_model_weights_repo,
    list_repositories,
    merge_into,
    repo_id_for_model,
    repository_exists,
    upload_object,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# The catalogue link
####################

# CONFIRMED against self.ai#140 (!462), which owns models/models.py and
# declares both of these directly on ModelMeta as Optional[str]. Named to match
# `model_versions.ModelLine.corpus_repo` and `ModelVersion.corpus_commit_id`, so
# one concept keeps one name across the three tables that reference it.
#
# They are flat declared fields rather than a nested blob on purpose: #140 put
# them on the row directly rather than behind `version_id` so a model can have
# weights in self.corpus without belonging to a #131 line, which is what makes
# check_catalogue_links() able to detect a mismatch for a model that is not part
# of a line. #141 does not edit ModelMeta; it writes the names #140 declared.
CORPUS_REPO_FIELD = "corpus_repo"
CORPUS_COMMIT_FIELD = "corpus_commit_id"

# Also declared by #140 (!462, commit 54c4823), as `Optional[bool]` and tri-state.
#
# This is the one thing the two fields above cannot express, and it is not
# derivable from them: `corpus_repo` being set does **not** mean the repo holds
# the bytes. backfill_missing_model_repos() creates the repo, commits the
# catalogue object and links both directions without uploading anything,
# because core has no filesystem access to /models. A consumer that reads
# `corpus_repo` and assumes the weights are retrievable from it would be wrong
# for every backfilled row.
#
# Tri-state, and all three states are real:
#   None  — never recorded (a row that predates this, or a plain PVC model)
#   True  — the repo holds the bytes; a restore can read them back
#   False — linked and provenanced, but the bytes are on the PVC only
#
# Not derivable from `corpus_commit_id` either: a backfill commit is a real
# commit of the catalogue object, so a non-null commit id does not imply
# weights. #140 declared it rather than defaulting it to `False`, because
# "we never looked" and "we looked and they are not there" are different claims.
CORPUS_WEIGHTS_PRESENT_FIELD = "corpus_weights_present"

# The other half of the link, and the half that makes a repo self-describing.
# The row -> repo direction is a field on the row; the repo -> row direction
# cannot be a field anywhere, so it is an object committed inside the repo.
# Deriving it from the repo id instead would not work: repo_id_for_model() is
# one-way by construction (it slugifies and digests).
CATALOGUE_OBJECT_PATH = "selfai-model.json"

# Weights live under a prefix so the catalogue object and any future provenance
# objects are not mixed in with shards.
WEIGHTS_PREFIX = "weights"

DEFAULT_BRANCH = "main"


class WeightsProvenance(BaseModel):
    """What produced a set of weights.

    `kind` is what the commit is tagged with, and the reason this is a declared
    shape rather than a free dict: the point of the whole exercise is that
    lineage is readable from the corpus side, and a commit whose provenance is
    whatever the caller felt like putting there is not readable, only present.
    """

    kind: str  # "hf-pull" | "quantization" | "distillation"
    source_model: Optional[str] = None  # teacher, or the model that was quantized
    source_corpus_repo: Optional[str] = None  # the text the teacher ran over
    source_corpus_commit: Optional[str] = None
    hf_repo: Optional[str] = None
    hf_revision: Optional[str] = None
    hf_commit: Optional[str] = None  # the resolved commit sha, not the ref
    recipe: Optional[dict] = None  # prune recipe, quant type, distillation params
    job_id: Optional[str] = None

    def as_commit_metadata(self) -> dict:
        """Flatten to the string->string map LakeFS commit metadata accepts.

        Nested values are JSON-encoded rather than dropped — a prune recipe is
        the part of a distillation's provenance most worth keeping, and LakeFS
        refusing nested values is not a reason to lose it.
        """
        out: dict = {}
        for key, value in self.model_dump(exclude_none=True).items():
            out[f"selfai.{key}"] = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return out


class WeightsObject(BaseModel):
    """One file to pull into the repo."""

    filename: str
    bytes_written: int = 0


class WeightsIngestResult(BaseModel):
    model_id: str
    corpus_repo: str
    corpus_commit_id: str
    objects: list[WeightsObject] = []
    total_bytes: int = 0
    provenance: Optional[WeightsProvenance] = None


class WeightsIngestError(Exception):
    """An ingest did not complete. Nothing is visible on the default branch."""


####################
# HuggingFace source
####################

HF_HOST = "https://huggingface.co"

# Both halves are validated because both are interpolated into a URL path. An
# unvalidated repo id is an SSRF and a path-traversal at once ("../../..", or a
# value containing "@" that re-points the host).
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_HF_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

WEIGHTS_CHUNK_SIZE = 8 * 1024 * 1024


def hf_resolve_url(hf_repo: str, filename: str, revision: str = "main") -> str:
    """The direct-download URL for one file in an HF repo.

    Raises ValueError rather than building a URL out of an unvalidated id.
    """
    if not _HF_REPO_RE.match(hf_repo or ""):
        raise ValueError(f"not a valid HuggingFace repo id: {hf_repo!r}")
    for part, label in ((filename, "filename"), (revision, "revision")):
        if not _HF_PATH_RE.match(part or "") or ".." in part:
            raise ValueError(f"not a valid HuggingFace {label}: {part!r}")
    return f"{HF_HOST}/{hf_repo}/resolve/{revision}/{filename}"


class RemoteObject(BaseModel):
    """An open remote body, described. `chunks` is deliberately not a field."""

    url: str
    size: Optional[int] = None
    hf_commit: Optional[str] = None


@asynccontextmanager
async def open_hf_object(
    hf_repo: str,
    filename: str,
    revision: str = "main",
    token: Optional[str] = None,
    chunk_size: int = WEIGHTS_CHUNK_SIZE,
):
    """Open one HF file and yield `(RemoteObject, async chunk iterator)`.

    The body is never read whole. `response.read()` is the thing this exists to
    avoid; callers get an iterator and hand it straight to upload_object(), so
    the largest object in memory at any moment is one chunk.
    """
    url = hf_resolve_url(hf_repo, filename, revision)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=WEIGHTS_TRANSFER_TIMEOUT, trust_env=True) as session:
        async with session.get(url, headers=headers) as response:
            if response.status >= 400:
                raise WeightsIngestError(f"HuggingFace download failed ({response.status}) for {hf_repo}/{filename}")
            size = response.headers.get("Content-Length")
            described = RemoteObject(
                url=url,
                size=int(size) if size and size.isdigit() else None,
                # HF returns the resolved commit for the ref it just served.
                # Recording it is what turns "from main" into something
                # reproducible.
                hf_commit=response.headers.get("X-Repo-Commit"),
            )
            yield described, response.content.iter_chunked(chunk_size)


####################
# Row <-> repo catalogue link
####################


def read_corpus_weights(meta: Optional[dict]) -> Optional[dict]:
    """The corpus link on a model row, or None if it has none.

    Reads the flat fields #140 declared and returns them as one dict, so
    callers need not know whether the link is one field or three. Keyed off
    `corpus_repo`: the repo is what detection needs, and a repo with a null
    commit id is a real intermediate state, not an absent link.
    """
    meta = meta or {}
    repo = meta.get(CORPUS_REPO_FIELD)
    if not repo:
        return None
    return {
        "repo": repo,
        "commit_id": meta.get(CORPUS_COMMIT_FIELD),
        "weights_present": meta.get(CORPUS_WEIGHTS_PRESENT_FIELD),
    }


def _write_corpus_weights(model_id: str, pointer: Optional[dict]) -> bool:
    """Read-merge-write the corpus link onto a model row's meta.

    update_model_by_id() overwrites the whole row from a ModelForm, so this
    reads first. Returns False when the row is gone rather than raising: an
    ingest that succeeded must not be reported as failed because someone
    deleted the model while it ran.
    """
    from selfai_ui.models.models import ModelForm, ModelMeta, Models

    model = Models.get_model_by_id(model_id)
    if model is None:
        return False
    meta = model.meta.model_dump() if model.meta else {}
    if pointer is None:
        for field in (CORPUS_REPO_FIELD, CORPUS_COMMIT_FIELD, CORPUS_WEIGHTS_PRESENT_FIELD):
            meta.pop(field, None)
    else:
        meta[CORPUS_REPO_FIELD] = pointer["repo"]
        meta[CORPUS_COMMIT_FIELD] = pointer.get("commit_id")
        meta[CORPUS_WEIGHTS_PRESENT_FIELD] = pointer.get("weights_present")
    Models.update_model_by_id(
        model_id,
        ModelForm(
            id=model_id,
            base_model_id=model.base_model_id,
            name=model.name,
            meta=ModelMeta(**meta),
            params=model.params,
            access_control=model.access_control,
            is_active=model.is_active,
        ),
    )
    return True


def _catalogue_document(model_id: str, provenance: Optional[WeightsProvenance], objects: list[WeightsObject]) -> dict:
    """What the repo carries so it can name its own model row."""
    return {
        "schema": "selfai.model-weights/1",
        "model_id": model_id,
        "corpus_repo": repo_id_for_model(model_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance.model_dump(exclude_none=True) if provenance else None,
        "objects": [o.model_dump() for o in objects],
    }


async def _single_chunk(payload: bytes) -> AsyncIterator[bytes]:
    """Adapt a small in-memory body to the streaming upload signature.

    Only ever used for the catalogue JSON, which is a few hundred bytes.
    Weights never go through here.
    """
    yield payload


####################
# Ingest
####################


def _corpus_credentials(app_state) -> tuple[str, str, str]:
    """Same accessor shape as self_corpus.backfill_missing_repos."""
    cfg = app_state.config
    return (
        cfg.SELF_CORPUS_LAKEFS_ENDPOINT,
        cfg.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID,
        cfg.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY,
    )


async def ensure_model_repo(app_state, model_id: str) -> str:
    """Return the model's repo id, creating the repo if it is missing.

    An already-existing repo is success. LakeFS reports that as a 409 whose
    message names the conflict, and treating it as a failure would make every
    ingest after the first one fail.
    """
    repo_id = repo_id_for_model(model_id)
    endpoint, key, secret = _corpus_credentials(app_state)
    try:
        await create_repository(endpoint=endpoint, access_key_id=key, secret_access_key=secret, repo_id=repo_id)
        log.info(f"self.corpus: created weights repo {repo_id} for model {model_id}")
    except SelfCorpusError as e:
        if not await repository_exists(endpoint, key, secret, repo_id):
            raise
        log.debug(f"self.corpus: weights repo {repo_id} already exists ({e})")
    return repo_id


def hf_source_from_model_row(model_id: str) -> tuple[Optional[str], list[str]]:
    """The HF source a model was pulled from, read off the row's pull lineage.

    This is the seam onto the existing pull path. `POST /llamolotl/api/pull`
    streams the download on llamolotl's side — core proxies it and never sees
    the bytes — but `register_model()` afterwards writes `hf_repo` / `quant` /
    `source_type` / `pulled_at` onto the row. Reusing that means a corpus
    ingest is a continuation of the pull that already happened rather than an
    operator retyping where the weights came from, which is the difference
    between one motion and two.

    A model id is the GGUF's filename (register_model uses
    ``Path(form_data.name).name``), so it doubles as the file to fetch. Returns
    ``(None, [])`` when the row carries no pull lineage — callers must supply
    the source themselves rather than have one guessed for them.
    """
    from selfai_ui.models.models import Models

    model = Models.get_model_by_id(model_id)
    if model is None or not model.meta:
        return None, []
    meta = model.meta.model_dump()
    hf_repo = meta.get("hf_repo")
    if not hf_repo:
        return None, []
    return hf_repo, [meta.get("hf_filename") or model_id]


async def download_to_repo(
    app_state,
    *,
    model_id: str,
    hf_repo: str,
    filenames: list[str],
    revision: str = "main",
    token: Optional[str] = None,
    provenance: Optional[WeightsProvenance] = None,
    link_model_row: bool = True,
) -> WeightsIngestResult:
    """Pull weights from HuggingFace into the model's repo, as one motion.

    Atomicity, which is the whole design of this function: every object is
    staged on a **per-ingest branch**, committed there, and only then merged
    into `main`. A failure or an interruption at any point before that merge
    leaves `main` exactly as it was — a reader of the default branch never sees
    a half-uploaded model, which is the outcome worth the most here. The ingest
    branch is deleted on the way out, success or failure; a branch left behind
    by a hard process kill is inert (it is not `main`) and the next ingest
    creates a fresh one rather than resuming it.

    The row pointer is written *after* the merge, never before, so the
    catalogue never claims weights that are not committed. The reverse gap —
    merged but not yet pointed at, if the process dies in between — is the one
    check_catalogue_links() reports as an unlinked repo, so it is visible
    rather than silent.

    Raises WeightsIngestError. Callers that must not fail on a corpus problem
    catch it; nothing here is on the serving path.
    """
    if not app_state.config.ENABLE_SELF_CORPUS:
        raise WeightsIngestError("self.corpus is disabled (ENABLE_SELF_CORPUS is False)")
    if not filenames:
        raise WeightsIngestError("no files requested; nothing to ingest")

    provenance = provenance or WeightsProvenance(kind="hf-pull")
    provenance = provenance.model_copy(update={"hf_repo": hf_repo, "hf_revision": revision})

    endpoint, key, secret = _corpus_credentials(app_state)
    repo_id = await ensure_model_repo(app_state, model_id)
    ingest_branch = f"ingest-{uuid.uuid4().hex[:12]}"

    try:
        await create_branch(endpoint, key, secret, repo_id, ingest_branch, source_reference=DEFAULT_BRANCH)
    except SelfCorpusError as e:
        raise WeightsIngestError(f"could not open an ingest branch on {repo_id}: {e}") from e

    objects: list[WeightsObject] = []
    try:
        for filename in filenames:
            async with open_hf_object(hf_repo, filename, revision=revision, token=token) as (described, chunks):
                if described.hf_commit and not provenance.hf_commit:
                    provenance = provenance.model_copy(update={"hf_commit": described.hf_commit})
                counted, counter = _counting(chunks)
                await upload_object(
                    endpoint,
                    key,
                    secret,
                    repo_id,
                    ingest_branch,
                    f"{WEIGHTS_PREFIX}/{filename}",
                    counted,
                )
                objects.append(WeightsObject(filename=filename, bytes_written=counter["bytes"]))
                log.info(
                    f"self.corpus: staged {filename} ({counter['bytes']} bytes) "
                    f"on {repo_id}@{ingest_branch} for model {model_id}"
                )

        catalogue = _catalogue_document(model_id, provenance, objects)
        await _stage_catalogue(endpoint, key, secret, repo_id, ingest_branch, catalogue)

        total = sum(o.bytes_written for o in objects)
        metadata = {
            **provenance.as_commit_metadata(),
            "selfai.model_id": model_id,
            "selfai.object_count": str(len(objects)),
            "selfai.total_bytes": str(total),
        }
        await commit_branch(
            endpoint,
            key,
            secret,
            repo_id,
            f"{provenance.kind}: {model_id} from {hf_repo}@{revision}",
            branch=ingest_branch,
            metadata=metadata,
        )
        commit_id = await merge_into(
            endpoint,
            key,
            secret,
            repo_id,
            ingest_branch,
            DEFAULT_BRANCH,
            message=f"{provenance.kind}: {model_id}",
            metadata=metadata,
        )
    except WeightsIngestError:
        await _discard_ingest_branch(endpoint, key, secret, repo_id, ingest_branch)
        raise
    except Exception as e:
        await _discard_ingest_branch(endpoint, key, secret, repo_id, ingest_branch)
        raise WeightsIngestError(f"ingest of {model_id} into {repo_id} failed: {e}") from e

    # Past the merge: main now holds the model. Cleanup failures below are
    # cosmetic and must not turn a completed ingest into a reported failure.
    await _discard_ingest_branch(endpoint, key, secret, repo_id, ingest_branch)

    result = WeightsIngestResult(
        model_id=model_id,
        corpus_repo=repo_id,
        corpus_commit_id=commit_id,
        objects=objects,
        total_bytes=sum(o.bytes_written for o in objects),
        provenance=provenance,
    )

    if link_model_row:
        linked = _write_corpus_weights(
            model_id,
            # Only what the row must carry. Object count, byte totals, the
            # branch and the producing job are all readable from the repo —
            # that is what the catalogue object and the commit metadata are
            # for, and duplicating them onto the row creates two copies that
            # can disagree.
            {"repo": repo_id, "commit_id": commit_id, "weights_present": True},
        )
        if not linked:
            log.warning(
                f"self.corpus: {repo_id}@{commit_id} holds weights for model row {model_id}, "
                f"which does not exist — the repo is linked one way only"
            )
    return result


def _counting(chunks: AsyncIterator[bytes]) -> tuple[AsyncIterator[bytes], dict]:
    """Wrap a chunk iterator so bytes are counted as they pass through.

    Counting on the way past rather than measuring afterwards: there is no
    "afterwards" that still has the bytes, and Content-Length is the source's
    claim rather than what was actually transferred.
    """
    counter = {"bytes": 0}

    async def _wrapped() -> AsyncIterator[bytes]:
        async for chunk in chunks:
            counter["bytes"] += len(chunk)
            yield chunk

    return _wrapped(), counter


async def _stage_catalogue(endpoint, key, secret, repo_id: str, branch: str, catalogue: dict) -> None:
    await upload_object(
        endpoint,
        key,
        secret,
        repo_id,
        branch,
        CATALOGUE_OBJECT_PATH,
        _single_chunk(json.dumps(catalogue, indent=2, sort_keys=True).encode("utf-8")),
        content_type="application/json",
    )


async def _discard_ingest_branch(endpoint, key, secret, repo_id: str, branch: str) -> None:
    """Best-effort branch cleanup. Never raises — it is not the real guarantee.

    The guarantee is that nothing was ever merged into main. Deleting the
    branch is tidiness on top of that, so a failure here is logged and dropped
    rather than masking the error that caused the abort.
    """
    try:
        await delete_branch(endpoint, key, secret, repo_id, branch)
    except Exception as e:  # noqa: BLE001 — deliberately terminal
        log.warning(f"self.corpus: could not delete ingest branch {repo_id}@{branch}: {e}")


####################
# Detection + backfill
####################


async def check_catalogue_links(app_state) -> dict:
    """Report every way the row <-> repo link can be broken, in both directions.

    Three findings, kept apart because they need different fixes:

    - `dangling_rows`   — a row points at a repo that does not exist.
    - `unlinked_repos`  — a weights repo no row points at (an ingest that
                          merged and then died before writing the pointer, or a
                          model row deleted out from under its weights).
    - `unreachable`     — self.corpus could not be asked. Reported as itself so
                          an outage never presents as "everything is orphaned".
    """
    if not app_state.config.ENABLE_SELF_CORPUS:
        return {"skipped": True, "reason": "ENABLE_SELF_CORPUS is False"}

    from selfai_ui.models.models import Models

    endpoint, key, secret = _corpus_credentials(app_state)
    rows = Models.get_all_models()
    linked: dict[str, str] = {}
    dangling: list[dict] = []
    unreachable: list[dict] = []

    for model in rows:
        pointer = read_corpus_weights(model.meta.model_dump() if model.meta else {})
        if not pointer:
            continue
        repo = pointer["repo"]
        linked[repo] = model.id
        try:
            if not await repository_exists(endpoint, key, secret, repo):
                dangling.append({"model_id": model.id, "repo": repo})
        except SelfCorpusError as e:
            unreachable.append({"model_id": model.id, "repo": repo, "error": str(e)})

    unlinked: list[str] = []
    try:
        for repo in await list_repositories(endpoint, key, secret):
            if is_model_weights_repo(repo) and repo not in linked:
                unlinked.append(repo)
    except SelfCorpusError as e:
        unreachable.append({"model_id": None, "repo": None, "error": str(e)})

    report = {
        "checked_rows": len(rows),
        "linked": len(linked),
        "dangling_rows": dangling,
        "unlinked_repos": unlinked,
        "unreachable": unreachable,
    }
    if dangling or unlinked or unreachable:
        log.warning(
            f"self.corpus catalogue check: {len(dangling)} dangling row(s), "
            f"{len(unlinked)} unlinked repo(s), {len(unreachable)} unreachable"
        )
    return report


async def backfill_missing_model_repos(app_state, limit: Optional[int] = None) -> dict:
    """Give models on the PVC a corpus repo and a two-way link, without weights.

    Mirrors self_corpus.backfill_missing_repos: best-effort per model, one
    failure never stops the rest, and it returns a report rather than raising.

    What it deliberately does **not** do is claim the weights are in the repo.
    Core has no filesystem access to `/models` — llamolotl owns that PVC — so
    an existing model's bytes cannot be uploaded from here at all. The backfill
    creates the repo and commits the catalogue object, so the repo exists, the
    link resolves in both directions, and `weights_present` is False. That is
    the honest state: an identity a later ingest can fill in, not a repo
    pretending to hold a model.

    Unlike the KB backfill this is **not wired into boot**. A fresh install has
    ~100 model rows, and creating ~100 LakeFS repos unprompted during startup is
    not something to do silently on every pod restart. Admin-triggered, with
    `limit` for a cautious first run.
    """
    if not app_state.config.ENABLE_SELF_CORPUS:
        return {"skipped": True, "reason": "ENABLE_SELF_CORPUS is False"}

    from selfai_ui.models.models import Models

    endpoint, key, secret = _corpus_credentials(app_state)
    created, failed, skipped = [], [], 0

    for model in Models.get_all_models():
        if read_corpus_weights(model.meta.model_dump() if model.meta else {}):
            skipped += 1
            continue
        if limit is not None and len(created) + len(failed) >= limit:
            break

        try:
            repo_id = await ensure_model_repo(app_state, model.id)
            meta = model.meta.model_dump() if model.meta else {}
            provenance = WeightsProvenance(
                kind="backfill",
                hf_repo=meta.get("hf_repo"),
                recipe={"quant": meta["quant"]} if meta.get("quant") else None,
            )
            catalogue = {**_catalogue_document(model.id, provenance, []), "weights_present": False}
            await _stage_catalogue(endpoint, key, secret, repo_id, DEFAULT_BRANCH, catalogue)
            commit_id = await commit_branch(
                endpoint,
                key,
                secret,
                repo_id,
                f"backfill: catalogue entry for {model.id}",
                branch=DEFAULT_BRANCH,
                metadata={**provenance.as_commit_metadata(), "selfai.model_id": model.id},
            )
            _write_corpus_weights(
                model.id,
                # weights_present=False, and this is the case that makes the
                # field necessary: the repo and the commit are both real, and
                # the bytes are still only on the PVC.
                {"repo": repo_id, "commit_id": commit_id, "weights_present": False},
            )
            created.append(model.id)
        except Exception as e:  # noqa: BLE001 — degrade, do not break serving
            log.warning(f"self.corpus model backfill: repo setup failed for model {model.id}: {e}")
            failed.append(model.id)

    if created or failed:
        log.info(
            f"self.corpus model backfill: {len(created)} repo(s) created, "
            f"{len(failed)} failed, {skipped} already linked"
        )
    return {"created": created, "failed": failed, "already_linked": skipped}


__all__ = [
    "CATALOGUE_OBJECT_PATH",
    "CORPUS_COMMIT_FIELD",
    "CORPUS_REPO_FIELD",
    "CORPUS_WEIGHTS_PRESENT_FIELD",
    "WEIGHTS_PREFIX",
    "WeightsIngestError",
    "WeightsIngestResult",
    "WeightsObject",
    "WeightsProvenance",
    "backfill_missing_model_repos",
    "check_catalogue_links",
    "download_to_repo",
    "ensure_model_repo",
    "hf_resolve_url",
    "hf_source_from_model_row",
    "open_hf_object",
    "read_corpus_weights",
]
