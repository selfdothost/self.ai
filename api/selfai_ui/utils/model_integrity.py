"""Periodic /models integrity sweep (self.ai/self.ai#38).

Background: `nomic-embed-text-v1.5.Q5_K_M.gguf` was wired into
`manifests/llamolotl/05-models-preset-configmap.yaml` as a preset section
but the GGUF was never pulled to the `/models` PVC. Nothing surfaced the
gap until `/v1/embeddings` started 500'ing in production. This module is
the self.ai-side half of the fix: a periodic check that a model self.ai
believes is usable (i.e. llama-server reports it via `/v1/models`, which
is what self.ai's own `/api/llamolotl/models` proxies to clients) still
has a real, non-truncated GGUF backing it on self.llamolotl's `/models`.

Scope note (see the issue): self.ai's API server has no filesystem access
to `/models` -- self.llamolotl (the training-api / control server) owns
that PVC and the pull/convert pipeline. This sweep is therefore HTTP-only:
it cross-references `GET {base_url}/v1/models` (known models) against
`GET {control_url}/api/models/available` (on-disk GGUFs with real byte
sizes, already implemented in self.llamolotl/api/routers/models.py --
no new self.llamolotl endpoint needed). It does NOT walk `--models-preset`
INI sections directly (self.ai has no visibility into that ConfigMap's
content), so it cannot catch a preset-only section that llama-server never
surfaced through `/v1/models` in the first place -- that class of check
needs to live in self.llamolotl itself, per the issue's own scope note.
What it does catch: any model self.ai currently lists to clients whose
backing file has since gone missing, or been truncated/corrupted, on disk.

No checksum scheme exists anywhere in this codebase for GGUFs --
self.llamolotl's `models_meta.json` (api/state.py::_record_model_meta)
records hf_repo/hf_filename/quant/source_type/pulled_at, never a size or
hash. Per the issue's "doesn't need a full checksum" guidance, integrity
here is existence + a minimum-size sanity floor, not a real digest.

Autopull is intentionally NOT implemented here (see the issue: "default
off... shouldn't happen silently without explicit opt-in"). It would also
be non-trivial from self.ai's side specifically for the *missing* case:
`models_meta.json` only gains an entry once a model has been successfully
pulled/baked, so a model that was configured via preset-only and never
pulled has no recorded hf_repo/hf_filename anywhere self.ai can read --
there is nothing to autopull *from*. Follow-up, if wanted: teach
self.llamolotl to record intended hf_repo/hf_filename for preset-only
sections so a future autopull has a real source of truth.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.routers.llamolotl import LLAMOLOTL_AUDIENCE, send_get_request
from selfai_ui.utils.service_auth import mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["LLAMOLOTL"])


# Sweep cadence and the size sanity floor are simple env-overridable module
# constants, matching the existing periodic-task pattern in this codebase
# (utils/gpu_queue.py's POLL_INTERVAL/LOCK_TIMEOUT, main.py's
# _ensure_curator_classifier_models' POLL_INTERVAL/MAX_WAIT) rather than a
# full PersistentConfig -- this is an internal ops cadence, not a
# per-connection admin setting exposed anywhere in the UI today.
SWEEP_ENABLED = os.environ.get("ENABLE_MODEL_INTEGRITY_SWEEP", "True").lower() == "true"
SWEEP_INTERVAL_SECONDS = int(os.environ.get("MODEL_INTEGRITY_SWEEP_INTERVAL", str(60 * 60)))  # hourly default

# Real GGUFs -- even the smallest embedding/reranker models in this fleet
# (nomic-embed, Qwen3-Reranker-0.6B) -- run tens of MB or more. Anything
# under this floor is certainly a truncated/interrupted download, not a
# legitimately tiny model.
MIN_SANE_GGUF_SIZE_BYTES = int(os.environ.get("MODEL_INTEGRITY_MIN_SIZE_BYTES", str(1 * 1024 * 1024)))


async def _fetch_known_model_ids(base_url: str, key: Optional[str]) -> Optional[set]:
    """Model ids llama-server currently reports via /v1/models.

    Returns None (not an empty set) on connection failure so callers can
    skip a sweep cycle instead of reporting every configured model as
    "missing" just because the backend was briefly unreachable.
    """
    result = await send_get_request(f"{base_url}/v1/models", key)
    if result is None or "data" not in result:
        return None
    return {model.get("id", "") for model in result["data"] if model.get("id")}


async def _fetch_available_models(control_url: str, key: Optional[str]) -> Optional[list]:
    """GGUFs self.llamolotl's control server reports as present on disk,
    via its existing /api/models/available introspection endpoint."""
    result = await send_get_request(
        f"{control_url}/api/models/available",
        key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:read"),
    )
    if result is None or not isinstance(result, list):
        return None
    return result


def _index_by_stem(available: list) -> dict:
    """Index on-disk entries by GGUF filename stem (basename, no
    extension) -- matches how llama-server router mode names its
    auto-generated presets (confirmed in
    manifests/llamolotl/05-models-preset-configmap.yaml: "matches on the
    GGUF's filename stem, no extension")."""
    index: dict[str, dict[str, Any]] = {}
    for entry in available:
        name = entry.get("name")
        if not name:
            continue
        index[Path(name).stem] = entry
    return index


async def sweep_once(app_state) -> dict:
    """Run one integrity sweep across every configured llamolotl backend.

    Returns the findings dict (also stashed on
    app_state.MODEL_INTEGRITY_WARNINGS, keyed by base_url, so an admin UI
    can query it later without waiting on the next sweep cycle).
    """
    cfg = app_state.config
    findings: dict[str, list] = {}

    if not getattr(cfg, "ENABLE_LLAMOLOTL_API", False):
        app_state.MODEL_INTEGRITY_WARNINGS = findings
        return findings

    base_urls = cfg.LLAMOLOTL_BASE_URLS or []
    control_urls = cfg.LLAMOLOTL_CONTROL_BASE_URLS or []
    api_configs = cfg.LLAMOLOTL_API_CONFIGS or {}

    for idx, base_url in enumerate(base_urls):
        if idx >= len(control_urls):
            log.warning(
                "Model integrity sweep: no matching LLAMOLOTL_CONTROL_BASE_URLS "
                f"entry for LLAMOLOTL_BASE_URLS[{idx}]={base_url!r}; skipping."
            )
            continue

        control_url = control_urls[idx]
        api_config = api_configs.get(base_url, {})
        if not api_config.get("enable", True):
            continue

        key = api_config.get("key")

        known_ids = await _fetch_known_model_ids(base_url, key)
        available = await _fetch_available_models(control_url, key)

        if known_ids is None or available is None:
            log.warning(
                "Model integrity sweep: could not reach llamolotl backend "
                f"(base_url={base_url!r}, control_url={control_url!r}); "
                "skipping this cycle."
            )
            continue

        available_by_stem = _index_by_stem(available)
        issues = []

        for model_id in sorted(known_ids):
            entry = available_by_stem.get(model_id)
            if entry is None:
                msg = (
                    f"Model integrity sweep: model '{model_id}' is known to "
                    f"{base_url} but self.llamolotl's control server "
                    f"({control_url}) reports no backing GGUF for it under "
                    "/models. Requests against it will fail at inference time."
                )
                log.warning(msg)
                issues.append({"model_id": model_id, "issue": "missing", "detail": msg})
                continue

            size = entry.get("size", 0) or 0
            if size < MIN_SANE_GGUF_SIZE_BYTES:
                msg = (
                    f"Model integrity sweep: model '{model_id}' backing file "
                    f"'{entry.get('name')}' on {control_url} is only {size} "
                    f"bytes (below the {MIN_SANE_GGUF_SIZE_BYTES}-byte sanity "
                    "floor) -- likely truncated or an interrupted/failed pull."
                )
                log.warning(msg)
                issues.append(
                    {
                        "model_id": model_id,
                        "issue": "undersized",
                        "size_bytes": size,
                        "min_sane_size_bytes": MIN_SANE_GGUF_SIZE_BYTES,
                        "detail": msg,
                    }
                )

        if issues:
            findings[base_url] = issues

    app_state.MODEL_INTEGRITY_WARNINGS = findings
    return findings


async def run_periodic_sweep(app_state) -> None:
    """Background task: run sweep_once on a fixed interval for the process
    lifetime. Mirrors the while-True/try-except/sleep shape used by
    utils/gpu_queue.py's process_gpu_queue_v2 and main.py's
    _ensure_curator_classifier_models."""
    if not SWEEP_ENABLED:
        log.info("Model integrity sweep disabled (ENABLE_MODEL_INTEGRITY_SWEEP=false).")
        return

    if not hasattr(app_state, "MODEL_INTEGRITY_WARNINGS"):
        app_state.MODEL_INTEGRITY_WARNINGS = {}

    while True:
        try:
            await sweep_once(app_state)
        except Exception as e:
            log.error(f"Model integrity sweep cycle failed: {e}", exc_info=True)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
