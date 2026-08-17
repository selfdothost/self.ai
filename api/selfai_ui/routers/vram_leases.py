"""GPU VRAM lease broker HTTP surface (cavekit-gpu-lease-broker, T-013).

Follows the shape of ``routers/windows.py`` (module-level ``APIRouter``, thin
handlers delegating to a singleton registry/broker, ``Depends``-injected auth).
It exposes the R1 registry and the R3 grant protocol over HTTP with two distinct
auth stories, deliberately kept apart:

  * **consumer-facing** (register / heartbeat / request-lease): a *service*
    calls these — e.g. self.llamolotl reporting its held VRAM or asking for an
    allocation. They are gated by an inbound service ticket
    (``require_service_ticket``, the symmetric inverse of the mint side the rest
    of the mesh already uses), NOT by an admin user — a peer service is not a
    human admin.
  * **operator-facing** (list / capacity / reconcile): an admin inspects broker
    state or performs the explicit reconciliation of a confirmed-dead consumer.
    Gated by ``get_admin_user``, exactly like ``routers/windows.py``.

``POST /{consumer_id}/reconcile`` is the single, admin-only realization of
R1-AC7: a stale consumer's leftover lease is *only ever* cleared by this
explicit operator action. Nothing else — no elapsed time, no heartbeat gap, no
grant path — frees a stale hold automatically (see ``models/vram_leases.py``).
"""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.vram_leases import (
    CapacitySummary,
    VramConsumerModel,
    VramConsumerRegisterForm,
    VramConsumerStatus,
    VramLeases,
)
from selfai_ui.utils.auth import get_admin_user
from selfai_ui.utils.service_auth import require_service_ticket
from selfai_ui.utils.vram_broker import (
    LeaseDenied,
    LeaseGranted,
    ReleaseOutcome,
    VramBroker,
)
from selfai_ui.utils.vram_consumer_config import apply_trusted_policy

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

router = APIRouter()


####################
# Consumer-facing request shapes (service-ticket gated)
####################


class HeartbeatForm(BaseModel):
    """A consumer's periodic self-report of how much VRAM it currently holds."""

    consumer_id: str
    held_bytes: int


class LeaseRequestForm(BaseModel):
    """An R3 lease request: who wants VRAM and how much.

    ``priority`` is ADVISORY only — the authoritative reclamation priority is the
    consumer's REGISTERED value (config-assigned), which the handler substitutes
    for whatever is sent here (#4 trust boundary; see ``request_lease``). Kept on
    the schema for backwards compatibility; a caller cannot elevate its own
    reclamation power by sending a higher value."""

    consumer_id: str
    amount_bytes: int
    priority: int = 0


####################
# Consumer-facing endpoints — inbound service ticket, NOT admin
####################


@router.post("/register", response_model=VramConsumerModel)
async def register_consumer(
    form_data: VramConsumerRegisterForm,
    _svc=Depends(require_service_ticket("vram:report")),
):
    """Idempotent consumer registration (R1-AC1). Re-registering a known
    consumer updates its record in place — never inserts a duplicate.

    Trust boundary (#79): a service ticket proves the caller holds the mesh's ONE
    shared secret, not WHICH service it is, so anything policy-shaped in the body
    is not the caller's to assert. ``priority`` (who may reclaim from whom),
    ``total_capacity_bytes`` (which ``max()``es into the pool ceiling and so can
    invent VRAM) and the ``k8s_*`` pod identity are all replaced with core's own
    configuration; only ``held_bytes`` is honoured, because that genuinely is a
    self-report. An unconfigured ``consumer_id`` is refused outright rather than
    inserting a row — an arbitrary new row is exactly how the capacity ceiling
    gets inflated.

    Core's own startup registration calls ``VramLeases.register`` directly and is
    unaffected; this constrains the untrusted HTTP path only."""
    trusted = apply_trusted_policy(form_data)
    if trusted is None:
        log.warning(
            "vram-lease: refusing registration for unconfigured consumer %r",
            form_data.consumer_id,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"consumer {form_data.consumer_id!r} is not configured on this "
                "deployment; core registers the consumers it is configured for"
            ),
        )
    consumer = VramLeases.register(trusted)
    if not consumer:
        raise HTTPException(status_code=500, detail="Failed to register consumer")
    return consumer


@router.post("/heartbeat", response_model=VramConsumerModel)
async def heartbeat(
    form_data: HeartbeatForm,
    _svc=Depends(require_service_ticket("vram:report")),
):
    """Consumer self-report of held VRAM (R1-AC6). A self-report is proof of
    life, so it also clears any stale marking. 404 if the consumer never
    registered — core will not track held for an unknown consumer."""
    consumer = VramLeases.heartbeat(form_data.consumer_id, form_data.held_bytes)
    if not consumer:
        raise HTTPException(
            status_code=404,
            detail=f"consumer {form_data.consumer_id!r} is not registered",
        )
    return consumer


@router.post("/request-lease", response_model=LeaseGranted)
async def request_lease(
    form_data: LeaseRequestForm,
    _svc=Depends(require_service_ticket("vram:lease")),
):
    """Request a VRAM allocation (R3). Grants from free capacity when possible,
    otherwise reclaims from lower-priority holders; returns the granted lease on
    success.

    A denial is not an error to hide: when the broker cannot satisfy the request
    it returns a structured :class:`LeaseDenied` (requested / free / freeable +
    per-holder breakdown), which we surface as a **409 Conflict** carrying that
    structured body (the request conflicts with the current allocation state —
    the same disposition ``routers/windows.py`` uses for a can't-act-given-
    current-state case). A malformed precondition (requester not registered) is
    a 400 instead.

    Trust boundary (#4): the requester's reclamation ``priority`` is authoritative
    from its REGISTRATION (config-assigned via the ``*_VRAM_LEASE_PRIORITY`` env),
    NOT self-declared in the request body. Trusting ``form_data.priority`` would
    let a low-priority consumer name itself high-priority and reclaim VRAM from a
    holder it must never touch — the priority-inversion the broker's ``priority <
    requester`` gate (utils/vram_broker.py) exists to prevent. So we look up the
    registered priority and pass THAT; ``form_data.priority`` is advisory only and
    deliberately ignored. (Binding ``consumer_id`` to the *caller's* identity would
    need per-consumer keys the shared-secret service mesh does not have — out of
    scope here; an unregistered ``consumer_id`` still fails the broker's own
    not-registered check -> 400.)"""
    registered = VramLeases.get(form_data.consumer_id)
    effective_priority = (
        registered.priority if registered is not None else form_data.priority
    )
    try:
        result = await VramBroker.request_lease(
            form_data.consumer_id, form_data.amount_bytes, effective_priority
        )
    except ValueError as e:
        # Protocol precondition failure (e.g. requester not registered) — a
        # client error, distinct from a capacity denial. Fail loudly, never a
        # silent untracked grant.
        raise HTTPException(status_code=400, detail=str(e))

    if isinstance(result, LeaseDenied):
        # Structured, machine-readable denial (R3-AC5/AC7) — never a bare 5xx.
        raise HTTPException(status_code=409, detail=result.model_dump())
    return result


####################
# Operator-facing endpoints — admin, exactly like routers/windows.py
####################


@router.get("", response_model=list[VramConsumerStatus])
async def list_consumers(user=Depends(get_admin_user)):
    """List every registered consumer with its read-time ``effective_state``
    (``stale`` surfaced distinguishably, never coerced to held=0 or dropped)."""
    return VramLeases.capacity_summary().consumers


@router.get("/capacity", response_model=CapacitySummary)
async def capacity(user=Depends(get_admin_user)):
    """Total / held / free summary plus the per-consumer breakdown — the number
    R3's grant decision reads, exposed for operator observability."""
    return VramLeases.capacity_summary()


@router.post("/{consumer_id}/reconcile", response_model=VramConsumerModel)
async def reconcile_consumer(consumer_id: str, user=Depends(get_admin_user)):
    """Explicit operator reconciliation of a confirmed-dead consumer's leftover
    lease (R1-AC7). This is the ONLY path that clears a stale hold — the
    registry never auto-frees one, no matter how long it has been stale. Admin
    only: it is an operator assertion that the consumer is dead, not an
    inference core is allowed to make on its own."""
    consumer = VramLeases.reconcile(consumer_id)
    if not consumer:
        raise HTTPException(
            status_code=404, detail=f"consumer {consumer_id!r} is not registered"
        )
    return consumer


####################
# System-wide GPU e-stop — admin, forceful, bypasses cooperative negotiation
####################

# The llamolotl consumer id (matches main._LLAMOLOTL_AUDIENCE /
# vram_llamolotl.LLAMOLOTL_AUDIENCE). Its e-stop leg is "unload all its models",
# NOT a transport release, so it is handled specially below.
_LLAMOLOTL_CONSUMER_ID = "self.llamolotl"

# self.curator IS in scope for the e-stop as of self.ai#88. It was excluded when
# this endpoint was written because it was not a registered VRAM lease consumer
# at all; it is now, holding up to 24GiB of the shared 4090 for a curation run,
# which makes it precisely the kind of holder an operator presses this button to
# clear. It needs no special case here: its transport is an ordinary
# ``ReleaseTransport`` (``vram_curator.CuratorReleaseTransport``) reached through
# the same ``force=True`` fan-out as self.speak and self.sketch, and on the
# curator side ``force`` terminates the pipeline process group.
_CURATOR_CONSUMER_ID = "self.curator"

# A short, blunt per-consumer timeout for the forceful release. force=True skips
# the drain-wait on the consumer side, so this only bounds the HTTP round-trip.
_ESTOP_RELEASE_TIMEOUT_SECONDS = 5.0

# Sentinel "free everything" target when a consumer's currently-held bytes are
# unknown. With force=True the target is advisory (speak/sketch unload fully
# regardless), so a large value simply means "give back all you can".
_ESTOP_RELEASE_SENTINEL_BYTES = 1 << 60


class ReleaseAllResult(BaseModel):
    """One consumer's outcome in a system-wide e-stop. ``ok`` is the blunt
    verdict; ``detail`` carries the transport ``status`` + reported new-held (for
    speak/sketch), the llamolotl unload summary, or an ``error`` string — never
    hidden, one consumer's failure never masks another's."""

    consumer_id: str
    ok: bool
    detail: Optional[dict[str, Any]] = None


class ReleaseAllSummary(BaseModel):
    results: list[ReleaseAllResult] = []
    # Curation runs the e-stop killed, put back in the queue for the next curator
    # window (self.ai#88). Empty on every deployment without curator jobs, so the
    # field costs nothing where it does not apply.
    curator_requeued: list[str] = []


async def _requeue_estopped_curator_jobs() -> list[str]:
    """Put curation runs the e-stop killed back in the queue for the next window.

    Curation work is restartable and often long, so clearing the card should not
    also make an admin rebuild every queued pipeline by hand.

    Done here, synchronously, rather than left to the queue's sync loop: that loop
    only looks at rows whose LOCAL status is ``running``, and it would see the
    remote job as ``cancelled`` and settle ours as cancelled too. Requeueing first
    — which also clears ``curator_job_id`` — takes the row out of that query, so
    there is no race and the next window starts a fresh run from the same stored
    ``pipeline_config``.

    Best-effort by design: a requeue failure is logged and never allowed to fail
    the e-stop. The card being clear is the point; the bookkeeping is not worth
    turning a successful stop into a 500."""
    try:
        from selfai_ui.models.curator_jobs import CuratorJobs

        running = CuratorJobs.get_jobs_by_status("running")
    except Exception as e:
        log.warning("vram-estop: could not read running curator jobs to requeue: %r", e)
        return []

    requeued: list[str] = []
    for job in running:
        try:
            CuratorJobs.requeue_for_next_window(
                job.id,
                "Stopped by the VRAM e-stop (Unload All Models) and requeued for "
                "the next curator window.",
            )
            requeued.append(job.id)
        except Exception as e:
            log.error("vram-estop: failed to requeue curator job %r: %r", job.id, e)
    if requeued:
        log.warning(
            "vram-estop: requeued %d curator job(s): %s", len(requeued), requeued
        )
    return requeued


async def _force_release_consumer(
    transport, consumer_id: str, amount_bytes: int
) -> ReleaseAllResult:
    """Issue ONE forceful release straight at a consumer's transport (force=True),
    deliberately bypassing ``VramBroker.request_lease`` cooperative priority/grant
    logic. Any exception is caught and reported as ``ok=False`` so gather never
    aborts the other consumers."""
    if transport is None:
        return ReleaseAllResult(
            consumer_id=consumer_id,
            ok=False,
            detail={"error": "no release transport installed on this deployment"},
        )
    try:
        resp = await transport.request_release(
            consumer_id,
            amount_bytes,
            _ESTOP_RELEASE_TIMEOUT_SECONDS,
            force=True,
        )
    except Exception as e:  # never let one consumer abort the fan-out
        log.warning("vram-estop: forceful release to %r raised %r", consumer_id, e)
        return ReleaseAllResult(
            consumer_id=consumer_id, ok=False, detail={"error": repr(e)}
        )
    ok = resp.outcome == ReleaseOutcome.CONFIRMED
    detail: dict[str, Any] = {
        "status": resp.outcome.value,
        "new_held_bytes": resp.new_held_bytes,
    }
    # The consumer's own words, when it gave any (self.ai#88) — e.g. self.curator
    # naming the pipeline it just terminated. Omitted rather than sent as null so
    # the admin UI can treat presence as "there is something to show".
    if resp.reason:
        detail["reason"] = resp.reason
    return ReleaseAllResult(consumer_id=consumer_id, ok=ok, detail=detail)


async def _estop_llamolotl(app_state) -> ReleaseAllResult:
    """The llamolotl leg: unload ALL its loaded models (reuses the existing
    unload-all logic). Imported locally to avoid a router-level import cycle."""
    from selfai_ui.routers.llamolotl import unload_all_llamolotl_models

    try:
        summary = await unload_all_llamolotl_models(app_state)
    except Exception as e:
        log.warning("vram-estop: llamolotl unload-all raised %r", e)
        return ReleaseAllResult(
            consumer_id=_LLAMOLOTL_CONSUMER_ID, ok=False, detail={"error": repr(e)}
        )
    # ok iff every attempted unload succeeded (no per-model errors).
    ok = not summary.get("errors")
    return ReleaseAllResult(
        consumer_id=_LLAMOLOTL_CONSUMER_ID, ok=ok, detail=summary
    )


def _estop_target_bytes(consumer: VramConsumerStatus) -> int:
    """The forceful-release target for a consumer: its currently-held bytes
    (measured or reserved, whichever is larger — the same effective-held view the
    ledger uses), or a large sentinel when unknown/zero."""
    held = max(consumer.held_bytes or 0, consumer.reserved_bytes or 0)
    return held if held > 0 else _ESTOP_RELEASE_SENTINEL_BYTES


@router.post("/release-all", response_model=ReleaseAllSummary)
async def release_all(request: Request, user=Depends(get_admin_user)):
    """System-wide GPU e-stop (admin only): FORCE-unload every GPU model — "stop
    now, short of pulling the plug." Deliberately blunt: it does NOT route through
    the broker's cooperative ``request_lease`` priority/lease-grant negotiation.
    Instead it hits every registered non-llamolotl consumer's transport with a
    ``force=True`` release (self.speak fans out internally to Kokoro + Chatterbox;
    self.sketch maps to ComfyUI ``/free``) AND invokes llamolotl's unload-all
    models logic — all IN PARALLEL (``asyncio.gather``). curator is out of scope.

    Every consumer is attempted independently; one failure never aborts the
    others (per-item try + ``return_exceptions``). Returns a per-consumer summary
    ``{"results": [{consumer_id, ok, detail}, ...]}``."""
    transport = VramBroker.get_transport()

    # Enumerate registered consumers from the ledger (the same source the admin
    # list/capacity endpoints use), then split: llamolotl → unload-all; curator →
    # skipped; everything else → forceful transport release.
    consumers = VramLeases.capacity_summary().consumers

    labels: list[str] = []
    tasks: list = []
    for consumer in consumers:
        cid = consumer.consumer_id
        if cid == _LLAMOLOTL_CONSUMER_ID:
            labels.append(cid)
            tasks.append(_estop_llamolotl(request.app.state))
            continue
        labels.append(cid)
        tasks.append(
            _force_release_consumer(transport, cid, _estop_target_bytes(consumer))
        )

    # If llamolotl is configured but somehow never registered as a consumer, still
    # fire its unload-all leg so the e-stop truly covers the LLM.
    if _LLAMOLOTL_CONSUMER_ID not in labels:
        labels.append(_LLAMOLOTL_CONSUMER_ID)
        tasks.append(_estop_llamolotl(request.app.state))

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[ReleaseAllResult] = []
    for label, outcome in zip(labels, gathered):
        if isinstance(outcome, ReleaseAllResult):
            results.append(outcome)
        elif isinstance(outcome, BaseException):
            # Defensive: the helpers catch their own exceptions, but never let a
            # stray one abort the summary.
            log.warning("vram-estop: %r leg raised %r", label, outcome)
            results.append(
                ReleaseAllResult(
                    consumer_id=label, ok=False, detail={"error": repr(outcome)}
                )
            )
    log.info(
        "vram-estop: system-wide GPU e-stop by admin %r — results: %s",
        getattr(user, "id", "?"),
        [(r.consumer_id, r.ok) for r in results],
    )
    # After the fan-out, not before: requeueing first would clear curator_job_id
    # while the pipeline was still being terminated, and a row with no remote job
    # id is one nothing can go back and cancel if the force release fails.
    requeued = await _requeue_estopped_curator_jobs()
    return ReleaseAllSummary(results=results, curator_requeued=requeued)
