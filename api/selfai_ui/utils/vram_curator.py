"""Concrete self.curator VRAM transports — release and e-stop (self.ai#88).

The FOURTH concrete :class:`~selfai_ui.utils.vram_broker.ReleaseTransport`
(after ``vram_llamolotl.py`` / ``vram_speak.py`` / ``vram_sketch.py``): the
outbound leg the broker uses when the target consumer is ``self.curator`` — the
NeMo Curator data-curation pod on the shared 4090. self.curator was the last GPU
pod on that card the broker could not see, i.e. an invisible holder core would
over-grant against.

Two moods, one transport
------------------------
``request_release(..., force=False)`` is the polite ask, and self.curator is the
first consumer that answers it with a real **DENIED** rather than a
busy-that-reads-as-timeout. That difference is not cosmetic:

  * ``DENIED``  → the registry keeps self.curator's held KNOWN and its lease
    state ``steady``. Core still sees exactly how much of the card is spoken for
    and simply cannot have it right now.
  * ``TIMEOUT`` → the consumer is marked ``stale``: its true state is unknown, it
    stops counting as an eligible holder, and it becomes force-reap eligible.

A curation run that is merely busy is not in an unknown state, and treating it as
one would eventually get its pod deleted mid-pipeline. So a refusal self.curator
can explain is passed through as a refusal, with its explanation attached
(``ReleaseResponse.reason``, surfaced in the ``LeaseDenied`` breakdown).

``request_release(..., force=True)`` is Admin > System > Unload All Models. It
sets ``force`` on the same endpoint, which self.curator honours by terminating
the pipeline process group outright. There is no refusal path — an e-stop that a
consumer could decline would not be an e-stop.

Being an ordinary ``ReleaseTransport`` rather than a bespoke e-stop protocol is
what lets ``POST /vram-leases/release-all`` reach self.curator through the very
same ``force=True`` fan-out that already covers self.speak and self.sketch, so
the e-stop grows no per-consumer special case.

Response mapping (defensive — "no silent success")
--------------------------------------------------
Same posture as the sibling transports: read only the fields we understand, never
guess at missing ones, and resolve every shape that is not an unambiguous
confirmation or an explicit refusal to :data:`ReleaseOutcome.TIMEOUT`.

Config guard
------------
The control base is the single string ``app.state.config.CURATOR_CONTROL_BASE_URL``
(manifest ``CURATOR_CONTROL_BASE_URL``, default ``http://self-curator:8094``).
Deliberately NOT ``CURATOR_BASE_URLS[0]``: that is an admin-editable list of job
API endpoints, and aiming a mechanism that kills a running pipeline at whatever
happens to be first in it is not a decision to inherit. Missing/blank resolves to
a clean TIMEOUT with no HTTP attempted.
"""

import logging
from typing import Callable, Optional

import httpx

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.vram_leases import VramLeases
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket
from selfai_ui.utils.vram_broker import ReleaseOutcome, ReleaseResponse

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# The self.curator service identity — the registry consumer_id for its lease and
# the audience its control API validates inbound tickets against
# (SERVICE_AUTH_AUDIENCE on that side). Same string routers/curator.py and
# gpu_queue.py mint against; keep all three in sync.
CURATOR_AUDIENCE = "self.curator"

RELEASE_SCOPE = "system:write"
RELEASE_PATH = "/api/system/vram-release"

# Statuses that mean a real release landed.
_CONFIRMED_STATUSES = frozenset({"released", "partial", "confirmed"})
# An explicit, explained refusal — passed through as DENIED, not coerced to a
# timeout (see the module docstring on why the difference matters).
_DENIED_STATUSES = frozenset({"denied", "refused"})
# A bare "not now" with nothing to say. self.curator does not emit this, but a
# future/older build might; with no reason to record it is genuinely closer to an
# unknown than to a refusal, so it maps to TIMEOUT as the siblings do.
_BUSY_STATUSES = frozenset({"busy"})


ClientFactory = Callable[[float], httpx.AsyncClient]


class CuratorReleaseTransport:
    """``ReleaseTransport`` for the ``self.curator`` consumer.

    Polite release and forceful e-stop are the same endpoint with one flag
    difference, so they are one method — ``force`` is the flag. The safety
    property is not that the two live behind separate protocols but that nothing
    on the cooperative reclamation path ever passes ``force=True``: grants call
    ``request_release`` with the default, and only the admin-gated
    ``POST /vram-leases/release-all`` sets it.

    ``app_state`` supplies ``.config.CURATOR_CONTROL_BASE_URL``. ``registry``
    supplies the pre-call held so a confirmed release can be written as an
    absolute new-held. ``client_factory`` is injectable for tests."""

    def __init__(
        self,
        app_state=None,
        registry=VramLeases,
        audience: str = CURATOR_AUDIENCE,
        client_factory: Optional[ClientFactory] = None,
    ):
        self._app_state = app_state
        self._registry = registry
        self._audience = audience
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    def _resolve_control_base(self) -> Optional[str]:
        """self.curator's control-port base URL, or ``None`` if unconfigured."""
        app_state = self._app_state
        if app_state is None:
            return None
        cfg = getattr(app_state, "config", None)
        if cfg is None:
            return None
        value = getattr(cfg, "CURATOR_CONTROL_BASE_URL", None)
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text.rstrip("/")

    async def request_release(
        self,
        consumer_id: str,
        amount_bytes: int,
        timeout_seconds: float,
        force: bool = False,
    ) -> ReleaseResponse:
        """Ask self.curator to give VRAM back.

        ``force=False`` is the polite ask a grant's reclamation makes, and a
        running pipeline answers DENIED with a reason naming the job. ``force=True``
        is the admin e-stop: self.curator terminates the pipeline process group and
        does not refuse. The two share one method because that is the shape the
        broker's ``ReleaseTransport`` protocol has, and being a plain transport is
        what lets the system-wide e-stop reach curator through the same fan-out
        that already covers self.speak and self.sketch.

        The distinction still matters on the wire: a forced request names no
        amount, because an e-stop wants the whole card rather than a shortfall."""
        return await self._post(
            consumer_id,
            {
                # An e-stop wants the whole card, not an amount. 0 is what
                # self.curator reads as "everything" on the force path; the
                # figure is unused there and only the flag decides behaviour.
                "target_bytes": 0 if force else int(amount_bytes),
                "timeout_seconds": float(timeout_seconds),
                "force": bool(force),
            },
            timeout_seconds,
            force=force,
        )

    async def _post(
        self, consumer_id: str, body: dict, timeout_seconds: float, force: bool
    ) -> ReleaseResponse:
        what = "e-stop" if force else "release"
        base = self._resolve_control_base()
        if base is None:
            log.warning(
                "vram-curator: no CURATOR_CONTROL_BASE_URL configured — cannot issue "
                "%s to %r; resolving as timeout (unknown)",
                what,
                consumer_id,
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.TIMEOUT,
                reason="CURATOR_CONTROL_BASE_URL is not configured",
            )

        # Mint up front: a can't-authenticate is a can't-know, not a success. We
        # must never call this endpoint unauthenticated — on the force path it
        # kills a running pipeline.
        try:
            headers = {TICKET_HEADER: mint_service_ticket(self._audience, RELEASE_SCOPE)}
        except Exception as e:
            log.warning(
                "vram-curator: could not mint %s ticket for %r (%r); "
                "resolving as timeout (unknown)",
                RELEASE_SCOPE,
                consumer_id,
                e,
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.TIMEOUT, reason=f"could not mint a service ticket: {e!r}"
            )

        endpoint = f"{base}{RELEASE_PATH}"
        try:
            async with self._client_factory(timeout_seconds) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
        except Exception as e:
            log.warning(
                "vram-curator: %s POST to %s for %r failed (%r); "
                "resolving as timeout (unknown)",
                what,
                endpoint,
                consumer_id,
                e,
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.TIMEOUT, reason=f"{what} request failed: {e!r}"
            )

        return self._map_response(consumer_id, resp, force=force)

    def _map_response(self, consumer_id: str, resp, force: bool) -> ReleaseResponse:
        """Map self.curator's reply onto a :class:`ReleaseResponse`, defensively."""
        what = "e-stop" if force else "release"
        status_code = getattr(resp, "status_code", None)

        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            log.warning(
                "vram-curator: %r %s returned non-2xx (%r); timeout (unknown)",
                consumer_id,
                what,
                status_code,
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.TIMEOUT, reason=f"HTTP {status_code}"
            )

        try:
            data = resp.json()
        except Exception as e:
            log.warning(
                "vram-curator: %r %s body was not decodable (%r); timeout (unknown)",
                consumer_id,
                what,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT, reason="undecodable reply")

        if not isinstance(data, dict):
            log.warning(
                "vram-curator: %r %s body was not an object (%r); timeout (unknown)",
                consumer_id,
                what,
                type(data).__name__,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT, reason="malformed reply")

        status = data.get("status")
        status_l = status.lower() if isinstance(status, str) else None
        reason = data.get("reason") if isinstance(data.get("reason"), str) else None

        # An explained refusal stays a refusal. Held remains known, the consumer
        # stays `steady` — NOT stale, which would eventually make a merely-busy
        # curation pod force-reap eligible mid-pipeline.
        if status_l in _DENIED_STATUSES:
            log.info(
                "vram-curator: %r DENIED the %s — %s",
                consumer_id,
                what,
                reason or "no reason given",
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.DENIED,
                reason=reason or "self.curator refused without a stated reason",
            )

        if status_l in _BUSY_STATUSES:
            return ReleaseResponse(
                outcome=ReleaseOutcome.TIMEOUT, reason=reason or "consumer reported busy"
            )

        freed = data.get("freed_bytes")
        # bool is an int subclass — exclude it so a stray `true` isn't read as 1.
        if (
            status_l in _CONFIRMED_STATUSES
            and isinstance(freed, int)
            and not isinstance(freed, bool)
        ):
            new_held = self._new_held_after(consumer_id, freed, force=force)
            log.info(
                "vram-curator: %r confirmed %s (status=%s, freed=%d); new held=%r",
                consumer_id,
                what,
                status_l,
                freed,
                new_held,
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.CONFIRMED,
                new_held_bytes=new_held,
                reason=reason,
            )

        log.warning(
            "vram-curator: %r %s reply not a recognisable confirmation "
            "(status=%r, freed_bytes=%r); timeout (unknown)",
            consumer_id,
            what,
            status,
            freed,
        )
        return ReleaseResponse(
            outcome=ReleaseOutcome.TIMEOUT, reason=reason or "unrecognised reply shape"
        )

    def _new_held_after(
        self, consumer_id: str, freed_bytes: int, force: bool
    ) -> Optional[int]:
        """The consumer's absolute held after a confirmed answer.

        A confirmed **e-stop** is 0, not ``pre - freed``: self.curator asserts it
        terminated every pipeline, so it is holding nothing regardless of how
        stale our idea of its previous held was. Deriving from a stale ``pre``
        would leave a phantom hold on the registry that only an operator
        reconcile could clear — the opposite of what an e-stop is for.

        A confirmed **release** is ``max(0, pre - freed)``, and an unknown ``pre``
        yields ``None``, which the broker treats as unresolvable (marks stale)
        rather than as a silent success."""
        if force:
            return 0
        try:
            pre = self._registry.get(consumer_id)
        except Exception:
            pre = None
        pre_held = getattr(pre, "held_bytes", None) if pre is not None else None
        if pre_held is None:
            return None
        return max(0, int(pre_held) - max(0, int(freed_bytes)))
