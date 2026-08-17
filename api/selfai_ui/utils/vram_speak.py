"""Concrete self.speak release transport (cavekit-vram-speak-consumer R2).

The second concrete
:class:`~selfai_ui.utils.vram_broker.ReleaseTransport` implementation
(mirroring ``vram_llamolotl.py``): the outbound leg the VRAM broker uses when
the target consumer is ``self.speak``. Everything above it (registry, release
protocol, grant protocol, HTTP surface) is transport-agnostic; this module is
where core actually calls the sibling self.speak pod.

Wire contract (self.speak's now-real endpoints — ``self.speak!8``)
------------------------------------------------------------------
``POST {control}/api/system/vram-release`` — ticket-scoped ``system:write``,
audience ``self.speak`` — accepts ``{"target_bytes": int, "timeout_seconds":
float}`` and returns a response carrying ``freed_bytes: int`` and a ``status``
field (``released`` / ``partial`` / ``busy``). A ``busy`` answer is HTTP 409.

Response mapping (defensive — "no silent success", identical to llamolotl)
--------------------------------------------------------------------------
The adapter reads only the fields it understands and NEVER guesses at missing
ones. Every path that is not an unambiguous confirmed release resolves to
:data:`ReleaseOutcome.TIMEOUT` (true state now unknown → the broker marks the
consumer stale), never to a fabricated success:

  * **2xx + ``status`` in {released, partial, confirmed} + integer
    ``freed_bytes``** → :data:`ReleaseOutcome.CONFIRMED`, with ``new_held_bytes``
    derived as ``max(0, pre_call_held - freed_bytes)`` from the registry (the
    consumer's last self-report), or ``None`` when pre-held is unknown.
  * **HTTP 409 or ``status == "busy"``** → :data:`ReleaseOutcome.TIMEOUT` (busy
    means "no real answer this time", NOT a confirmed denial — the sibling
    contract has no explicit-denial status, so this transport never emits
    :data:`ReleaseOutcome.DENIED`).
  * **connection error / non-2xx / malformed or unrecognised body / a ticket
    that cannot be minted** → :data:`ReleaseOutcome.TIMEOUT`.

Config guard
------------
self.speak's control base is a **single string** already on
``app.state.config.TTS_CONTROL_BASE_URL`` (fed by ``AUDIO_TTS_CONTROL_BASE_URL``
in the manifest) — NOT a list like llamolotl's ``LLAMOLOTL_CONTROL_BASE_URLS``.
If it is missing/empty/whitespace-only, the transport resolves to a clean
:data:`ReleaseOutcome.TIMEOUT` without attempting any HTTP. **No new
control-URL env var is introduced** — the existing TTS control base is reused.
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

# The self.speak service identity — the same string ``routers/audio.py`` already
# mints self.speak service tickets with, and the registry consumer_id used for
# the self.speak lease.
SPEAK_AUDIENCE = "self.speak"

# The self.sketch (ComfyUI) service identity — the registry consumer_id and
# ticket audience for the self.sketch VRAM lease (Color Phase 2b). Defined here
# (matching SPEAK_AUDIENCE) so the consumer-aware dispatcher can route to it
# without importing vram_sketch; kept identical to SketchReleaseTransport's.
SKETCH_AUDIENCE = "self.sketch"

# Ticket scope for the release POST (self.speak gates it on ``system:write``).
RELEASE_SCOPE = "system:write"

# Path on self.speak's control port that honours a VRAM release request.
RELEASE_PATH = "/api/system/vram-release"

# Statuses the sibling returns that mean "a real release landed". ``partial`` is
# a smaller-than-asked but genuine release. ``confirmed`` is accepted defensively
# in case the sibling's status vocabulary uses it.
_CONFIRMED_STATUSES = frozenset({"released", "partial", "confirmed"})

# Statuses that mean "not now" — mapped to TIMEOUT, never a confirmed denial.
_BUSY_STATUSES = frozenset({"busy"})


ClientFactory = Callable[[float], httpx.AsyncClient]


class SpeakReleaseTransport:
    """Concrete ``ReleaseTransport`` for the ``self.speak`` consumer.

    Reads the control-port base URL from the app config at each call (so a
    runtime config change is honoured), mints a ``system:write`` service ticket
    for audience ``self.speak``, POSTs the amount-based release request, and maps
    the reply defensively onto a :class:`ReleaseResponse`.

    ``app_state`` supplies ``.config.TTS_CONTROL_BASE_URL`` (a single string, NOT
    a list). ``registry`` supplies the consumer's pre-call held so a confirmed
    release can be written as an absolute new-held. ``client_factory`` is
    injectable so unit tests can drive the response mapping with no real
    network."""

    def __init__(
        self,
        app_state=None,
        registry=VramLeases,
        audience: str = SPEAK_AUDIENCE,
        client_factory: Optional[ClientFactory] = None,
    ):
        self._app_state = app_state
        self._registry = registry
        self._audience = audience
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    def _resolve_control_base(self) -> Optional[str]:
        """self.speak's control-port base URL, or ``None`` if unconfigured (the
        config guard — an unconfigured transport must fail as a clean TIMEOUT,
        never a silent success).

        Unlike llamolotl (a ``LLAMOLOTL_CONTROL_BASE_URLS`` list), self.speak's
        control base is the SINGLE STRING ``config.TTS_CONTROL_BASE_URL``. Treat
        a missing app_state/config, a ``None`` value, or a blank/whitespace-only
        string as unconfigured; otherwise ``.rstrip("/")`` the string."""
        app_state = self._app_state
        if app_state is None:
            return None
        cfg = getattr(app_state, "config", None)
        if cfg is None:
            return None
        value = getattr(cfg, "TTS_CONTROL_BASE_URL", None)
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
        base = self._resolve_control_base()
        if base is None:
            log.warning(
                "vram-speak: no TTS_CONTROL_BASE_URL configured — cannot issue "
                "release to %r; resolving as timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        # Mint the ticket up front: if the shared secret is unconfigured this
        # raises, and we must NOT call the endpoint unauthenticated — a
        # can't-authenticate is a can't-know, i.e. a timeout, not a success.
        try:
            headers = {TICKET_HEADER: mint_service_ticket(self._audience, RELEASE_SCOPE)}
        except Exception as e:
            log.warning(
                "vram-speak: could not mint %s ticket for %r (%r); "
                "resolving as timeout (unknown)",
                RELEASE_SCOPE,
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        endpoint = f"{base}{RELEASE_PATH}"
        # ``force`` (admin e-stop) tells self.speak to skip its drain-wait and
        # unload BOTH engines (Kokoro + Chatterbox) immediately. Default False →
        # the cooperative body is byte-for-byte unchanged apart from the extra
        # explicit ``"force": false`` (the cooperative broker path never sets it).
        body = {
            "target_bytes": int(amount_bytes),
            "timeout_seconds": float(timeout_seconds),
            "force": bool(force),
        }

        try:
            async with self._client_factory(timeout_seconds) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
        except Exception as e:
            # Connection refused, DNS, read timeout, protocol error — the true
            # state is unknown. Never a silent success.
            log.warning(
                "vram-speak: release POST to %s for %r failed (%r); "
                "resolving as timeout (unknown)",
                endpoint,
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        return self._map_response(consumer_id, resp)

    def _map_response(self, consumer_id: str, resp) -> ReleaseResponse:
        """Map a self.speak HTTP reply onto a :class:`ReleaseResponse`,
        defensively (see module docstring). Any shape that is not an
        unambiguous confirmed release resolves to TIMEOUT."""
        status_code = getattr(resp, "status_code", None)

        # 409 == "busy" per the sibling contract — a "not now", not a denial.
        if status_code == 409:
            log.info(
                "vram-speak: %r replied 409/busy to release; timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            log.warning(
                "vram-speak: %r release returned non-2xx (%r); timeout (unknown)",
                consumer_id,
                status_code,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        try:
            data = resp.json()
        except Exception as e:
            log.warning(
                "vram-speak: %r release body was not decodable (%r); "
                "timeout (unknown)",
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        if not isinstance(data, dict):
            log.warning(
                "vram-speak: %r release body was not an object (%r); "
                "timeout (unknown)",
                consumer_id,
                type(data).__name__,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        status = data.get("status")
        status_l = status.lower() if isinstance(status, str) else None

        # An in-band busy (200 body but status=busy) — same as 409: not now.
        if status_l in _BUSY_STATUSES:
            log.info(
                "vram-speak: %r replied status=busy to release; timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        freed = data.get("freed_bytes")
        # A real confirmed release: recognisable status AND an integer freed.
        # bool is an int subclass — exclude it so a stray ``true`` isn't read as 1.
        if status_l in _CONFIRMED_STATUSES and isinstance(freed, int) and not isinstance(freed, bool):
            new_held = self._new_held_after(consumer_id, freed)
            log.info(
                "vram-speak: %r confirmed release (status=%s, freed=%d); "
                "new held=%r",
                consumer_id,
                status_l,
                freed,
                new_held,
            )
            return ReleaseResponse(
                outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=new_held
            )

        # Anything else — unrecognised status, missing/typed-wrong freed_bytes,
        # a shape we don't understand — is unknown, never a guessed success.
        log.warning(
            "vram-speak: %r release reply not a recognisable confirmation "
            "(status=%r, freed_bytes=%r); timeout (unknown)",
            consumer_id,
            status,
            freed,
        )
        return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

    def _new_held_after(self, consumer_id: str, freed_bytes: int) -> Optional[int]:
        """Absolute new held after a confirmed release =
        ``max(0, pre_call_held - freed_bytes)``. ``pre_call_held`` is the
        consumer's last self-report in the registry. If it is unknown, return
        ``None`` — the broker treats a confirmed-without-held as unresolvable and
        marks the consumer stale (never a silent success)."""
        try:
            pre = self._registry.get(consumer_id)
        except Exception:
            pre = None
        pre_held = getattr(pre, "held_bytes", None) if pre is not None else None
        if pre_held is None:
            return None
        return max(0, int(pre_held) - max(0, int(freed_bytes)))


class ConsumerAwareReleaseTransport:
    """A ``ReleaseTransport`` that owns the concrete per-consumer transports and
    routes each release by ``consumer_id`` (cavekit-vram-speak-consumer T-003 —
    the one production-critical piece; extended for self.sketch in Color Phase 2b).

    The broker (``VramBrokerImpl``) holds a SINGLE global transport
    (``set_transport``) and calls ``transport.request_release(consumer_id, ...)``
    with the target consumer already resolved — it does NOT route by
    ``consumer_id`` itself. llamolotl's transport is already installed and
    DEPLOYED as that single global; a naive ``set_transport(speak_transport)``
    would clobber it and send llamolotl's own releases to self.speak's endpoint,
    breaking live VRAM arbitration in production. This dispatcher keeps EVERY
    consumer reachable through the one global setter: ``self.speak`` → the speak
    transport; ``self.sketch`` → the sketch transport (when wired); anything else
    (i.e. ``self.llamolotl``) → the llamolotl transport, preserving the deployed
    llamolotl behaviour (its URL + audience + scope) exactly.

    ``sketch_transport`` is optional so existing wiring/tests that construct this
    with only speak + llamolotl keep working unchanged (self.sketch then simply
    falls through to the llamolotl branch — but production always passes it, so a
    real ``self.sketch`` release is never mis-routed). This is the sharp edge the
    Phase-2b broker map called out: the ``else`` fallthrough MUST NOT be allowed
    to swallow ``self.sketch``, hence the explicit branch above the fallthrough.

    It satisfies the same ``ReleaseTransport`` Protocol (one ``request_release``)
    and delegates unchanged. Lives in this lightweight module (not ``main.py``)
    so the routing is unit-testable without importing the whole app; ``main.py``
    instantiates and installs it at the lifespan seam."""

    def __init__(
        self,
        speak_transport,
        llamolotl_transport,
        sketch_transport=None,
        extra_transports=None,
    ):
        self._speak = speak_transport
        self._llamolotl = llamolotl_transport
        self._sketch = sketch_transport
        # {consumer_id: transport} checked BEFORE the named branches and the
        # fallthrough (self.ai#88). Every consumer added since llamolotl has
        # needed its own hardcoded branch here, and forgetting one does not fail
        # loudly — it silently routes that consumer's release to self.llamolotl's
        # endpoint, the exact mis-route this class was written to prevent. A map
        # lets a new consumer be wired at the seam in main.py with no edit here,
        # so the sharp edge stops growing a new corner per consumer.
        self._extra = dict(extra_transports or {})

    async def request_release(
        self,
        consumer_id: str,
        amount_bytes: int,
        timeout_seconds: float,
        force: bool = False,
    ) -> ReleaseResponse:
        transport = self._extra.get(consumer_id)
        if transport is not None:
            return await transport.request_release(
                consumer_id, amount_bytes, timeout_seconds, force=force
            )
        if consumer_id == SPEAK_AUDIENCE:
            return await self._speak.request_release(
                consumer_id, amount_bytes, timeout_seconds, force=force
            )
        if consumer_id == SKETCH_AUDIENCE and self._sketch is not None:
            return await self._sketch.request_release(
                consumer_id, amount_bytes, timeout_seconds, force=force
            )
        return await self._llamolotl.request_release(
            consumer_id, amount_bytes, timeout_seconds, force=force
        )


def install_speak_release_transport(app_state) -> SpeakReleaseTransport:
    """Build the concrete self.speak transport and install it as the VRAM
    broker's outbound release client.

    Installed via the broker's single global ``set_transport``. NOTE: llamolotl
    already installs its own transport onto that same single global; installing
    the speak transport here as the last writer would CLOBBER llamolotl. The
    correct wiring (see ``main._install_speak_release_transport``) installs a
    consumer-aware dispatcher owning BOTH concrete transports instead of calling
    this directly. This function stays a clean single-audience installer for
    completeness/tests; production wiring uses the dispatcher.

    Returns the installed transport (handy for tests / callers)."""
    from selfai_ui.utils.vram_broker import VramBroker

    transport = SpeakReleaseTransport(app_state=app_state)
    VramBroker.set_transport(transport)
    log.info("vram-speak: installed self.speak release transport on VramBroker")
    return transport
