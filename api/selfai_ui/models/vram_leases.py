"""VRAM lease registry (R1) — per-consumer VRAM bookkeeping for the GPU lease
broker.

This is a new data model living **alongside** ``JobWindows``/``JobWindowSlot``
(``models/job_windows.py``), not a modification to it: the time-window system
schedules GPU access by job *type* on a poll loop with a pure integer
``max_concurrent`` count and no notion of memory, while this registry tracks
real-time VRAM *allocation* per registered consumer. It is the ground truth
R2 (release-request) and R3 (grant) read and write.

Trust invariant (R1-AC4)
------------------------
A consumer's ``held_bytes`` is ONLY ever written from a source core can trust:

  1. ``register()``  — the consumer's self-report at (re-)registration
  2. ``heartbeat()`` — the consumer's periodic self-report
  3. ``record_confirmed_release()`` — a consumer-confirmed release (R2, lands
     with T-008)
  4. ``record_grant()`` — a core-authoritative R3 lease grant (T-009): core is
     the authority that just granted the lease, so it knows with certainty the
     requester now holds that much more. This is NOT a probe or a guess (same
     spirit as ``reconcile`` being an authoritative operator action, not an
     inference) — it is core writing down an allocation it just made.

No method in this registry probes or guesses a consumer's VRAM use. This is
the deliberate replacement for ``gpu_queue.py``'s
``_ensure_llamolotl_model_ready`` (``gpu_queue.py:138-189``), which probed the
llamolotl router's model-listing endpoint and guessed what to unload. Here we
trust the self-report instead of probing.

``reconcile()`` is the single exception, and it is not a probe: it is an
explicit operator action that zeroes a confirmed-dead consumer's leftover
lease (R1-AC7). Nothing else — no elapsed time, no staleness — ever frees a
held amount automatically.

Staleness (R1-AC5/AC6/AC7)
--------------------------
Staleness is derived lazily from ``last_reported_at`` against
``STALE_THRESHOLD_SECONDS`` (config-driven). A stale consumer keeps its held
value intact and a distinguishable ``stale`` effective-state — it is never
coerced to "0 held" or dropped — and is excluded from ``eligible_holders()``
(core cannot trust a release confirmation from an unreachable consumer).
"""

import enum
import logging
import os
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Integer, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# Lease-state enum + config
####################


class LeaseState(str, enum.Enum):
    STEADY = "steady"
    RELEASE_REQUESTED = "release-requested"
    RELEASING = "releasing"
    STALE = "stale"


LEASE_STATE_STEADY = LeaseState.STEADY.value
LEASE_STATE_RELEASE_REQUESTED = LeaseState.RELEASE_REQUESTED.value
LEASE_STATE_RELEASING = LeaseState.RELEASING.value
LEASE_STATE_STALE = LeaseState.STALE.value


class LeaseMode(str, enum.Enum):
    """How a consumer's lease shares the card (Decision 6 / R1).

    ``SHARED`` is the ordinary VRAM-lease posture — the consumer holds some
    ``held_bytes`` and coexists with other shared holders under the broker's
    normal grant/reclaim logic. ``EXCLUSIVE`` is a training/pipeline (or, this
    phase, curator) window that has seized the whole card: while an exclusive
    lease is active, the broker denies every other consumer's grant for its
    duration. At most one consumer is ``EXCLUSIVE`` *and* active at any time —
    an invariant the broker enforces (T-002), not the schema."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


LEASE_MODE_SHARED = LeaseMode.SHARED.value
LEASE_MODE_EXCLUSIVE = LeaseMode.EXCLUSIVE.value

# Config-driven staleness bound (R1-AC5/AC6): a consumer whose last self-report
# is older than this is treated as stale. Not a hardcoded constant per the
# kit's spirit — override with VRAM_LEASE_STALE_THRESHOLD_SECONDS.
STALE_THRESHOLD_SECONDS = int(os.environ.get("VRAM_LEASE_STALE_THRESHOLD_SECONDS", 120))


####################
# VramConsumer DB Schema
####################


class VramConsumer(Base):
    __tablename__ = "vram_consumer"

    # Stable identifier, e.g. "self.llamolotl" (matches the LLAMOLOTL_AUDIENCE
    # identity convention). Primary key — registration is an upsert on it.
    consumer_id = Column(Text, unique=True, primary_key=True)
    total_capacity_bytes = Column(BigInteger, nullable=False)
    held_bytes = Column(BigInteger, default=0, nullable=False)
    # R3 reclamation ordering: lower priority is asked to release first.
    priority = Column(Integer, default=0, nullable=False)
    lease_state = Column(Text, default=LEASE_STATE_STEADY, nullable=False)
    # R1/Decision 6: an EXCLUSIVE holder (a training/pipeline/curator window)
    # blocks all other grants for its duration; SHARED is the ordinary posture.
    # Defaults to "shared"; the broker (T-002) enforces the "≤1 active exclusive
    # holder" invariant. server_default backfills pre-existing rows on migrate.
    lease_mode = Column(
        Text, default=LEASE_MODE_SHARED, server_default=LEASE_MODE_SHARED, nullable=False
    )
    # Self-report / confirmed-release timestamp — the basis for staleness.
    last_reported_at = Column(BigInteger)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)
    # R5 force-reap pod identity (opt-in registration config, alongside
    # capacity/priority — NOT a discovery mechanism). Both nullable: a consumer
    # whose k8s_namespace/k8s_pod_selector are BOTH None is simply never
    # force-reap eligible (the AC7 opt-in switch; enforced by the escalation in
    # T-005). k8s_pod_selector is a label selector like "app=self-llamolotl" (or
    # a Deployment name the reaper resolves to pods).
    k8s_namespace = Column(Text)
    k8s_pod_selector = Column(Text)
    # R4/Decision 6: the model this consumer currently has loaded, relayed by the
    # R6 VRAM-state poller (T-012) so core's chat admission checkpoint (T-014+) can
    # route an eval-window request to the already-loaded model instead of forcing a
    # competing load. Both nullable: None before the first successful poll (never
    # fabricated), and loaded_model_reported_at (epoch seconds) lets the checkpoint
    # judge freshness via the same staleness bound the lease uses (T-019).
    loaded_model_id = Column(Text)
    loaded_model_reported_at = Column(BigInteger)


####################
# Pydantic Models
####################


class VramConsumerModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    consumer_id: str
    total_capacity_bytes: int
    held_bytes: int = 0
    priority: int = 0
    lease_state: str = LEASE_STATE_STEADY
    # R1/Decision 6: exclusive-lease posture (an active exclusive holder blocks
    # all other grants; the ≤1 invariant is enforced by the broker).
    lease_mode: str = LEASE_MODE_SHARED
    last_reported_at: Optional[int] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    # R5 force-reap pod identity; both-None = not force-reap eligible (AC7).
    k8s_namespace: Optional[str] = None
    k8s_pod_selector: Optional[str] = None
    # R4/Decision 6: the model this consumer currently has loaded (relayed by the
    # R6 poller) + when it was last reported, for the chat eval-coexist route.
    loaded_model_id: Optional[str] = None
    loaded_model_reported_at: Optional[int] = None


class VramConsumerStatus(VramConsumerModel):
    # Read-time-derived view: effective_state surfaces `stale` distinguishably
    # without the stored lease_state ever being silently overwritten.
    effective_state: str = LEASE_STATE_STEADY
    is_stale: bool = False


class VramConsumerRegisterForm(BaseModel):
    consumer_id: str
    total_capacity_bytes: int
    held_bytes: int = 0
    priority: int = 0
    # R5 force-reap pod identity (opt-in; a re-register refreshes these). Both
    # left None = the consumer is registered but never force-reap eligible.
    k8s_namespace: Optional[str] = None
    k8s_pod_selector: Optional[str] = None


class CapacitySummary(BaseModel):
    total_capacity_bytes: int
    total_held_bytes: int
    free_bytes: int
    consumers: list[VramConsumerStatus] = []


####################
# Staleness helpers
####################


def _is_stale(last_reported_at: Optional[int], now: Optional[int] = None) -> bool:
    """A consumer is stale if it has never reported or its last self-report is
    older than STALE_THRESHOLD_SECONDS."""
    if last_reported_at is None:
        return True
    now = now if now is not None else int(time.time())
    return (now - last_reported_at) > STALE_THRESHOLD_SECONDS


def _effective_state(consumer, now: Optional[int] = None) -> str:
    """Read-time lease state. Surfaces `stale` (distinguishably) when the row is
    past its staleness bound OR was explicitly marked stale, without mutating
    the stored lease_state or the held value. Accepts an ORM row or a
    VramConsumerModel (attribute access only)."""
    if consumer.lease_state == LEASE_STATE_STALE or _is_stale(consumer.last_reported_at, now):
        return LEASE_STATE_STALE
    return consumer.lease_state


####################
# Table CRUD Class
####################


class VramLeasesTable:
    # ---- R1-AC1/AC2: registration (idempotent upsert) ----

    def register(self, form: VramConsumerRegisterForm) -> Optional[VramConsumerModel]:
        """Register (or re-register) a consumer. Idempotent: keyed on
        consumer_id, a re-register updates the existing record in place and
        never inserts a duplicate. The reported held_bytes is a trusted
        self-report (AC4-legal held writer)."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=form.consumer_id).first()
            if row:
                row.total_capacity_bytes = form.total_capacity_bytes
                row.held_bytes = form.held_bytes
                row.priority = form.priority
                # R5 opt-in pod identity: a re-register refreshes it (both None
                # leaves the consumer force-reap ineligible).
                row.k8s_namespace = form.k8s_namespace
                row.k8s_pod_selector = form.k8s_pod_selector
                row.last_reported_at = now
                row.updated_at = now
                # A fresh registration is proof of life -> clear any stale flag.
                if row.lease_state == LEASE_STATE_STALE:
                    row.lease_state = LEASE_STATE_STEADY
            else:
                row = VramConsumer(
                    consumer_id=form.consumer_id,
                    total_capacity_bytes=form.total_capacity_bytes,
                    held_bytes=form.held_bytes,
                    priority=form.priority,
                    k8s_namespace=form.k8s_namespace,
                    k8s_pod_selector=form.k8s_pod_selector,
                    lease_state=LEASE_STATE_STEADY,
                    last_reported_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            try:
                db.commit()
                db.refresh(row)
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    def get(self, consumer_id: str) -> Optional[VramConsumerModel]:
        with get_db() as db:
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            return VramConsumerModel.model_validate(row) if row else None

    def get_all(self) -> list[VramConsumerModel]:
        with get_db() as db:
            rows = db.query(VramConsumer).order_by(VramConsumer.consumer_id.asc()).all()
            return [VramConsumerModel.model_validate(r) for r in rows]

    # ---- R1-AC5/AC6: heartbeat + staleness ----

    def heartbeat(self, consumer_id: str, held_bytes: int) -> Optional[VramConsumerModel]:
        """Consumer self-report reconfirming its held amount. Updates
        held_bytes + last_reported_at and clears any stale marking (a
        self-report is proof of life). AC4-legal held writer."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.held_bytes = held_bytes
            row.last_reported_at = now
            row.updated_at = now
            if row.lease_state == LEASE_STATE_STALE:
                row.lease_state = LEASE_STATE_STEADY
            try:
                db.commit()
                db.refresh(row)
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    # ---- R2-AC2/AC3: broker-driven held writer + lease-state transitions ----

    def record_confirmed_release(
        self, consumer_id: str, new_held_bytes: int
    ) -> Optional[VramConsumerModel]:
        """Record a consumer-CONFIRMED release (R2-AC2). The third and final
        AC4-legal held writer (besides register/heartbeat): writes whatever the
        consumer now reports it holds — which may be more OR less than the amount
        asked, since how it frees (whole-model evict, partial offload) is the
        consumer's choice. A confirmation is also proof of life, so it refreshes
        last_reported_at, clears any stale marking, and returns to `steady`.

        Never infers or probes — new_held_bytes comes from the consumer's own
        confirmed response, resolved by the broker (utils/vram_broker.py)."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.held_bytes = new_held_bytes
            row.last_reported_at = now
            row.updated_at = now
            row.lease_state = LEASE_STATE_STEADY
            try:
                db.commit()
                db.refresh(row)
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    def record_grant(
        self, consumer_id: str, amount_bytes: int, priority: Optional[int] = None
    ) -> Optional[VramConsumerModel]:
        """Record a core-authoritative R3 lease grant on the requester (R3-AC6):
        the requester now holds ``amount_bytes`` MORE VRAM. The fourth (and
        final) AC4-legal held writer alongside register / heartbeat /
        record_confirmed_release — and, like them, NOT a probe or a guess: core
        is the authority that just granted the lease, so it knows with certainty
        the requester holds that much more. (reconcile, the operator zero-out,
        is the only other held writer.)

        Increments held_bytes by amount_bytes (a grant adds to any existing
        hold), refreshes last_reported_at (the requester is actively talking to
        core, which is proof of life), keeps the lease `steady`, and — when
        ``priority`` is given — records the request's reclamation priority on the
        row so a LATER grant asks this newly-minted holder to release in the
        correct ascending-priority order. Returns None if the requester is not
        registered (grants are only tracked for registered consumers)."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.held_bytes = (row.held_bytes or 0) + amount_bytes
            if priority is not None:
                row.priority = priority
            row.last_reported_at = now
            row.updated_at = now
            row.lease_state = LEASE_STATE_STEADY
            try:
                db.commit()
                db.refresh(row)
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    def set_lease_state(self, consumer_id: str, lease_state: str) -> Optional[VramConsumerModel]:
        """Pure lease-state transition (steady / release-requested / releasing).
        Does NOT touch held_bytes or last_reported_at — the broker uses this to
        mark a release request outstanding (R2-AC4) and to return the consumer to
        `steady` after an explicit denial (held left known/unchanged). Use
        ``mark_stale`` for the timeout path, not this."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.lease_state = lease_state
            row.updated_at = now
            try:
                db.commit()
                db.refresh(row)
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    def mark_stale(self, consumer_id: str) -> Optional[VramConsumerModel]:
        """Flag a consumer stale because a release request TIMED OUT (R2-AC3):
        its true state is now unknown, feeding R1's stale handling. Sets the
        stored ``stale`` lease_state (surfaced distinguishably by
        ``effective_state`` and excluded from ``eligible_holders``) but NEVER
        writes a new held value and does NOT refresh last_reported_at — a timeout
        is not proof of anything, least of all a new held amount or life. A
        timeout is never treated as a successful release."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.lease_state = LEASE_STATE_STALE
            row.updated_at = now
            try:
                db.commit()
                db.refresh(row)
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    def is_stale(self, consumer: VramConsumerModel) -> bool:
        """True if the consumer is past its staleness bound. Read-only."""
        return _is_stale(consumer.last_reported_at)

    def effective_state(self, consumer: VramConsumerModel) -> str:
        """Read-time lease state, surfacing `stale` distinguishably. Read-only —
        never mutates stored state or held."""
        return _effective_state(consumer)

    def eligible_holders(self) -> list[VramConsumerModel]:
        """Non-stale consumers holding VRAM, ascending priority (lower first) —
        the pool R2/R3 may ask to release (R1-AC6). Stale consumers are excluded
        because core cannot trust a release confirmation from one; their held is
        NOT touched here (see reconcile)."""
        with get_db() as db:
            now = int(time.time())
            rows = db.query(VramConsumer).filter(VramConsumer.held_bytes > 0).all()
            holders = [
                VramConsumerModel.model_validate(r)
                for r in rows
                if _effective_state(r, now) != LEASE_STATE_STALE
            ]
            holders.sort(key=lambda c: c.priority)
            return holders

    def stale_holders(self) -> list[VramConsumerModel]:
        """STALE consumers holding VRAM, ascending priority (lower first) — the
        exact complement of ``eligible_holders()``, which EXCLUDES stale
        consumers. These are precisely the holders cooperative reclamation
        (R2/R3) will never ask, so R5's force-reap escalation
        (``utils/vram_broker.py``) walks this pool when the normal reclamation
        loop is exhausted and the remaining shortfall can only be covered by a
        stale holder's capacity.

        Read-only: a stale holder's held is NEVER touched here — only a
        CONFIRMED pod deletion clears it, via ``record_reaped_release`` (the
        same evidentiary bar ``reconcile``/confirmed-release use). Mirrors
        ``eligible_holders()`` exactly but with the staleness test inverted."""
        with get_db() as db:
            now = int(time.time())
            rows = db.query(VramConsumer).filter(VramConsumer.held_bytes > 0).all()
            holders = [
                VramConsumerModel.model_validate(r)
                for r in rows
                if _effective_state(r, now) == LEASE_STATE_STALE
            ]
            holders.sort(key=lambda c: c.priority)
            return holders

    # ---- Decision 6 / R1: exclusive-lease posture (T-002) ----

    def active_exclusive_holder(self) -> Optional[VramConsumerModel]:
        """The single consumer currently holding an active EXCLUSIVE lease — a
        training/pipeline/curator window that has seized the whole card (Decision
        6). While one exists the broker denies every other consumer's grant
        (``utils/vram_broker.py``). Returns None when no exclusive lease is active.

        A STALE exclusive holder does NOT count: staleness releases the lock the
        same way it excludes a holder from ``eligible_holders()`` — an unreachable
        window cannot keep the card locked forever; it gets force-reaped/reconciled
        like any other stale holder. If more than one active exclusive holder
        somehow exists (the ≤1 invariant ``set_exclusive`` enforces was violated),
        the lowest-priority one is returned and the anomaly is logged."""
        with get_db() as db:
            now = int(time.time())
            rows = (
                db.query(VramConsumer)
                .filter(VramConsumer.lease_mode == LEASE_MODE_EXCLUSIVE)
                .all()
            )
            active = [
                VramConsumerModel.model_validate(r)
                for r in rows
                if _effective_state(r, now) != LEASE_STATE_STALE
            ]
            if not active:
                return None
            if len(active) > 1:
                log.warning(
                    "vram-lease: INVARIANT VIOLATION — %d active exclusive holders "
                    "(%s); returning lowest-priority. set_exclusive should prevent this.",
                    len(active),
                    [c.consumer_id for c in active],
                )
            active.sort(key=lambda c: c.priority)
            return active[0]

    def set_exclusive(self, consumer_id: str, exclusive: bool) -> bool:
        """Mark (True) or clear (False) a consumer's EXCLUSIVE lease posture
        (Decision 6 / R1), enforcing the ≤1-active-exclusive-holder invariant.

        Setting ``exclusive=True`` when a DIFFERENT consumer already holds an
        active (non-stale) exclusive lease refuses and returns False — the caller
        (T-003 ``acquire_exclusive``) is responsible for clearing the card and
        confirming no other exclusive holder before marking its own. Re-marking the
        current holder is idempotent (True). ``exclusive=False`` clears back to
        ``shared`` (T-004 release). Returns False for an unregistered consumer.
        Does NOT touch ``held_bytes`` — the exclusive lease is a policy flag,
        independent of the physical VRAM figure the poller/heartbeat tracks."""
        with get_db() as db:
            row = (
                db.query(VramConsumer)
                .filter(VramConsumer.consumer_id == consumer_id)
                .first()
            )
            if row is None:
                return False
            if exclusive:
                now = int(time.time())
                others = (
                    db.query(VramConsumer)
                    .filter(
                        VramConsumer.lease_mode == LEASE_MODE_EXCLUSIVE,
                        VramConsumer.consumer_id != consumer_id,
                    )
                    .all()
                )
                if any(_effective_state(r, now) != LEASE_STATE_STALE for r in others):
                    log.warning(
                        "vram-lease: refusing exclusive lease for %r — another "
                        "consumer already holds an active exclusive lease (≤1 invariant)",
                        consumer_id,
                    )
                    return False
                row.lease_mode = LEASE_MODE_EXCLUSIVE
            else:
                row.lease_mode = LEASE_MODE_SHARED
            row.updated_at = int(time.time())
            db.commit()
            return True

    # ---- Decision 6 / R4: loaded-model read accessor (T-013) ----

    def loaded_model(self, consumer_id: str) -> tuple[Optional[str], Optional[int]]:
        """The model a consumer currently has loaded, and when it was last
        reported (epoch seconds) — a synchronous registry read (no network) for
        core's chat admission checkpoint (Decision 6 / R4, feeds T-014+). The R6
        VRAM-state poller (T-012) relays these. Returns ``(None, None)`` for an
        unregistered consumer or before the first successful poll — never a
        fabricated model id, so the checkpoint can distinguish "not reported yet"
        (fail-open, T-019) from "reported, a specific model is loaded". The caller
        judges freshness from the timestamp against the same staleness bound the
        lease uses."""
        with get_db() as db:
            row = (
                db.query(VramConsumer)
                .filter(VramConsumer.consumer_id == consumer_id)
                .first()
            )
            if row is None:
                return (None, None)
            return (row.loaded_model_id, row.loaded_model_reported_at)

    def record_loaded_model(self, consumer_id: str, model_id: Optional[str]) -> None:
        """Relay a consumer's currently-loaded model into the registry (Decision 6
        / R4, called by the R6 poller, T-012), stamping a fresh
        ``loaded_model_reported_at`` (epoch seconds). A no-op for an unregistered
        consumer. Like ``heartbeat``, this is a self-report writer — the poller
        only ever calls it with a value the consumer actually reported (never
        fabricated); a failed/absent read is simply not relayed (the poller skips
        it), leaving the prior value and its age intact for the checkpoint's
        staleness judgement (T-019)."""
        with get_db() as db:
            row = (
                db.query(VramConsumer)
                .filter(VramConsumer.consumer_id == consumer_id)
                .first()
            )
            if row is None:
                return
            row.loaded_model_id = model_id
            row.loaded_model_reported_at = int(time.time())
            db.commit()

    # ---- R1-AC7: explicit operator reconciliation (the ONLY auto-free path) ----

    def reconcile(self, consumer_id: str) -> Optional[VramConsumerModel]:
        """Explicit operator action clearing a confirmed-dead consumer's
        leftover lease (R1-AC7). This is the ONLY path that frees a stale
        consumer's held — the registry never auto-frees it, no matter how long
        it has been stale. Not a probe: the operator, not core, asserts the
        consumer is dead."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.held_bytes = 0
            row.lease_state = LEASE_STATE_STEADY
            row.updated_at = now
            try:
                db.commit()
                db.refresh(row)
                log.info(f"vram-lease: operator reconciled {consumer_id!r} -> held cleared")
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    def record_reaped_release(self, consumer_id: str) -> Optional[VramConsumerModel]:
        """Clear a stale consumer's leftover held after a CONFIRMED force-reap
        (R5-AC5). Mechanically identical to ``reconcile()`` — ``held_bytes`` -> 0,
        ``lease_state`` -> steady, ``updated_at`` refreshed — but a DISTINCT
        method with a distinct ``vram-reap:`` log line, so the automatic R5
        escalation clear is distinguishable in the logs from the operator
        ``reconcile`` clear (AC8: killing a pod is a materially different action
        from a negotiated release or an operator reconcile).

        This does NOT loosen the trust invariant: staleness alone never triggers
        it. The broker calls this ONLY on the evidence of a T-004 *confirmed*
        pod deletion (``ReapStatus.CONFIRMED``) — the same evidentiary bar R2's
        confirmed-release path uses. A confirmed kill is the "explicit action"
        R1-AC7 requires before a stale hold is cleared; the trigger is
        automatic, the evidence requirement is not relaxed."""
        with get_db() as db:
            now = int(time.time())
            row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
            if not row:
                return None
            row.held_bytes = 0
            row.lease_state = LEASE_STATE_STEADY
            row.updated_at = now
            try:
                db.commit()
                db.refresh(row)
                log.info(
                    f"vram-reap: force-reaped {consumer_id!r} -> held cleared "
                    "(confirmed pod deletion)"
                )
                return VramConsumerModel.model_validate(row)
            except Exception as e:
                log.exception(e)
                return None

    # ---- R1-AC3: capacity queries (read-only, never write held) ----

    def total_held(self) -> int:
        """Sum of held_bytes across all consumers (stale ones included — a stale
        consumer's held is never auto-freed, so it still counts as held)."""
        with get_db() as db:
            rows = db.query(VramConsumer).all()
            return sum(r.held_bytes or 0 for r in rows)

    def total_capacity(self) -> int:
        """Known total addressable VRAM of the shared pool.

        Named modeling choice: on the yard's single shared 4090
        (gpu-single-factory-scheduling) every consumer reports the *same* card
        as its total addressable capacity, so the pool ceiling is the max of the
        reported totals — not their sum, which would double-count the one
        physical card. A genuine multi-card pool would revisit this.
        """
        with get_db() as db:
            rows = db.query(VramConsumer).all()
            return max((r.total_capacity_bytes or 0 for r in rows), default=0)

    def free_capacity(self) -> int:
        """Free = known total capacity − total held. The number R3's grant
        decision reads. Never negative."""
        return max(0, self.total_capacity() - self.total_held())

    def capacity_summary(self) -> CapacitySummary:
        """Total capacity, total held, free, plus a per-consumer breakdown with
        each consumer's read-time effective_state / is_stale."""
        with get_db() as db:
            now = int(time.time())
            rows = db.query(VramConsumer).order_by(VramConsumer.consumer_id.asc()).all()
            total_cap = max((r.total_capacity_bytes or 0 for r in rows), default=0)
            held = sum(r.held_bytes or 0 for r in rows)
            consumers = []
            for r in rows:
                base = VramConsumerModel.model_validate(r).model_dump()
                consumers.append(
                    VramConsumerStatus(
                        **base,
                        effective_state=_effective_state(r, now),
                        is_stale=_is_stale(r.last_reported_at, now),
                    )
                )
            return CapacitySummary(
                total_capacity_bytes=total_cap,
                total_held_bytes=held,
                free_bytes=max(0, total_cap - held),
                consumers=consumers,
            )


VramLeases = VramLeasesTable()
