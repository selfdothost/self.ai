"""Legacy audio-config migration — relocate the flat ``*_ENGINE`` settings.

cavekit-audio-connections.md R5 (No Behavior Change on Relocation).

Before this kit, an audio backend lived on a separate settings tab as a flat
engine selector (``AUDIO_STT_ENGINE`` / ``AUDIO_TTS_ENGINE`` in ``config.py``)
plus that engine's sibling knobs (base URL, key, model, voice, ...). R5 requires
that every such already-configured backend keep working *immediately* after it is
relocated into the typed Connections surface — same address, same credential, no
re-authentication.

This module performs that relocation as a one-time, idempotent migration:

  1. Read the flat engine selector (``AUDIO_STT_ENGINE`` / ``AUDIO_TTS_ENGINE``)
     to decide which typed connection each capability was using. The mapping is
     not invented here — it is read straight off each type's
     ``AudioConnectionTypeInfo.legacy_engine`` anchor (built by T-001), so the
     STT/TTS engine string a type replaces is the type it migrates into.
  2. Read the old per-engine field values and lay them onto the new typed
     connection's fields using ``AudioConnectionField.legacy_config`` (built by
     T-004) as the anchor — each field names the exact old ``config.py`` knob it
     relocates, so the copy is field-for-field verbatim, no transformation.
  3. Produce one :class:`SavedAudioConnection` per previously-configured engine
     (STT and TTS, if both were configured), preserving each address/credential
     exactly — nothing is re-entered or re-authenticated.
  4. Stay safe to run on every startup: migrate only when the typed-connection
     store is still empty, so a user's post-relocation connections are never
     clobbered.

Deliberately NOT here: which connection actually *serves* a given request — that
serving-engine selection is untouched by this task (owned by T-007 / cavekit R6).
This migration only makes the relocated connections *exist* and *resolve*
correctly; it never changes which one serves.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

from selfai_ui.audio.connections import (
    TYPE_FIELDS,
    AudioConnectionStore,
    AudioConnectionType,
    available_connection_types,
)

# The flat engine-selector knob names in ``config.py`` (the two values that decide
# which typed connection each capability relocates into).
LEGACY_STT_ENGINE_KEY = "AUDIO_STT_ENGINE"
LEGACY_TTS_ENGINE_KEY = "AUDIO_TTS_ENGINE"


def _engine_to_type(capability: str) -> dict[str, AudioConnectionType]:
    """Map each legacy engine string to the typed connection that replaced it.

    Built straight off ``AudioConnectionTypeInfo.legacy_engine`` (T-001), scoped
    to types that carry ``capability`` — so the STT selector only resolves among
    STT-capable types and the TTS selector only among TTS-capable ones. This is
    why an empty TTS engine string resolves to *nothing* (no TTS-capable type
    claims ``""``): an unset TTS engine was never a configured backend.
    """

    return {
        info.legacy_engine: info.type
        for info in available_connection_types()
        if capability in info.capabilities
    }


# Precomputed from the type registry (see ``connections.py``):
#   STT: {"": SELF_HOSTED_STT, "openai": HOSTED_GENERAL}
#   TTS: {"transformers": SELF_HOSTED_TTS, "openai": HOSTED_GENERAL,
#         "elevenlabs": HOSTED_SPECIALIZED_A, "azure": HOSTED_SPECIALIZED_B}
_STT_ENGINE_TO_TYPE = _engine_to_type("stt")
_TTS_ENGINE_TO_TYPE = _engine_to_type("tts")


def _fields_from_legacy(
    connection_type: AudioConnectionType, legacy: Mapping[str, object]
) -> dict[str, object]:
    """Copy each of ``connection_type``'s fields from its legacy knob, verbatim.

    Uses ``AudioConnectionField.legacy_config`` (T-004) as the anchor: for every
    field the type presents, the value is lifted directly from the exact old
    ``config.py`` knob it relocates — no re-entry, no transformation. A knob
    absent from the snapshot is simply skipped (the field is left unset rather
    than fabricated).
    """

    fields: dict[str, object] = {}
    for connection_field in TYPE_FIELDS[connection_type]:
        if connection_field.legacy_config in legacy:
            fields[connection_field.name] = legacy[connection_field.legacy_config]
    return fields


def _stt_capability_configured(
    stt_engine: str, legacy: Mapping[str, object]
) -> bool:
    """Whether the STT side was actually configured (vs a bare fresh default).

    A non-empty STT engine string is an explicit hosted-engine choice — always
    configured. The empty string maps to the self-hosted local backend, which is
    also the fresh-install default; treat *that* as configured only when a model
    or control endpoint was actually set (both default to ``""``), so a brand-new
    install with nothing set does not mint a phantom self-hosted STT connection.
    """

    if stt_engine != "":
        return True
    model = legacy.get("AUDIO_STT_MODEL", "") or ""
    control = legacy.get("AUDIO_STT_CONTROL_BASE_URL", "") or ""
    return bool(model) or bool(control)


def migrate_legacy_audio_config(
    legacy: Mapping[str, object],
    existing_configs: Optional[dict[str, dict[str, object]]] = None,
    id_factory: Optional[Callable[[], str]] = None,
) -> AudioConnectionStore:
    """Relocate flat legacy audio settings into a typed connection store (R5).

    ``legacy`` is a plain ``{knob_name: value}`` snapshot of the old flat audio
    settings (see :func:`build_legacy_snapshot` for the adapter that lifts it off
    the ``config.py`` ``PersistentConfig`` objects). ``existing_configs`` is the
    current persisted ``AUDIO_CONNECTION_CONFIGS`` value.

    Idempotent by construction: if the store already holds any connection (a prior
    migration, or connections the user created post-relocation), it is returned
    untouched — nothing is re-migrated or clobbered. Otherwise one
    :class:`SavedAudioConnection` is minted per previously-configured engine, its
    fields copied verbatim from the legacy knobs.

    When both the STT and TTS selectors resolve to the *same* type (both
    ``"openai"`` -> ``HOSTED_GENERAL``, the one type that serves both), a single
    connection is minted carrying both sides' fields — not two duplicates.
    """

    store = AudioConnectionStore.from_serializable(existing_configs, id_factory=id_factory)
    if len(store) > 0:
        # Already migrated, or the operator has created connections since — never
        # clobber. Safe to call on every startup.
        return store

    stt_engine = str(legacy.get(LEGACY_STT_ENGINE_KEY, "") or "")
    tts_engine = str(legacy.get(LEGACY_TTS_ENGINE_KEY, "") or "")

    # Ordered + de-duplicated: STT side first, then TTS; a type already queued
    # (HOSTED_GENERAL serving both) is not added twice.
    ordered_types: list[AudioConnectionType] = []

    stt_type = _STT_ENGINE_TO_TYPE.get(stt_engine)
    if stt_type is not None and _stt_capability_configured(stt_engine, legacy):
        ordered_types.append(stt_type)

    tts_type = _TTS_ENGINE_TO_TYPE.get(tts_engine)
    if tts_type is not None and tts_type not in ordered_types:
        ordered_types.append(tts_type)

    for connection_type in ordered_types:
        store.add(connection_type, _fields_from_legacy(connection_type, legacy))

    return store


# The union of every knob the migration reads: the two engine selectors plus every
# field's ``legacy_config`` anchor. Computed once so :func:`build_legacy_snapshot`
# knows exactly which ``config.py`` attributes to lift.
def _legacy_knob_names() -> set[str]:
    names = {LEGACY_STT_ENGINE_KEY, LEGACY_TTS_ENGINE_KEY}
    for connection_fields in TYPE_FIELDS.values():
        for connection_field in connection_fields:
            names.add(connection_field.legacy_config)
    return names


def build_legacy_snapshot(config: object) -> dict[str, object]:
    """Lift the flat audio knobs off the ``config`` module into a plain dict.

    ``config`` is the ``selfai_ui.config`` module; each relevant attribute is a
    ``PersistentConfig`` whose ``.value`` holds the resolved setting. This adapter
    reads that ``.value`` (falling back to the attribute itself for a plain value)
    so :func:`migrate_legacy_audio_config` can stay a pure function over a plain
    mapping — decoupled from the config machinery and directly unit-testable.
    """

    snapshot: dict[str, object] = {}
    for name in _legacy_knob_names():
        knob = getattr(config, name, None)
        if knob is None:
            continue
        # PersistentConfig exposes the live setting on ``.value``; a plain value
        # passes straight through.
        snapshot[name] = getattr(knob, "value", knob)
    return snapshot
