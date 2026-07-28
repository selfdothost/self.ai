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

import logging

from fastapi import APIRouter, Depends, HTTPException
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
from selfai_ui.utils.vram_broker import LeaseDenied, LeaseGranted, VramBroker

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
    consumer updates its record in place — never inserts a duplicate."""
    consumer = VramLeases.register(form_data)
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
