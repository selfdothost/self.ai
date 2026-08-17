"""Chat/API lease admission checkpoint (Decision 6 / R3).

Core's local-generation request path (``routers/llamolotl.py``
``generate_chat_completion`` / ``generate_completion``) consults this AFTER
resolving the requested model and BEFORE dispatching to self.llamolotl, so a
request either:

  * **proceeds** normally, or
  * is **refused** with a structured 503 because a training/pipeline/curator
    window holds an EXCLUSIVE lease (T-015), or
  * is transparently **substituted** onto the already-loaded eval model instead
    of forcing a competing load during an eval window (T-016).

It reads the VRAM lease registry SYNCHRONOUSLY (no network call belongs in the
request path) — the exclusive-holder flag (``active_exclusive_holder``, T-002)
and the loaded model (``loaded_model``, T-013), both relayed there by the R6
poller (T-012). The staleness asymmetry (training fails closed, eval fails open,
T-019) refines those reads.

This module is the **T-014 scaffold**: the decision type, the evaluation entry
point (currently PROCEED-only), and the seam both router entry points dispatch
on. T-015 / T-016 / T-019 fill in the exclusive-refuse / eval-substitute /
staleness logic against the same seam, so the router wiring never changes again.
"""

import enum
import logging
import time
from typing import Optional

from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.eval_jobs import EvalJobs
from selfai_ui.models.vram_leases import STALE_THRESHOLD_SECONDS, VramLeases

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# The lease-registry consumer id self.llamolotl registers under — the same string
# gpu_queue.LLAMOLOTL_AUDIENCE / main._LLAMOLOTL_AUDIENCE use. This is the consumer
# whose loaded model the eval-coexist route reads (T-016). Defined locally rather
# than imported to avoid pulling gpu_queue's heavy import surface into the request
# path; it MUST stay in sync with that registration id.
LLAMOLOTL_CONSUMER_ID = "self.llamolotl"

# Job-window types that dedicate the WHOLE 4090 to a background job while active,
# so local inference is hard-locked-out for the window (Decision 6 / R3 T-007).
# The GPU job-window scheduler already gates these to admin-specified time windows,
# so the active window IS the lockout signal — no per-job lease acquire needed.
# training + curator are VRAM-exclusive against inference (curator per G-CUR: it
# loads its own curation models, can't share llama-server's); eval windows are
# deliberately NOT here — eval coexists (the substitute branch).
# `publish` joins these on a measurement, not a guess (self.ai#136): a publish
# merge runs through llamolotl's pipeline, which loads the base at fp16 with
# device_map="auto" (self.llamolotl api/merge_lora.py:51-52), so it holds
# roughly the fp16 footprint of the base on the card. A resident chat model
# routinely holds most of the rest of the 4090, so sharing would bet that the
# sum fits on every publish, with an OOM inside llamolotl as the losing
# outcome. Inference is locked out for the length of a merge — the same trade
# curation already makes, for the same reason.
GPU_EXCLUSIVE_WINDOW_JOB_TYPES = frozenset({"training", "curator", "publish"})


def _active_exclusive_window_type() -> Optional[str]:
    """The job-type of the active GPU window if it dedicates the whole card
    (training / curator), else None. Reads the job-window table synchronously.
    May RAISE on a read error — the caller fails CLOSED on that (T-019: the
    training/curator lockout is safety-critical, so an unknown window state must
    not silently admit a request that could collide with a training run; a
    job-window read failing is in any case an app-wide yard-pg fault, so chat is
    already degraded)."""
    from selfai_ui.models.job_windows import JobWindows

    window = JobWindows.get_active_window()
    if window is None:
        return None
    for slot in getattr(window, "slots", None) or []:
        if slot.job_type in GPU_EXCLUSIVE_WINDOW_JOB_TYPES:
            return slot.job_type
    return None


class AdmissionAction(str, enum.Enum):
    PROCEED = "proceed"  # dispatch the request as-is
    REFUSE = "refuse"  # a training/pipeline window holds the card -> 503 (T-015)
    SUBSTITUTE = "substitute"  # eval window -> serve the loaded model instead (T-016)


class LeaseAdmission(BaseModel):
    """The checkpoint's decision for one local-generation request — a structured,
    machine-readable verdict the router dispatches on (never a bare bool)."""

    action: AdmissionAction = AdmissionAction.PROCEED
    # REFUSE (T-015): the HTTP status + machine-readable detail body, distinct from
    # a llamolotl connectivity error so a caller can tell "GPU policy" from "down".
    refuse_status: int = 503
    refuse_detail: Optional[dict] = None
    # SUBSTITUTE (T-016): the model to serve instead, and the originally-requested
    # id (surfaced to the caller via an X-Selfai-Model-Substituted response header).
    substitute_model: Optional[str] = None
    requested_model: Optional[str] = None

    @classmethod
    def proceed(cls) -> "LeaseAdmission":
        return cls(action=AdmissionAction.PROCEED)


def evaluate_admission(request, model_id: str) -> LeaseAdmission:
    """Decide whether/how to admit a local-generation request for ``model_id``.

    Reads the VRAM lease registry synchronously (no network in the request path):

      * **T-015 — training/pipeline hard lockout.** An active EXCLUSIVE lease
        (``active_exclusive_holder()``) means a training/pipeline/curator window
        has seized the whole card → REFUSE with a structured 503 whose ``detail``
        names the holder, distinct from a llamolotl connectivity error so a caller
        can tell "GPU policy" from "down". (T-019 additionally fails CLOSED on a
        *stale* exclusive holder — an unreachable window we can't confirm ended.)
      * **T-016 — eval-window coexistence.** With no exclusive lease, if an eval
        job is running (``EvalJobs.get_jobs_by_status("running")``) AND
        self.llamolotl has a DIFFERENT model loaded (``loaded_model()``), serve the
        loaded model instead of forcing a competing load (the 2026-07-12 thrash).
        The loaded model is the registry's authority (relayed by the R6 poller),
        NOT the eval job's ``model_id``. Fails OPEN (T-019): a missing/stale
        loaded-model read yields no substitution, just normal passthrough.

    Otherwise → PROCEED (today's behavior)."""
    reg = VramLeases

    # --- T-007: training/curator WINDOW hard lockout. The job-window scheduler
    # already gates these VRAM-exclusive job types to admin-specified time windows;
    # while such a window is active the whole card is dedicated to it, so local
    # inference is refused outright (#35's "hard lockout"). Window-driven, not
    # per-job: the active window IS the signal — no training job acquires a lease.
    # T-019: this path FAILS CLOSED — an unreadable window state refuses rather
    # than risk admitting a request that collides with an active training run.
    try:
        locking_window = _active_exclusive_window_type()
    except Exception as e:
        log.warning(
            "lease-admission: active-window read failed (%r); failing CLOSED "
            "(refusing local generation for %r)",
            e,
            model_id,
        )
        return LeaseAdmission(
            action=AdmissionAction.REFUSE,
            refuse_status=503,
            refuse_detail={
                "detail": "GPU window status unknown — local inference temporarily unavailable",
                "gpu_status_unknown": True,
            },
        )
    if locking_window is not None:
        log.info(
            "lease-admission: REFUSE local generation for %r — %r window active "
            "(GPU dedicated)",
            model_id,
            locking_window,
        )
        return LeaseAdmission(
            action=AdmissionAction.REFUSE,
            refuse_status=503,
            refuse_detail={
                "detail": (
                    f"GPU dedicated to the active {locking_window} window — local "
                    "inference temporarily unavailable"
                ),
                "gpu_locked_by": locking_window,
            },
        )

    # --- T-015: a registry EXCLUSIVE lease held -> refuse. Kept as the general
    # broker-level mechanism (a consumer that acquires an exclusive lease via the
    # broker is respected here too); the training/curator lockout above is the
    # window-driven path the producer actually uses. (Fail-closed-on-stale: T-019.)
    holder = reg.active_exclusive_holder()
    if holder is not None:
        log.info(
            "lease-admission: REFUSE local generation for %r — GPU held by an "
            "exclusive lease (%r)",
            model_id,
            holder.consumer_id,
        )
        return LeaseAdmission(
            action=AdmissionAction.REFUSE,
            refuse_status=503,
            refuse_detail={
                "detail": (
                    f"GPU held for {holder.consumer_id} — local inference "
                    "temporarily unavailable"
                ),
                "gpu_locked_by": holder.consumer_id,
            },
        )

    # --- T-016: eval window active + a different model loaded -> substitute
    try:
        eval_running = bool(EvalJobs.get_jobs_by_status("running"))
    except Exception as e:
        # A registry/DB hiccup reading eval state must never block a chat request:
        # eval-coexist is an optimization, so fail OPEN (normal passthrough).
        log.warning("lease-admission: eval-window check failed (%r); passthrough", e)
        eval_running = False
    if eval_running:
        loaded_model, reported_at = reg.loaded_model(LLAMOLOTL_CONSUMER_ID)
        # T-019: eval-coexist FAILS OPEN. Substitute only on a FRESH loaded-model
        # read — a missing (None, never polled) or STALE report (older than the
        # lease staleness bound) yields no substitution, just normal passthrough.
        # Eval-coexist is an optimization, not a safety guard, so an unknown loaded
        # model must never block or mis-route; worst case is today's behavior (a
        # possibly-redundant load), never the training-collision T-007 guards.
        fresh = (
            reported_at is not None
            and (int(time.time()) - reported_at) <= STALE_THRESHOLD_SECONDS
        )
        if loaded_model and fresh and loaded_model != model_id:
            log.info(
                "lease-admission: SUBSTITUTE %r -> already-loaded eval model %r "
                "(eval window active)",
                model_id,
                loaded_model,
            )
            return LeaseAdmission(
                action=AdmissionAction.SUBSTITUTE,
                substitute_model=loaded_model,
                requested_model=model_id,
            )

    return LeaseAdmission.proceed()
