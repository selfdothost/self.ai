"""Which models can actually take an image, read off what the router publishes
(self.ai#139).

self.llamolotl already answers this for **every** model, loaded or not::

    "architecture": {"input_modalities": ["text", "image"],
                     "output_modalities": ["text"]}

It is derived from the preset's ``mmproj`` path (``server_model_meta::
update_caps()``), not from a running child, so an unloaded model answers just as
well — which matters, because unloaded is the normal state and a consumer builds
its model table before anything is loaded.

Core dropped the field on the floor. Nothing mapped it onto
``info.meta.capabilities.vision``, which is the only place the client looks, and
97 of 101 rows in the ``model`` table carry a null ``capabilities`` — so the
client's ``?? true`` default treated every text-only model as vision-capable and
posted images that llama.cpp rejects with::

    image input is not supported - hint: if this is unexpected, you may need to
    provide the mmproj

Two rules hold throughout this module:

1. **Absent is not false.** A model from a backend that publishes no
   ``architecture`` (Ollama's ``/api/tags``, an OpenAI-compatible gateway, an
   Anthropic listing) gets *no* ``vision`` key. Writing ``false`` there would
   turn "we do not know" into "we know it cannot", and would disable image
   upload against models that handle images perfectly well.
2. **An admin's explicit answer wins.** A workspace ``model`` row that sets
   ``vision`` — in *either* direction — is a deliberate override of what the
   backend reports, and is never recomputed from upstream.

The raw ``architecture`` object is carried through onto the model entry rather
than being consumed and discarded: ``output_modalities`` and the ``audio``
input modality are already in the payload shape and will matter for self.speak.
"""

import logging
from typing import Any, Optional

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# The contract, named once. Same spelling llamolotl publishes, so the object is
# recognisable end to end rather than renamed halfway along.
ARCHITECTURE_FIELD = "architecture"
INPUT_MODALITIES_FIELD = "input_modalities"

VISION_CAPABILITY = "vision"
IMAGE_MODALITY = "image"


def architecture_fields(model: Any) -> dict:
    """``{"architecture": {...}}`` for one raw ``/v1/models`` entry, or ``{}``.

    Empty dict when the backend publishes nothing — omitted rather than nulled,
    matching how ``model_context`` omits an underivable window and
    ``model_vram`` omits an unknown footprint. The object is passed through
    whole; this module never rewrites it.
    """
    if not isinstance(model, dict):
        return {}
    architecture = model.get(ARCHITECTURE_FIELD)
    if not isinstance(architecture, dict) or not architecture:
        return {}
    return {ARCHITECTURE_FIELD: architecture}


def input_modalities(*payloads: Any) -> Optional[list[str]]:
    """The modalities a model accepts, from the first payload that declares any.

    Several copies of the same entry travel together — the flattened core entry,
    the nested ``llamolotl``/``openai`` original — and only some of them carry
    ``architecture`` depending on which transport reshaped the model. Reading
    them in order means neither transport has to be special-cased at the call
    site.

    ``None`` means *nobody said*, which is deliberately distinguishable from an
    empty list (a backend that declared it accepts nothing).
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        architecture = payload.get(ARCHITECTURE_FIELD)
        if not isinstance(architecture, dict):
            continue
        modalities = architecture.get(INPUT_MODALITIES_FIELD)
        if isinstance(modalities, (list, tuple)):
            return [modality for modality in modalities if isinstance(modality, str)]
    return None


def derived_vision(*payloads: Any) -> Optional[bool]:
    """``True``/``False`` when upstream declared its input modalities, else None.

    None is the third state and it is load-bearing: it is what leaves the
    ``vision`` key absent, so a consumer applies its own policy instead of being
    told something we do not know.
    """
    modalities = input_modalities(*payloads)
    if modalities is None:
        return None
    return IMAGE_MODALITY in modalities


def _capabilities_bucket(model: dict) -> dict:
    """``info.meta.capabilities`` on a model entry, created if it is not there.

    Entries with no workspace row have no ``info`` at all, and those are exactly
    the 97 rows the bug is about — so the nesting is built rather than required.
    Every consumer of ``info`` in core reads it with ``.get(..., {})`` chains
    (``main.py`` access control, ``middleware`` filter ids, ``model_versions``
    line pointers), so an entry that gains only ``meta.capabilities`` reads
    identically to one that had no ``info`` at all.
    """
    info = model.get("info")
    if not isinstance(info, dict):
        info = {}
        model["info"] = info

    meta = info.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        info["meta"] = meta

    capabilities = meta.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
        meta["capabilities"] = capabilities

    return capabilities


def apply_derived_capabilities(models: list) -> list:
    """Fill in ``info.meta.capabilities.vision`` from upstream modalities.

    Precedence, in order:

    1. The workspace row said ``vision`` (true *or* false) — kept untouched.
    2. The row is silent or its ``capabilities`` is null, and upstream declared
       ``input_modalities`` — derived.
    3. Neither — the key stays absent, and the model entry is not touched at
       all.

    Mutates in place and returns the same list, the shape
    ``attach_lines_to_models`` already uses on this pipeline.
    """
    for model in models:
        if not isinstance(model, dict):
            continue

        derived = derived_vision(model, model.get("llamolotl"), model.get("openai"))
        if derived is None:
            # Case 3. Nothing is created — a model from a backend that publishes
            # no architecture must not grow a bogus `vision: false`.
            continue

        capabilities = _capabilities_bucket(model)
        existing = capabilities.get(VISION_CAPABILITY)
        if isinstance(existing, bool):
            # Case 1. An admin overrode the backend; that is the whole point of
            # the field being editable.
            if existing != derived:
                log.debug(
                    "%r declares vision=%s upstream; keeping the workspace override %s",
                    model.get("id"),
                    derived,
                    existing,
                )
            continue

        capabilities[VISION_CAPABILITY] = derived

    return models
