"""Asking the broker for VRAM before llamolotl loads a model (self.ai#107).

The bug being closed is not "the broker is wrong" — the broker was already
correct and self.llamolotl was already registered at the highest priority.
Nothing on the load path ever called it, so a load allocated around a
lower-priority holder instead of reclaiming from it and died on CUDA OOM.

These tests care most about the cases where the helper must *decline to act*:
an unknown footprint must not become a guess, and a broker fault must not turn
one model's OOM risk into an outage on every request.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.utils import vram_admission
from selfai_ui.utils.model_vram import (
    llamolotl_admission_footprint,
    llamolotl_footprint,
    llamolotl_vram_fields,
)
from selfai_ui.utils.vram_broker import LeaseDenied, LeaseGranted

GIB = 1024**3
MIB = 1024 * 1024

SPLIT_PRESET = """[GLM-4.5-Air-q8_0]
model = /models/GLM-4.5-Air-q8_0.gguf
n-cpu-moe = 40
ctx-size = 131072
vram-footprint-mib = 21504

"""


def _state(models=None, raw=None, enabled=True):
    return SimpleNamespace(
        config=SimpleNamespace(ENABLE_LLAMOLOTL_API=enabled),
        LLAMOLOTL_MODELS=models or {},
        LLAMOLOTL_RAW_MODELS=raw or {},
    )


def _run(state, model_id):
    return asyncio.run(vram_admission.ensure_llamolotl_vram(state, model_id))


####################
# reading the footprint
####################


@pytest.mark.tier0
def test_published_footprint_is_preferred_over_the_preset():
    """The router's own figure carries provenance and may be a MEASUREMENT; the
    preset only ever carries the operator's declaration. Preferring the preset
    would mean sizing a lease off a stale claim after a real measurement exists."""
    model = {
        "id": "m",
        "vram_footprint": {"bytes": 20 * GIB, "source": "measured"},
        "status": {"preset": SPLIT_PRESET},
    }
    assert llamolotl_footprint(model) == (20 * GIB, "measured")


@pytest.mark.tier0
def test_preset_declaration_is_the_fallback_for_an_older_router():
    """A router that predates the published field still accepts the preset key,
    so the two repos have no rollout ordering constraint."""
    model = {"id": "m", "status": {"preset": SPLIT_PRESET}}
    assert llamolotl_footprint(model) == (21504 * MIB, "declared")


@pytest.mark.tier0
@pytest.mark.parametrize(
    "model",
    [
        {"id": "m"},
        {"id": "m", "vram_footprint": {"bytes": 0, "source": "measured"}},
        {"id": "m", "vram_footprint": {"bytes": 5 * GIB}},  # no provenance
        {"id": "m", "vram_footprint": {"bytes": "lots", "source": "measured"}},
        {"id": "m", "status": {"preset": "[m]\nvram-footprint-mib = 0\n"}},
        {"id": "m", "status": {"preset": "[m]\nvram-footprint-mib = -1\n"}},
        {"id": "m", "status": {"preset": "[m]\nvram-footprint-mib = nonsense\n"}},
        {"id": "m", "status": {"preset": "[m]\nn-cpu-moe = 40\n"}},
    ],
)
def test_unusable_footprints_are_unknown_not_zero(model):
    """Every one of these must be UNKNOWN. A footprint of 0 would let a lease
    request ask for nothing and be granted it, which is worse than not asking."""
    assert llamolotl_footprint(model) is None
    assert llamolotl_vram_fields(model) == {}


@pytest.mark.tier0
def test_bytes_without_provenance_is_rejected():
    """`source` is not decoration: a consumer about to reclaim another tenant's
    VRAM needs to know whether it got a measurement or a file-size guess."""
    assert llamolotl_footprint({"id": "m", "vram_footprint": {"bytes": 5 * GIB}}) is None


####################
# an `estimated` footprint is not deny-grade
#
# Before self.llamolotl!46 the router published nothing, so core saw None for
# every model without an operator declaration -- unknown, and unknown never
# blocks. !46 publishes a footprint for EVERY model, and for most of them the
# source is `estimated`: a GGUF file size times an overhead percentage. Left
# untreated that silently converts "we do not know" into a refusable number and
# re-arms self.llamolotl#36 one layer up, in core rather than the router.
#
# Measured live on 7c11f0bc, which is why the margin is not academic:
#   Qwen2.5-VL-7B                          estimated 6909 MiB / measured 5212
#   gemma-4-26B                            estimated 16307    / measured ~14848
#   GLM-4.5-Air-UD-Q4_K_XL-00001-of-00002  estimated 56740    -- twice the card
####################

ESTIMATED_VL7B = {
    "id": "Qwen2.5-VL-7B-Instruct-Q4_K_M",
    "vram_footprint": {"bytes": 6909 * MIB, "source": "estimated"},
}


@pytest.mark.tier0
def test_an_estimated_footprint_is_not_deny_grade():
    """Reporting keeps it; anything that can REFUSE a load must not."""
    assert llamolotl_footprint(ESTIMATED_VL7B) == (6909 * MIB, "estimated")
    assert llamolotl_admission_footprint(ESTIMATED_VL7B) is None


@pytest.mark.tier0
@pytest.mark.parametrize("source", ["measured", "declared"])
def test_measured_and_declared_stay_deny_grade(source):
    """The guard must not swallow the two sources that ARE good enough, or the
    whole admission gate quietly stops working."""
    model = {"id": "m", "vram_footprint": {"bytes": 20 * GIB, "source": source}}
    assert llamolotl_admission_footprint(model) == (20 * GIB, source)


@pytest.mark.tier0
def test_a_preset_declaration_still_wins_over_an_estimate():
    """An operator's declaration is deny-grade even when the router's own
    published figure is only an estimate -- but the published entry is consulted
    first, so this pins that an estimate does not shadow the declaration into
    unknown."""
    model = {
        "id": "GLM-4.5-Air-q8_0",
        "vram_footprint": {"bytes": 131 * GIB, "source": "estimated"},
        "status": {"preset": SPLIT_PRESET},
    }
    assert llamolotl_admission_footprint(model) == (21504 * MIB, "declared")


@pytest.mark.tier0
def test_an_estimated_footprint_does_not_deny_a_load():
    """The behaviour that matters, end to end: an estimate must leave the load
    proceeding unguarded to the router's own check, never produce a 503.

    Without this, the shard entry above (56740 MiB estimated, more than twice
    the card) would be permanently unloadable through core on the strength of a
    file listing."""
    state = _state(raw={"m": ESTIMATED_VL7B})
    with patch.object(vram_admission, "VramBroker") as broker:
        broker.request_lease = AsyncMock()
        result = _run(state, "m")
    assert result.ok
    assert result.reason == "unknown-footprint"
    # The point is not just the verdict -- no lease may be sized off a guess.
    broker.request_lease.assert_not_awaited()


####################
# the admission helper
####################


@pytest.mark.tier0
def test_a_resident_model_never_touches_the_broker():
    """The overwhelmingly common case, and it must cost nothing: no lease call,
    no network, answered from the 3s-cached list."""
    state = _state(models={"m": {"id": "m", "status": "loaded"}})
    with patch.object(vram_admission.VramBroker, "request_lease", AsyncMock()) as lease:
        result = _run(state, "m")
    assert result.ok and result.reason == "already-resident"
    lease.assert_not_awaited()


@pytest.mark.tier0
def test_an_unknown_footprint_proceeds_unguarded_rather_than_guessing():
    """Refusing on a number we do not have is how self.llamolotl#36 got filed.
    Unknown steps aside and lets the router's own check decide."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "status": {"preset": "[m]\nn-cpu-moe = 40\n"}}},
    )
    with patch.object(vram_admission.VramBroker, "request_lease", AsyncMock()) as lease:
        result = _run(state, "m")
    assert result.ok and result.reason == "unknown-footprint"
    lease.assert_not_awaited()


@pytest.mark.tier0
def test_a_known_footprint_requests_exactly_that_many_bytes():
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "declared"}}},
    )
    granted = LeaseGranted(consumer_id="self.llamolotl", granted_bytes=21 * GIB, held_bytes=21 * GIB, reclaimed=[])
    with patch.object(vram_admission.VramLeases, "get", return_value=SimpleNamespace(priority=10)), patch.object(
        vram_admission.VramBroker, "request_lease", AsyncMock(return_value=granted)
    ) as lease:
        result = _run(state, "m")

    assert result.ok and result.reason == "granted"
    lease.assert_awaited_once_with("self.llamolotl", 21 * GIB, 10)


@pytest.mark.tier0
def test_the_registered_priority_is_used_not_a_literal():
    """Priority is authoritative from registration — hardcoding 10 here would
    silently diverge the moment the deployment's LLAMOLOTL_VRAM_LEASE_PRIORITY
    changes, and priority is what decides who gets reclaimed."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": GIB, "source": "measured"}}},
    )
    granted = LeaseGranted(consumer_id="self.llamolotl", granted_bytes=GIB, held_bytes=GIB, reclaimed=[])
    with patch.object(vram_admission.VramLeases, "get", return_value=SimpleNamespace(priority=7)), patch.object(
        vram_admission.VramBroker, "request_lease", AsyncMock(return_value=granted)
    ) as lease:
        _run(state, "m")
    assert lease.await_args.args[2] == 7


@pytest.mark.tier0
def test_a_shortfall_survives_structured():
    """The broker's requested/free/freeable breakdown is the whole diagnostic —
    flattening it into a bare 503 is the mistake self.ai#103 records elsewhere.

    Since self.ai#126 the shortfall is ADVISORY: the detail still survives, but
    it no longer stops the load."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "declared"}}},
    )
    denied = LeaseDenied(
        requested_bytes=21 * GIB, free_bytes=3 * GIB, freeable_bytes=4 * GIB, holders_asked=[]
    )
    with patch.object(vram_admission.VramLeases, "get", return_value=SimpleNamespace(priority=10)), patch.object(
        vram_admission.VramBroker, "request_lease", AsyncMock(return_value=denied)
    ):
        result = _run(state, "m")

    assert not result.denied and result.reason == "shortfall-advisory"
    assert result.detail["requested_bytes"] == 21 * GIB
    assert result.detail["free_bytes"] == 3 * GIB
    assert result.detail["model"] == "m"
    assert result.detail["footprint_source"] == "declared"


@pytest.mark.tier0
def test_a_broker_fault_proceeds_instead_of_failing_the_request():
    """Deliberate asymmetry: this exists to prevent an OOM on one model load.
    Trading that for an outage on every chat request would be a bad bargain."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": GIB, "source": "measured"}}},
    )
    with patch.object(vram_admission.VramLeases, "get", return_value=SimpleNamespace(priority=10)), patch.object(
        vram_admission.VramBroker, "request_lease", AsyncMock(side_effect=RuntimeError("yard-pg down"))
    ):
        result = _run(state, "m")
    assert result.ok and result.reason == "error"


@pytest.mark.tier0
def test_a_model_llamolotl_does_not_serve_is_skipped():
    """A cloud/remote model id reaches the same code path and must not be sized
    or refused — there is nothing local to reserve for it."""
    assert _run(_state(), "claude-opus-5").ok


@pytest.mark.tier0
def test_llamolotl_disabled_is_not_a_capacity_answer():
    state = _state(enabled=False)
    with patch.object(vram_admission.VramBroker, "request_lease", AsyncMock()) as lease:
        result = _run(state, "m")
    assert result.ok and result.reason == "disabled"
    lease.assert_not_awaited()


####################
# a load is a swap, not an addition (self.ai#114)
####################
#
# The live failure this closes: `POST /llamolotl/models/load` for
# GLM-4.5-Air-UD-Q4_K_XL returned 409 with freeable_bytes=243269632 (232 MiB)
# while llamolotl itself held 15 GiB of resident models it would have evicted to
# make room. The broker excludes the requester from its own reclaim set — right
# for a cooperative release, wrong for a consumer that frees its own VRAM — and
# llamolotl is also the highest-priority consumer, so nothing else was
# reclaimable either. Every switch to a larger model was refused permanently.


def _registered(priority=10, held=0, reserved=None, reserved_at=None):
    """A registration row as ``VramLeases.get`` returns it. ``held`` is the
    consumer's own measured hold — the thing the swap nets out."""
    return SimpleNamespace(
        priority=priority,
        held_bytes=held,
        reserved_bytes=reserved,
        reserved_at=reserved_at,
    )


@pytest.mark.tier0
def test_llamolotls_own_residents_are_netted_out_of_the_ask():
    """The load evicts them, so the card only has to supply the difference."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "declared"}}},
    )
    granted = LeaseGranted(consumer_id="self.llamolotl", granted_bytes=6 * GIB, held_bytes=21 * GIB, reclaimed=[])
    with patch.object(
        vram_admission.VramLeases, "get", return_value=_registered(held=15 * GIB)
    ), patch.object(vram_admission.VramBroker, "request_lease", AsyncMock(return_value=granted)) as lease:
        result = _run(state, "m")

    assert result.ok and result.reason == "granted"
    lease.assert_awaited_once_with("self.llamolotl", 6 * GIB, 10)


@pytest.mark.tier0
def test_the_live_409_would_now_be_admitted():
    """Replays the exact numbers observed on 2026-08-04: 21.25 GiB declared
    against llamolotl's own 15.06 GiB resident. Gross would be refused on a card
    with 8.5 GiB free; the net 7.22 GiB fits without touching another tenant."""
    footprint = 22280142848  # GLM-4.5-Air-UD-Q4_K_XL, declared
    self_held = 15062105984  # gemma-4 resident, as the registry reported it
    state = _state(
        models={"glm": {"id": "glm", "status": "unloaded"}},
        raw={"glm": {"id": "glm", "vram_footprint": {"bytes": footprint, "source": "declared"}}},
    )
    granted = LeaseGranted(
        consumer_id="self.llamolotl", granted_bytes=footprint - self_held, held_bytes=footprint, reclaimed=[]
    )
    with patch.object(
        vram_admission.VramLeases, "get", return_value=_registered(held=self_held)
    ), patch.object(vram_admission.VramBroker, "request_lease", AsyncMock(return_value=granted)) as lease:
        result = _run(state, "glm")

    assert result.ok
    asked = lease.await_args.args[1]
    assert asked == footprint - self_held
    # The point of the fix: the ask is now smaller than the 8543797248 bytes the
    # card actually had free, where the gross footprint was nearly three times it.
    assert asked < 8543797248 < footprint


@pytest.mark.tier0
def test_a_model_smaller_than_what_we_already_hold_needs_no_lease():
    """Swapping a 21 GiB resident for a 7 GiB one frees VRAM net. Asking the
    broker for capacity we are about to hand back would be a lie in the
    direction that denies other tenants room."""
    state = _state(
        models={"small": {"id": "small", "status": "unloaded"}},
        raw={"small": {"id": "small", "vram_footprint": {"bytes": 7 * GIB, "source": "measured"}}},
    )
    with patch.object(
        vram_admission.VramLeases, "get", return_value=_registered(held=21 * GIB)
    ), patch.object(vram_admission.VramBroker, "request_lease", AsyncMock()) as lease:
        result = _run(state, "small")

    assert result.ok and result.reason == "self-freeable"
    lease.assert_not_awaited()


@pytest.mark.tier0
def test_a_live_reservation_counts_as_self_freeable_too():
    """#76 keeps granted-but-unobserved VRAM in reserved_bytes. A swap that
    ignored it would ask for capacity twice inside one poll cycle."""
    import time

    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 20 * GIB, "source": "declared"}}},
    )
    granted = LeaseGranted(consumer_id="self.llamolotl", granted_bytes=8 * GIB, held_bytes=20 * GIB, reclaimed=[])
    registered = _registered(held=2 * GIB, reserved=12 * GIB, reserved_at=int(time.time()))
    with patch.object(vram_admission.VramLeases, "get", return_value=registered), patch.object(
        vram_admission.VramBroker, "request_lease", AsyncMock(return_value=granted)
    ) as lease:
        _run(state, "m")

    # max(measured 2, reserved 12) = 12 netted out, not the 2 GiB measurement.
    assert lease.await_args.args[1] == 8 * GIB


@pytest.mark.tier0
def test_a_shortfall_still_reports_the_gross_footprint():
    """``requested_bytes`` off the broker is the NET ask. On its own it matches
    no model's size, so a reader cannot tell whether the number is a bug. The
    gross footprint and what was netted out ride along."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "declared"}}},
    )
    denied = LeaseDenied(requested_bytes=6 * GIB, free_bytes=GIB, freeable_bytes=2 * GIB, holders_asked=[])
    with patch.object(
        vram_admission.VramLeases, "get", return_value=_registered(held=15 * GIB)
    ), patch.object(vram_admission.VramBroker, "request_lease", AsyncMock(return_value=denied)):
        result = _run(state, "m")

    assert not result.denied
    assert result.detail["requested_bytes"] == 6 * GIB
    assert result.detail["footprint_bytes"] == 21 * GIB
    assert result.detail["self_freeable_bytes"] == 15 * GIB


# ── self.ai#126: a shortfall is advisory, not fatal ──────────────────────


@pytest.mark.tier0
def test_a_shortfall_does_not_block_the_load():
    """The point of #126. This module refused a load llamolotl then performed
    successfully — 503 through the gate, 200 direct to the router, same card,
    same instant. It is the strictest of three memory models while holding the
    least information: it cannot express the router evicting its own residents,
    the expert-layer shed, or the child's --fit.

    Asserted on ``ok`` rather than only on ``reason`` because ``ok`` is what the
    three callers branch on to raise a 503."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "measured"}}},
    )
    denied = LeaseDenied(
        requested_bytes=21 * GIB, free_bytes=GIB, freeable_bytes=GIB, holders_asked=[]
    )
    with patch.object(
        vram_admission.VramLeases, "get", return_value=SimpleNamespace(priority=10)
    ), patch.object(vram_admission.VramBroker, "request_lease", AsyncMock(return_value=denied)):
        result = _run(state, "m")

    assert result.ok is True, "a shortfall must not stop the load (self.ai#126)"
    assert result.denied is False
    assert result.reason == "shortfall-advisory"


@pytest.mark.tier0
def test_a_shortfall_still_requests_the_lease():
    """The half of self.ai#107 that is NOT being given up. The lease request is
    what makes llamolotl a *taker* rather than only a giver, so the broker still
    reclaims from lower-priority holders on the grant path. #126 retires the
    refusal, not the request — if this call ever stops happening, a load that
    should reclaim 4.5 GiB from self.speak goes back to dying on CUDA OOM."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "declared"}}},
    )
    denied = LeaseDenied(
        requested_bytes=6 * GIB, free_bytes=GIB, freeable_bytes=2 * GIB, holders_asked=[]
    )
    spy = AsyncMock(return_value=denied)
    with patch.object(
        vram_admission.VramLeases, "get", return_value=_registered(held=15 * GIB)
    ), patch.object(vram_admission.VramBroker, "request_lease", spy):
        result = _run(state, "m")

    spy.assert_awaited_once()
    consumer, amount, priority = spy.await_args.args
    assert consumer == vram_admission.LLAMOLOTL_CONSUMER_ID
    # the NET ask (self.ai#114), not the gross 21 GiB
    assert amount == 6 * GIB
    assert result.reason == "shortfall-advisory"


@pytest.mark.tier0
def test_an_unregistered_llamolotl_still_short_circuits_before_sizing():
    """Registration precedes a lease request (R1); with no row there is no hold
    to net against, and inventing one would ask for the wrong amount."""
    state = _state(
        models={"m": {"id": "m", "status": "unloaded"}},
        raw={"m": {"id": "m", "vram_footprint": {"bytes": 21 * GIB, "source": "declared"}}},
    )
    with patch.object(vram_admission.VramLeases, "get", return_value=None), patch.object(
        vram_admission.VramBroker, "request_lease", AsyncMock()
    ) as lease:
        result = _run(state, "m")
    assert result.ok and result.reason == "skipped"
    lease.assert_not_awaited()
