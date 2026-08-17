"""VRAM lease broker (R2 release-request / R3 grant).

This module carries the machine-readable result types the broker returns and
the R2 release-request protocol itself (issue + response handling). R3's grant
protocol (T-009/T-010/T-012) builds on top of these.

The structured denial mirrors the sibling repo's structured-503 pattern
(self.llamolotl#27 / R6): a lease that cannot be satisfied is never a bare
failure — it names exactly how much was requested, how much is free/freeable,
and which holders were asked and what each did.

R2 release protocol (T-006/T-008)
---------------------------------
``VramBroker.request_release(consumer_id, amount_bytes, timeout_seconds)`` asks
one registered consumer to free a target amount, then resolves the response
into a :class:`ReleaseOutcome`:

  * ``confirmed`` -> registry ``record_confirmed_release`` with whatever the
    consumer now reports it holds (may differ from the amount asked).
  * ``denied``    -> held left known/unchanged, lease_state back to ``steady``.
  * ``timeout``   -> consumer marked ``stale`` (true state now unknown); no new
    held is ever written and it is NEVER counted as a success.

Two guarantees mirror what the old ``_ensure_llamolotl_model_ready`` lacked
(``gpu_queue.py``): (1) at most one outstanding release request per consumer,
enforced by a per-consumer ``asyncio.Lock`` — a second concurrent call for the
same consumer awaits the first rather than racing an unsynchronised
unload->load window; (2) the timeout is a required per-call bound, so an urgent
R3-driven reclaim can pass a tighter deadline than a routine one.

The actual call-out to a consumer is abstracted behind :class:`ReleaseTransport`
(injected). This packet ships only a fake/injectable interface; the concrete
authenticated self.llamolotl client (service-ticket ``httpx`` POST to its evict
endpoint) is T-016 and is deliberately not implemented here.

R3 grant protocol (T-009/T-010/T-012)
-------------------------------------
``VramBroker.request_lease(consumer_id, amount_bytes, priority)`` grants VRAM to
a registered requester, returning a :class:`LeaseGranted` on success or a
structured :class:`LeaseDenied` (never a bare failure):

  * **free-path** (T-009): if ``free_capacity() >= amount`` the lease is granted
    straight from free capacity with NO release-request issued at all; the
    requester's hold is written to the registry (``record_grant``) BEFORE the
    success is returned (R3-AC6).
  * **reclamation** (T-010): if free is short but free-plus-releasable can cover
    it, eligible holders are asked (R2 ``request_release``) in ascending-priority
    order (lower first) for the running shortfall. **Only CONFIRMED releases
    count toward progress** — the grant completes strictly after real
    confirmations land in the registry, never speculatively against a
    timed-out/denied release (R3-AC4).
  * **denial** (T-012): a request exceeding total free-plus-releasable is denied
    up front regardless of priority — the broker brokers real capacity, it never
    invents VRAM (R3-AC7); a request that looked satisfiable but fell short after
    exhausting holders is denied with a fully-populated ``LeaseDenied`` carrying
    the per-holder ``holders_asked`` breakdown (R3-AC5). No held is ever written
    on the requester on any denial path — a denial never leaves a phantom hold.

  * **force-reap escalation** (R5 / T-005): if cooperative reclamation is
    exhausted and the grant is still short, and a :class:`PodReaper` is
    installed (``set_reaper``), the broker escalates against the ``stale``
    holders whose capacity the shortfall still needs — deleting each one's k8s
    pod and clearing its held via ``record_reaped_release`` ONLY on a
    *confirmed* deletion, then retrying the grant. A holder that explicitly
    ``denied`` a release is ``steady``, not ``stale``, so it is never reaped;
    a reap that fails or a holder with no configured pod identity flows into
    the same structured denial, recorded distinguishably. With no reaper
    installed the escalation is skipped entirely and the flow behaves exactly
    as before R5.

The whole grant decision runs under one broker-wide grant lock so two concurrent
requests can never both pass the free check and double-spend the same VRAM. The
force-reap escalation runs under that same lock (never a nested per-consumer
release lock), so it cannot deadlock and cannot race a concurrent grant.

Where a reap target comes from (self.ai#75/#79)
----------------------------------------------
Force-reap deletes a pod, so WHERE it aims is a security decision, not a
bookkeeping one. The target namespace/selector are read from a TRUSTED map
installed at startup from core's own environment (``set_reap_targets``), NEVER
from the consumer's registry row.

The row still carries ``k8s_namespace``/``k8s_pod_selector`` for observability,
but they are deliberately not authoritative: ``POST /vram-leases/register`` is
gated on a service ticket minted from ONE shared mesh secret with no caller
identity (#79), so any service that can register could otherwise re-register a
consumer with a selector pointing at some OTHER pod in the namespace — the chat
pod, or core itself — and then engineer a grant that deletes it. Namespaced RBAC
bounds the blast radius to the tenant; it does not bound it to the right pod.
Sourcing the target from env removes the redirect entirely.

A consumer with no entry in the trusted map is simply never force-reap eligible
(``ineligible-no-pod-identity``), which is the same R5-AC7 opt-in as before —
only the source of the opt-in moved from a writable table to core's config.
"""

import asyncio
import enum
import logging
import os
import time
from typing import Optional, Protocol, Union, runtime_checkable

from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.vram_leases import (
    LEASE_STATE_RELEASE_REQUESTED,
    LEASE_STATE_RELEASING,
    LEASE_STATE_STEADY,
    VramLeases,
)
from selfai_ui.utils.vram_k8s import PodReaper, ReapStatus

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# Config-driven default timeout (seconds) for the R2 release-requests a grant
# issues during reclamation (R2-AC5: per-call, never a hardcoded constant). An
# urgent grant can pass a tighter bound; routine ones fall back to this.
GRANT_RELEASE_TIMEOUT_SECONDS = float(
    os.environ.get("VRAM_GRANT_RELEASE_TIMEOUT_SECONDS", "30")
)

# Overall wall-clock bound on ONE grant decision (self.ai#80). The per-release
# timeout above bounds each holder individually, which left the total unbounded:
# (eligible holders + stale holders) x 30s, all of it under the broker-wide grant
# lock, serialising every other grant and both exclusive operations. An HTTP
# client that gives up does not cancel the handler, so the lock stayed held
# regardless. 90s is ~3 holders at the default per-release bound — enough for a
# real reclamation, short enough that a wedged consumer cannot stall the broker
# for minutes.
GRANT_TOTAL_TIMEOUT_SECONDS = float(
    os.environ.get("VRAM_GRANT_TOTAL_TIMEOUT_SECONDS", "90")
)


class ReleaseOutcome(str, enum.Enum):
    """Outcome of a single R2 release-request against one consumer.

    A confirmed release updates the registry's held; a denied release leaves
    the consumer's held known/unchanged; a timeout renders the consumer's true
    state unknown (feeding R1's stale handling). Denial and timeout are held
    distinct — neither is ever silently treated as success. Reused by T-008.
    """

    CONFIRMED = "confirmed"
    DENIED = "denied"
    TIMEOUT = "timeout"


# Per-holder outcome as recorded in a LeaseDenied breakdown. Extends the three
# ReleaseOutcome values with `ineligible-stale`: a holder core did not even ask
# because it was stale (excluded from eligible_holders), distinct from a holder
# that was asked and denied or timed out.
HOLDER_OUTCOME_CONFIRMED = ReleaseOutcome.CONFIRMED.value
HOLDER_OUTCOME_DENIED = ReleaseOutcome.DENIED.value
HOLDER_OUTCOME_TIMEOUT = ReleaseOutcome.TIMEOUT.value
HOLDER_OUTCOME_INELIGIBLE_STALE = "ineligible-stale"

# R5 force-reap escalation outcomes — the materially-different, more-consequential
# actions (a pod kill, not a negotiated release) that AC8 requires be recorded
# distinguishably from the cooperative vocabulary above. A stale holder is
# force-reaped (`reaped` = confirmed pod deletion cleared its held), the reap was
# attempted and failed (`reap-failed` = k8s error / RBAC denied / not confirmed
# gone), or it was never even eligible because it has no configured pod identity
# (`ineligible-no-pod-identity`, the AC7 opt-in switch — never passed to the
# reaper). All three surface in the LeaseDenied breakdown alongside the
# cooperative outcomes (AC6).
HOLDER_OUTCOME_REAPED = "reaped"
HOLDER_OUTCOME_REAP_FAILED = "reap-failed"
HOLDER_OUTCOME_INELIGIBLE_NO_POD_IDENTITY = "ineligible-no-pod-identity"

# R5 eligibility (self.ai#105): a registered consumer from which no genuine
# observation has EVER arrived. Distinct from `ineligible-no-pod-identity` (an
# operator chose not to opt it in) and from an ordinary stale holder (it reported
# healthily, then went quiet) — this one is a consumer core has never once heard
# from, which is the signature of a deployment gap rather than a fault.
HOLDER_OUTCOME_INELIGIBLE_NEVER_OBSERVED = "ineligible-never-observed"


def _ever_observed(holder) -> bool:
    """Whether a genuine consumer-originated observation has ever arrived for
    this holder (self.ai#105).

    ``last_reported_at`` cannot answer this: ``register()`` writes it, and
    registration is core asserting a consumer exists from its own configuration,
    not the consumer saying anything. So a consumer that is registered at boot and
    never successfully polled becomes indistinguishable from one that reported
    healthily and then died — both simply age past the staleness bound.

    Force-reap DELETES A POD, so it must not act on that ambiguity. Only
    ``last_observed_at`` (written by heartbeat / confirmed release, never by
    registration or a core-side grant) proves the consumer was ever really there.
    """
    return getattr(holder, "last_observed_at", None) is not None


class HolderAsked(BaseModel):
    """One holder's contribution to a denied grant: who was asked, how much was
    asked of them, how much they actually released, and the outcome."""

    consumer_id: str
    requested_release: int
    actually_released: int = 0
    # confirmed | denied | timeout | ineligible-stale
    outcome: str
    # The holder's own words for WHY, when it gave any (self.ai#88). A holder
    # that can explain its refusal — "curation pipeline in progress, job X,
    # running 400s" — turns an opaque denial into something an operator can act
    # on. Informational only; nothing branches on it.
    reason: Optional[str] = None


class LeaseDenied(BaseModel):
    """Structured lease-denial reason (R3-AC5).

    Identifies the amount requested, the amount actually free and the amount
    freeable (free plus what eligible holders could give back), and the
    per-holder breakdown of who was asked and what each did. A specific,
    machine-readable denial — never a bare failure.
    """

    requested_bytes: int
    free_bytes: int
    freeable_bytes: int
    holders_asked: list[HolderAsked] = []
    # Decision 6 / R1: set to the consumer_id of the active EXCLUSIVE holder when
    # the denial is because a training/pipeline/curator window has seized the whole
    # card (freeable_bytes is 0 and holders_asked is empty in that case — nothing
    # was asked, nothing is grantable). None for an ordinary capacity denial. The
    # chat admission checkpoint (T-015) reads this to build a "GPU held for X" 503.
    exclusive_holder: Optional[str] = None


class LeaseGranted(BaseModel):
    """A successful R3 lease grant (T-009/T-010).

    The requester's hold is ALREADY written to the registry (``record_grant``)
    before this is returned to the caller (R3-AC6). ``held_bytes`` is the
    requester's resulting total hold; ``reclaimed`` lists any holders asked to
    release during reclamation (empty on the free-path — R3-AC2)."""

    consumer_id: str
    granted_bytes: int
    held_bytes: int
    priority: int = 0
    reclaimed: list[HolderAsked] = []


# A grant decision resolves to exactly one of these. Denial is a returned value,
# not an exception (mirroring R2's returned ReleaseOutcome) — the router (T-013)
# maps it onto a structured HTTP response.
LeaseResult = Union[LeaseGranted, LeaseDenied]


class ExclusiveResult(BaseModel):
    """Outcome of an exclusive-lease acquire (Decision 6 / R1, T-003).

    ``acquired`` is the machine-readable verdict; ``reason`` always explains it.
    ``reclaimed`` is the per-holder breakdown of who was asked to release / reaped
    while clearing the card (the same ``HolderAsked`` vocabulary a grant's
    reclamation uses), empty on a no-op. ``blocking_holder`` names another consumer
    whose active exclusive lease refused this acquire. On a failed acquire the card
    was NOT seized and no exclusive flag was set — the "confirmed or nothing" bar
    (R1-AC4): a half-cleared card never becomes an exclusive lease."""

    acquired: bool
    consumer_id: str
    reason: str
    reclaimed: list[HolderAsked] = []
    blocking_holder: Optional[str] = None


####################
# R2 release transport (T-006/T-008) — pluggable, concrete client is T-016
####################


class ReleaseResponse(BaseModel):
    """What a :class:`ReleaseTransport` returns for one release request.

    ``outcome`` is the consumer's answer. ``new_held_bytes`` is REQUIRED when
    ``outcome == ReleaseOutcome.CONFIRMED`` (the consumer's self-reported held
    after freeing, which may be more or less than the amount asked) and ignored
    otherwise. A transport signals a timeout either by returning
    ``outcome=TIMEOUT`` or simply by not returning within ``timeout_seconds``
    (the broker's ``asyncio.wait_for`` converts the latter into the former).

    ``reason`` is the consumer's own explanation, carried through to the
    ``LeaseDenied`` breakdown so an operator reading a refused grant can see WHY
    a holder would not yield — not just that it wouldn't (self.ai#88). Optional
    and purely informational: no decision is made on its contents, and a
    transport that has nothing to say leaves it ``None``."""

    outcome: ReleaseOutcome
    new_held_bytes: Optional[int] = None
    reason: Optional[str] = None


@runtime_checkable
class ReleaseTransport(Protocol):
    """The outbound call the broker makes to ask a consumer to free VRAM.

    Intentionally amount-based and mechanism-free: the request names only a
    target amount, never *how* to free it (whole-model evict, partial offload,
    n-cpu-moe) — that choice is entirely the consumer's, so a future
    self.llamolotl strategy is a same-shape upgrade.

    T-016 implements the concrete self.llamolotl transport: an authenticated
    ``httpx`` POST that mints a service ticket (``mint_service_ticket(
    LLAMOLOTL_AUDIENCE, ...)``, same pattern as ``gpu_queue.py``) to
    llamolotl's release/evict endpoint and maps its reply onto ReleaseResponse.
    Until then this is satisfied only by test fakes — the broker has no default
    transport and refuses to invent a success (see ``request_release``)."""

    async def request_release(
        self,
        consumer_id: str,
        amount_bytes: int,
        timeout_seconds: float,
        force: bool = False,
    ) -> ReleaseResponse:
        ...


class _Budget:
    """Remaining wall-clock for one grant decision (self.ai#80).

    Deliberately NOT an ``asyncio.wait_for`` around the whole decision: cancelling
    mid-``request_release`` would abandon a consumer in the ``releasing`` state
    with its true held unknown, which is exactly the ambiguity R2 works to avoid.
    Instead the budget is *checked between* steps and *shrinks* each step's own
    bound, so an in-flight release always runs to its own conclusion and the loop
    simply stops starting new ones once the budget is spent. Running out is not an
    error — it falls through to the same structured denial as any other
    exhausted reclamation.
    """

    def __init__(self, total_seconds: float):
        self._deadline = time.monotonic() + max(0.0, total_seconds)

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def spent(self) -> bool:
        return self.remaining() <= 0

    def bound(self, per_call_seconds: float) -> float:
        """This step's timeout, never longer than what is left overall."""
        return min(per_call_seconds, self.remaining())


def _granted_effective_held(granted, amount_bytes: int) -> int:
    """The requester's resulting total hold to report back on a grant.

    Since self.ai#76 a grant lands in ``reserved_bytes`` rather than
    ``held_bytes``, so the figure a caller cares about — "how much do I hold
    now" — is the effective one: the larger of what the consumer last measured
    and what core has just reserved for it. ``max`` not a sum, for the same
    reason ``_effective_held`` uses it — the two describe the SAME VRAM at two
    different moments, so adding them would double-count the grant the instant
    the consumer began allocating it.
    """
    if granted is None:
        return amount_bytes
    return max(granted.held_bytes or 0, granted.reserved_bytes or 0)


class VramBrokerImpl:
    """R2 release-request protocol over the VRAM lease registry.

    Holds one ``asyncio.Lock`` per consumer so at most one release request is
    outstanding against a given consumer at a time (R2-AC4). The registry
    (``VramLeases``) and the outbound transport are both injectable so unit
    tests can drive the lock/timeout/state mechanics against a fake consumer
    with no network (T-011)."""

    def __init__(
        self,
        transport: Optional[ReleaseTransport] = None,
        registry=VramLeases,
        reaper: Optional[PodReaper] = None,
    ):
        # Default transport is None on purpose: the concrete self.llamolotl
        # client is T-016. An unconfigured broker must never fabricate a
        # success — request_release raises rather than silently "succeeding".
        self._transport = transport
        self._registry = registry
        # R5 force-reap capability (T-004 KubernetesPodReaper). Injectable,
        # mirroring the transport: defaults to None, and when None the grant
        # flow's stale-holder escalation is SKIPPED ENTIRELY — a broker with no
        # reaper installed behaves exactly as before R5 (every stale holder is
        # simply never asked, the grant falls straight through to denial). So
        # force-reap is opt-in at the broker level too, and an unconfigured
        # broker can never fabricate a reap success or block the grant.
        self._reaper = reaper
        # TRUSTED reap targets: {consumer_id: (namespace, selector)}, installed at
        # startup from core's own env (see set_reap_targets). Empty by default, so
        # a broker whose targets were never installed reaps NOTHING even if a
        # reaper is present — the safe direction (see #75/#79 in the module
        # docstring).
        self._reap_targets: dict[str, tuple[str, str]] = {}
        # Keyed per consumer_id. get-or-create below does no await between the
        # lookup and the insert, so it is atomic on the event loop — no extra
        # guard lock is needed (mirrors gpu_queue.py's single-loop async style).
        self._locks: dict[str, asyncio.Lock] = {}
        # One broker-wide lock guarding the whole R3 grant decision (capacity
        # read -> reclamation -> held write). Fully serialising grants is
        # correct for the single shared 4090: without it two concurrent requests
        # could both pass the free check and double-spend the same VRAM. The
        # nesting is always grant_lock -> per-consumer release lock, never the
        # reverse, so it cannot deadlock against request_release.
        #
        # SINGLE-WRITER ASSUMPTION (#6): this is an in-process ``asyncio.Lock`` —
        # it serializes grants within ONE api process, NOT across replicas/ pods.
        # VRAM grant correctness therefore requires selfai-api to be the SOLE
        # broker: the Deployment runs replicas:1 + strategy:Recreate
        # (manifests/api/10-deployment.yaml), so two broker pods can never run at
        # once and double-grant the card. Bumping replicas or switching to
        # RollingUpdate would silently break this — move the grant decision under
        # a cross-process lock (e.g. the RedisLock gpu_queue.process_gpu_queue_v2
        # already uses) BEFORE going multi-replica.
        self._grant_lock = asyncio.Lock()
        # {consumer_id: reason} from that consumer's most recent release answer
        # (self.ai#88), so the HolderAsked breakdown can carry the holder's own
        # words for why it would not yield. Written in _resolve_response and read
        # immediately after the awaited request_release in the reclamation loops.
        # Both loops run under _grant_lock, so only one is ever in flight per
        # process and the read cannot be crossed by another loop's write.
        self._last_release_reason: dict[str, Optional[str]] = {}

    def last_release_reason(self, consumer_id: str) -> Optional[str]:
        """The reason ``consumer_id`` gave for its most recent release answer, if
        it gave one. Informational — nothing branches on it."""
        return self._last_release_reason.get(consumer_id)

    def set_transport(self, transport: ReleaseTransport) -> None:
        """Install the outbound transport (T-016 wires the llamolotl client)."""
        self._transport = transport

    def get_transport(self) -> Optional[ReleaseTransport]:
        """The installed outbound transport (the consumer-aware dispatcher in
        production), or ``None`` if none is wired. Used by the admin e-stop
        (``routers/vram_leases.py`` ``/release-all``) to issue FORCEFUL releases
        directly to each consumer's transport, deliberately bypassing the
        cooperative ``request_lease`` priority/grant machinery. Never fabricates
        a transport — a caller must handle ``None`` (no forceful release possible
        this deployment) rather than inventing a success."""
        return self._transport

    def set_reaper(self, reaper: PodReaper) -> None:
        """Install the force-reap capability (R5). Mirrors ``set_transport``:
        startup wires the concrete ``KubernetesPodReaper`` here; until then (or
        when deliberately left unset) the grant flow's stale-holder escalation
        is skipped and the broker behaves exactly as before R5."""
        self._reaper = reaper

    def set_reap_targets(self, targets: dict) -> None:
        """Install the trusted ``{consumer_id: (namespace, selector)}`` map that
        force-reap aims at (self.ai#75/#79).

        Startup builds this from core's own environment. Entries with a blank
        namespace or selector are dropped rather than stored half-formed: a
        partial target is not something to aim a pod deletion with."""
        clean = {}
        for consumer_id, target in (targets or {}).items():
            if not target:
                continue
            namespace, selector = target
            namespace = (namespace or "").strip()
            selector = (selector or "").strip()
            if namespace and selector:
                clean[consumer_id] = (namespace, selector)
        self._reap_targets = clean
        log.info(
            "vram-reap: trusted reap targets installed for %s",
            sorted(clean) or "no consumers (force-reap will reap nothing)",
        )

    def _reap_target_for(self, consumer_id: str):
        """The trusted ``(namespace, selector)`` for ``consumer_id``, or ``None``
        when it has no configured pod identity and is therefore never force-reap
        eligible (R5-AC7). Deliberately does NOT consult the consumer's registry
        row — see the module docstring."""
        return self._reap_targets.get(consumer_id)

    def _lock_for(self, consumer_id: str) -> asyncio.Lock:
        lock = self._locks.get(consumer_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[consumer_id] = lock
        return lock

    def _commit_grant(
        self,
        consumer_id: str,
        amount_bytes: int,
        priority: int,
        reclaimed: list,
        freeable: int,
        why: str,
    ) -> LeaseResult:
        """Write the grant and return the result — a DENIAL if the write did not
        land (self.ai#77).

        ``record_grant`` swallows DB exceptions and returns ``None`` (and also
        returns ``None`` if the row vanished between this decision's
        registration check and the write). Every success return here used to do
        ``held_bytes=... if granted else amount_bytes``, so a failed registry
        write still produced a ``LeaseGranted`` carrying a plausible-looking
        figure for a hold that was never recorded. The consumer would then
        allocate VRAM the registry does not know about — an untracked hold, which
        is precisely the over-grant precursor this module refuses everywhere
        else ("never a silent success", "never invents VRAM").

        A denial here is honest and safe: nothing was written, so there is no
        phantom hold to unwind, and the caller gets the same structured
        ``LeaseDenied`` it already knows how to handle.
        """
        reg = self._registry
        granted = reg.record_grant(consumer_id, amount_bytes, priority)
        if granted is None:
            log.error(
                "vram-broker: record_grant FAILED for %r (%dB) — DENYING rather "
                "than reporting a grant the registry never recorded (#77)",
                consumer_id,
                amount_bytes,
            )
            return LeaseDenied(
                requested_bytes=amount_bytes,
                free_bytes=reg.free_capacity(),
                freeable_bytes=freeable,
                holders_asked=reclaimed,
            )
        log.info(
            "vram-broker: GRANT %dB to %r (%s)", amount_bytes, consumer_id, why
        )
        return LeaseGranted(
            consumer_id=consumer_id,
            granted_bytes=amount_bytes,
            held_bytes=_granted_effective_held(granted, amount_bytes),
            priority=priority,
            reclaimed=reclaimed,
        )

    async def request_release(
        self,
        consumer_id: str,
        amount_bytes: int,
        timeout_seconds: float,
        transport: Optional[ReleaseTransport] = None,
    ) -> ReleaseOutcome:
        """Ask ``consumer_id`` to free ``amount_bytes``, honouring a per-call
        ``timeout_seconds`` bound. Returns the resolved :class:`ReleaseOutcome`.

        Single-outstanding (R2-AC4): the whole issue+resolve runs under the
        consumer's lock, so a second concurrent call for the same consumer
        awaits the first instead of racing it. Requests to *different* consumers
        do not block each other (separate locks).

        ``transport`` overrides the broker's installed transport for this call
        (used by tests to inject a fake consumer)."""
        transport = transport or self._transport
        if transport is None:
            # No configured client (T-016 unlanded) — fail loudly. Never a
            # silent success.
            raise RuntimeError(
                f"vram-broker: no release transport configured for {consumer_id!r} "
                "(concrete self.llamolotl client is T-016)"
            )

        lock = self._lock_for(consumer_id)
        async with lock:
            # State: we own the release for this consumer now. release-requested
            # = accepted/queued; releasing = dispatched to the consumer, awaiting
            # its free. A second caller waiting on the lock sees `releasing`.
            self._registry.set_lease_state(consumer_id, LEASE_STATE_RELEASE_REQUESTED)
            self._registry.set_lease_state(consumer_id, LEASE_STATE_RELEASING)
            try:
                resp = await asyncio.wait_for(
                    transport.request_release(consumer_id, amount_bytes, timeout_seconds),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                # No response within the per-call bound -> true state unknown.
                log.warning(
                    f"vram-broker: release request to {consumer_id!r} for "
                    f"{amount_bytes} bytes timed out after {timeout_seconds}s"
                )
                resp = ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)
            except Exception as e:
                # Any transport failure (network, protocol) leaves the true
                # state unknown — treated as a timeout, never a silent success,
                # and never leaves the consumer stuck in `releasing`.
                log.warning(
                    f"vram-broker: release request to {consumer_id!r} failed "
                    f"({e!r}); treating as timeout (state unknown)"
                )
                resp = ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

            return self._resolve_response(consumer_id, resp)

    def _resolve_response(self, consumer_id: str, resp: ReleaseResponse) -> ReleaseOutcome:
        """Map a transport response onto registry state + a ReleaseOutcome
        (T-008). Confirmed writes the reported new held; denied leaves held
        unchanged; timeout marks stale. Denial and timeout are held strictly
        distinct and neither is ever recorded as freed."""
        # Record whatever the consumer said about itself, before resolving the
        # outcome — a reason is worth keeping on every path, not just denials.
        self._last_release_reason[consumer_id] = resp.reason
        if resp.outcome == ReleaseOutcome.CONFIRMED:
            if resp.new_held_bytes is None:
                # A confirmed release with no reported held is malformed — we
                # cannot trust it as a new held value, so the true state is
                # unknown. Fall back to the timeout/stale path (never success).
                log.warning(
                    f"vram-broker: {consumer_id!r} confirmed release without a "
                    "reported held value; treating as unknown/stale"
                )
                self._registry.mark_stale(consumer_id)
                return ReleaseOutcome.TIMEOUT
            self._registry.record_confirmed_release(consumer_id, resp.new_held_bytes)
            return ReleaseOutcome.CONFIRMED

        if resp.outcome == ReleaseOutcome.DENIED:
            # Explicit refusal: held is still known/unchanged, back to steady.
            self._registry.set_lease_state(consumer_id, LEASE_STATE_STEADY)
            return ReleaseOutcome.DENIED

        # TIMEOUT (or coerced-unknown): mark stale, never write held.
        self._registry.mark_stale(consumer_id)
        return ReleaseOutcome.TIMEOUT

    ####################
    # R3 grant protocol (T-009 free-path / T-010 reclamation / T-012 denial)
    ####################

    async def request_lease(
        self,
        consumer_id: str,
        amount_bytes: int,
        priority: int = 0,
        release_timeout_seconds: Optional[float] = None,
        transport: Optional[ReleaseTransport] = None,
    ) -> LeaseResult:
        """Grant ``amount_bytes`` of VRAM to registered requester ``consumer_id``
        (R3). Returns :class:`LeaseGranted` on success or :class:`LeaseDenied`
        (structured, never a bare failure) if it cannot be satisfied.

        ``priority`` is the request's reclamation priority — carried onto the
        requester's registry row when granted so a LATER grant asks it to release
        in the right order. It NEVER lets a request invent VRAM: an over-capacity
        request is denied at any priority (R3-AC7).

        ``release_timeout_seconds`` bounds each R2 release-request issued during
        reclamation; defaults to :data:`GRANT_RELEASE_TIMEOUT_SECONDS`.
        ``transport`` overrides the installed release transport (tests inject a
        fake). The whole decision runs under the broker-wide grant lock."""
        if release_timeout_seconds is None:
            release_timeout_seconds = GRANT_RELEASE_TIMEOUT_SECONDS
        # #80: one overall bound for the whole decision, so the grant lock is
        # never held for (holders x per-release timeout) minutes.
        budget = _Budget(GRANT_TOTAL_TIMEOUT_SECONDS)
        async with self._grant_lock:
            return await self._decide_lease(
                consumer_id,
                amount_bytes,
                priority,
                release_timeout_seconds,
                transport,
                budget,
            )

    async def _decide_lease(
        self,
        consumer_id: str,
        amount_bytes: int,
        priority: int,
        release_timeout_seconds: float,
        transport: Optional[ReleaseTransport],
        budget: Optional[_Budget] = None,
    ) -> LeaseResult:
        reg = self._registry
        if budget is None:
            budget = _Budget(GRANT_TOTAL_TIMEOUT_SECONDS)

        # The requester must be a registered consumer so its granted hold can be
        # tracked (R1 registration precedes an R3 lease request). A protocol
        # error, distinct from a capacity denial — fail loudly rather than
        # granting an untracked hold.
        if reg.get(consumer_id) is None:
            raise ValueError(
                f"vram-broker: lease requester {consumer_id!r} is not registered; "
                "register (R1) before requesting a lease"
            )

        # --- Decision 6 / R1: an active EXCLUSIVE lease (a training/pipeline/curator
        # window that has seized the whole card) blocks every other consumer's grant
        # for its duration. Short-circuit BEFORE any reclamation: nothing is grantable
        # while it's held, and we must not ask shared holders to release into a locked
        # card. The requester being the exclusive holder itself falls through to
        # normal logic (it already owns the card). A STALE exclusive holder does not
        # count (active_exclusive_holder excludes stale), so a wedged window can't lock
        # the card forever — it gets force-reaped/reconciled like any stale holder.
        exclusive = reg.active_exclusive_holder()
        if exclusive is not None and exclusive.consumer_id != consumer_id:
            log.info(
                f"vram-broker: DENY lease {amount_bytes}B to {consumer_id!r} — GPU "
                f"held by an exclusive lease ({exclusive.consumer_id!r})"
            )
            return LeaseDenied(
                requested_bytes=amount_bytes,
                free_bytes=reg.free_capacity(),
                freeable_bytes=0,  # nothing grantable while an exclusive lease is held
                holders_asked=[],
                exclusive_holder=exclusive.consumer_id,
            )

        free = reg.free_capacity()
        # Holders we may ask to release: non-stale, held>0, ascending priority
        # (lower asked first), EXCLUDING the requester itself — asking a consumer
        # to free VRAM so it can grant *itself* more is nonsensical. PRIORITY GATE
        # (#3): only holders STRICTLY LOWER priority than the requester are
        # reclaimable — a grant never evicts an equal- or higher-priority holder
        # (e.g. image generation at priority 3 must not reclaim the inference
        # brain's VRAM at priority 10). ``priority`` is authoritative from the
        # requester's registration: the router passes the registered value, not a
        # self-declared one (routers/vram_leases.py), so a consumer cannot bypass
        # this gate by asking at an inflated priority. Strict ``<`` on purpose — an
        # equal-priority peer is NOT reclaimable; peers coexist or the newcomer is
        # denied, never thrash each other. ``acquire_exclusive`` is exempt (a
        # training window is meant to seize the whole card; that is its definition).
        holders = [
            h
            for h in reg.eligible_holders()
            if h.consumer_id != consumer_id and h.priority < priority
        ]
        releasable = sum(h.held_bytes for h in holders)

        # --- R5: when a reaper is installed, stale holders with a configured pod
        # identity are ALSO reclaimable — by force-reap, not cooperation — so
        # their held counts toward the freeable ceiling. This is not "inventing"
        # VRAM (R3-AC7): a confirmed pod kill is real, evidenced reclamation, the
        # same standard as a confirmed release. Crucially it also keeps the
        # escalation REACHABLE: a request whose shortfall can ONLY be met by a
        # stale holder's capacity would otherwise trip the up-front over-freeable
        # denial below and never reach the escalation step it exists for (AC2).
        # With no reaper installed, stale capacity is genuinely unreachable and
        # excluded here, so the whole grant flow behaves exactly as before R5.
        reapable = 0
        if self._reaper is not None:
            reapable = sum(
                h.held_bytes
                for h in reg.stale_holders()
                if h.consumer_id != consumer_id
                # #3 priority gate: only STRICTLY-lower-priority stale holders are
                # reap-eligible, mirroring the cooperative `holders` gate above.
                and h.priority < priority
                # #75: eligibility follows the TRUSTED target map, not the
                # consumer's (writable) registry row.
                and self._reap_target_for(h.consumer_id) is not None
                # #105: never-observed consumers are not reapable, so their held
                # must not count toward `freeable` either — otherwise the grant
                # passes the AC7 "can this ever be satisfied" check against
                # capacity the escalation will then refuse to reclaim, and the
                # request fails later and less legibly than an up-front denial.
                and _ever_observed(h)
            )
        freeable = free + releasable + reapable

        # --- R3-AC7: never invent VRAM. If the request exceeds everything free
        # PLUS everything eligible holders could ever give back (cooperatively or,
        # when armed, by force-reap), deny up front — regardless of the request's
        # priority, before asking anyone to release.
        if amount_bytes > freeable:
            log.info(
                f"vram-broker: DENY lease {amount_bytes}B to {consumer_id!r} "
                f"(priority {priority}) — exceeds free+releasable ({free}+{releasable}"
                f"={freeable}); never invents VRAM"
            )
            return LeaseDenied(
                requested_bytes=amount_bytes,
                free_bytes=free,
                freeable_bytes=freeable,
                holders_asked=[],
            )

        # --- R3-AC2: satisfiable from currently-free capacity -> grant now, with
        # NO release-request issued at all. Record the hold BEFORE returning
        # success (R3-AC6).
        if free >= amount_bytes:
            return self._commit_grant(
                consumer_id,
                amount_bytes,
                priority,
                [],
                freeable,
                f"from free (free {free}, no reclamation)",
            )

        # --- R3-AC3/AC4: free is short but free+releasable can cover it. Ask
        # holders in ascending-priority order for the running shortfall. Count
        # ONLY confirmed releases toward progress: free_capacity() is re-read
        # from the registry each round, and because a CONFIRMED release is the
        # only thing that lowers a holder's held (a timeout marks stale / a
        # denial leaves held unchanged — see _resolve_response), free_capacity()
        # rises only on real, landed confirmations. The grant below is therefore
        # never speculative.
        asked: list[HolderAsked] = []
        for holder in holders:
            if reg.free_capacity() >= amount_bytes:
                break
            # #80: stop starting new releases once the overall budget is spent.
            # Not an error — it falls through to the structured denial below,
            # with whatever WAS reclaimed already banked in the registry.
            if budget.spent():
                log.warning(
                    "vram-broker: grant budget exhausted for %r after asking %d "
                    "holder(s); denying rather than holding the grant lock longer",
                    consumer_id,
                    len(asked),
                )
                break
            shortfall = amount_bytes - reg.free_capacity()
            before = reg.get(holder.consumer_id)
            before_held = before.held_bytes if before else holder.held_bytes
            outcome = await self.request_release(
                holder.consumer_id,
                shortfall,
                budget.bound(release_timeout_seconds),
                transport=transport,
            )
            after = reg.get(holder.consumer_id)
            after_held = after.held_bytes if after else before_held
            released = (
                max(0, before_held - after_held)
                if outcome == ReleaseOutcome.CONFIRMED
                else 0
            )
            asked.append(
                HolderAsked(
                    consumer_id=holder.consumer_id,
                    requested_release=shortfall,
                    actually_released=released,
                    outcome=outcome.value,
                    reason=self.last_release_reason(holder.consumer_id),
                )
            )

        # Post-reclamation decision reads the registry's TRUE free capacity,
        # which reflects only confirmed releases — never a speculative grant.
        free_after = reg.free_capacity()
        if free_after >= amount_bytes:
            return self._commit_grant(
                consumer_id,
                amount_bytes,
                priority,
                asked,
                freeable,
                f"after reclaiming from {len(asked)} holder(s) (free now {free_after})",
            )

        # --- R5 FORCE-REAP ESCALATION (AC1/AC2/AC5/AC6/AC7/AC8) ---------------
        # Cooperative reclamation is exhausted and we are still short. If — and
        # ONLY if — a reaper is installed, escalate to the last resort: force-reap
        # the STALE holders whose capacity the shortfall still needs. This runs
        # strictly AFTER the reclamation loop and BEFORE the denial below, so it
        # never pre-empts a holder that is still answering: a consumer that
        # explicitly `denied` a release is in `steady` state (see
        # _resolve_response), so it is NEVER in stale_holders() and NEVER reaped
        # (AC1). Only holders that failed to respond at all — and are therefore
        # `stale` — are candidates (including any that JUST timed out in the loop
        # above, which is why we re-read stale_holders() fresh here).
        #
        # The non-speculative guarantee mirrors the reclamation loop exactly:
        # record_reaped_release (which clears a holder's held) is called ONLY on
        # a `confirmed` ReapOutcome — a real, evidenced pod deletion — and
        # free_capacity() is re-read from the registry afterward, so the retry
        # grant below sees only capacity that was actually reclaimed. A reap that
        # fails / cannot be confirmed frees nothing and can never produce a grant.
        if self._reaper is not None:
            for holder in [
                h
                for h in reg.stale_holders()
                # #3 priority gate: force-reap, like cooperative reclaim, only ever
                # targets STRICTLY-lower-priority holders — a grant never reaps an
                # equal-/higher-priority consumer's pod.
                if h.consumer_id != consumer_id and h.priority < priority
            ]:
                if reg.free_capacity() >= amount_bytes:
                    break
                # #80: the escalation shares the same overall budget as the
                # cooperative loop that ran before it — a hung k8s API must not
                # extend the grant lock past the bound either.
                if budget.spent():
                    log.warning(
                        "vram-broker: grant budget exhausted for %r before "
                        "force-reap could finish; denying",
                        consumer_id,
                    )
                    break
                shortfall = amount_bytes - reg.free_capacity()

                # #105: refuse to reap a consumer core has never once heard from.
                # Checked BEFORE the target lookup so the reaper is not even
                # consulted: a consumer whose endpoint has never answered is
                # almost certainly not deployed yet, and deleting its pod turns a
                # rollout-ordering gap into destroyed work.
                if not _ever_observed(holder):
                    log.warning(
                        "vram-reap: %r is stale but has NEVER been observed "
                        "(no heartbeat or confirmed release since registration) "
                        "— refusing to force-reap a consumer that may simply not "
                        "be deployed yet (self.ai#105)",
                        holder.consumer_id,
                    )
                    asked.append(
                        HolderAsked(
                            consumer_id=holder.consumer_id,
                            requested_release=shortfall,
                            actually_released=0,
                            outcome=HOLDER_OUTCOME_INELIGIBLE_NEVER_OBSERVED,
                            reason=(
                                "never observed since registration — core has "
                                "had no heartbeat or confirmed release from this "
                                "consumer, so its pod is not a safe reap target"
                            ),
                        )
                    )
                    continue

                # #75/#79: the target comes from core's trusted, env-derived map
                # — NEVER from holder.k8s_namespace / holder.k8s_pod_selector,
                # which any mesh ticket can rewrite via /register and thereby aim
                # a pod deletion at something else in the namespace.
                target = self._reap_target_for(holder.consumer_id)

                # AC7: no configured pod identity -> never eligible for
                # force-reap. Record it distinguishably and move on; the reaper
                # is never even called for this holder.
                if target is None:
                    log.info(
                        "vram-reap: %r is stale but has no configured pod "
                        "identity — ineligible for force-reap (AC7)",
                        holder.consumer_id,
                    )
                    asked.append(
                        HolderAsked(
                            consumer_id=holder.consumer_id,
                            requested_release=shortfall,
                            actually_released=0,
                            outcome=HOLDER_OUTCOME_INELIGIBLE_NO_POD_IDENTITY,
                        )
                    )
                    continue

                before = reg.get(holder.consumer_id)
                before_held = before.held_bytes if before else holder.held_bytes
                # Bounded by the same release-timeout so a hung k8s API can never
                # block the grant response indefinitely (AC6). Never raises — the
                # reaper resolves every failure to a `failed` ReapOutcome.
                namespace, selector = target
                reap = await self._reaper.reap(
                    namespace, selector, budget.bound(release_timeout_seconds)
                )
                if reap.status == ReapStatus.CONFIRMED:
                    # AC5: a CONFIRMED deletion clears held via the same
                    # mechanism reconcile() uses — but triggered by this
                    # automatic escalation, recorded distinguishably.
                    reg.record_reaped_release(holder.consumer_id)
                    after = reg.get(holder.consumer_id)
                    after_held = after.held_bytes if after else 0
                    released = max(0, before_held - after_held)
                    log.info(
                        "vram-reap: force-reap of %r CONFIRMED (%s); reclaimed "
                        "%dB toward grant to %r",
                        holder.consumer_id,
                        reap.reason,
                        released,
                        consumer_id,
                    )
                    asked.append(
                        HolderAsked(
                            consumer_id=holder.consumer_id,
                            requested_release=shortfall,
                            actually_released=released,
                            outcome=HOLDER_OUTCOME_REAPED,
                            reason=reap.reason,
                        )
                    )
                else:
                    # AC6: a failed reap frees nothing and is surfaced in the
                    # breakdown — never silently treated as success.
                    log.warning(
                        "vram-reap: force-reap of %r FAILED (%s); no VRAM "
                        "reclaimed",
                        holder.consumer_id,
                        reap.reason,
                    )
                    asked.append(
                        HolderAsked(
                            consumer_id=holder.consumer_id,
                            requested_release=shortfall,
                            actually_released=0,
                            outcome=HOLDER_OUTCOME_REAP_FAILED,
                            reason=reap.reason,
                        )
                    )

            # AC2/AC5: if force-reap made room, retry the grant now. Reads the
            # registry's TRUE free capacity, which rose only on CONFIRMED
            # reaps — the grant is never speculative.
            free_after = reg.free_capacity()
            if free_after >= amount_bytes:
                return self._commit_grant(
                    consumer_id,
                    amount_bytes,
                    priority,
                    asked,
                    freeable,
                    f"after force-reap escalation (free now {free_after})",
                )

        # --- R3-AC5 / R5-AC6: exhausted eligible holders (cooperative AND, when
        # armed, force-reap) and still short -> structured denial. No record_grant
        # ran on any path but the success returns above, so there is NO phantom
        # partial hold on the requester to unwind. freeable is the optimistic
        # free+releasable(+reapable) at request time; the holders_asked breakdown
        # explains why it was not realized (who denied / timed out / could not
        # release enough / was reaped or reap-failed / had no pod identity). A
        # force-reap failure thus flows straight into the existing structured
        # denial (R5-AC6) — never a silent success, never an indefinite block.
        log.info(
            f"vram-broker: DENY lease {amount_bytes}B to {consumer_id!r} after "
            f"reclamation — free {free_after} < requested; asked {len(asked)} holder(s)"
        )
        return LeaseDenied(
            requested_bytes=amount_bytes,
            free_bytes=free_after,
            freeable_bytes=freeable,
            holders_asked=asked,
        )

    ####################
    # Decision 6 / R1: exclusive-lease acquire (T-003)
    ####################

    async def acquire_exclusive(
        self,
        consumer_id: str,
        release_timeout_seconds: Optional[float] = None,
        transport: Optional[ReleaseTransport] = None,
    ) -> ExclusiveResult:
        """Seize the whole card for an exclusive window (training/pipeline/curator).

        Reclaims EVERY other holder's VRAM — cooperatively (R2 release-request)
        and, when a reaper is installed, by force-reap of stale holders (R5) —
        until no other consumer holds anything, then marks ``consumer_id`` the
        exclusive holder so the broker denies all other grants for the window
        (T-002 ``_decide_lease``). Reuses the same reclamation + confirmed-reap
        accounting as ``_decide_lease`` and the same "confirmed or nothing" bar: a
        holder that cannot be reclaimed (denies, times out, reap-fails) leaves the
        card un-cleared, so exclusive is NOT set (no half-held lease, R1-AC4) and a
        structured failure is returned with the per-holder breakdown.

        Refuses if a DIFFERENT consumer already holds an active exclusive lease
        (≤1 invariant, T-002). Does NOT grant ``consumer_id`` any ``held_bytes`` —
        the exclusive lease is a policy flag; the window's real VRAM is relayed by
        the poller/heartbeat. Runs under the broker-wide grant lock, so it is
        serialized against ordinary grants (a grant can't slip VRAM to someone
        else mid-acquire). Release with ``release_exclusive`` (T-004)."""
        if release_timeout_seconds is None:
            release_timeout_seconds = GRANT_RELEASE_TIMEOUT_SECONDS
        # #80: an exclusive acquire holds the SAME broker-wide lock as a grant and
        # walks every holder, so it needs the same overall bound. Running out
        # simply means the card was not cleared -> the "confirmed or nothing" bar
        # below already refuses to mark exclusive on a half-cleared card.
        budget = _Budget(GRANT_TOTAL_TIMEOUT_SECONDS)
        async with self._grant_lock:
            reg = self._registry
            if reg.get(consumer_id) is None:
                raise ValueError(
                    f"vram-broker: exclusive-lease requester {consumer_id!r} is not "
                    "registered; register (R1) before acquiring an exclusive lease"
                )
            transport = transport if transport is not None else self._transport

            # ≤1 invariant: another consumer already holds an active exclusive lease.
            other = reg.active_exclusive_holder()
            if other is not None and other.consumer_id != consumer_id:
                log.info(
                    "vram-broker: REFUSE exclusive lease to %r — %r already holds one",
                    consumer_id,
                    other.consumer_id,
                )
                return ExclusiveResult(
                    acquired=False,
                    consumer_id=consumer_id,
                    reason=(
                        f"another consumer ({other.consumer_id!r}) already holds an "
                        "active exclusive lease"
                    ),
                    blocking_holder=other.consumer_id,
                )

            asked: list[HolderAsked] = []

            # --- cooperative reclaim: ask every OTHER eligible holder for its full held
            for holder in [
                h for h in reg.eligible_holders() if h.consumer_id != consumer_id
            ]:
                before = reg.get(holder.consumer_id)
                before_held = before.held_bytes if before else holder.held_bytes
                if before_held <= 0:
                    continue
                if budget.spent():
                    log.warning(
                        "vram-broker: exclusive-acquire budget exhausted for %r; "
                        "stopping (the card is not cleared, so exclusive is NOT set)",
                        consumer_id,
                    )
                    break
                outcome = await self.request_release(
                    holder.consumer_id,
                    before_held,
                    budget.bound(release_timeout_seconds),
                    transport=transport,
                )
                after = reg.get(holder.consumer_id)
                after_held = after.held_bytes if after else before_held
                released = (
                    max(0, before_held - after_held)
                    if outcome == ReleaseOutcome.CONFIRMED
                    else 0
                )
                asked.append(
                    HolderAsked(
                        consumer_id=holder.consumer_id,
                        requested_release=before_held,
                        actually_released=released,
                        outcome=outcome.value,
                        reason=self.last_release_reason(holder.consumer_id),
                    )
                )

            # --- force-reap escalation: stale holders with a configured pod identity
            # (mirrors _decide_lease's confirmed-or-nothing reap accounting exactly).
            if self._reaper is not None:
                for holder in [
                    h for h in reg.stale_holders() if h.consumer_id != consumer_id
                ]:
                    before = reg.get(holder.consumer_id)
                    before_held = before.held_bytes if before else holder.held_bytes
                    if before_held <= 0:
                        continue
                    # #105: same never-observed refusal as the grant path.
                    if not _ever_observed(holder):
                        asked.append(
                            HolderAsked(
                                consumer_id=holder.consumer_id,
                                requested_release=before_held,
                                actually_released=0,
                                outcome=HOLDER_OUTCOME_INELIGIBLE_NEVER_OBSERVED,
                                reason=(
                                    "never observed since registration — core has "
                                    "had no heartbeat or confirmed release from "
                                    "this consumer, so its pod is not a safe reap "
                                    "target"
                                ),
                            )
                        )
                        continue
                    # #75/#79: trusted map, not the writable registry row.
                    target = self._reap_target_for(holder.consumer_id)
                    if target is None:
                        asked.append(
                            HolderAsked(
                                consumer_id=holder.consumer_id,
                                requested_release=before_held,
                                actually_released=0,
                                outcome=HOLDER_OUTCOME_INELIGIBLE_NO_POD_IDENTITY,
                            )
                        )
                        continue
                    if budget.spent():
                        log.warning(
                            "vram-broker: exclusive-acquire budget exhausted for "
                            "%r during force-reap; stopping",
                            consumer_id,
                        )
                        break
                    namespace, selector = target
                    reap = await self._reaper.reap(
                        namespace, selector, budget.bound(release_timeout_seconds)
                    )
                    if reap.status == ReapStatus.CONFIRMED:
                        reg.record_reaped_release(holder.consumer_id)
                        after = reg.get(holder.consumer_id)
                        after_held = after.held_bytes if after else 0
                        asked.append(
                            HolderAsked(
                                consumer_id=holder.consumer_id,
                                requested_release=before_held,
                                actually_released=max(0, before_held - after_held),
                                outcome=HOLDER_OUTCOME_REAPED,
                                reason=reap.reason,
                            )
                        )
                    else:
                        asked.append(
                            HolderAsked(
                                consumer_id=holder.consumer_id,
                                requested_release=before_held,
                                actually_released=0,
                                outcome=HOLDER_OUTCOME_REAP_FAILED,
                                reason=reap.reason,
                            )
                        )

            # --- confirmed or nothing (R1-AC4): every OTHER holder must be at 0.
            remaining = [
                h
                for h in reg.eligible_holders() + reg.stale_holders()
                if h.consumer_id != consumer_id and h.held_bytes > 0
            ]
            if remaining:
                log.warning(
                    "vram-broker: exclusive acquire for %r FAILED — %d holder(s) still "
                    "hold VRAM (%s); NOT marking exclusive",
                    consumer_id,
                    len(remaining),
                    [h.consumer_id for h in remaining],
                )
                return ExclusiveResult(
                    acquired=False,
                    consumer_id=consumer_id,
                    reason=(
                        f"could not clear the card: {len(remaining)} holder(s) still "
                        "hold VRAM after reclamation"
                    ),
                    reclaimed=asked,
                )

            # --- card clear -> mark exclusive (set_exclusive re-checks the invariant).
            if not reg.set_exclusive(consumer_id, True):
                return ExclusiveResult(
                    acquired=False,
                    consumer_id=consumer_id,
                    reason="lost a race to another exclusive acquire",
                    reclaimed=asked,
                )
            log.info(
                "vram-broker: %r ACQUIRED exclusive lease (reclaimed %d holder(s))",
                consumer_id,
                len(asked),
            )
            return ExclusiveResult(
                acquired=True,
                consumer_id=consumer_id,
                reason="card cleared; exclusive lease active",
                reclaimed=asked,
            )

    async def release_exclusive(self, consumer_id: str) -> bool:
        """Release an exclusive lease at window end (Decision 6 / R1, T-004).

        Clears the consumer's exclusive posture back to ``shared`` so the broker
        returns to normal grant behavior on the very NEXT request — no manual reset
        (R1-AC3). Idempotent: releasing a consumer that is not the current
        exclusive holder is a successful no-op. Returns True iff it actually
        cleared an active exclusive lease held by this consumer. Runs under the
        grant lock so it cannot race a concurrent acquire/grant. Does NOT touch
        ``held_bytes`` — the window's real VRAM is whatever the poller/heartbeat
        last relayed and is reconciled/aged normally once the window ends."""
        async with self._grant_lock:
            reg = self._registry
            holder = reg.active_exclusive_holder()
            was_holder = holder is not None and holder.consumer_id == consumer_id
            reg.set_exclusive(consumer_id, False)
            if was_holder:
                log.info(
                    "vram-broker: %r released its exclusive lease; normal grants resumed",
                    consumer_id,
                )
            return was_holder


# Module-level singleton, mirroring ``VramLeasesTable`` -> ``VramLeases``. T-016
# installs the concrete self.llamolotl transport via
# ``VramBroker.set_transport(...)`` at startup. Tests instantiate their own
# ``VramBrokerImpl(transport=fake)`` for isolation.
VramBroker = VramBrokerImpl()
