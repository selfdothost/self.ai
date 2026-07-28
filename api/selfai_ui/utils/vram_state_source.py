"""Generic single-control-base VRAM state source (R6 poller gap-fix, Color 2b).

A read-only poller source for consumers whose control base is a SINGLE STRING on
``app.state.config`` — self.speak (``TTS_CONTROL_BASE_URL``) and self.sketch
(``SKETCH_CONTROL_BASE_URL``). It is the sibling of
:class:`~selfai_ui.utils.vram_llamolotl.LlamolotlVramStateSource`, which reads a
LIST (``LLAMOLOTL_CONTROL_BASE_URLS``); same wire contract, same defensive parse.
The R6 poller (``utils/vram_poller.py``) calls :meth:`read_held_bytes` on an
interval and relays whatever it returns into the registry via the EXISTING
``heartbeat(consumer_id, held_bytes)`` entry point — this adapter never writes the
registry itself, it only reports what the consumer self-reports.

WHY THIS EXISTS
---------------
self.speak and self.sketch are registered VRAM consumers, but the poller only
polled self.llamolotl. Their vram-state endpoint is PULL-based (they *serve* ``GET
/api/system/vram-state`` for core to read; they do not push heartbeats), so with
no state source here their real held VRAM was NEVER relayed into the registry.
Their held then stayed at the registration value (0) and aged to ``stale`` — an
INVISIBLE holder core would over-grant the shared 4090 against (the exact
over-grant-→-OOM hazard the self.speak GPU deploy was gated on). This source lets
the poller read them the same way it reads llamolotl.

Wire contract
-------------
``GET {control}/api/system/vram-state`` — ticket-scoped ``system:read`` — returns
the consumer's self-reported VRAM state. Only the HELD figure is read (capacity is
never read; R6-AC2 relays held only, capacity is fixed at ``register()``). The
consumer's tri-state body maps naturally with no need to read ``status``:

  * ``status=ok``          → integer held      → relayed as-is
  * ``status=unreachable`` → held is ``null``  → no integer → ``None`` → poller
    SKIPS this cycle, leaving the prior held + its age intact (never fabricates 0)
  * ``status=no_gpu``      → held is ``0`` (a real int) → relayed as 0 (CPU mode)

Defensive posture (mirrors ``LlamolotlVramStateSource``): every failure shape —
unconfigured control base, unmintable ticket, connection error, non-2xx,
undecodable/malformed body, no integer held field — resolves to ``None`` = "no
reading this cycle", NEVER a raise and NEVER a fabricated value. Returning ``None``
is what lets the poller skip the heartbeat and leave ``STALE_THRESHOLD_SECONDS`` as
the SOLE staleness detector (R6-AC4).
"""

import logging
import os
from typing import Callable, Optional

import httpx

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# Ticket scope the consumer gates its vram-state READ on (same `system:read` the
# release path pairs with `system:write`). A poll is a read, so it mints this.
STATE_SCOPE = "system:read"

# Path the consumer self-reports its current VRAM state on.
STATE_PATH = "/api/system/vram-state"

# Fields the reply may carry the self-reported held figure under. Read defensively
# (first integer match wins); capacity is NEVER read. `held_vram_bytes` matches
# llamolotl's field; `held_bytes`/`held` cover self.speak / self.sketch's shim.
_HELD_FIELDS = ("held_vram_bytes", "held_bytes", "held")

# Per-read httpx timeout so one slow consumer cannot stall the poll cycle
# (the poller also isolates each read via gather(return_exceptions=True)).
STATE_READ_TIMEOUT_SECONDS = float(os.environ.get("VRAM_STATE_READ_TIMEOUT_SECONDS", 10))


ClientFactory = Callable[[float], httpx.AsyncClient]


class ControlBaseVramStateSource:
    """Read-only VRAM state source for a consumer whose control base is a single
    string config attribute. Parameterized by that attribute name and the ticket
    audience; otherwise a defensive analogue of ``LlamolotlVramStateSource``.

    ``app_state`` supplies ``.config.<config_attr>`` (a single string). Only
    :meth:`read_held_bytes` is implemented — the poller's ``read_state`` fallback
    yields ``(held, None)`` for these consumers, which is correct: only
    self.llamolotl's loaded-model datum feeds the eval-coexist route.
    ``client_factory`` is injectable so unit tests drive the parse with no network.
    """

    def __init__(
        self,
        app_state=None,
        config_attr: str = "",
        audience: str = "",
        client_factory: Optional[ClientFactory] = None,
        timeout_seconds: float = STATE_READ_TIMEOUT_SECONDS,
    ):
        self._app_state = app_state
        self._config_attr = config_attr
        self._audience = audience
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )
        self._timeout_seconds = timeout_seconds

    def _resolve_control_base(self) -> Optional[str]:
        """The consumer's single-string control base, or ``None`` if unconfigured
        (the config guard — an unconfigured source is a "no source" the poller
        skips, never an error). Missing app_state/config, a ``None`` value, or a
        blank/whitespace-only string are all unconfigured; otherwise
        ``.rstrip('/')``."""
        app_state = self._app_state
        if app_state is None:
            return None
        cfg = getattr(app_state, "config", None)
        if cfg is None:
            return None
        value = getattr(cfg, self._config_attr, None)
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text.rstrip("/")

    async def read_held_bytes(self, consumer_id: str) -> Optional[int]:
        """The consumer's self-reported held VRAM in bytes, or ``None`` if it
        cannot be read this cycle (see class docstring). Only ``held`` is ever
        returned — never capacity."""
        base = self._resolve_control_base()
        if base is None:
            log.debug(
                "vram-state: no %s configured — no source for %r; skipping this cycle",
                self._config_attr,
                consumer_id,
            )
            return None

        # Mint the read ticket up front: an unmintable ticket (shared secret unset)
        # is a can't-read, i.e. no reading, not a fabricated value.
        try:
            headers = {TICKET_HEADER: mint_service_ticket(self._audience, STATE_SCOPE)}
        except Exception as e:
            log.warning(
                "vram-state: could not mint %s ticket for %r (%r); no reading this cycle",
                STATE_SCOPE,
                consumer_id,
                e,
            )
            return None

        endpoint = f"{base}{STATE_PATH}"
        try:
            async with self._client_factory(self._timeout_seconds) as client:
                resp = await client.get(endpoint, headers=headers)
        except Exception as e:
            # Connection refused, DNS, read timeout — no reading this cycle.
            log.warning(
                "vram-state: state GET to %s for %r failed (%r); no reading this cycle",
                endpoint,
                consumer_id,
                e,
            )
            return None

        return self._parse_held(consumer_id, resp)

    def _parse_held(self, consumer_id: str, resp) -> Optional[int]:
        """Defensively read the self-reported held figure. Any shape that is not an
        unambiguous integer held resolves to ``None`` — never a guessed value,
        never a raise (mirrors ``LlamolotlVramStateSource._parse_held``)."""
        status_code = getattr(resp, "status_code", None)
        if not isinstance(status_code, int) or not (200 <= status_code < 300):
            log.warning(
                "vram-state: %r state read returned non-2xx (%r); no reading",
                consumer_id,
                status_code,
            )
            return None

        try:
            data = resp.json()
        except Exception as e:
            log.warning(
                "vram-state: %r state body was not decodable (%r); no reading",
                consumer_id,
                e,
            )
            return None

        if not isinstance(data, dict):
            log.warning(
                "vram-state: %r state body was not an object (%r); no reading",
                consumer_id,
                type(data).__name__,
            )
            return None

        # First integer held field wins. bool is an int subclass — exclude it so a
        # stray ``true`` isn't read as 1.
        for key in _HELD_FIELDS:
            held = data.get(key)
            if isinstance(held, int) and not isinstance(held, bool):
                log.debug("vram-state: %r self-reported held=%d bytes", consumer_id, held)
                return held

        log.warning(
            "vram-state: %r state reply carried no integer held field (keys=%r); "
            "no reading",
            consumer_id,
            list(data.keys()),
        )
        return None
