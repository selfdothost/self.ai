"""Concrete self.sketch release transport (Color epic Phase 2b).

The THIRD concrete
:class:`~selfai_ui.utils.vram_broker.ReleaseTransport` implementation (mirroring
``vram_speak.py`` / ``vram_llamolotl.py``): the outbound leg the VRAM broker uses
when the target consumer is ``self.sketch`` — the ComfyUI image-generation pod on
the shared 4090. Everything above it (registry, release protocol, grant protocol,
HTTP surface) is transport-agnostic; this module is where core actually calls the
sibling self.sketch pod.

Wire contract (self.sketch's ComfyUI shim — same shape self.speak exposes)
-------------------------------------------------------------------------
``POST {control}/api/system/vram-release`` — ticket-scoped ``system:write``,
audience ``self.sketch`` — accepts ``{"target_bytes": int, "timeout_seconds":
float}`` and returns a response carrying ``freed_bytes: int`` and a ``status``
field (``released`` / ``partial`` / ``busy``). A ``busy`` answer is HTTP 409.

Response mapping (defensive — "no silent success", identical to speak/llamolotl)
-------------------------------------------------------------------------------
The adapter reads only the fields it understands and NEVER guesses at missing
ones. Every path that is not an unambiguous confirmed release resolves to
:data:`ReleaseOutcome.TIMEOUT` (true state now unknown → the broker marks the
consumer stale), never to a fabricated success. See ``vram_speak.py`` for the
full rationale; this is a byte-for-byte analogue with a different audience and
control-base config key.

Config guard
------------
self.sketch's control base is a **single string** on
``app.state.config.SKETCH_CONTROL_BASE_URL`` (fed by ``SKETCH_CONTROL_BASE_URL``
in the manifest, default ``http://self-sketch:8188``) — like self.speak's TTS
control base, NOT a list like llamolotl's. If it is missing/empty/whitespace-only
the transport resolves to a clean :data:`ReleaseOutcome.TIMEOUT` without
attempting any HTTP. It is deliberately its OWN env var (not the Phase-4
``COMFYUI_BASE_URL`` serving client), mirroring the serving-vs-control split.
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

# The self.sketch service identity — the registry consumer_id used for the
# self.sketch lease and the audience the ComfyUI shim validates inbound tickets
# against (its SERVICE_AUTH_AUDIENCE).
SKETCH_AUDIENCE = "self.sketch"

# Ticket scope for the release POST (self.sketch gates it on ``system:write``).
RELEASE_SCOPE = "system:write"

# Path on self.sketch's control port that honours a VRAM release request.
RELEASE_PATH = "/api/system/vram-release"

# Statuses the sibling returns that mean "a real release landed".
_CONFIRMED_STATUSES = frozenset({"released", "partial", "confirmed"})

# Statuses that mean "not now" — mapped to TIMEOUT, never a confirmed denial.
_BUSY_STATUSES = frozenset({"busy"})


ClientFactory = Callable[[float], httpx.AsyncClient]


class SketchReleaseTransport:
    """Concrete ``ReleaseTransport`` for the ``self.sketch`` consumer.

    Reads the control-port base URL from the app config at each call (so a
    runtime config change is honoured), mints a ``system:write`` service ticket
    for audience ``self.sketch``, POSTs the amount-based release request, and maps
    the reply defensively onto a :class:`ReleaseResponse`.

    ``app_state`` supplies ``.config.SKETCH_CONTROL_BASE_URL`` (a single string,
    NOT a list). ``registry`` supplies the consumer's pre-call held so a confirmed
    release can be written as an absolute new-held. ``client_factory`` is
    injectable so unit tests can drive the response mapping with no real
    network."""

    def __init__(
        self,
        app_state=None,
        registry=VramLeases,
        audience: str = SKETCH_AUDIENCE,
        client_factory: Optional[ClientFactory] = None,
    ):
        self._app_state = app_state
        self._registry = registry
        self._audience = audience
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    def _resolve_control_base(self) -> Optional[str]:
        """self.sketch's control-port base URL, or ``None`` if unconfigured (the
        config guard — an unconfigured transport must fail as a clean TIMEOUT,
        never a silent success).

        self.sketch's control base is the SINGLE STRING
        ``config.SKETCH_CONTROL_BASE_URL``. Treat a missing app_state/config, a
        ``None`` value, or a blank/whitespace-only string as unconfigured;
        otherwise ``.rstrip("/")`` the string."""
        app_state = self._app_state
        if app_state is None:
            return None
        cfg = getattr(app_state, "config", None)
        if cfg is None:
            return None
        value = getattr(cfg, "SKETCH_CONTROL_BASE_URL", None)
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text.rstrip("/")

    async def request_release(
        self, consumer_id: str, amount_bytes: int, timeout_seconds: float
    ) -> ReleaseResponse:
        base = self._resolve_control_base()
        if base is None:
            log.warning(
                "vram-sketch: no SKETCH_CONTROL_BASE_URL configured — cannot issue "
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
                "vram-sketch: could not mint %s ticket for %r (%r); "
                "resolving as timeout (unknown)",
                RELEASE_SCOPE,
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        endpoint = f"{base}{RELEASE_PATH}"
        body = {"target_bytes": int(amount_bytes), "timeout_seconds": float(timeout_seconds)}

        try:
            async with self._client_factory(timeout_seconds) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
        except Exception as e:
            # Connection refused, DNS, read timeout, protocol error — the true
            # state is unknown. Never a silent success.
            log.warning(
                "vram-sketch: release POST to %s for %r failed (%r); "
                "resolving as timeout (unknown)",
                endpoint,
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        return self._map_response(consumer_id, resp)

    def _map_response(self, consumer_id: str, resp) -> ReleaseResponse:
        """Map a self.sketch HTTP reply onto a :class:`ReleaseResponse`,
        defensively (see module docstring). Any shape that is not an
        unambiguous confirmed release resolves to TIMEOUT."""
        status_code = getattr(resp, "status_code", None)

        # 409 == "busy" per the sibling contract — a "not now", not a denial.
        if status_code == 409:
            log.info(
                "vram-sketch: %r replied 409/busy to release; timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            log.warning(
                "vram-sketch: %r release returned non-2xx (%r); timeout (unknown)",
                consumer_id,
                status_code,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        try:
            data = resp.json()
        except Exception as e:
            log.warning(
                "vram-sketch: %r release body was not decodable (%r); "
                "timeout (unknown)",
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        if not isinstance(data, dict):
            log.warning(
                "vram-sketch: %r release body was not an object (%r); "
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
                "vram-sketch: %r replied status=busy to release; timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        freed = data.get("freed_bytes")
        # A real confirmed release: recognisable status AND an integer freed.
        # bool is an int subclass — exclude it so a stray ``true`` isn't read as 1.
        if status_l in _CONFIRMED_STATUSES and isinstance(freed, int) and not isinstance(freed, bool):
            new_held = self._new_held_after(consumer_id, freed)
            log.info(
                "vram-sketch: %r confirmed release (status=%s, freed=%d); "
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
            "vram-sketch: %r release reply not a recognisable confirmation "
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
