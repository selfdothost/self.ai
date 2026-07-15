import logging

import aiohttp

from selfai_ui.env import AIOHTTP_CLIENT_TIMEOUT, SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


class SelfCorpusError(Exception):
    pass


# self.corpus/self.corpus#3: self.ai-public's ACL grant (fs:CreateRepository/
# DeleteRepository) is scoped to arn:lakefs:fs:::repository/selfai-*, not a
# blanket grant. Every repo self.ai creates must carry this prefix or repo
# creation/deletion 401s regardless of credential validity.
SELFAI_REPO_PREFIX = "selfai-"


def repo_id_for_kb(kb_id: str) -> str:
    """self.corpus repo id for a Knowledge Base, matching self.ai-public's
    scoped ACL grant (selfai-* resource pattern)."""
    return f"{SELFAI_REPO_PREFIX}{kb_id}"


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
