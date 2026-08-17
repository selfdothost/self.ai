"""R5 force-reap escalation unit tests — covers AC1–AC8.

GPU-less AND cluster-less, mirroring test_vram_grant_protocol.py: each broker
test drives a temp file-backed SQLite DB with only the vram_consumer table
(patching the vram_leases module's get_db) and injects a FAKE PodReaper into the
broker (``VramBrokerImpl(reaper=...)``). No real Kubernetes, no HTTP — the fake
returns a scripted ``ReapOutcome`` per selector and records every reap it was
asked to perform, so a test can assert exactly which holders were (and were NOT)
reaped and that the escalation only ever fires against ``stale`` holders.

The load-bearing property under test is R5's non-speculative guarantee (mirrors
R3-AC4): a stale holder's ``held`` is cleared — and the grant retried — ONLY on
a *confirmed* pod deletion. A reap that fails, a holder with no configured pod
identity, and a holder that explicitly *denied* (and is therefore cooperative /
``steady``, never ``stale``) all fall through to the existing structured denial,
recorded distinguishably, never a fabricated success and never an indefinite
block.

A second group of tests drives the concrete ``KubernetesPodReaper`` directly
against a FAKE k8s ``PodApi`` (no cluster): confirmed delete, 403/RBAC-denied,
not-found, accepted-but-unconfirmed, and the distinguishable ``vram-reap:``
logging (AC4/AC8).

Async code is driven with ``asyncio.run(...)`` inside sync tests, matching
test_gpu_queue.py (the repo registers no pytest-asyncio marker and runs under
--strict-markers).
"""

import asyncio
import logging
import types
from contextlib import asynccontextmanager, contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    LEASE_STATE_STALE,
    LEASE_STATE_STEADY,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)
from selfai_ui.utils.vram_broker import (
    HOLDER_OUTCOME_DENIED,
    HOLDER_OUTCOME_INELIGIBLE_NEVER_OBSERVED,
    HOLDER_OUTCOME_INELIGIBLE_NO_POD_IDENTITY,
    HOLDER_OUTCOME_REAP_FAILED,
    HOLDER_OUTCOME_REAPED,
    LeaseDenied,
    LeaseGranted,
    ReleaseOutcome,
    ReleaseResponse,
    VramBrokerImpl,
)
from selfai_ui.utils.vram_k8s import (
    KubernetesPodReaper,
    ReapOutcome,
    ReapStatus,
)

CARD = 24 * 1024**3
GiB = 1024**3


# ---------------------------------------------------------------------------
# Temp-SQLite fixture (mirrors test_vram_grant_protocol.py)
# ---------------------------------------------------------------------------


def _make_session_get_db(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    return Session, _get_db


@pytest.fixture
def vram_db(tmp_path):
    """Fresh temp SQLite with only vram_consumer; patches vram_leases.get_db."""
    db_file = tmp_path / "vram_force_reap_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    VramConsumer.__table__.create(engine)
    Session, get_db = _make_session_get_db(engine)

    original = vram_leases.get_db
    vram_leases.get_db = get_db
    _TARGETS.clear()
    yield {"engine": engine, "file": str(db_file), "Session": Session}
    _TARGETS.clear()
    vram_leases.get_db = original
    engine.dispose()


# Trusted reap targets, mirroring what startup installs from env (self.ai#75).
# Force-reap no longer aims using the consumer's REGISTRY ROW — that row is
# writable by any mesh service via /vram-leases/register (#79) — so a test that
# wants a holder to be reap-eligible has to install a target for it, exactly as
# _install_pod_reaper() does at boot. _register() keeps doing both so every
# existing case reads the same as before.
_TARGETS: dict = {}


def _broker(**kwargs):
    """A broker with the trusted target map installed for whatever _register()
    has been given a namespace+selector for."""
    broker = VramBrokerImpl(**kwargs)
    broker.set_reap_targets(dict(_TARGETS))
    return broker


def _register(
    consumer_id,
    held,
    priority=0,
    capacity=CARD,
    namespace=None,
    selector=None,
    observed=True,
):
    """Register a holder, and by default record that core has actually HEARD
    from it at least once (self.ai#105).

    ``register()`` deliberately does not set ``last_observed_at`` — registration
    is core asserting a consumer exists from its own config, not the consumer
    reporting. Every case in this file models a holder that reported healthily
    and *then* went quiet, so the heartbeat here is what makes that pre-condition
    true rather than accidental. Pass ``observed=False`` for the opposite case: a
    consumer core has never once heard from, which R5 must refuse to reap."""
    if namespace and selector:
        _TARGETS[consumer_id] = (namespace, selector)
    row = VramLeases.register(
        VramConsumerRegisterForm(
            consumer_id=consumer_id,
            total_capacity_bytes=capacity,
            held_bytes=held,
            priority=priority,
            k8s_namespace=namespace,
            k8s_pod_selector=selector,
        )
    )
    if observed:
        VramLeases.heartbeat(consumer_id, held)
    return row


def _make_stale(consumer_id):
    """Flag a registered holder ``stale`` the way an R2 release-timeout would
    (mark_stale sets the stored stale lease_state; effective_state surfaces it
    distinguishably). Its held is left intact — exactly the pre-condition R5
    escalates from."""
    VramLeases.mark_stale(consumer_id)


# ---------------------------------------------------------------------------
# Fakes — a scripted PodReaper and a scripted release transport
# ---------------------------------------------------------------------------


class FakeReaper:
    """Scripted force-reap responses keyed by *selector*, recording call order.

    The escalation calls ``reap(namespace, selector, timeout)`` once per eligible
    stale holder; this fake answers each with a preprogrammed
    :class:`ReapOutcome` and records every ``(namespace, selector, timeout)`` so a
    test can assert exactly WHICH holders were reaped (ascending priority) and —
    crucially for AC1/AC7 — that a cooperative denier or a no-pod-identity holder
    is NEVER passed to it (an unexpected selector raises)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def reap(self, namespace, selector, timeout_seconds):
        self.calls.append((namespace, selector, timeout_seconds))
        if selector not in self.responses:
            raise AssertionError(f"unexpected reap of selector {selector!r}")
        return self.responses[selector]

    @property
    def reaped_selectors(self):
        return [c[1] for c in self.calls]


def _reap_confirmed(pods_deleted=1):
    return ReapOutcome.confirmed(pods_deleted=pods_deleted, reason="fake confirmed")


def _reap_failed(reason="fake RBAC denied / k8s error"):
    return ReapOutcome.failed(reason)


class MappedTransport:
    """Scripted cooperative release responses keyed by consumer_id (reused from
    the R2/R3 test style) so a case can mix a cooperative denier with stale
    force-reap targets."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        self.calls.append((consumer_id, amount_bytes, timeout_seconds))
        if consumer_id not in self.responses:
            raise AssertionError(f"unexpected release-request to {consumer_id!r}")
        return self.responses[consumer_id]


_DENIED = ReleaseResponse(outcome=ReleaseOutcome.DENIED)


def _confirmed(new_held):
    return ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=new_held)


# ===========================================================================
# Broker escalation tests (AC1, AC2, AC5, AC6, AC7, AC8)
# ===========================================================================


# ---------------------------------------------------------------------------
# AC1 — force-reap only ever targets a STALE holder; a cooperative denier
#       (steady) is never reaped
# ---------------------------------------------------------------------------


def test_ac1_explicit_denier_never_reaped_only_stale(vram_db):
    _register("trainer", held=0)
    # A NON-stale holder that cooperatively DENIES the release — it answered, so
    # it stays `steady` and must never be force-reaped.
    _register(
        "denier",
        held=8 * GiB,
        priority=1,
        namespace="ns",
        selector="app=denier",
    )
    # A STALE holder with pod identity — the only legitimate reap target.
    _register(
        "ghost",
        held=8 * GiB,
        priority=2,
        namespace="ns",
        selector="app=ghost",
    )
    _make_stale("ghost")

    transport = MappedTransport(responses={"denier": _DENIED})
    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = _broker(transport=transport, reaper=reaper)

    # free = 24 - 16 = 8 GiB. Request 16 GiB: cooperative reclamation asks the
    # denier (refused), then escalation reaps the stale ghost (8 GiB) -> 16 free.
    # Requester priority 100 outranks both holders (1, 2) — the #3 priority gate
    # only lets a request reclaim/reap STRICTLY-lower-priority holders.
    result = asyncio.run(broker.request_lease("trainer", 16 * GiB, priority=100))

    assert isinstance(result, LeaseGranted)
    # The denier was asked to RELEASE (cooperative) but NEVER reaped.
    assert "app=denier" not in reaper.reaped_selectors
    # Only the stale holder was reaped.
    assert reaper.reaped_selectors == ["app=ghost"]
    # The cooperative denier's held is untouched and it stays steady.
    denier = VramLeases.get("denier")
    assert denier.held_bytes == 8 * GiB
    assert VramLeases.effective_state(denier) == LEASE_STATE_STEADY


# ---------------------------------------------------------------------------
# AC2 — escalation fires ONLY after cooperative reclamation is exhausted AND
#       only when stale capacity is actually needed
# ---------------------------------------------------------------------------


def test_ac2_free_path_never_calls_reaper(vram_db):
    # A stale, reap-eligible holder exists, but free capacity already suffices —
    # the reaper must never be consulted.
    _register("trainer", held=0)
    _register(
        "ghost", held=8 * GiB, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")
    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    # free = 24 - 8 = 16 GiB; a 4 GiB request is satisfied straight from free.
    result = asyncio.run(broker.request_lease("trainer", 4 * GiB, priority=0))

    assert isinstance(result, LeaseGranted)
    assert reaper.calls == []  # never escalated
    # Stale holder untouched.
    assert VramLeases.get("ghost").held_bytes == 8 * GiB


def test_ac2_cooperative_reclamation_sufficient_never_calls_reaper(vram_db):
    # The shortfall is fully covered by a cooperative (non-stale) release, so the
    # escalation is never reached even though a stale reap-eligible holder exists.
    _register("trainer", held=0)
    _register(
        "coop", held=8 * GiB, priority=0, namespace="ns", selector="app=coop"
    )
    _register(
        "ghost", held=8 * GiB, priority=1, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")

    transport = MappedTransport(responses={"coop": _confirmed(0)})  # frees its 8
    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = _broker(transport=transport, reaper=reaper)

    # free = 24 - 16 = 8 GiB; request 12 GiB -> coop frees 8 -> 16 free -> grant,
    # before the escalation loop is ever entered. Requester priority 100 outranks
    # both holders (0, 1) so the #3 gate lets it reclaim them.
    result = asyncio.run(broker.request_lease("trainer", 12 * GiB, priority=100))

    assert isinstance(result, LeaseGranted)
    # Cooperative release happened; NO reap.
    assert transport.calls and transport.calls[0][0] == "coop"
    assert reaper.calls == []
    # Stale holder never touched.
    assert VramLeases.get("ghost").held_bytes == 8 * GiB


def test_ac2_no_reaper_installed_behaves_as_before_r5(vram_db):
    # A broker with NO reaper installed must behave exactly as before R5: the
    # stale holder's capacity is genuinely unreachable, so a request that needs
    # it is denied (never escalated, never fabricated).
    _register("trainer", held=0)
    _register(
        "ghost", held=10 * GiB, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")
    broker = _broker(transport=MappedTransport())  # reaper defaults to None

    # free = 14 GiB; request 20 GiB needs the stale holder — with no reaper it is
    # unreachable, so this is denied up front and no holder appears in breakdown.
    result = asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=0))

    assert isinstance(result, LeaseDenied)
    assert result.holders_asked == []
    # Stale holder untouched; requester never held anything.
    assert VramLeases.get("ghost").held_bytes == 10 * GiB
    assert VramLeases.get("trainer").held_bytes == 0


# ---------------------------------------------------------------------------
# AC5 — a confirmed reap clears held->0/steady (record_reaped_release) and the
#       grant retries and succeeds
# ---------------------------------------------------------------------------


def test_ac5_confirmed_reap_clears_held_and_grant_retries(vram_db):
    _register("trainer", held=0)
    _register(
        "ghost", held=10 * GiB, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")

    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed(pods_deleted=1)})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    # free = 14 GiB; request 20 GiB. No non-stale holders to ask, so cooperative
    # reclamation is a no-op; the escalation reaps the stale ghost (10 GiB) ->
    # 24 free -> grant retries and succeeds. Requester priority 100 outranks the
    # stale holder (0) so the #3 gate lets the escalation reap it.
    result = asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=100))

    assert isinstance(result, LeaseGranted)
    assert result.granted_bytes == 20 * GiB
    # The requester's hold is written to the registry before success returns —
    # as a reservation, since nothing has been observed on the card yet (#76).
    assert VramLeases.get("trainer").reserved_bytes == 20 * GiB

    # The stale holder's held was cleared via record_reaped_release: held->0,
    # lease_state back to steady (same mechanism reconcile uses).
    ghost = VramLeases.get("ghost")
    assert ghost.held_bytes == 0
    assert ghost.lease_state == LEASE_STATE_STEADY
    assert VramLeases.effective_state(ghost) == LEASE_STATE_STEADY

    # The breakdown records the reap distinctly, with the freed amount.
    by_id = {h.consumer_id: h for h in result.reclaimed}
    assert by_id["ghost"].outcome == HOLDER_OUTCOME_REAPED
    assert by_id["ghost"].actually_released == 10 * GiB


# ---------------------------------------------------------------------------
# AC6 — a FAILED reap falls through to a structured LeaseDenied carrying the
#       failure; never a silent success, never hangs (bounded)
# ---------------------------------------------------------------------------


def test_ac6_failed_reap_falls_through_to_structured_denial(vram_db):
    _register("trainer", held=0)
    _register(
        "ghost", held=10 * GiB, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")

    reaper = FakeReaper(
        responses={"app=ghost": _reap_failed("RBAC denied (HTTP 403)")}
    )
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    # free = 14 GiB; request 20 GiB. The only capacity is the stale ghost's, but
    # its reap FAILS -> nothing is freed -> structured denial. Requester priority
    # 100 outranks the stale holder (0) so the escalation is reached (#3 gate).
    result = asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=100))

    assert isinstance(result, LeaseDenied)
    # The reaper WAS attempted (escalation fired) ...
    assert reaper.reaped_selectors == ["app=ghost"]
    # ... but nothing was reclaimed: no phantom hold, holder untouched + stale.
    assert VramLeases.get("trainer").held_bytes == 0
    ghost = VramLeases.get("ghost")
    assert ghost.held_bytes == 10 * GiB
    assert VramLeases.effective_state(ghost) == LEASE_STATE_STALE
    # The failure is surfaced distinguishably in the denial breakdown (AC6/AC8).
    ha = {h.consumer_id: h for h in result.holders_asked}["ghost"]
    assert ha.outcome == HOLDER_OUTCOME_REAP_FAILED
    assert ha.actually_released == 0
    assert result.free_bytes == 14 * GiB


# ---------------------------------------------------------------------------
# AC7 — a stale holder with BOTH pod-identity fields None is recorded
#       ineligible-no-pod-identity and never passed to the reaper
# ---------------------------------------------------------------------------


def test_ac7_no_pod_identity_stale_holder_never_reaped(vram_db):
    _register("trainer", held=0)
    # Stale, but NO pod identity -> opt-out: never eligible for force-reap.
    _register("noid", held=4 * GiB, priority=1)  # namespace/selector both None
    _make_stale("noid")
    # A second stale holder WITH pod identity provides the actual capacity.
    _register(
        "ghost", held=10 * GiB, priority=2, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")

    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    # free = 24 - 14 = 10 GiB; request 18 GiB. The no-identity holder is asked
    # first (lower priority) and recorded ineligible WITHOUT a reap; the ghost is
    # then reaped for the shortfall -> grant. Requester priority 100 outranks both
    # stale holders (1, 2) so both are in-scope for the escalation (#3 gate).
    result = asyncio.run(broker.request_lease("trainer", 18 * GiB, priority=100))

    assert isinstance(result, LeaseGranted)
    # The no-identity holder was NEVER passed to the reaper.
    assert "noid" not in [c[0] for c in reaper.calls]  # never by namespace
    assert reaper.reaped_selectors == ["app=ghost"]
    # It is recorded distinguishably as ineligible-no-pod-identity, with nothing
    # released, and its held is untouched (still stale).
    by_id = {h.consumer_id: h for h in result.reclaimed}
    assert by_id["noid"].outcome == HOLDER_OUTCOME_INELIGIBLE_NO_POD_IDENTITY
    assert by_id["noid"].actually_released == 0
    noid = VramLeases.get("noid")
    assert noid.held_bytes == 4 * GiB
    assert VramLeases.effective_state(noid) == LEASE_STATE_STALE


# ---------------------------------------------------------------------------
# AC8 — the denial breakdown distinguishes reaped / reap-failed from the
#       cooperative confirmed/denied/timeout vocabulary
# ---------------------------------------------------------------------------


def test_ac8_denial_breakdown_distinguishes_reap_outcomes(vram_db):
    _register("trainer", held=0)
    # A cooperative non-stale holder that DENIES.
    _register(
        "coop", held=4 * GiB, priority=0, namespace="ns", selector="app=coop"
    )
    # A stale holder that reaps CONFIRMED (frees 4).
    _register(
        "ghostA", held=4 * GiB, priority=1, namespace="ns", selector="app=ghostA"
    )
    _make_stale("ghostA")
    # A stale holder whose reap FAILS.
    _register(
        "ghostB", held=4 * GiB, priority=2, namespace="ns", selector="app=ghostB"
    )
    _make_stale("ghostB")

    transport = MappedTransport(responses={"coop": _DENIED})
    reaper = FakeReaper(
        responses={
            "app=ghostA": _reap_confirmed(),
            "app=ghostB": _reap_failed("k8s API error (HTTP 500)"),
        }
    )
    broker = _broker(transport=transport, reaper=reaper)

    # free = 24 - 12 = 12 GiB; request 22 GiB. coop denies (0 freed), ghostA is
    # reaped (+4 -> 16 free), ghostB reap fails (0) -> still short of 22 -> DENY,
    # with all three distinct outcomes in the breakdown. Requester priority 100
    # outranks all three holders (0, 1, 2) so all are in-scope (#3 gate).
    result = asyncio.run(broker.request_lease("trainer", 22 * GiB, priority=100))

    assert isinstance(result, LeaseDenied)
    by_id = {h.consumer_id: h for h in result.holders_asked}
    assert set(by_id) == {"coop", "ghostA", "ghostB"}
    # Cooperative vocabulary (unchanged from R3).
    assert by_id["coop"].outcome == HOLDER_OUTCOME_DENIED
    assert by_id["coop"].actually_released == 0
    # Force-reap vocabulary — distinct from cooperative outcomes (AC8).
    assert by_id["ghostA"].outcome == HOLDER_OUTCOME_REAPED
    assert by_id["ghostA"].actually_released == 4 * GiB
    assert by_id["ghostB"].outcome == HOLDER_OUTCOME_REAP_FAILED
    assert by_id["ghostB"].actually_released == 0
    # Only the confirmed reap actually freed anything.
    assert result.free_bytes == 16 * GiB
    # No phantom hold on the denied requester.
    assert VramLeases.get("trainer").held_bytes == 0


# ===========================================================================
# Reaper-module tests — KubernetesPodReaper against a FAKE k8s PodApi (AC4/AC8)
# ===========================================================================


def _pod(name, uid=None):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, uid=uid)
    )


class FakePodList:
    """A ``V1PodList`` stand-in. Each entry is either a bare pod name (uid None,
    the common case) or a ``(name, uid)`` tuple when a test needs pod identity to
    distinguish a reaped pod from its controller-created replacement."""

    def __init__(self, names):
        self.items = [
            _pod(*n) if isinstance(n, tuple) else _pod(n) for n in names
        ]


class FakeApiException(Exception):
    """Duck-typed stand-in for kubernetes_asyncio's ApiException — carries a
    ``.status`` the reaper reads via ``_status_code`` (no client lib needed)."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"api-exception status={status}")


class FakePodApi:
    """A scriptable :class:`PodApi`. ``list_script`` is a queue of results for
    successive ``list_namespaced_pod`` calls — each entry is either a list of pod
    names (wrapped in a FakePodList) or an Exception to raise; once exhausted the
    LAST result repeats (so the confirm-phase re-list sees a stable world).
    ``delete_error`` (if set) is raised on every delete."""

    def __init__(self, list_script, delete_error=None):
        self._list_script = list(list_script)
        self._last = None
        self._delete_error = delete_error
        self.deleted = []

    async def list_namespaced_pod(self, namespace, label_selector):
        if self._list_script:
            self._last = self._list_script.pop(0)
        result = self._last
        if isinstance(result, Exception):
            raise result
        return FakePodList(result if result is not None else [])

    async def delete_namespaced_pod(self, name, namespace):
        self.deleted.append(name)
        if self._delete_error is not None:
            raise self._delete_error


def _factory_for(api):
    @asynccontextmanager
    async def factory():
        yield api

    return factory


def _reaper_for(api):
    # Small poll interval so the confirm phase iterates quickly under test.
    return KubernetesPodReaper(
        client_factory=_factory_for(api), poll_interval_seconds=0.001
    )


def test_reaper_confirmed_delete(vram_db):
    # list -> ["pod-1"]; delete ok; confirm re-list -> [] => CONFIRMED.
    api = FakePodApi(list_script=[["pod-1"], []])
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.CONFIRMED
    assert out.pods_deleted == 1
    assert api.deleted == ["pod-1"]


def test_reaper_rbac_denied_403_is_failed(vram_db):
    # A 403 on the initial list -> failed with an RBAC reason, never raises.
    api = FakePodApi(list_script=[FakeApiException(403)])
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.FAILED
    assert "403" in out.reason
    assert api.deleted == []


def test_reaper_not_found_404_is_failed(vram_db):
    # A 404 on the initial list (namespace/resource gone) -> failed not-found.
    api = FakePodApi(list_script=[FakeApiException(404)])
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.FAILED
    assert "not found" in out.reason.lower() or "404" in out.reason


def test_reaper_nothing_to_reap_is_failed(vram_db):
    # An empty initial match is a FAILURE, not a no-op success — there is no
    # deletion evidence, so the broker must never clear held on it.
    api = FakePodApi(list_script=[[]])
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.FAILED
    assert "nothing to reap" in out.reason
    assert api.deleted == []


def test_reaper_confirms_when_deployment_replaces_pod(vram_db):
    # THE T-007 REGRESSION. A Deployment/ReplicaSet replaces the reaped pod under
    # the SAME selector within milliseconds, so the selector never empties. The
    # reaper must confirm on the *deleted pod's* absence, not on the selector
    # going empty: delete pod-1 (uidA); confirm re-list shows pod-2 (uidB), the
    # fresh replacement => CONFIRMED, because uidA is gone.
    api = FakePodApi(list_script=[[("pod-1", "uidA")], [("pod-2", "uidB")]])
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.CONFIRMED
    assert out.pods_deleted == 1
    assert api.deleted == ["pod-1"]


def test_reaper_confirms_statefulset_same_name_new_uid(vram_db):
    # A StatefulSet recreates the pod under the SAME name but a NEW uid. Matching
    # on uid (not name) still confirms: pod-1/uidA is gone even though a pod-1
    # (uidB) is present again.
    api = FakePodApi(list_script=[[("pod-1", "uidA")], [("pod-1", "uidB")]])
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.CONFIRMED
    assert api.deleted == ["pod-1"]


def test_reaper_unconfirmed_when_same_uid_persists(vram_db):
    # If the very pod we deleted (same uid) is still present at the bound, that is
    # NOT confirmed — the reap is unconfirmed exactly as before, just keyed on
    # identity rather than an empty selector.
    api = FakePodApi(list_script=[[("pod-1", "uidA")]])  # uidA never leaves
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=0.0))

    assert out.status == ReapStatus.FAILED
    assert "not confirmed" in out.reason
    assert api.deleted == ["pod-1"]


def test_reaper_accepted_but_unconfirmed_is_failed(vram_db):
    # list -> ["pod-1"]; delete ok; but the pod stays present forever. With a
    # zero confirm bound the reaper checks once, sees it still there, and returns
    # FAILED (accepted != confirmed — same bar as R2's confirmed-release).
    api = FakePodApi(list_script=[["pod-1"]])  # last result repeats: never empty
    reaper = _reaper_for(api)

    out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=0.0))

    assert out.status == ReapStatus.FAILED
    assert "not confirmed" in out.reason
    # The delete WAS issued — it just could not be confirmed gone.
    assert api.deleted == ["pod-1"]
    assert out.pods_deleted == 1


def test_reaper_logs_are_distinguishable_vram_reap_prefix(vram_db, caplog):
    api = FakePodApi(list_script=[["pod-1"], []])
    reaper = _reaper_for(api)

    with caplog.at_level(logging.INFO, logger="selfai_ui.utils.vram_k8s"):
        out = asyncio.run(reaper.reap("ns", "app=x", timeout_seconds=1.0))

    assert out.status == ReapStatus.CONFIRMED
    reap_records = [r for r in caplog.records if r.name == "selfai_ui.utils.vram_k8s"]
    messages = [r.getMessage() for r in reap_records]
    # Every reaper log line is under the distinct vram-reap: marker (AC8) ...
    assert messages, "expected the reaper to log its attempt/outcome"
    assert all("vram-reap:" in m for m in messages)
    # ... and never masquerades as a cooperative vram-broker: release.
    assert not any("vram-broker:" in m for m in messages)


# ---------------------------------------------------------------------------
# self.ai#75/#79 — the reap target comes from core's trusted config, and a
#                  registration can NEVER redirect a pod deletion
# ---------------------------------------------------------------------------


def test_registry_row_cannot_redirect_the_reaper(vram_db):
    """A holder's registry row carries k8s_namespace/k8s_pod_selector, and
    /vram-leases/register is gated on a shared-secret ticket with NO caller
    identity (#79) — so any mesh service could rewrite them. If the broker aimed
    with those fields, that would be an arbitrary-pod-delete primitive inside the
    namespace. It must aim with core's trusted map instead."""
    _register("trainer", held=0)
    # Registered with the LEGITIMATE identity, so a trusted target is installed.
    _register("ghost", held=10 * GiB, namespace="ns", selector="app=ghost")
    _make_stale("ghost")

    # Now simulate a hostile/mistaken re-registration pointing the row at core
    # itself. The trusted map is untouched by this.
    VramLeases.register(
        VramConsumerRegisterForm(
            consumer_id="ghost",
            total_capacity_bytes=CARD,
            held_bytes=10 * GiB,
            k8s_namespace="self-ai",
            k8s_pod_selector="app.kubernetes.io/name=selfai-api",
        )
    )
    _make_stale("ghost")

    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed(pods_deleted=1)})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=100))

    # The reaper was aimed at the CONFIGURED selector, never the row's.
    assert reaper.reaped_selectors == ["app=ghost"]
    assert "app.kubernetes.io/name=selfai-api" not in reaper.reaped_selectors


def test_holder_with_no_trusted_target_is_never_reaped(vram_db):
    """R5-AC7 opt-in, now sourced from config: a stale holder core has no
    configured identity for is ineligible even though its ROW names one."""
    _register("trainer", held=0)
    # Straight to the registry, bypassing _register, so NO trusted target exists.
    VramLeases.register(
        VramConsumerRegisterForm(
            consumer_id="ghost",
            total_capacity_bytes=CARD,
            held_bytes=10 * GiB,
            k8s_namespace="ns",
            k8s_pod_selector="app=ghost",
        )
    )
    _make_stale("ghost")

    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    result = asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=100))

    assert reaper.reaped_selectors == []
    assert isinstance(result, LeaseDenied)
    assert VramLeases.get("ghost").held_bytes == 10 * GiB


def test_set_reap_targets_drops_half_formed_entries(vram_db):
    """A partial target is not something to aim a pod deletion with."""
    broker = VramBrokerImpl()
    broker.set_reap_targets(
        {
            "good": ("ns", "app=good"),
            "blank-ns": ("", "app=x"),
            "blank-sel": ("ns", "   "),
            "none": None,
        }
    )
    assert broker._reap_target_for("good") == ("ns", "app=good")
    assert broker._reap_target_for("blank-ns") is None
    assert broker._reap_target_for("blank-sel") is None
    assert broker._reap_target_for("none") is None
    assert broker._reap_target_for("never-registered") is None


def test_broker_with_no_targets_installed_reaps_nothing(vram_db):
    """Safe default: a reaper present but no targets installed must reap nothing
    rather than fall back to the registry row."""
    _register("trainer", held=0)
    _register("ghost", held=10 * GiB, namespace="ns", selector="app=ghost")
    _make_stale("ghost")

    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = VramBrokerImpl(transport=MappedTransport(), reaper=reaper)  # no targets

    result = asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=100))

    assert reaper.reaped_selectors == []
    assert isinstance(result, LeaseDenied)


# ---------------------------------------------------------------------------
# self.ai#105 — a consumer core has NEVER heard from is not a reap target
#
# `last_reported_at` cannot tell "registered at boot and never reachable" apart
# from "reported healthily, then died": register() writes it, so both simply age
# past the staleness bound. Force-reap DELETES A POD, so it must not act on that
# ambiguity — the first case is a rollout-ordering gap (the consumer probably is
# not deployed yet), and deleting its pod turns that gap into destroyed work.
#
# Concretely, this is the self.curator case: core registers it from its own
# manifest env, polls an endpoint that 404s until self.curator ships, and about
# two minutes later has a stale, pod-identified, reap-eligible consumer that was
# never once reachable.
# ---------------------------------------------------------------------------


def test_a_never_observed_consumer_is_never_reaped(vram_db):
    """The regression test for self.ai#105: registered, given a pod identity,
    aged into stale, and STILL not a reap target — because nothing has ever come
    back from it."""
    _register("trainer", held=0)
    _register(
        "newborn",
        held=10 * GiB,
        priority=1,
        namespace="ns",
        selector="app=newborn",
        observed=False,          # <- never once heard from
    )
    _make_stale("newborn")

    reaper = FakeReaper(responses={"app=newborn": _reap_confirmed()})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    result = asyncio.run(broker.request_lease("trainer", 18 * GiB, priority=100))

    # The reaper was never even consulted for it.
    assert reaper.reaped_selectors == []
    assert "newborn" not in [c[0] for c in reaper.calls]
    # Its held is untouched — a refusal to reap is not a refusal to account.
    newborn = VramLeases.get("newborn")
    assert newborn.held_bytes == 10 * GiB
    assert VramLeases.effective_state(newborn) == LEASE_STATE_STALE
    # And the grant is denied rather than silently satisfied from capacity the
    # escalation declined to reclaim.
    assert isinstance(result, LeaseDenied)


def test_never_observed_is_distinguishable_from_no_pod_identity(vram_db):
    """Two different refusals with two different fixes: one is 'an operator chose
    not to opt this consumer in', the other is 'this consumer has never been
    reachable'. Collapsing them would send someone to check the wrong config.

    Shaped like the AC7 test: the refused holder is asked FIRST (lower priority)
    and a second, genuinely reapable holder supplies the shortfall — otherwise
    the request is denied up front by the freeable arithmetic and the escalation
    loop never runs to record an outcome at all."""
    _register("trainer", held=0)
    _register(
        "newborn",
        held=4 * GiB,
        priority=1,
        namespace="ns",
        selector="app=newborn",
        observed=False,          # <- never once heard from
    )
    _make_stale("newborn")
    # A properly-observed stale holder that IS reapable, providing the capacity.
    _register(
        "ghost", held=10 * GiB, priority=2, namespace="ns", selector="app=ghost"
    )
    _make_stale("ghost")

    reaper = FakeReaper(responses={"app=ghost": _reap_confirmed()})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    result = asyncio.run(broker.request_lease("trainer", 18 * GiB, priority=100))

    assert isinstance(result, LeaseGranted)
    # The never-observed holder was never passed to the reaper; the ghost was.
    assert reaper.reaped_selectors == ["app=ghost"]
    by_id = {h.consumer_id: h for h in result.reclaimed}
    assert by_id["newborn"].outcome == HOLDER_OUTCOME_INELIGIBLE_NEVER_OBSERVED
    assert by_id["newborn"].outcome != HOLDER_OUTCOME_INELIGIBLE_NO_POD_IDENTITY
    assert by_id["newborn"].actually_released == 0
    assert "never observed" in by_id["newborn"].reason
    # Its held is untouched — refusing to reap is not refusing to account.
    assert VramLeases.get("newborn").held_bytes == 4 * GiB


def test_one_heartbeat_makes_a_stale_holder_reapable_again(vram_db):
    """The guard is about evidence, not a permanent exemption. Once a consumer
    has proven it exists, going quiet later is an ordinary fault and R5 behaves
    exactly as it always did."""
    _register("trainer", held=0)
    _register(
        "newborn",
        held=10 * GiB,
        priority=1,
        namespace="ns",
        selector="app=newborn",
        observed=False,
    )
    # One real observation arrives ...
    VramLeases.heartbeat("newborn", 10 * GiB)
    # ... and only THEN does it go quiet.
    _make_stale("newborn")

    reaper = FakeReaper(responses={"app=newborn": _reap_confirmed()})
    broker = _broker(transport=MappedTransport(), reaper=reaper)

    result = asyncio.run(broker.request_lease("trainer", 18 * GiB, priority=100))

    assert reaper.reaped_selectors == ["app=newborn"]
    assert isinstance(result, LeaseGranted)


def test_registration_alone_is_not_an_observation(vram_db):
    """The distinction the whole fix rests on. If register() ever started writing
    last_observed_at, the guard would silently become a no-op and #105 would
    reopen with no failing test to catch it."""
    _register("newborn", held=GiB, observed=False)
    assert VramLeases.get("newborn").last_observed_at is None
    # Re-registering (what a core restart does) must not manufacture evidence.
    _register("newborn", held=GiB, observed=False)
    assert VramLeases.get("newborn").last_observed_at is None
    # A heartbeat is what counts.
    VramLeases.heartbeat("newborn", GiB)
    assert VramLeases.get("newborn").last_observed_at is not None


def test_a_confirmed_release_also_counts_as_observation(vram_db):
    """A consumer that answered a release request has demonstrably been reached,
    which is the same evidence a heartbeat provides."""
    _register("newborn", held=4 * GiB, observed=False)
    assert VramLeases.get("newborn").last_observed_at is None

    VramLeases.record_confirmed_release("newborn", 0)

    assert VramLeases.get("newborn").last_observed_at is not None


def test_never_observed_capacity_is_not_counted_as_freeable(vram_db):
    """AC7 honesty: if the escalation will refuse to reclaim it, the up-front
    'can this request ever be satisfied' arithmetic must not count it either —
    otherwise the grant passes that check and fails later, less legibly."""
    _register("trainer", held=0)
    _register(
        "newborn",
        held=20 * GiB,
        priority=1,
        namespace="ns",
        selector="app=newborn",
        observed=False,
    )
    _make_stale("newborn")

    broker = _broker(transport=MappedTransport(), reaper=FakeReaper(responses={}))
    # free = 4 GiB. Only the never-observed holder's 20 GiB could cover 18 GiB,
    # and it is not reclaimable — so this is denied up front.
    result = asyncio.run(broker.request_lease("trainer", 18 * GiB, priority=100))

    assert isinstance(result, LeaseDenied)
    assert result.freeable_bytes < 18 * GiB
