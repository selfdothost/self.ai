"""How big a llamolotl model is, read off what the router publishes (self.ai#107).

Core cannot compute a model's VRAM footprint. Doing so needs the effective
preset, the router's measured-footprint cache, and the ``-ncmoe`` gate that
disqualifies a file-size estimate the moment weights are deliberately kept in
system RAM — all of which live in llama-server. So core reads a published
figure or it has none, and "none" is a real answer that must stay
distinguishable from zero: a footprint of 0 would let a lease request ask for
nothing and be granted it.

Two sources, in order:

1. ``vram_footprint`` on the ``/v1/models`` entry (self.llamolotl!46) — carries
   ``bytes`` plus a ``source`` of ``measured`` / ``declared`` / ``estimated``,
   and is simply absent when the router does not know.
2. ``vram-footprint-mib`` parsed out of ``status.preset`` — the operator's
   declaration, readable from any router build that accepts the key at all
   (self.llamolotl!45). This is the fallback for a router that predates (1), so
   the two repos have no rollout ordering constraint.

Neither present means unknown. Callers must not substitute a guess.
"""

import logging
from typing import Optional

from selfai_ui.utils.model_context import parse_preset_ini

log = logging.getLogger(__name__)

FOOTPRINT_BYTES_FIELD = "vram_footprint_bytes"
FOOTPRINT_SOURCE_FIELD = "vram_footprint_source"

_MIB = 1024 * 1024


def _published_footprint(model: dict) -> Optional[tuple[int, str]]:
    """``vram_footprint`` as the router publishes it, or None."""
    published = model.get("vram_footprint")
    if not isinstance(published, dict):
        return None
    raw_bytes = published.get("bytes")
    source = published.get("source")
    if not isinstance(raw_bytes, int) or isinstance(raw_bytes, bool) or raw_bytes <= 0:
        return None
    if not isinstance(source, str) or not source:
        # bytes without a provenance is not usable: the whole point of `source`
        # is that a caller about to reclaim another tenant's VRAM can tell a
        # measurement from a file-size guess.
        return None
    return (raw_bytes, source)


def _declared_footprint(model: dict) -> Optional[tuple[int, str]]:
    """``vram-footprint-mib`` out of the effective preset, or None.

    Same defensive parse the router applies: a zero, negative or unparseable
    value is *no claim*, never a footprint of zero.
    """
    status = model.get("status")
    if not isinstance(status, dict):
        return None
    preset = status.get("preset")
    if not isinstance(preset, str) or not preset:
        return None
    raw = parse_preset_ini(preset).get("vram-footprint-mib")
    if raw is None:
        return None
    try:
        mib = int(str(raw).strip())
    except (TypeError, ValueError):
        log.debug("ignoring unparseable vram-footprint-mib %r on %r", raw, model.get("id"))
        return None
    if mib <= 0:
        return None
    return (mib * _MIB, "declared")


def llamolotl_footprint(model: dict) -> Optional[tuple[int, str]]:
    """``(bytes, source)`` for one raw ``/v1/models`` entry, or None for unknown.

    Reporting view: whatever the router knows, ``estimated`` included. Anything
    that might REFUSE a load on the answer wants
    :func:`llamolotl_admission_footprint` instead.
    """
    return _published_footprint(model) or _declared_footprint(model)


# Sources a refusal may be built on. ``estimated`` is deliberately absent.
ADMISSION_TRUSTED_SOURCES = frozenset({"measured", "declared"})


def llamolotl_admission_footprint(model: dict) -> Optional[tuple[int, str]]:
    """``(bytes, source)`` only when the figure is good enough to DENY on.

    ``estimated`` is a GGUF file size times an overhead percentage, and the
    whole reason ``source`` is published is so a caller about to refuse a load
    can tell that apart from a measurement. The gap is not a rounding error —
    all three measured live on 7c11f0bc:

    * ``Qwen2.5-VL-7B`` — estimated 6909 MiB, measured **5212 MiB** (33% over).
    * ``gemma-4-26B`` — estimated 16307 MiB, measured ~14848 MiB (1.4 GiB over).
    * ``GLM-4.5-Air-UD-Q4_K_XL-00001-of-00002`` — estimated **56740 MiB**, more
      than twice the card. Sizing a lease off that refuses the model forever, on
      a number derived entirely from a file listing.

    It is also systematically wrong in exactly the regime the expert-offload
    work exists for: a file size cannot see ``n-cpu-moe``, so weights
    deliberately kept in system RAM get counted against the GPU
    (self.llamolotl#36).

    This is a REGRESSION GUARD, not a new policy. Before self.llamolotl!46 the
    router published nothing, so core saw None for every model without an
    operator declaration — unknown, and unknown never blocks. Publishing
    ``estimated`` silently converts those into refusable numbers and re-arms #36
    one layer up. The publishing change did not intend that; this prevents it.

    Treating it as unknown costs nothing real. The load proceeds to the router's
    own admission check — which has the preset, the measured cache and the
    ``-ncmoe`` gate — and then to the child's ``--fit``. Two better-informed
    guards remain. What we give up is a guess.

    Resolution FALLS THROUGH rather than short-circuiting. A published
    ``estimated`` value must not shadow a perfectly good preset declaration into
    unknown: ``_published_footprint() or _declared_footprint()`` returns the
    estimate, which is truthy, so the declaration would never be looked at.
    """
    for candidate in (_published_footprint(model), _declared_footprint(model)):
        if candidate is None:
            continue
        if candidate[1] in ADMISSION_TRUSTED_SOURCES:
            return candidate
        log.debug(
            "ignoring %r footprint for %r when sizing a lease — not deny-grade",
            candidate[1],
            model.get("id"),
        )
    return None


def llamolotl_vram_fields(model: dict) -> dict:
    """Flat fields to merge onto a core model-list entry.

    Empty dict when unknown — omitted rather than nulled, matching how the
    router omits the key and how ``model_context`` omits an underivable window.
    """
    found = llamolotl_footprint(model)
    if found is None:
        return {}
    footprint_bytes, source = found
    return {FOOTPRINT_BYTES_FIELD: footprint_bytes, FOOTPRINT_SOURCE_FIELD: source}
