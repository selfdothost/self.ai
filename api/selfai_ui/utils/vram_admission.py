"""Ask the broker for VRAM before llamolotl loads a model (self.ai#107).

The broker has always been able to reclaim from lower-priority holders, and
self.llamolotl has always been registered as the highest-priority consumer
(10, against self.speak's 5 and self.sketch's 3). What was missing was anybody
calling it on the load path: ``gpu_queue`` dispatched straight to
``_ensure_llamolotl_model_ready()``, the admin load route posted straight to
the router, and chat let the router autoload. So llamolotl was a *giver* —
``_install_llamolotl_release_transport`` could ask it to yield — and never a
taker. The priorities were decorative for exactly the case they exist for, and
a load that should have reclaimed 4.5 GiB from self.speak instead allocated
against whatever was left and died on CUDA OOM.

This module is the missing caller. One helper, used by every path core can
see, so the three cannot drift apart.

Three principles, each of which is a decision rather than an implementation
detail:

**Unknown never blocks.** If no footprint is published, we do not guess and we
do not refuse — we step aside and let the load proceed exactly as it does
today, under the router's own admission check. Refusing on a number we do not
have is how self.llamolotl#36 got filed.

An ``estimated`` footprint counts as *not having the number*. Since
self.llamolotl!46 the router publishes a ``source`` alongside the bytes, and a
file-size-times-overhead guess is exactly what #36 was about — measured live, it
runs 33% over on Qwen2.5-VL-7B and puts one shard entry at 56740 MiB, more than
twice the card. So ``llamolotl_admission_footprint`` is what this module asks
for; the reporting path keeps the full picture.

**Already-resident costs nothing.** The common case is a model that is already
loaded, and it is answered from the 3s-cached model list with no broker call
and no network of its own.

**A shortfall is structured, and advisory** (self.ai#126). The broker returns
requested / free / freeable plus the per-holder breakdown, and that survives to
the caller intact — a user told "GPU busy" with no numbers cannot tell a real
capacity limit from a bug. But it no longer *stops* the load.

This module was measured refusing a load that llamolotl then performed
successfully: same model, same card, same instant, 503 through the gate and 200
in 5.4 s direct to the router. It is a third memory model and the strictest of
the three, while holding the least information — it cannot express the router's
eviction of its own residents, the expert-layer shed, or the child's ``--fit``,
all of which absorb a deficit it would refuse on. It was also the only hard stop
in a function that proceeds on every other uncertainty, which is backwards:
blocking hardest exactly where the information is best.

The lease request itself still runs, and that is the part worth keeping — it is
what makes llamolotl a *taker* so the broker reclaims from lower-priority
holders on the grant path. That was this module's actual purpose; the refusal
never was.

**A load is a swap.** llamolotl's own resident models are freeable by llamolotl
— its router evicts them to make room — so we ask the broker only for what the
card must supply on top of that (self.ai#114). Asking for the gross footprint
made every model switch impossible once the card filled, which is the opposite
of the OOM this module exists to prevent.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from selfai_ui.models.vram_leases import VramLeases, effective_held
from selfai_ui.utils.model_vram import llamolotl_admission_footprint
from selfai_ui.utils.vram_broker import LeaseDenied, VramBroker

log = logging.getLogger(__name__)

LLAMOLOTL_CONSUMER_ID = "self.llamolotl"


@dataclass
class VramAdmission:
    """What happened when we asked for room for a model.

    ``ok`` is deliberately true for both "granted" and "skipped": callers act on
    a *denial*, and every other outcome means carry on. Keeping the reason
    around makes the logs answer "why did this load not get a lease?" without a
    second investigation.

    Since self.ai#126 **no path sets ``ok=False``** — a shortfall reports
    ``shortfall-advisory`` and the load proceeds. The flag and the callers'
    handling are kept rather than deleted because they are the enforcement
    mechanism, not dead weight: re-arming is one return statement in
    :func:`ensure_llamolotl_vram`, and the three callers 503 again the moment
    anything sets it.
    """

    ok: bool = True
    # granted | already-resident | self-freeable | unknown-footprint | disabled
    # | skipped | shortfall-advisory | error
    # ("denied" is retired — see the class docstring and self.ai#126)
    reason: str = "skipped"
    detail: Optional[dict] = field(default=None)

    @property
    def denied(self) -> bool:
        return not self.ok


def _resident(models_map: dict, model_id: str) -> bool:
    entry = models_map.get(model_id) or {}
    return entry.get("status") in ("loaded", "loading")


async def ensure_llamolotl_vram(app_state, model_id: str) -> VramAdmission:
    """Reserve VRAM for ``model_id`` before llamolotl loads it.

    ``app_state`` is the FastAPI ``app.state`` (routers pass ``request.app.state``,
    the GPU queue passes the module-level state it already holds) — the helper
    takes state rather than a Request so every caller can share it.

    Returns a :class:`VramAdmission`; only ``denied`` should stop a caller.
    Never raises — a broker or registry fault degrades to today's behaviour
    (proceed unguarded) rather than taking chat down. That asymmetry is
    deliberate: the failure this exists to prevent is an OOM on one model load,
    and trading it for an outage on every request would be a bad bargain.
    """
    if not model_id:
        return VramAdmission(reason="skipped")

    if app_state is None:
        return VramAdmission(reason="skipped")

    try:
        state = app_state
        if not getattr(state.config, "ENABLE_LLAMOLOTL_API", False):
            return VramAdmission(reason="disabled")

        # Cheap path first: the 3s-cached list core already keeps. A resident
        # model needs no lease — llamolotl's hold for it is already counted.
        models_map = getattr(state, "LLAMOLOTL_MODELS", None) or {}
        if _resident(models_map, model_id):
            return VramAdmission(reason="already-resident")

        raw = getattr(state, "LLAMOLOTL_RAW_MODELS", None) or {}
        entry = raw.get(model_id)
        if entry is None:
            # Not a model this llamolotl serves (a cloud/remote config), or the
            # list has not been fetched yet. Either way there is nothing to size.
            return VramAdmission(reason="unknown-footprint")

        found = llamolotl_admission_footprint(entry)
        if found is None:
            log.info(
                "vram-admission: %r publishes no footprint — proceeding unguarded "
                "(the router's own check still applies)",
                model_id,
            )
            return VramAdmission(reason="unknown-footprint")
        amount_bytes, source = found

        registered = VramLeases.get(LLAMOLOTL_CONSUMER_ID)
        if registered is None:
            # Registration precedes a lease request (R1). Unregistered is a
            # deployment state, not a capacity answer — proceed.
            return VramAdmission(reason="skipped")

        # A load is a SWAP, not an addition (self.ai#114). The broker's freeable
        # ceiling excludes the requester's own hold, on the sound reasoning that
        # asking a consumer to release so it can be granted its own VRAM back is
        # nonsensical for a *cooperative* release. But llamolotl does not need to
        # be asked: its router evicts its own resident models to make room, so
        # every byte it currently holds is the most freeable VRAM on the card.
        # Asking for the gross footprint therefore denied every swap the moment
        # free < footprint — permanently, since llamolotl is also the highest
        # priority consumer and nothing else is reclaimable by it either.
        #
        # So we ask for the NET: what the card must give us on top of what we
        # will free ourselves. Under-reserves if the router evicts only part of
        # its residents (it evicts LRU until its own fit check passes, which may
        # be less than everything) — that is a race against another tenant
        # grabbing the difference, not a correctness break, and it is strictly
        # better than the permanent denial it replaces. Chosen over teaching the
        # broker a self-swap case so record_grant's accumulate semantics and
        # every other consumer's accounting stay untouched (self.ai#74-80).
        self_freeable = effective_held(registered)
        request_bytes = max(0, amount_bytes - self_freeable)
        if request_bytes == 0:
            log.info(
                "vram-admission: %r (%d bytes, source=%s) fits inside llamolotl's "
                "own %d held — self-freeable, no lease needed",
                model_id,
                amount_bytes,
                source,
                self_freeable,
            )
            return VramAdmission(reason="self-freeable")

        result = await VramBroker.request_lease(
            LLAMOLOTL_CONSUMER_ID, request_bytes, registered.priority
        )
    except Exception as e:
        log.warning(
            "vram-admission: lease request for %r failed (%r); proceeding unguarded",
            model_id,
            e,
        )
        return VramAdmission(reason="error")

    if not isinstance(result, LeaseDenied):
        # LeaseGranted. The hold is already written to the registry before this
        # returns (R3-AC6), so there is nothing further to record here.
        log.info(
            "vram-admission: granted %d of %d bytes for %r (%d self-freeable, "
            "footprint source=%s)",
            request_bytes,
            amount_bytes,
            model_id,
            self_freeable,
            source,
        )
        return VramAdmission(reason="granted")

    # LeaseDenied — structured, and it survives to the caller intact.
    #
    # ``requested_bytes`` off the broker is the NET ask, which on its own reads
    # like the model is smaller than it is. Carry the gross footprint and what we
    # netted out beside it so a reader can reconstruct the arithmetic rather than
    # being handed a number that does not match any model's size.
    detail = result.model_dump()
    detail["model"] = model_id
    detail["footprint_source"] = source
    detail["footprint_bytes"] = amount_bytes
    detail["self_freeable_bytes"] = self_freeable
    # ADVISORY, not fatal (self.ai#126). A shortfall is logged and reported, and
    # the load proceeds.
    #
    # This module refused a load that llamolotl then performed successfully —
    # reproduced side by side on the same card at the same instant: 503 through
    # the gate, 200 in 5.4s direct to the router. The gate was the only thing
    # saying no.
    #
    # It is a third memory model, and the strictest of the three, while having
    # the least information. Beneath it the router evicts its own residents and
    # then lets the load proceed, and the child's ``--fit`` reduces layers to
    # what the card actually has. Neither of those is expressible in a lease
    # request, so a deficit the layers below would absorb became a 503 here.
    # ``try_shed_to_fit`` (self.llamolotl#29) is a fourth recourse this
    # arithmetic cannot see.
    #
    # It was also the ONLY hard stop in a function that proceeds on every other
    # uncertainty — unknown footprint, estimated footprint, unregistered
    # consumer, any exception. Blocking hardest exactly where the information is
    # best is backwards.
    #
    # The lease REQUEST above still runs and still matters: it is what makes
    # llamolotl a taker rather than only a giver, so the broker still reclaims
    # from lower-priority holders on the grant path. That was self.ai#107's
    # actual purpose; the refusal was never the point of it.
    #
    # What still protects the card: the router's own VRAM-aware eviction, the
    # pin fallback (self.ai#128), the shed, and the child's ``--fit``. If this
    # needs re-arming, this return is the one line to change — the three callers
    # still handle ``denied`` and will 503 again the moment anything sets it.
    log.warning(
        "vram-admission: SHORTFALL %d bytes (net of %d self-freeable, %d gross, "
        "source=%s) for %r — PROCEEDING; llamolotl's own eviction and the child's "
        "--fit are the guard. Detail: %s",
        request_bytes,
        self_freeable,
        amount_bytes,
        source,
        model_id,
        detail,
    )
    return VramAdmission(reason="shortfall-advisory", detail=detail)
