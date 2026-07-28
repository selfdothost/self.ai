"""Concrete self.llamolotl release transport (T-016).

This is the one real :class:`~selfai_ui.utils.vram_broker.ReleaseTransport`
implementation: the outbound leg the VRAM broker (R2) uses when the target
consumer is ``self.llamolotl``. Everything above it (registry R1, release
protocol R2, grant protocol R3, HTTP surface) is transport-agnostic and unit
tested against fakes; this module is where core actually calls the sibling pod.

Wire contract (self.llamolotl's now-real endpoints)
---------------------------------------------------
``POST {control}/api/system/vram-release`` — ticket-scoped ``system:write`` —
accepts ``{"target_bytes": int, "timeout_seconds": float}`` and returns a
``VramReleaseResponse`` carrying ``freed_bytes: int`` and a ``status`` field
(``released`` / ``partial`` / ``busy``). A ``busy`` answer is HTTP 409.

Response mapping (defensive — see R2-AC3 "no silent success")
-------------------------------------------------------------
The adapter reads only the fields it understands and NEVER guesses at missing
ones. Every path that is not an unambiguous confirmed release resolves to
:data:`ReleaseOutcome.TIMEOUT` (true state now unknown → the broker marks the
consumer stale), never to a fabricated success:

  * **2xx + ``status`` in {released, partial, confirmed} + integer
    ``freed_bytes``** → :data:`ReleaseOutcome.CONFIRMED`. ``new_held_bytes`` is
    derived as ``max(0, pre_call_held - freed_bytes)`` where ``pre_call_held``
    is read from the registry (the consumer's last self-report). ``partial``
    is a real, smaller release — R2-AC2 explicitly allows freeing less than
    asked. If the pre-call held is unknown, ``new_held_bytes`` is left ``None``
    and the broker treats the confirmation as unresolvable → stale (never a
    silent success).
  * **HTTP 409 or ``status == "busy"``** → :data:`ReleaseOutcome.TIMEOUT`. Busy
    means "didn't get a real answer this time," NOT a confirmed denial — the
    sibling contract has no explicit-denial status, so this transport never
    emits :data:`ReleaseOutcome.DENIED`.
  * **connection error / non-2xx / malformed or unrecognised body / a ticket
    that cannot be minted** → :data:`ReleaseOutcome.TIMEOUT`. Never crashes the
    caller, never a silent success.

Config guard
------------
If ``LLAMOLOTL_CONTROL_BASE_URLS`` is unconfigured (no control port to call),
the transport resolves to a clean :data:`ReleaseOutcome.TIMEOUT` without
attempting any HTTP — matching the build site's explicit instruction that an
absent endpoint fails as a timeout, never a silent success.

NOTE (explicit, T-016 scope): installing this transport does **NOT** remove
``_ensure_llamolotl_model_ready`` from ``gpu_queue.py``. That dispatch-side
cutover (having the eval/dispatch path go through the broker instead of the
"unload everything, hope for the best" mitigation) is deliberately separate
follow-up work gated on live validation (T-018/T-019), not silently done here.
"""

import logging
import os
from typing import Callable, Optional

import httpx

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.vram_leases import VramLeases
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket
from selfai_ui.utils.vram_broker import ReleaseOutcome, ReleaseResponse

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# The self.llamolotl service identity — the same string used for the outbound
# ticket audience elsewhere (``gpu_queue.LLAMOLOTL_AUDIENCE`` /
# ``main._LLAMOLOTL_AUDIENCE``) and for the registry consumer_id (T-015).
# Defined locally to avoid importing the heavy gpu_queue module just for a str.
LLAMOLOTL_AUDIENCE = "self.llamolotl"

# Ticket scope for the release POST (self.llamolotl gates it on ``system:write``).
RELEASE_SCOPE = "system:write"

# Path on self.llamolotl's control port that honours a VRAM release request.
RELEASE_PATH = "/api/system/vram-release"

# Statuses the sibling returns that mean "a real release landed". ``partial`` is
# a smaller-than-asked but genuine release (R2-AC2). ``confirmed`` is accepted
# defensively in case the sibling's status vocabulary uses it.
_CONFIRMED_STATUSES = frozenset({"released", "partial", "confirmed"})

# Statuses that mean "not now" — mapped to TIMEOUT, never a confirmed denial.
_BUSY_STATUSES = frozenset({"busy"})

# --- R6 state-query (T-008) constants ---------------------------------------
# Ticket scope self.llamolotl gates its system READS on (routers/llamolotl.py:978
# — the same `system:read` scope the active-loras GET mints). The state poll is a
# read, so it mints this rather than the release path's `system:write`.
STATE_SCOPE = "system:read"

# Path on self.llamolotl's control port that self-reports its current VRAM state.
STATE_PATH = "/api/system/vram-state"

# Fields the state reply may carry the self-reported held figure under. Read
# defensively (first integer match wins); capacity is NEVER read — R6-AC2 relays
# only the held figure (capacity is fixed at register()). The live self.llamolotl
# endpoint (its VramStateResponse, api/state.py) returns `held_vram_bytes` — that
# is the authoritative field and MUST be listed first; the others are defensive
# fallbacks only. Verified against the deployed endpoint's real response shape
# (a live GET returned {"held_vram_bytes": ..., "total_capacity_bytes": ..., ...}).
_HELD_FIELDS = ("held_vram_bytes", "held_bytes", "held")

# Fields the state reply may carry the currently-loaded model id under (Decision 6
# / R4). self.llamolotl already computes this — `_check_inference_health()` returns
# the loaded model, surfaced today on `HealthResponse.loaded_model` (/health); the
# small producer task T-000-VS (self.llamolotl#9) adds `loaded_model` to the
# vram-state reply too. Listed first; the others are defensive fallbacks. Absent
# (→ None, fail-open) until that field lands. Read defensively: first non-empty
# string match wins, never fabricated.
_LOADED_MODEL_FIELDS = ("loaded_model", "loaded_model_id", "active_model")

# Per-read httpx timeout. Each read_held_bytes() is bounded by this so one slow
# consumer cannot stall the poller's cycle (R6-AC5, enforced at the poll layer).
STATE_READ_TIMEOUT_SECONDS = float(os.environ.get("LLAMOLOTL_VRAM_STATE_TIMEOUT_SECONDS", 10))


ClientFactory = Callable[[float], httpx.AsyncClient]


class LlamolotlReleaseTransport:
    """Concrete ``ReleaseTransport`` for the ``self.llamolotl`` consumer.

    Reads the control-port base URL from the app config at each call (so a
    runtime config change is honoured, matching ``gpu_queue``'s late-read
    style), mints a ``system:write`` service ticket, POSTs the amount-based
    release request, and maps the reply defensively onto a
    :class:`ReleaseResponse`.

    ``app_state`` supplies ``.config.LLAMOLOTL_CONTROL_BASE_URLS`` (the
    ``AppConfig`` returns the underlying list). ``registry`` supplies the
    consumer's pre-call held so a confirmed release can be written as an
    absolute new-held. ``client_factory`` is injectable so unit tests can drive
    the response mapping with no real network (T-016 tests)."""

    def __init__(
        self,
        app_state=None,
        registry=VramLeases,
        audience: str = LLAMOLOTL_AUDIENCE,
        client_factory: Optional[ClientFactory] = None,
    ):
        self._app_state = app_state
        self._registry = registry
        self._audience = audience
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    def _resolve_control_base(self) -> Optional[str]:
        """The first configured llamolotl control-port base URL, or ``None`` if
        unconfigured (the config guard — an unconfigured transport must fail as a
        clean TIMEOUT, never a silent success)."""
        app_state = self._app_state
        if app_state is None:
            return None
        cfg = getattr(app_state, "config", None)
        if cfg is None:
            return None
        try:
            urls = list(getattr(cfg, "LLAMOLOTL_CONTROL_BASE_URLS", None) or [])
        except Exception:
            return None
        if not urls:
            return None
        return str(urls[0]).rstrip("/")

    async def request_release(
        self, consumer_id: str, amount_bytes: int, timeout_seconds: float
    ) -> ReleaseResponse:
        base = self._resolve_control_base()
        if base is None:
            log.warning(
                "vram-llamolotl: no LLAMOLOTL_CONTROL_BASE_URLS configured — "
                "cannot issue release to %r; resolving as timeout (unknown)",
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
                "vram-llamolotl: could not mint %s ticket for %r (%r); "
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
                "vram-llamolotl: release POST to %s for %r failed (%r); "
                "resolving as timeout (unknown)",
                endpoint,
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        return self._map_response(consumer_id, resp)

    def _map_response(self, consumer_id: str, resp) -> ReleaseResponse:
        """Map a self.llamolotl HTTP reply onto a :class:`ReleaseResponse`,
        defensively (see module docstring). Any shape that is not an
        unambiguous confirmed release resolves to TIMEOUT."""
        status_code = getattr(resp, "status_code", None)

        # 409 == "busy" per the sibling contract — a "not now", not a denial.
        if status_code == 409:
            log.info(
                "vram-llamolotl: %r replied 409/busy to release; timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            log.warning(
                "vram-llamolotl: %r release returned non-2xx (%r); timeout (unknown)",
                consumer_id,
                status_code,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        try:
            data = resp.json()
        except Exception as e:
            log.warning(
                "vram-llamolotl: %r release body was not decodable (%r); "
                "timeout (unknown)",
                consumer_id,
                e,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        if not isinstance(data, dict):
            log.warning(
                "vram-llamolotl: %r release body was not an object (%r); "
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
                "vram-llamolotl: %r replied status=busy to release; timeout (unknown)",
                consumer_id,
            )
            return ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)

        freed = data.get("freed_bytes")
        # A real confirmed release: recognisable status AND an integer freed.
        # bool is an int subclass — exclude it so a stray ``true`` isn't read as 1.
        if status_l in _CONFIRMED_STATUSES and isinstance(freed, int) and not isinstance(freed, bool):
            new_held = self._new_held_after(consumer_id, freed)
            log.info(
                "vram-llamolotl: %r confirmed release (status=%s, freed=%d); "
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
            "vram-llamolotl: %r release reply not a recognisable confirmation "
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
        marks the consumer stale (never a silent success), which is the correct
        defensive outcome for the (practically-impossible) unregistered case."""
        try:
            pre = self._registry.get(consumer_id)
        except Exception:
            pre = None
        pre_held = getattr(pre, "held_bytes", None) if pre is not None else None
        if pre_held is None:
            return None
        return max(0, int(pre_held) - max(0, int(freed_bytes)))


class LlamolotlVramStateSource:
    """Read-only self.llamolotl VRAM state-query adapter (T-008, R6).

    The sibling of :class:`LlamolotlReleaseTransport`: same control port, same
    ticket-minting pattern, but a *read* rather than a *release*. The R6 poller
    (``utils/vram_poller.py``) calls :meth:`read_held_bytes` on an interval and
    relays whatever it returns into the registry via the EXISTING
    ``heartbeat(consumer_id, held_bytes)`` entry point — this adapter never
    writes the registry itself, it only reports what the consumer self-reports.

    ``GET {control}/api/system/vram-state`` — ticket-scoped ``system:read`` —
    returns the consumer's self-reported VRAM state. Only the HELD figure is
    read and returned; capacity is deliberately never read (R6-AC2: the poller
    relays ``held_bytes`` only, capacity is fixed at ``register()``).

    Defensive posture (mirrors ``_map_response``): every failure shape resolves
    to ``None`` = "no reading this cycle", NEVER a raise and NEVER a fabricated
    ``0``:

      * unconfigured control base (config guard) → ``None`` = "no source" (AC3)
      * unmintable ticket / connection error / non-2xx / undecodable or
        malformed body / no integer held field → ``None`` (AC4)

    Returning ``None`` (not a fabricated value) is what lets the poller simply
    skip the heartbeat for that cycle and leave R1's ``STALE_THRESHOLD_SECONDS``
    as the SOLE staleness detector — this adapter never marks a consumer stale
    (AC4, no second redundant failure path).

    ``app_state`` supplies ``.config.LLAMOLOTL_CONTROL_BASE_URLS`` (resolved via
    the exact same guard the release transport uses). ``client_factory`` is
    injectable so unit tests (T-011) drive the parse with no real network."""

    def __init__(
        self,
        app_state=None,
        audience: str = LLAMOLOTL_AUDIENCE,
        client_factory: Optional[ClientFactory] = None,
        timeout_seconds: float = STATE_READ_TIMEOUT_SECONDS,
    ):
        self._app_state = app_state
        self._audience = audience
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )
        self._timeout_seconds = timeout_seconds

    def _resolve_control_base(self) -> Optional[str]:
        """The first configured llamolotl control-port base URL, or ``None`` if
        unconfigured (the config guard — identical to
        :meth:`LlamolotlReleaseTransport._resolve_control_base`; an unconfigured
        source is a "no source" that the poller skips, never an error)."""
        app_state = self._app_state
        if app_state is None:
            return None
        cfg = getattr(app_state, "config", None)
        if cfg is None:
            return None
        try:
            urls = list(getattr(cfg, "LLAMOLOTL_CONTROL_BASE_URLS", None) or [])
        except Exception:
            return None
        if not urls:
            return None
        return str(urls[0]).rstrip("/")

    async def _fetch_state(self, consumer_id: str):
        """Do the single authenticated GET to self.llamolotl's vram-state endpoint
        and return the raw response, or ``None`` if it cannot be fetched this cycle
        (no control port configured, unmintable ticket, or a transport error). One
        fetch backs both ``read_held_bytes`` and ``read_state`` so a poll cycle
        makes ONE GET, not one per field."""
        base = self._resolve_control_base()
        if base is None:
            # No control port configured — "no source" (AC3). The poller skips.
            log.debug(
                "vram-llamolotl: no LLAMOLOTL_CONTROL_BASE_URLS configured — "
                "no state source for %r; skipping this cycle",
                consumer_id,
            )
            return None

        # Mint the read ticket up front: an unmintable ticket (shared secret
        # unset) is a can't-read, i.e. no reading, not a fabricated value.
        try:
            headers = {TICKET_HEADER: mint_service_ticket(self._audience, STATE_SCOPE)}
        except Exception as e:
            log.warning(
                "vram-llamolotl: could not mint %s ticket for %r state read (%r); "
                "no reading this cycle",
                STATE_SCOPE,
                consumer_id,
                e,
            )
            return None

        endpoint = f"{base}{STATE_PATH}"
        try:
            async with self._client_factory(self._timeout_seconds) as client:
                return await client.get(endpoint, headers=headers)
        except Exception as e:
            # Connection refused, DNS, read timeout — no reading this cycle.
            log.warning(
                "vram-llamolotl: state GET to %s for %r failed (%r); "
                "no reading this cycle",
                endpoint,
                consumer_id,
                e,
            )
            return None

    async def read_held_bytes(self, consumer_id: str) -> Optional[int]:
        """The consumer's self-reported held VRAM in bytes, or ``None`` if it
        cannot be read this cycle (see class docstring). Only ``held_bytes`` is
        ever returned — never capacity."""
        resp = await self._fetch_state(consumer_id)
        if resp is None:
            return None
        return self._parse_held(consumer_id, resp)

    async def read_state(self, consumer_id: str):
        """One GET, both figures: ``(held_bytes, loaded_model_id)`` (Decision 6 /
        R4, T-012). Either may be ``None`` independently — the same defensive parse
        as ``read_held_bytes``, so an absent/malformed field yields ``None`` (never
        a fabricated value) and the poller simply does not relay that field this
        cycle. ``loaded_model_id`` stays ``None`` until self.llamolotl's vram-state
        reply carries it (T-000-VS, self.llamolotl#9) — fail-open until then."""
        resp = await self._fetch_state(consumer_id)
        if resp is None:
            return (None, None)
        return (
            self._parse_held(consumer_id, resp),
            self._parse_loaded_model(consumer_id, resp),
        )

    def _parse_held(self, consumer_id: str, resp) -> Optional[int]:
        """Defensively read the self-reported held figure from the reply. Any
        shape that is not an unambiguous integer held resolves to ``None`` —
        never a guessed/fabricated value, never a raise (mirrors
        ``_map_response``'s posture)."""
        status_code = getattr(resp, "status_code", None)
        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            log.warning(
                "vram-llamolotl: %r state read returned non-2xx (%r); no reading",
                consumer_id,
                status_code,
            )
            return None

        try:
            data = resp.json()
        except Exception as e:
            log.warning(
                "vram-llamolotl: %r state body was not decodable (%r); no reading",
                consumer_id,
                e,
            )
            return None

        if not isinstance(data, dict):
            log.warning(
                "vram-llamolotl: %r state body was not an object (%r); no reading",
                consumer_id,
                type(data).__name__,
            )
            return None

        # First integer held field wins. bool is an int subclass — exclude it so
        # a stray ``true`` isn't read as 1 (same guard as _map_response's freed).
        for key in _HELD_FIELDS:
            held = data.get(key)
            if isinstance(held, int) and not isinstance(held, bool):
                log.debug(
                    "vram-llamolotl: %r self-reported held=%d bytes", consumer_id, held
                )
                return held

        log.warning(
            "vram-llamolotl: %r state reply carried no integer held field "
            "(keys=%r); no reading",
            consumer_id,
            list(data.keys()),
        )
        return None

    def _parse_loaded_model(self, consumer_id: str, resp) -> Optional[str]:
        """Defensively read the currently-loaded model id from the reply (Decision
        6 / R4). First non-empty string field wins; any other shape (missing,
        null, non-string, empty) yields ``None`` — never a fabricated id, never a
        raise (mirrors ``_parse_held``). ``None`` is the ordinary state until
        self.llamolotl's vram-state reply carries ``loaded_model`` (T-000-VS), so
        the checkpoint fail-opens (T-019) rather than mis-routing until then."""
        status_code = getattr(resp, "status_code", None)
        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        for key in _LOADED_MODEL_FIELDS:
            model = data.get(key)
            if isinstance(model, str) and model:
                log.debug(
                    "vram-llamolotl: %r self-reported loaded_model=%r", consumer_id, model
                )
                return model
        return None


def install_llamolotl_release_transport(app_state) -> LlamolotlReleaseTransport:
    """Build the concrete self.llamolotl transport and install it as the VRAM
    broker's outbound release client (T-016 startup wiring).

    Installed UNCONDITIONALLY: the config guard lives inside the transport
    (``_resolve_control_base`` → clean TIMEOUT when unconfigured), so an absent
    control URL degrades to a timeout rather than the broker's transport-less
    RuntimeError. ``VramBroker`` has a single global transport setter
    (``set_transport``); self.llamolotl is this phase's only lease client, so a
    per-consumer transport registry is unnecessary — installing this one global
    transport is correct.

    Returns the installed transport (handy for tests / callers)."""
    from selfai_ui.utils.vram_broker import VramBroker

    transport = LlamolotlReleaseTransport(app_state=app_state)
    VramBroker.set_transport(transport)
    log.info("vram-llamolotl: installed self.llamolotl release transport on VramBroker")
    return transport
