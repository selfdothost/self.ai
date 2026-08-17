"""Consumer VRAM state poller (T-009, R6) — the background loop that closes the
gap live validation found: R1's ``held`` was only ever updated *through the
lease protocol itself* (register / heartbeat / confirmed-release / grant), never
from a consumer's *actual* VRAM usage. A normal chat request that loads a model
with no lease negotiation moved real VRAM the registry never saw. This poller
periodically reads each configured consumer's self-reported VRAM state and
relays the HELD figure into the registry via the EXISTING
``heartbeat(consumer_id, held_bytes)`` entry point — NOT a new write path.

Loop shape (mirrors ``gpu_queue.process_gpu_queue_v2`` — gpu_queue.py:884)
--------------------------------------------------------------------------
``while True:`` → run one cycle in a ``try`` → a broad ``except`` that logs and
NEVER breaks the loop → ``await asyncio.sleep(interval)``. One cycle's failure
can never kill the poller (R6-AC1), exactly like the GPU dispatcher.

Deliberate departure from ``process_gpu_queue_v2``: NO ``RedisLock``.
--------------------------------------------------------------------
``process_gpu_queue_v2`` is Redis-locked because a double-*dispatch* across
nodes would do duplicate work. This loop is deliberately NOT locked: a redundant
heartbeat from two nodes is harmless under R1-AC4's trust rule (a heartbeat only
writes the consumer's own self-reported held — two nodes writing the same
self-report is idempotent), and the kit's Out-of-Scope explicitly declines to
guard poll/release ordering. Skipping the lock is intentional, not an omission.

What a cycle does (and deliberately does NOT do)
------------------------------------------------
  * enumerate registered consumers via ``VramLeases.get_all()``;
  * for each, look up its state source in the ``{consumer_id: state_source}``
    map supplied at construction — a consumer ABSENT from the map has no
    configured state-query endpoint and is SKIPPED (R6-AC3, mirroring R5's
    opt-in);
  * poll the mapped consumers CONCURRENTLY via
    ``asyncio.gather(*tasks, return_exceptions=True)`` so one slow or failing
    consumer neither blocks nor delays the others in the same cycle (R6-AC5) and
    a raised exception is isolated to that consumer (R6-AC1);
  * for each non-``None`` held returned, call
    ``VramLeases.heartbeat(consumer_id, held_bytes)`` — the EXISTING R1 entry
    point, only ``held_bytes``, never capacity (R6-AC2);
  * a ``None``/failed read is simply NOT heartbeated this cycle and the poller
    does NOTHING else — it NEVER calls ``mark_stale``, leaving
    ``STALE_THRESHOLD_SECONDS`` as the sole staleness mechanism (R6-AC4).
"""

import asyncio
import logging
import os

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.vram_leases import VramLeases

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


# Poll cadence (R6-AC6): a named, documented module constant. 30s deliberately
# matches ``gpu_queue.process_gpu_queue_v2``'s ``POLL_INTERVAL`` (gpu_queue.py:884
# — "Polls every 30s") so the VRAM view refreshes on the same beat the GPU
# dispatcher runs on. Overridable via VRAM_POLL_INTERVAL_SECONDS.
VRAM_POLL_INTERVAL_SECONDS = int(os.environ.get("VRAM_POLL_INTERVAL_SECONDS", 30))


async def _poll_consumer(consumer_id, source, registry) -> None:
    """Poll ONE consumer and relay its self-reported state. Two figures, each
    relayed INDEPENDENTLY via its own existing/parallel write path:
      * ``held`` → the EXISTING R1 ``heartbeat`` (held_bytes ONLY, never capacity;
        R6-AC2);
      * ``loaded_model`` → ``record_loaded_model`` (Decision 6 / R4, T-012) so
        core's chat checkpoint can route an eval-window request to the loaded
        model instead of forcing a competing load.
    A ``None`` for either figure (unreachable / unconfigured / malformed / a field
    not yet emitted) simply does not relay THAT figure this cycle — the poller does
    nothing else (never ``mark_stale``; R6-AC4), leaving the prior value and its
    age intact for the checkpoint's staleness judgement (T-019). Reads are
    defensive and return ``None`` rather than raising; a raise is still isolated to
    this consumer by the surrounding ``gather(return_exceptions=True)`` (R6-AC1).

    A third figure rides along since self.ai#74: ``device_used``/``device_total``,
    the CARD-level occupancy, relayed via ``record_device_occupancy`` — a separate
    writer that never touches ``held_bytes``, because card-used is a different
    quantity from any one consumer's held and must never be summed with it. It is
    what lets ``free_capacity()`` account for CUDA contexts and non-consumer
    processes that no consumer can report as its own.

    Prefers ``read_full`` (one GET, every figure), then ``read_state`` (held +
    loaded model), then bare ``read_held_bytes``. The chain is walked with
    ``getattr`` rather than assumed so pre-existing fakes — and any consumer
    source that has not learned the newer fields — keep working untouched; each
    older shape simply contributes fewer figures."""
    read_full = getattr(source, "read_full", None)
    read_state = getattr(source, "read_state", None)
    if read_full is not None:
        held, loaded_model, device_used, device_total = await read_full(consumer_id)
    elif read_state is not None:
        (held, loaded_model), device_used, device_total = (
            await read_state(consumer_id),
            None,
            None,
        )
    else:
        held, loaded_model, device_used, device_total = (
            await source.read_held_bytes(consumer_id),
            None,
            None,
            None,
        )

    if held is not None:
        registry.heartbeat(consumer_id, held)
    if loaded_model is not None:
        registry.record_loaded_model(consumer_id, loaded_model)
    if device_used is not None:
        # Ordered AFTER the heartbeat on purpose: record_device_occupancy derives
        # the unattributed-overhead term from the ledger's total held AT THIS
        # INSTANT, so it must see this cycle's held, not the previous one's.
        registry.record_device_occupancy(consumer_id, device_used, device_total)


async def _poll_once(state_sources, registry=VramLeases) -> None:
    """Run exactly ONE poll cycle. Factored out of :func:`process_vram_poll_loop`
    so the cycle can be unit-tested (T-011) without racing ``asyncio.sleep``.

    Enumerates registered consumers, skips any absent from ``state_sources``
    (R6-AC3), and polls the mapped ones concurrently with exception isolation
    (R6-AC5/AC1)."""
    consumers = registry.get_all()
    tasks = []
    for consumer in consumers:
        consumer_id = consumer.consumer_id
        source = state_sources.get(consumer_id)
        if source is None:
            # No configured state-query endpoint for this consumer — skip it
            # (R6-AC3, opt-in). Its held ages toward stale purely via
            # last_reported_at, exactly as if the poller did not exist for it.
            continue
        tasks.append(_poll_consumer(consumer_id, source, registry))

    if not tasks:
        return

    # Concurrent poll with per-consumer isolation: one slow/failing consumer
    # neither blocks nor delays the others (R6-AC5), and a raised exception is
    # captured rather than cancelling the sibling polls (R6-AC1).
    await asyncio.gather(*tasks, return_exceptions=True)


async def process_vram_poll_loop(
    state_sources, registry=VramLeases, interval: int = VRAM_POLL_INTERVAL_SECONDS
) -> None:
    """Resilient interval loop that polls each configured consumer's VRAM state
    and relays it via the existing heartbeat. Mirrors
    ``gpu_queue.process_gpu_queue_v2``'s shape (see module docstring) minus the
    RedisLock (a redundant heartbeat is harmless — deliberate).

    ``state_sources`` is a ``{consumer_id: state_source}`` map (built at startup
    wiring, T-010); a consumer absent from it is skipped (R6-AC3). An empty map
    is the graceful unconfigured case — the loop runs but polls nobody."""
    while True:
        try:
            await _poll_once(state_sources, registry)
        except Exception as e:
            # A whole-cycle failure must never break the loop (R6-AC1), same
            # posture as process_gpu_queue_v2's outer except.
            log.error(f"VRAM poll cycle error: {e}", exc_info=True)

        await asyncio.sleep(interval)
