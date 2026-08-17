"""Line/version service — self.ai#131.

The layer every producer calls. Three things live here that the table layer
deliberately does not do:

- **Append commits first and writes the row second.** A version records the
  commit backing it, so the row must never exist before that commit succeeds.
  Validation runs before the commit, so an invalid append costs nothing.
- **Provenance walks report the specific broken link**, rather than stopping
  early and presenting a truncated history as a complete one.
- **Resolution is a lookup, never a rebuild**, and a missing artifact is
  reported as itself — never quietly substituted with the line's current
  version, which would answer the wrong question convincingly.

Kit: context/kits/cavekit-model-versioning.md R2, R8.
"""

import json
import logging
from typing import Callable, Optional

from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.model_versions import (
    ModelLineForm,
    ModelLineModel,
    ModelLines,
    ModelVersionError,
    ModelVersionForm,
    ModelVersionModel,
    ModelVersions,
    PublishJobForm,
    PublishJobModel,
    PublishJobs,
)
from selfai_ui.utils.self_corpus import (
    SelfCorpusError,
    commit_branch,
    create_repository,
    repo_id_for_model_line,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class ProvenanceBroken(ModelVersionError):
    """A version's parent chain does not reach the line's first version."""


class VersionUnresolvable(ModelVersionError):
    """A version exists but cannot be turned into something servable."""


class ResolvedVersion(BaseModel):
    """What a version resolves to, by lookup.

    An ``adapter`` resolves to *both* artifacts: the base it runs on top of and
    the adapter itself. A caller that only reads ``artifact_ref`` for an
    adapter would serve a bare LoRA.
    """

    line_id: str
    version_id: str
    kind: str
    corpus_repo: Optional[str] = None
    corpus_commit_id: str
    artifact_ref: Optional[str] = None
    base_version_id: Optional[str] = None
    base_artifact_ref: Optional[str] = None


def _corpus_credentials(app_state) -> tuple[str, str, str]:
    """Same accessor shape as self_corpus.backfill_missing_repos."""
    cfg = app_state.config
    return (
        cfg.SELF_CORPUS_LAKEFS_ENDPOINT,
        cfg.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID,
        cfg.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY,
    )


async def append_version(
    app_state,
    line_id: str,
    *,
    kind: str,
    commit_message: str,
    parent_version_id: Optional[str] = None,
    base_version_id: Optional[str] = None,
    produced_by: Optional[dict] = None,
    published_by: Optional[str] = None,
    artifact_ref: Optional[str] = None,
    commit_metadata: Optional[dict] = None,
    meta: Optional[dict] = None,
) -> ModelVersionModel:
    """Commit the line's staged changes, then record the version.

    Order is the whole point. Validation first, so a bad append never reaches
    self.corpus; the commit second; the row last, carrying the commit id the
    commit returned.

    Raises ModelVersionError if the append is invalid, and SelfCorpusError (or
    SelfCorpusNothingToCommit) if the commit fails — in both cases **no version
    row is written**. Callers staging objects for a bake or publish should treat
    either as "this version did not happen".
    """
    line = ModelLines.get_line_by_id(line_id)
    if line is None:
        raise ModelVersionError(f"line {line_id} not found")
    if not line.corpus_repo:
        raise ModelVersionError(f"line {line_id} has no self.corpus repo to commit into")

    # Before the commit, not after: a commit made for a version that then fails
    # validation is a stray commit nobody will ever find.
    ModelVersions.validate_append(
        line_id,
        kind,
        parent_version_id=parent_version_id,
        base_version_id=base_version_id,
    )

    endpoint, access_key_id, secret_access_key = _corpus_credentials(app_state)
    commit_id = await commit_branch(
        endpoint,
        access_key_id,
        secret_access_key,
        line.corpus_repo,
        commit_message,
        metadata=commit_metadata,
    )

    try:
        return ModelVersions.insert_new_version(
            line_id,
            ModelVersionForm(
                kind=kind,
                corpus_commit_id=commit_id,
                parent_version_id=parent_version_id,
                base_version_id=base_version_id,
                produced_by=produced_by,
                published_by=published_by,
                artifact_ref=artifact_ref,
                meta=meta,
            ),
        )
    except ModelVersionError:
        # The commit is already in the repo and cannot be taken back. Say so
        # loudly with the id: recovering means attaching a version to this
        # commit by hand, and a silent failure here leaves an orphan nobody
        # knows to look for.
        log.error(
            f"self.corpus commit {commit_id} on repo {line.corpus_repo} has no version record: "
            f"the row write failed after the commit succeeded"
        )
        raise


def latest_version(line_id: str) -> Optional[ModelVersionModel]:
    """The highest-sequence version on a line, or None for an empty line."""
    history = ModelVersions.get_versions_by_line(line_id)
    return history[-1] if history else None


def walk_provenance(version_id: str) -> list[ModelVersionModel]:
    """Walk a version back to its line's first version.

    Returns newest-first, ending at the version whose parent is None.

    Raises ProvenanceBroken naming the specific link that is missing, rather
    than returning the partial chain — a truncated history that looks complete
    is worse than an error, because it reads as "this model came from nowhere".
    """
    version = ModelVersions.get_version_by_id(version_id)
    if version is None:
        raise ProvenanceBroken(f"version {version_id} not found")

    chain: list[ModelVersionModel] = [version]
    seen = {version_id}

    while version.parent_version_id is not None:
        parent_id = version.parent_version_id
        if parent_id in seen:
            raise ProvenanceBroken(
                f"provenance for version {version_id} cycles: {parent_id} is already in the chain"
            )
        parent = ModelVersions.get_version_by_id(parent_id)
        if parent is None:
            raise ProvenanceBroken(
                f"provenance for version {version_id} is broken: "
                f"version {version.id} names parent {parent_id}, which does not exist"
            )
        seen.add(parent_id)
        chain.append(parent)
        version = parent

    return chain


def resolve_version(
    version_id: str,
    artifact_exists: Optional[Callable[[str, str], bool]] = None,
) -> ResolvedVersion:
    """Resolve a version to something servable, by lookup.

    Nothing is rebuilt here: if the artifact exists, this is a read of records
    that already exist. An ``adapter`` resolves to its base plus itself — the
    base named by ``base_version_id`` when it has one, otherwise the nearest
    ``base`` ancestor in its provenance.

    ``artifact_exists`` is an optional ``(corpus_repo, artifact_ref) -> bool``
    predicate supplied by the caller that knows how to look at storage. When
    given, a False answer raises VersionUnresolvable naming the version and its
    commit. When omitted, only the records are checked — the record layer
    cannot tell whether a file is still on disk, and pretending otherwise would
    be the more dangerous default.
    """
    version = ModelVersions.get_version_by_id(version_id)
    if version is None:
        raise VersionUnresolvable(f"version {version_id} not found")

    line = ModelLines.get_line_by_id(version.line_id)
    corpus_repo = line.corpus_repo if line else None

    base = None
    if version.kind == "adapter":
        if version.base_version_id:
            base = ModelVersions.get_version_by_id(version.base_version_id)
            if base is None:
                raise VersionUnresolvable(
                    f"version {version_id} (commit {version.corpus_commit_id}) names base version "
                    f"{version.base_version_id}, which does not exist"
                )
        else:
            base = next((v for v in walk_provenance(version_id) if v.kind == "base"), None)
            if base is None:
                raise VersionUnresolvable(
                    f"version {version_id} (commit {version.corpus_commit_id}) is an adapter with no "
                    f"base version anywhere in its provenance"
                )

    _require_artifact(version, corpus_repo, artifact_exists)
    if base is not None:
        _require_artifact(base, corpus_repo, artifact_exists)

    return ResolvedVersion(
        line_id=version.line_id,
        version_id=version.id,
        kind=version.kind,
        corpus_repo=corpus_repo,
        corpus_commit_id=version.corpus_commit_id,
        artifact_ref=version.artifact_ref,
        base_version_id=base.id if base is not None else None,
        base_artifact_ref=base.artifact_ref if base is not None else None,
    )


def _require_artifact(
    version: ModelVersionModel,
    corpus_repo: Optional[str],
    artifact_exists: Optional[Callable[[str, str], bool]],
) -> None:
    """Report a missing artifact as itself — never fall back to another one."""
    if not version.artifact_ref:
        raise VersionUnresolvable(
            f"version {version.id} (commit {version.corpus_commit_id}) records no artifact to serve"
        )
    if artifact_exists is not None and not artifact_exists(corpus_repo, version.artifact_ref):
        raise VersionUnresolvable(
            f"version {version.id} (commit {version.corpus_commit_id}) points at "
            f"{version.artifact_ref}, which is not present in {corpus_repo}"
        )


####################
# Producers — ingest (R7) and bake (R5)
#
# DUCT TAPE, named deliberately: **core cannot stage bytes it does not hold.**
# A GGUF registered from llamolotl, and a LoRA a training job just fitted, both
# live on llamolotl's disk. Core never sees them, so what it stages into the
# line's repo is a *provenance manifest* — what was produced, from what, by
# which job — and the commit backing the version is a commit of that manifest,
# not of the artifact.
#
# The version is therefore honest about what it can prove: `artifact_ref` names
# the artifact as llamolotl knows it, and resolve_version()'s injected
# existence predicate is what actually confirms it is still there. Getting the
# artifact itself into self.corpus is the producing side's job and is filed as
# self.llamolotl#43 ("pipeline outputs carry structured provenance rather than
# being identified by path convention"). Until that lands, a line's history is
# complete and walkable while its repo holds manifests rather than weights.
####################

PROVENANCE_PREFIX = "provenance"


def _stage_provenance_manifest(corpus_repo: str, name: str, manifest: dict) -> bool:
    """Stage a manifest object on the line's branch so the commit has content.

    Best-effort by return value, not by silence: the caller decides. Returns
    False when self.corpus is not wired up at all, in which case there is
    nothing to commit and the caller should not try.
    """
    from selfai_ui.storage.provider import SELF_CORPUS_BRANCH, Storage

    if Storage.corpus_client is None:
        return False
    try:
        Storage.corpus_client.put_object(
            Bucket=corpus_repo,
            Key=f"{SELF_CORPUS_BRANCH}/{PROVENANCE_PREFIX}/{name}.json",
            Body=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )
        return True
    except Exception as e:
        log.warning(f"self.corpus manifest staging failed for repo {corpus_repo}: {e}")
        return False


async def ensure_line(
    app_state,
    user_id: str,
    *,
    line_id: Optional[str] = None,
    line_name: Optional[str] = None,
) -> ModelLineModel:
    """Return the named line, or create one with its self.corpus repo."""
    if line_id:
        line = ModelLines.get_line_by_id(line_id)
        if line is None:
            raise ModelVersionError(f"line {line_id} not found")
        return line

    line = ModelLines.insert_new_line(user_id, ModelLineForm(name=line_name or "model line"))
    if line is None:
        raise ModelVersionError("could not create a model line")

    repo_id = repo_id_for_model_line(line.id)
    endpoint, access_key_id, secret_access_key = _corpus_credentials(app_state)
    await create_repository(
        endpoint=endpoint,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        repo_id=repo_id,
    )
    updated = ModelLines.update_corpus_repo(line.id, repo_id)
    if updated is None:
        raise ModelVersionError(f"could not attach line {line.id} to repo {repo_id}")
    return updated


async def record_version_for_artifact(
    app_state,
    *,
    user_id: str,
    kind: str,
    artifact_ref: Optional[str],
    manifest: dict,
    line_id: Optional[str] = None,
    line_name: Optional[str] = None,
    produced_by: Optional[dict] = None,
    commit_message: Optional[str] = None,
) -> ModelVersionModel:
    """Attach an artifact produced elsewhere to a line, as its next version.

    The parent is the line's current latest version, so repeated calls build a
    chain rather than a set of orphans — three quantizations of one source land
    as three versions of one line.
    """
    line = await ensure_line(app_state, user_id, line_id=line_id, line_name=line_name)

    manifest_name = artifact_ref or f"{kind}-{len(ModelVersions.get_versions_by_line(line.id)) + 1}"
    if not _stage_provenance_manifest(line.corpus_repo, manifest_name.replace("/", "_"), manifest):
        raise ModelVersionError(
            f"could not stage a provenance manifest into {line.corpus_repo}; no version was recorded"
        )

    parent = latest_version(line.id)
    base_version_id = None
    if kind == "adapter":
        base = next((v for v in reversed(ModelVersions.get_versions_by_line(line.id)) if v.kind == "base"), None)
        base_version_id = base.id if base else None

    return await append_version(
        app_state,
        line.id,
        kind=kind,
        commit_message=commit_message or f"{kind} {manifest_name} on line {line.name}",
        parent_version_id=parent.id if parent else None,
        base_version_id=base_version_id,
        produced_by=produced_by,
        published_by=user_id,
        artifact_ref=artifact_ref,
        meta={"manifest": manifest},
    )


####################
# Publish (R6) — the expensive tier
####################


def current_base_and_adapters(line_id: str) -> tuple[ModelVersionModel, list[ModelVersionModel]]:
    """The base a publish would start from, and the adapters it would merge.

    The base is the line's current version when that is a `base`, otherwise the
    nearest `base` ancestor of it — a line sitting on an adapter still publishes
    onto the base underneath it, not onto nothing.

    Adapters are those after that base in `sequence`. Reverting the line first
    therefore narrows what a publish would merge, which is the behaviour an
    artist expects from "go back, then publish".
    """
    line = ModelLines.get_line_by_id(line_id)
    if line is None:
        raise ModelVersionError(f"line {line_id} not found")

    history = ModelVersions.get_versions_by_line(line_id)
    if not history:
        raise ModelVersionError(f"line {line_id} has no versions to publish")

    current = None
    if line.current_version_id:
        current = ModelVersions.get_version_by_id(line.current_version_id)
    current = current or history[-1]

    if current.kind == "base":
        base = current
    else:
        base = next((v for v in walk_provenance(current.id) if v.kind == "base"), None)
        if base is None:
            raise ModelVersionError(f"line {line_id} has no base version to publish onto")

    adapters = [v for v in history if v.kind == "adapter" and v.sequence > base.sequence]
    return base, adapters


async def create_publish_job(
    app_state,
    line_id: str,
    *,
    user_id: str,
    form_data: PublishJobForm,
) -> PublishJobModel:
    """Validate a publish and enqueue it. Does NOT touch the GPU.

    Everything that can be known without the card is decided here, so a user
    learns immediately that there is nothing to publish rather than discovering
    it a window later. The merge itself is dispatched by `utils/gpu_queue.py`
    once a window and a VRAM lease are available (self.ai#136).
    """
    line = ModelLines.get_line_by_id(line_id)
    if line is None:
        raise ModelVersionError(f"line {line_id} not found")
    if not line.corpus_repo:
        raise ModelVersionError(f"line {line_id} has no self.corpus repo to publish into")

    base, adapters = current_base_and_adapters(line_id)
    if not adapters:
        # Several GB of merge and quantize to reproduce the base byte for byte.
        raise ModelVersionError(
            f"line {line_id} has no adapter versions since its current base "
            f"({base.id}); there is nothing to publish"
        )

    if not form_data.output_name:
        form_data = form_data.model_copy(
            update={"output_name": f"{line.name}-v{base.sequence + len(adapters) + 1}"}
        )

    return PublishJobs.insert_new_job(line_id, user_id, base.id, [v.id for v in adapters], form_data)


async def start_publish_merge(app_state, job: PublishJobModel, merge: Optional[Callable] = None) -> str:
    """Ask llamolotl to merge. Returns its pipeline task id.

    Deliberately does not record a version: the POST returns as soon as the
    remote task is *queued*, and llamolotl's own `_GPU_PIPELINE_TYPES` says a
    merge is GPU work that runs for as long as it runs. Recording here would
    claim a base GGUF that does not exist yet — and would release the card
    while the merge was still using it.
    """
    base = ModelVersions.get_version_by_id(job.base_version_id)
    if base is None:
        raise ModelVersionError(f"publish job {job.id} names base version {job.base_version_id}, which is gone")

    adapters = [ModelVersions.get_version_by_id(v) for v in (job.adapter_version_ids or [])]
    missing = [v for v, resolved in zip(job.adapter_version_ids or [], adapters) if resolved is None]
    if missing:
        raise ModelVersionError(f"publish job {job.id} names adapter version(s) that are gone: {missing}")

    if merge is None:
        from selfai_ui.utils.publish_transport import merge_adapters_into_base as merge

    result = await merge(app_state, base, adapters, job.output_name, job.quant_type)
    task_id = (result or {}).get("task_id") or (result or {}).get("id")
    if not task_id:
        raise ModelVersionError(f"llamolotl accepted the merge for job {job.id} but returned no task id")
    return task_id


async def complete_publish_job(
    app_state,
    job: PublishJobModel,
    artifact_ref: Optional[str] = None,
) -> PublishJobModel:
    """Record the finished merge as the line's next base version.

    Called once llamolotl reports the pipeline task completed — not before.
    Everything R6 requires happens here: a `base` version parented on the base
    the publish started from, provenance naming every adapter merged, and the
    line moved to the result. A failure at any step leaves the pointer where it
    was and writes no version row.
    """
    line = ModelLines.get_line_by_id(job.line_id)
    if line is None:
        raise ModelVersionError(f"line {job.line_id} not found")

    base = ModelVersions.get_version_by_id(job.base_version_id)
    adapter_ids = list(job.adapter_version_ids or [])
    artifact_ref = artifact_ref or job.output_name

    try:
        if not _stage_provenance_manifest(
            line.corpus_repo,
            (artifact_ref or job.id).replace("/", "_"),
            {
                "publish_job": job.id,
                "base_version": job.base_version_id,
                "merged_adapter_versions": adapter_ids,
                "output": artifact_ref,
                "quant_type": job.quant_type,
            },
        ):
            raise ModelVersionError(f"could not stage a provenance manifest into {line.corpus_repo}")

        version = await append_version(
            app_state,
            job.line_id,
            kind="base",
            # The base it started from, per R6 — not the newest version, which
            # is the last adapter that fed it.
            parent_version_id=job.base_version_id,
            commit_message=f"publish {artifact_ref} onto line {line.name}",
            produced_by={"job_kind": "publish", "job_id": job.id},
            published_by=job.user_id,
            artifact_ref=artifact_ref,
            meta={"merged_adapter_versions": adapter_ids, "base_version": job.base_version_id},
        )
    except Exception as e:
        PublishJobs.update_status(job.id, "failed", error_message=str(e))
        log.warning(f"publish job {job.id} failed while recording its version: {e}")
        raise

    ModelLines.set_current_version(job.line_id, version.id, moved_by=job.user_id)
    log.info(f"publish job {job.id} recorded version {version.id} on line {job.line_id} (base was {base.id})")
    return PublishJobs.update_status(job.id, "completed", result_version_id=version.id)


def _line_pointer(entry: dict) -> tuple[Optional[str], Optional[str]]:
    """Read (line_id, version_id) off a model-surface entry, if it has them."""
    info = entry.get("info") or {}
    meta = info.get("meta") or {}
    if not isinstance(meta, dict):
        return None, None
    return meta.get("line_id"), meta.get("version_id")


def attach_lines_to_models(models: list[dict]) -> list[dict]:
    """Collapse a line's versions into one entry, carrying its history.

    A line appears **once** in the picker with its version selectable inside
    that entry — not once per adapter and once per publish (Decision 10). An
    artist who bakes four characters and publishes twice would otherwise see
    six lookalike rows with nothing indicating they are one model at six points
    in its history.

    Entries that belong to no line are returned **untouched**, and a line whose
    record has vanished is left alone rather than half-annotated: a user who
    never touches versioning should not be able to tell this shipped.
    """
    grouped: dict[str, list[dict]] = {}
    for entry in models:
        line_id, _version_id = _line_pointer(entry)
        if line_id:
            grouped.setdefault(line_id, []).append(entry)

    if not grouped:
        return models

    keep: dict[int, dict] = {}
    drop: set[int] = set()

    for line_id, entries in grouped.items():
        line = ModelLines.get_line_by_id(line_id)
        if line is None:
            continue

        history = ModelVersions.get_versions_by_line(line_id)
        sequence_by_version = {v.id: v.sequence for v in history}

        chosen = next(
            (e for e in entries if _line_pointer(e)[1] == line.current_version_id),
            None,
        ) or max(entries, key=lambda e: sequence_by_version.get(_line_pointer(e)[1], 0))

        chosen_version_id = _line_pointer(chosen)[1]
        chosen["line"] = {
            "id": line.id,
            "name": line.name,
            "current_version_id": line.current_version_id,
            # The version this entry *is*, which is what a client shows without
            # expanding anything.
            "version_id": chosen_version_id,
            "versions": [
                {
                    "id": v.id,
                    "sequence": v.sequence,
                    "kind": v.kind,
                    "corpus_commit_id": v.corpus_commit_id,
                    "artifact_ref": v.artifact_ref,
                    "created_at": v.created_at,
                }
                for v in history
            ],
        }

        keep[id(chosen)] = chosen
        for entry in entries:
            if entry is not chosen:
                drop.add(id(entry))

    if not drop:
        return models
    return [entry for entry in models if id(entry) not in drop]


def revert_line(line_id: str, version_id: str, moved_by: Optional[str] = None) -> ModelLineModel:
    """Move a line back to an earlier version.

    Rebuilds nothing and commits nothing: the artifacts a line has already
    produced still exist, so going back is a pointer move. That is what makes
    an artist's undo affordable (Decision 5).
    """
    line = ModelLines.set_current_version(line_id, version_id, moved_by=moved_by)
    if line is None:
        raise ModelVersionError(f"could not move line {line_id} to version {version_id}")
    return line


__all__ = [
    "ProvenanceBroken",
    "ResolvedVersion",
    "SelfCorpusError",
    "VersionUnresolvable",
    "append_version",
    "attach_lines_to_models",
    "current_base_and_adapters",
    "ensure_line",
    "complete_publish_job",
    "create_publish_job",
    "start_publish_merge",
    "record_version_for_artifact",
    "latest_version",
    "resolve_version",
    "revert_line",
    "walk_provenance",
]
