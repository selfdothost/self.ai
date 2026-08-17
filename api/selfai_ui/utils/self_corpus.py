import hashlib
import json
import logging
import re
from typing import AsyncIterator, Optional

import aiohttp

from selfai_ui.env import AIOHTTP_CLIENT_TIMEOUT, SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


class SelfCorpusError(Exception):
    pass


class SelfCorpusNothingToCommit(SelfCorpusError):
    """A commit was requested but the branch held no staged changes.

    Distinct from a rejected commit on purpose: a publish that staged no bytes
    and a publish whose commit LakeFS refused are different outcomes, and a
    caller that cannot tell them apart will record a version backed by nothing.
    """


# self.corpus/self.corpus#3: self.ai-public's ACL grant (fs:CreateRepository/
# DeleteRepository) is scoped to arn:lakefs:fs:::repository/selfai-*, not a
# blanket grant. Every repo self.ai creates must carry this prefix or repo
# creation/deletion 401s regardless of credential validity.
SELFAI_REPO_PREFIX = "selfai-"

# Model-line repos are distinguished from KB repos by this infix, not by a
# separate prefix — the ACL grant above is the whole namespace self.ai has.
# cavekit-corpus-model-ingest.md R6 allows non-KB repos to skip the prefix;
# under the live policy that is unusable, so model lines carry it too (see
# cavekit-model-versioning.md R4).
MODEL_LINE_INFIX = "line-"


def repo_id_for_kb(kb_id: str) -> str:
    """self.corpus repo id for a Knowledge Base, matching self.ai-public's
    scoped ACL grant (selfai-* resource pattern)."""
    return f"{SELFAI_REPO_PREFIX}{kb_id}"


def repo_id_for_model_line(line_id: str) -> str:
    """self.corpus repo id for a model line (self.ai#131).

    Carries SELFAI_REPO_PREFIX for the same reason repo_id_for_kb() does — the
    self-ai-public-repo-crud policy is scoped to
    arn:lakefs:fs:::repository/selfai-*, so an unprefixed create 401s whatever
    the credential — and adds MODEL_LINE_INFIX so a repo id says which kind of
    artifact it belongs to without consulting the database.
    """
    return f"{SELFAI_REPO_PREFIX}{MODEL_LINE_INFIX}{line_id}"


# Weights repos (self.ai#141). A third infix under the same single granted
# prefix, for the same reason as MODEL_LINE_INFIX: the ACL is the whole
# namespace self.ai has, so the kind of artifact has to be encoded inside it.
MODEL_WEIGHTS_INFIX = "model-"

# LakeFS validates repository names as ^[a-z0-9][a-z0-9-]{2,62}$ — lowercase,
# digits and dashes only, 3..63 characters. A model id is a GGUF filename
# ("Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf"): uppercase, dots, underscores.
# repo_id_for_kb() gets away with pasting its id straight in because a KB id is
# a uuid; a model id must be transformed, and a transformation that is not
# injective silently merges two models' weights into one repo.
LAKEFS_REPO_MAX_LEN = 63
_MODEL_REPO_DIGEST_LEN = 8
_NON_REPO_CHAR = re.compile(r"[^a-z0-9]+")


def _repo_slug(value: str, budget: int) -> str:
    """Lowercase/dash-only fragment of `value`, at most `budget` characters."""
    slug = _NON_REPO_CHAR.sub("-", value.lower()).strip("-")
    return slug[:budget].strip("-")


def repo_id_for_model(model_id: str) -> str:
    """self.corpus repo id for one model's weights (self.ai#141).

    Carries SELFAI_REPO_PREFIX because the self-ai-public-repo-crud policy is
    scoped to arn:lakefs:fs:::repository/selfai-* — a repo id without it is
    refused by IAM, not merely by convention, so this is not cosmetic.

    A digest of the *original* model id is always appended rather than only
    when the slug came out lossy. "Qwen2.5-Coder-32B" and "Qwen2_5_Coder_32B"
    slugify identically, and two models sharing one weights repo is a data-loss
    bug, not a naming wart. The digest makes the mapping injective in practice;
    the human-readable slug in front of it keeps a repo listing legible. The
    reverse direction (repo -> model row) is not derived from this string at
    all — it is read from the catalogue object inside the repo.
    """
    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:_MODEL_REPO_DIGEST_LEN]
    fixed = f"{SELFAI_REPO_PREFIX}{MODEL_WEIGHTS_INFIX}"
    budget = LAKEFS_REPO_MAX_LEN - len(fixed) - _MODEL_REPO_DIGEST_LEN - 1
    slug = _repo_slug(model_id, budget)
    # An id that slugifies to nothing at all (e.g. "___") still has to produce a
    # valid name, so the digest stands in for the slug rather than leaving a
    # leading dash.
    return f"{fixed}{slug}-{digest}" if slug else f"{fixed}{digest}"


def is_model_weights_repo(repo_id: str) -> bool:
    """Whether a repo id is one of ours and holds a model's weights."""
    return repo_id.startswith(f"{SELFAI_REPO_PREFIX}{MODEL_WEIGHTS_INFIX}")


async def create_repository(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    bucket: str = "self-corpus",
    default_branch: str = "main",
) -> dict:
    """Create a LakeFS repository in self.corpus, keyed by repo_id.

    One repo per artifact (KB/Dataset/Course/Model), per
    2026-07-11-selfai-corpus-connections.md. Raises SelfCorpusError on
    failure — callers decide whether that's fatal to their own operation.
    """
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    payload = {
        "name": repo_id,
        "storage_namespace": f"s3://{bucket}/{repo_id}",
        "default_branch": default_branch,
    }
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, auth=auth) as session:
            async with session.post(
                f"{endpoint}/api/v1/repositories",
                json=payload,
            ) as response:
                result = await response.json()
                if response.status >= 400:
                    detail = result.get("message") if isinstance(result, dict) else result
                    raise SelfCorpusError(f"self.corpus repo creation failed ({response.status}): {detail}")
                return result
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus connection error: {e}")
        raise SelfCorpusError(f"self.corpus connection error: {e}") from e


async def backfill_missing_repos(app_state) -> dict:
    """Create self.corpus repos for public KBs/Datasets that don't have one yet.

    Datasets are plain Knowledge rows (no separate model), so this covers both
    in one pass. Covers KBs created before the selfai- prefix fix (this file),
    or before self.corpus's ACL grant existed at all (self.corpus#3) — anyone
    fixing either of those needs a way to catch up existing rows, not just new
    ones. Best-effort per KB; one failure doesn't stop the rest.
    """
    from selfai_ui.models.knowledge import Knowledges

    cfg = app_state.config
    if not cfg.ENABLE_SELF_CORPUS:
        return {"skipped": True, "reason": "ENABLE_SELF_CORPUS is False"}

    created, failed = [], []
    for kb in Knowledges.get_knowledge_bases():
        if kb.access_control is not None:
            continue
        if (kb.meta or {}).get("self_corpus", {}).get("repo"):
            continue

        repo_id = repo_id_for_kb(kb.id)
        try:
            await create_repository(
                endpoint=cfg.SELF_CORPUS_LAKEFS_ENDPOINT,
                access_key_id=cfg.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID,
                secret_access_key=cfg.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY,
                repo_id=repo_id,
            )
            Knowledges.update_knowledge_meta_by_id(
                kb.id, {**(kb.meta or {}), "self_corpus": {"repo": repo_id}}
            )
            created.append(kb.id)
        except SelfCorpusError as e:
            log.warning(f"self.corpus backfill: repo creation failed for KB {kb.id}: {e}")
            failed.append(kb.id)

    if created or failed:
        log.info(f"self.corpus backfill: {len(created)} repo(s) created, {len(failed)} failed")
    return {"created": created, "failed": failed}


####################
# Strict versioning primitives (self.ai#131)
#
# storage/provider.py's _commit_corpus_branch() is deliberately best-effort and
# never raises: a self.corpus outage must not break the Knowledge Base feature.
# That posture is correct there and wrong here. A model version records the
# commit that backs it, so a swallowed commit failure writes a version row
# pointing at a commit that does not exist. These operations raise; the KB path
# is untouched.
#
# They go over LakeFS's REST API with aiohttp, matching create_repository()
# above, rather than the native `lakefs` client provider.py uses — same
# endpoint and credentials, one HTTP style in this module.
####################


def _corpus_is_enabled() -> bool:
    """Read ENABLE_SELF_CORPUS at call time.

    PersistentConfig values are mutable at runtime, so an import-time read
    would pin whatever the flag was at process start.
    """
    from selfai_ui.config import ENABLE_SELF_CORPUS

    return bool(getattr(ENABLE_SELF_CORPUS, "value", ENABLE_SELF_CORPUS))


def _require_corpus(endpoint: str, access_key_id: str, secret_access_key: str) -> None:
    """Refuse before issuing a request that cannot succeed."""
    if not _corpus_is_enabled():
        raise SelfCorpusError("self.corpus is disabled (ENABLE_SELF_CORPUS is False)")
    if not endpoint or not access_key_id or not secret_access_key:
        raise SelfCorpusError("self.corpus endpoint or credentials are not configured")


def _looks_like_empty_commit(status: int, detail: str) -> bool:
    """LakeFS reports an empty commit as a 4xx, same shape as a real refusal.

    It is only the message that separates them, so this match is deliberately
    narrow — anything unrecognised stays a SelfCorpusError rather than being
    reported as a benign no-op.
    """
    if status not in (400, 409):
        return False
    text = (detail or "").lower()
    return "no changes" in text or "nothing to commit" in text or "empty commit" in text


async def _corpus_post(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    path: str,
    payload: dict,
    operation: str,
) -> dict:
    """POST to the LakeFS API, raising SelfCorpusError on any failure."""
    _require_corpus(endpoint, access_key_id, secret_access_key)
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, auth=auth) as session:
            async with session.post(f"{endpoint}{path}", json=payload) as response:
                body = await response.text()
                if response.status >= 400:
                    detail = body
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict) and parsed.get("message"):
                            detail = parsed["message"]
                    except ValueError:
                        pass
                    if operation == "commit" and _looks_like_empty_commit(response.status, detail):
                        raise SelfCorpusNothingToCommit(f"self.corpus {operation}: nothing to commit ({detail})")
                    raise SelfCorpusError(f"self.corpus {operation} failed ({response.status}): {detail}")
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    return {}
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus connection error: {e}")
        raise SelfCorpusError(f"self.corpus connection error: {e}") from e


async def commit_branch(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    message: str,
    branch: str = "main",
    metadata: Optional[dict] = None,
) -> str:
    """Commit whatever is staged on a branch. Returns the commit id.

    Raises SelfCorpusNothingToCommit when the branch held no changes, and
    SelfCorpusError for every other failure — callers recording a version need
    to tell those apart.
    """
    payload: dict = {"message": message}
    if metadata:
        payload["metadata"] = {str(k): str(v) for k, v in metadata.items()}
    result = await _corpus_post(
        endpoint,
        access_key_id,
        secret_access_key,
        f"/api/v1/repositories/{repo_id}/branches/{branch}/commits",
        payload,
        "commit",
    )
    commit_id = result.get("id")
    if not commit_id:
        raise SelfCorpusError(f"self.corpus commit succeeded but returned no commit id for repo {repo_id}")
    log.info(f"self.corpus commit for repo {repo_id} on {branch}: {commit_id}")
    return commit_id


async def create_branch(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    branch: str,
    source_reference: str = "main",
) -> str:
    """Create a branch from a source ref. Returns the new branch's ref id."""
    result = await _corpus_post(
        endpoint,
        access_key_id,
        secret_access_key,
        f"/api/v1/repositories/{repo_id}/branches",
        {"name": branch, "source": source_reference},
        "branch creation",
    )
    return result.get("id") or branch


async def create_tag(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    tag: str,
    source_reference: str,
) -> str:
    """Tag a ref. Returns the tag's id."""
    result = await _corpus_post(
        endpoint,
        access_key_id,
        secret_access_key,
        f"/api/v1/repositories/{repo_id}/tags",
        {"id": tag, "ref": source_reference},
        "tag creation",
    )
    return result.get("id") or tag


async def merge_into(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    source_reference: str,
    destination_branch: str,
    message: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Merge a ref into a branch. Returns the merge commit's id."""
    payload: dict = {}
    if message:
        payload["message"] = message
    if metadata:
        payload["metadata"] = {str(k): str(v) for k, v in metadata.items()}
    result = await _corpus_post(
        endpoint,
        access_key_id,
        secret_access_key,
        f"/api/v1/repositories/{repo_id}/refs/{source_reference}/merge/{destination_branch}",
        payload,
        "merge",
    )
    merge_id = result.get("reference") or result.get("id")
    if not merge_id:
        raise SelfCorpusError(f"self.corpus merge succeeded but returned no reference for repo {repo_id}")
    return merge_id


####################
# Object transfer + repo/branch inspection (self.ai#141)
#
# Model weights are multi-GB, which breaks two assumptions the primitives above
# were written under.
#
# 1. AIOHTTP_CLIENT_TIMEOUT is a *total* timeout. Applied to a 20 GiB transfer
#    it aborts a healthy download mid-flight, so the transfer path uses a read
#    timeout instead: a stalled socket still fails, a slow-but-moving one does
#    not.
# 2. Nothing here may hold a whole object. upload_object() takes an async
#    iterator of chunks and hands it to aiohttp as a chunked request body, so
#    the bytes move source -> socket without a full copy existing anywhere in
#    this process. Passing `bytes` would work and is exactly what must not
#    happen for weights; callers with a real file stream it.
####################

# total=None on purpose (see above). sock_read bounds a stalled peer.
WEIGHTS_TRANSFER_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=60, sock_connect=60, sock_read=300)


async def _corpus_get(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    path: str,
    operation: str,
    params: Optional[dict] = None,
) -> dict:
    """GET from the LakeFS API, raising SelfCorpusError on any failure."""
    _require_corpus(endpoint, access_key_id, secret_access_key)
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, auth=auth) as session:
            async with session.get(f"{endpoint}{path}", params=params or {}) as response:
                body = await response.text()
                if response.status >= 400:
                    raise SelfCorpusError(f"self.corpus {operation} failed ({response.status}): {body}")
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    return {}
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus connection error: {e}")
        raise SelfCorpusError(f"self.corpus connection error: {e}") from e


async def repository_exists(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
) -> bool:
    """Whether a repo is really there.

    A 404 is the answer False, not a failure. Every other error still raises:
    "self.corpus is unreachable" and "this repo does not exist" must not
    collapse into one another, or a mismatch sweep reports every model as
    dangling the moment LakeFS blips.
    """
    _require_corpus(endpoint, access_key_id, secret_access_key)
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, auth=auth) as session:
            async with session.get(f"{endpoint}/api/v1/repositories/{repo_id}") as response:
                if response.status == 404:
                    return False
                if response.status >= 400:
                    detail = await response.text()
                    raise SelfCorpusError(f"self.corpus repo lookup failed ({response.status}): {detail}")
                return True
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus connection error: {e}")
        raise SelfCorpusError(f"self.corpus connection error: {e}") from e


async def list_repositories(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    prefix: str = SELFAI_REPO_PREFIX,
    page_size: int = 100,
) -> list[str]:
    """Every repo id under `prefix`, following LakeFS pagination to the end.

    Stopping at the first page would under-report, and an under-reported
    listing reads as "no orphans" — the reassuring answer, arrived at wrongly.
    """
    repos: list[str] = []
    after = ""
    while True:
        result = await _corpus_get(
            endpoint,
            access_key_id,
            secret_access_key,
            "/api/v1/repositories",
            "repo listing",
            params={"prefix": prefix, "amount": str(page_size), **({"after": after} if after else {})},
        )
        repos.extend(r["id"] for r in result.get("results", []) if isinstance(r, dict) and r.get("id"))
        pagination = result.get("pagination") or {}
        if not pagination.get("has_more"):
            return repos
        next_after = pagination.get("next_offset")
        if not next_after or next_after == after:
            # has_more with no usable cursor would spin forever.
            log.warning(f"self.corpus repo listing: has_more with no next_offset after {len(repos)} repo(s)")
            return repos
        after = next_after


async def delete_branch(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    branch: str,
) -> None:
    """Delete a branch. A branch that is already gone is success, not failure."""
    _require_corpus(endpoint, access_key_id, secret_access_key)
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, auth=auth) as session:
            async with session.delete(f"{endpoint}/api/v1/repositories/{repo_id}/branches/{branch}") as response:
                if response.status >= 400 and response.status != 404:
                    detail = await response.text()
                    raise SelfCorpusError(f"self.corpus branch deletion failed ({response.status}): {detail}")
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus connection error: {e}")
        raise SelfCorpusError(f"self.corpus connection error: {e}") from e


async def upload_object(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    branch: str,
    path: str,
    stream: AsyncIterator[bytes],
    content_type: str = "application/octet-stream",
) -> dict:
    """Stage one object on a branch, streaming it — never materialising it.

    `stream` is an async iterator of chunks, handed to aiohttp as a chunked
    request body. This is the whole reason the function exists: a signature
    taking `bytes` would be shorter and would make a 20 GiB model a 20 GiB
    resident buffer.

    Staging only. Nothing here is visible on a ref until a commit, which is
    what lets the ingest pipeline abandon a failed transfer with nothing to
    clean up on the branch a reader would look at.
    """
    _require_corpus(endpoint, access_key_id, secret_access_key)
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    url = f"{endpoint}/api/v1/repositories/{repo_id}/branches/{branch}/objects"
    try:
        async with aiohttp.ClientSession(timeout=WEIGHTS_TRANSFER_TIMEOUT, trust_env=True, auth=auth) as session:
            async with session.put(
                url,
                params={"path": path},
                data=stream,
                headers={"Content-Type": content_type},
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise SelfCorpusError(f"self.corpus object upload failed ({response.status}): {body}")
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    return {}
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus object upload connection error: {e}")
        raise SelfCorpusError(f"self.corpus object upload connection error: {e}") from e


async def delete_repository(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
) -> None:
    """Delete a LakeFS repository in self.corpus. Raises SelfCorpusError on
    failure — callers decide whether that's fatal to their own operation."""
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    auth = aiohttp.BasicAuth(access_key_id, secret_access_key)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, auth=auth) as session:
            async with session.delete(f"{endpoint}/api/v1/repositories/{repo_id}") as response:
                if response.status >= 400 and response.status != 404:
                    detail = await response.text()
                    raise SelfCorpusError(f"self.corpus repo deletion failed ({response.status}): {detail}")
    except SelfCorpusError:
        raise
    except Exception as e:
        log.error(f"self.corpus connection error: {e}")
        raise SelfCorpusError(f"self.corpus connection error: {e}") from e
