"""Real per-connection self-hosted TTS voice catalog (cavekit-audio-voice-catalog R1).

Pure, backend-agnostic normalization of the voice list a self-hosted TTS
connection (self.speak — the ``SELF_HOSTED_TTS`` type from ``audio/connections.py``)
*actually* reports, into a stable catalog contract.

The prior arrangement (``routers/audio.py``'s ``get_available_voices``) branched
on a flat ``TTS_ENGINE`` string and had **no** case for the self-hosted TTS
engine at all — a self-hosted connection fell through every branch and returned
an empty/placeholder voice map rather than the voices its backend offers. This
module replaces that guess: the catalog is built *only* from what the connection's
backend returned, so:

* the list reflects the connection's actual voices (R1 AC1);
* two connections whose backends report different voice sets produce different
  catalogs — there is no shared hardcoded/placeholder set anywhere in this
  module (R1 AC2);
* a voice the backend does not report is never synthesized into the catalog
  (R1 AC3).

Scope of THIS module now covers **existence** (T-010 — which voices a connection
offers, identified by id), **human-facing metadata** (T-011, R2 — a
distinguishing name, language and gender for each voice), **enabled-only
end-user filtering** (T-014, R5 — the pure :func:`filter_enabled_voices` overlay),
and **multi-connection aggregation with source attribution** (T-012, R3 — the
pure :func:`aggregate_voice_catalogs` merge plus the :func:`resolve_tts_control_url`
address-resolution rule). Hosted-provider catalogs are T-013 (R4) and remain
deliberately absent here.

Voice metadata (R2) is read from the backend response *tolerantly*, mirroring
``transcribe/listing.py``'s ``_display_name`` precedent: backends label the same
attribute under different keys, so each field falls back through the common
aliases. Language and gender are ``Optional`` because a backend may not report
them — but ``name`` always resolves, falling back to the voice id when the
backend supplies no human name, so a voice is never represented by identifier
alone with its name field absent (R2 AC4).

The fetch itself lives in ``routers/voice_catalog.py``; this module takes the
already-fetched raw backend response so the merge/normalize logic is pure and
directly testable, exactly as ``transcribe/listing.py`` does for STT models.
"""

from typing import Any, Optional

from pydantic import BaseModel


class VoiceCatalogEntry(BaseModel):
    """One voice in a self-hosted TTS connection's real catalog.

    Carries the voice's stable identifier — its *existence* in the connection's
    offering (T-010) — plus the human-facing metadata that lets a person tell one
    voice from another (T-011, cavekit-audio-voice-catalog R2):

    * ``name`` — a human-readable label. Always present: it falls back to the
      voice id when the backend supplies no name, so a voice is never carried by
      identifier alone with its name field absent (R2 AC4).
    * ``language`` / ``gender`` — the reported language and gender, or ``None``
      when the backend does not report them (tolerated, not fabricated).
    """

    id: str
    name: str
    language: Optional[str] = None
    gender: Optional[str] = None


def _as_list(raw: Any) -> list:
    """Coerce a backend voice response into a list of rows.

    Accepts a bare list, or a dict wrapping the rows under ``voices`` or
    ``data`` (both shapes appear across the platform's backends). Anything else
    yields an empty list rather than raising — a malformed or missing response
    must not fabricate voices nor break the catalog.
    """
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, dict):
        inner = raw.get("voices")
        if inner is None:
            inner = raw.get("data")
        return list(inner) if isinstance(inner, list) else []
    return []


def _row_voice_id(row: Any) -> Optional[str]:
    """Extract a voice identifier from a single backend row.

    Tolerates two backend shapes: a bare string voice id, or a dict carrying the
    id under one of the common keys. Returns None for a row with no usable id so
    the caller can skip it rather than inventing a voice.
    """
    if isinstance(row, str):
        return row if row else None
    if isinstance(row, dict):
        for key in ("id", "voice_id", "name", "voice"):
            val = row.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _row_str_field(row: Any, keys: tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty string value among ``keys`` in a dict row.

    Tolerant of the various key names backends use for the same attribute (the
    caller supplies the alias list). A bare-string row carries no metadata dict,
    so it yields None. Returns None when no listed key holds a usable string,
    rather than fabricating a value.
    """
    if isinstance(row, dict):
        for key in keys:
            val = row.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _display_name(row: Any, voice_id: str) -> str:
    """Resolve a human-readable voice name, falling back to the id.

    Mirrors ``transcribe/listing.py``'s ``_display_name`` precedent: tries the
    common name keys (``name`` / ``display_name`` / ``label``), and when the
    backend supplies none, returns the voice id. This guarantees every entry
    carries a name (R2 AC4) — a voice is never left as a bare identifier with the
    name field absent.
    """
    name = _row_str_field(row, ("name", "display_name", "label"))
    return name if name else voice_id


def _language_of(row: Any) -> Optional[str]:
    """Extract the reported language, tolerating ``language``/``lang``/``locale``.

    None when the backend does not report a language — it is not fabricated.
    """
    return _row_str_field(row, ("language", "lang", "locale"))


def _gender_of(row: Any) -> Optional[str]:
    """Extract the reported gender, tolerating ``gender``/``sex``.

    None when the backend does not report a gender — it is not fabricated.
    """
    return _row_str_field(row, ("gender", "sex"))


def build_voice_catalog(voices_raw: Any) -> dict:
    """Build a self-hosted TTS connection's real voice catalog from its backend.

    ``voices_raw`` is the raw response the connection's own backend returned for
    its voice listing. The returned catalog contains exactly the voices present
    in that response (deduplicated, order-preserving) and nothing else — there is
    no default, fallback, or placeholder voice set. A different backend response
    therefore yields a different catalog (R1 AC2), and a voice the backend never
    reported never appears (R1 AC3).

    Each entry additionally carries human-facing metadata read tolerantly from
    the same backend row (R2): a ``name`` (always present — the id when the
    backend supplies no name), and an optional ``language`` and ``gender`` (None
    when the backend does not report them, never fabricated).

    Returns ``{"voices": [entry, ...]}`` where each entry is a
    :class:`VoiceCatalogEntry` serialized to a dict.
    """
    entries: list[VoiceCatalogEntry] = []
    seen: set[str] = set()

    for row in _as_list(voices_raw):
        voice_id = _row_voice_id(row)
        if not voice_id or voice_id in seen:
            continue
        seen.add(voice_id)
        entries.append(
            VoiceCatalogEntry(
                id=voice_id,
                name=_display_name(row, voice_id),
                language=_language_of(row),
                gender=_gender_of(row),
            )
        )

    return {"voices": [entry.model_dump() for entry in entries]}


# ---------------------------------------------------------------------------
# R3: Multi-Connection Aggregation with source attribution (T-012)
#
# cavekit-audio-voice-catalog.md R3 (Multi-Connection Aggregation).
#
# T-010 gave a single connection's real catalog; this layer merges the catalogs
# of *several* configured self-hosted TTS connections into one combined list,
# with every voice staying attributable to the connection it came from:
#
#   * the aggregated catalog contains voices from every source connection (AC1);
#   * each aggregated voice carries the id of its source connection (AC2);
#   * two similarly-named voices from different connections stay individually
#     distinguishable, because the source connection id is carried on each and
#     the merge never collapses two voices from *different* connections into one
#     (AC3) — dedup is scoped to (source connection, voice id), never voice id
#     alone.
#
# ``aggregate_voice_catalogs`` is pure and takes already-fetched raw backend
# responses, one per connection, exactly as :func:`build_voice_catalog` does for
# a single connection — so the merge logic is directly unit-testable. The fetch
# and the resolution of *where* each connection is fetched from live in the
# router; the address resolution itself is captured by
# :func:`resolve_tts_control_url` below.
#
# Aggregation is deliberately source-agnostic: each source is an
# ``(connection_id, raw_response)`` pair, so hosted-provider catalogs (T-013, R4)
# can be fed into the same merge once they exist, without touching this function.
# ---------------------------------------------------------------------------


# Per-connection address fields checked, in order, when resolving a self-hosted
# TTS connection's control URL. The current SELF_HOSTED_TTS field set (T-004:
# ``model`` + ``split_on``) carries NONE of these — so every self-hosted TTS
# connection today falls back to the shared ``AUDIO_TTS_CONTROL_BASE_URL``. The
# lookup is kept as the first link of the chain so a future field set that adds a
# per-connection address is picked up here with no further change.
_TTS_CONTROL_URL_FIELDS: tuple[str, ...] = ("control_base_url", "base_url", "url")


def resolve_tts_control_url(
    connection_fields: Any, default_control_url: Optional[str]
) -> Optional[str]:
    """Resolve a self-hosted TTS connection's control base URL (T-012).

    Fallback chain: a per-connection address field on ``connection_fields`` if one
    is present and non-empty, else the shared ``default_control_url`` (the
    ``AUDIO_TTS_CONTROL_BASE_URL`` config value). Returns ``None`` when neither
    yields a usable address, so the caller can skip an unaddressable connection
    rather than fetch from an empty URL.

    Because the SELF_HOSTED_TTS field set carries no per-connection address today,
    every current self-hosted TTS connection resolves to the shared control URL —
    the deliberate consequence of that field set, made explicit here so multi-
    connection aggregation has a single, testable address-resolution rule.
    """
    fields = connection_fields if isinstance(connection_fields, dict) else {}
    for key in _TTS_CONTROL_URL_FIELDS:
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if isinstance(default_control_url, str) and default_control_url.strip():
        return default_control_url.strip()
    return None


def aggregate_voice_catalogs(sources: Any) -> dict:
    """Merge several connections' real voice catalogs into one attributed list (R3).

    ``sources`` is an iterable of ``(connection_id, voices_raw)`` pairs — one per
    configured self-hosted TTS connection, where ``voices_raw`` is that
    connection's own raw backend response (or ``None`` if it was unreachable). Each
    is normalized through :func:`build_voice_catalog`, and every resulting voice is
    tagged with its ``source_connection_id`` before being concatenated into the
    combined catalog.

    The merge preserves source order and, within a source, the connection's own
    voice order. Two voices sharing an id but originating from *different*
    connections are both kept (each carries its own ``source_connection_id``), so
    similarly-named voices stay distinguishable by source (AC3); a duplicate only
    collapses when the same ``(source_connection_id, voice id)`` pair is seen twice.
    A source that reported no voices (empty or unreachable) simply contributes
    nothing — it neither fabricates voices nor breaks the aggregate.

    Returns ``{"voices": [entry, ...]}`` where each entry is a
    :func:`build_voice_catalog` entry dict plus a ``source_connection_id`` key.
    """
    voices: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for source in sources or []:
        connection_id, voices_raw = source
        per_connection = build_voice_catalog(voices_raw)
        for entry in per_connection.get("voices", []):
            key = (connection_id, entry.get("id"))
            if key in seen:
                continue
            seen.add(key)
            tagged = dict(entry)
            tagged["source_connection_id"] = connection_id
            voices.append(tagged)

    return {"voices": voices}


def filter_enabled_voices(catalog: dict, enabled_map: Any) -> dict:
    """Return only the voices an admin has enabled, for the end-user picker (R5).

    ``catalog`` is a catalog as produced by :func:`build_voice_catalog` (a
    ``{"voices": [entry, ...]}`` dict). ``enabled_map`` is the admin-set
    voice-id -> bool curation map (``AUDIO_TTS_ENABLED_VOICES``). A voice is kept
    iff ``enabled_map.get(voice_id) is True`` — a voice absent from the map, or
    mapped to a falsy value, is dropped. The conservative default (absent ->
    disabled) mirrors ``transcribe/listing.py``'s ``curate_listing``: admins
    curate voices *in* rather than having to disable a large default set.

    Pure: it neither reads config nor mutates its inputs, so it is directly
    unit-testable. The full catalog (including disabled voices) remains available
    to the admin view — this filter is applied only on the end-user path, so a
    disabled voice stays visible in admin curation while dropping out of what a
    user can select (R5 AC1/AC2), and flipping its flag adds/removes it from the
    end-user catalog with no other change (R5 AC3).
    """
    enabled = enabled_map if isinstance(enabled_map, dict) else {}

    kept: list[dict] = []
    for entry in catalog.get("voices", []):
        entry = dict(entry)
        if enabled.get(entry.get("id")) is True:
            kept.append(entry)

    return {"voices": kept}


# ---------------------------------------------------------------------------
# R1 (cavekit-audio-voice-picker): the admin curation surface overlay (T-015)
#
# The admin curation surface lists *every* voice in the aggregated catalog —
# including disabled ones — each carrying an ``enabled`` flag the admin toggles,
# and the list is text-searchable. This is the voices analog of
# ``transcribe/listing.py``'s :func:`curate_listing`: where
# :func:`filter_enabled_voices` (T-014, R2's counterpart) *removes* disabled
# voices for the end-user picker, this overlay *keeps* every voice and merely
# annotates its enabled state, so a disabled voice stays visible for curation
# (R1 AC1). It is the deliberate admin-side mirror of the end-user filter, not a
# duplicate of it.
#
# Kept pure and separate from the fetch/aggregate wiring in
# ``routers/voice_catalog.py`` — it takes an already-aggregated catalog and the
# admin-set enabled map, so the overlay+search logic is directly unit-testable,
# exactly as ``curate_listing`` is for STT models.
# ---------------------------------------------------------------------------

# Entry fields the curation search matches against, case-insensitively. Covers
# the voice's identity, its human-facing metadata (T-011), and — for the
# aggregated admin surface — the source connection it came from, so an admin can
# narrow the list by backend as well as by voice (aligns with R4/T-018's
# source-visible curation). Mirrors ``transcribe/listing.py``'s id/name search,
# widened to the richer metadata a voice carries.
_VOICE_SEARCH_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "language",
    "gender",
    "source_connection_id",
)


def _voice_matches_search(entry: dict, needle: str) -> bool:
    """True iff the search needle appears in any searchable field of the voice.

    Case-insensitive substring match over the voice's id, name, language, gender
    and source connection — consistent with the established text-model curation
    search (a plain text filter over the visible fields), widened to the metadata
    a voice carries.
    """
    for key in _VOICE_SEARCH_FIELDS:
        val = entry.get(key)
        if isinstance(val, str) and needle in val.lower():
            return True
    return False


def curate_voice_catalog(
    catalog: dict,
    enabled_map: Any,
    search: Optional[str] = None,
) -> dict:
    """Overlay admin curation state onto a catalog and apply search (R1).

    ``catalog`` is a catalog dict as produced by :func:`aggregate_voice_catalogs`
    (or :func:`build_voice_catalog`) — a ``{"voices": [entry, ...]}`` mapping.
    ``enabled_map`` is the admin-set voice-id -> bool curation map
    (``AUDIO_TTS_ENABLED_VOICES``). This is the admin curation surface's core:

    * every voice is carried through, including those an admin has disabled, so
      the surface lists the whole catalog for curation (R1 AC1) — unlike
      :func:`filter_enabled_voices`, which drops disabled voices for end users;
    * each entry gains an ``enabled`` flag from ``enabled_map`` (an id absent from
      the map is treated as not enabled — the conservative "curate in" default),
      giving every listed voice a toggleable state (R1 AC2);
    * when ``search`` is a non-empty string, the list is narrowed to voices whose
      id, name, language, gender or source connection contain it
      (case-insensitive), consistent with the text-model curation pattern
      (R1 AC3).

    Pure: it neither reads config nor mutates its inputs (each entry is copied
    before the ``enabled`` flag is set), so it is directly unit-testable and safe
    to layer on top of a freshly-aggregated catalog. The source attribution
    (``source_connection_id``) each aggregated entry carries is preserved
    untouched, so the curation list keeps showing which connection every voice
    came from (R4/T-018 builds directly on this).
    """
    enabled = enabled_map if isinstance(enabled_map, dict) else {}
    needle = search.strip().lower() if isinstance(search, str) else ""

    curated: list[dict] = []
    for entry in catalog.get("voices", []):
        entry = dict(entry)
        # Strict ``is True`` (not truthiness) so the admin's displayed enabled
        # state matches exactly what the end-user filter (:func:`filter_enabled_voices`)
        # would honor — a stray non-bool map value never shows as enabled here while
        # being dropped there.
        entry["enabled"] = enabled.get(entry.get("id")) is True
        if needle and not _voice_matches_search(entry, needle):
            continue
        curated.append(entry)

    return {"voices": curated}


# ---------------------------------------------------------------------------
# R4 (cavekit-audio-voice-picker): source connection shown in curation (T-018)
#
# cavekit-audio-voice-picker.md R4 (Source Connection Visible in Curation).
#
# T-012 tags every aggregated voice with its ``source_connection_id`` and T-015
# preserves that id through the curation overlay — so the *data* attributing each
# voice to a connection is already present in ``GET /voices/curation``. But that
# id is the connection's opaque stable identifier (a generated uuid hex), which is
# not what an admin should be shown: two similarly-named voices from two backends
# must be distinguishable by a *meaningful* shown source (R4 AC1/AC2), not by
# reading raw uuids.
#
# This layer resolves each ``source_connection_id`` to a human-readable
# ``source_connection_label`` for display, without disturbing the id (which stays
# the authoritative identity). It is deliberately a separate, pure overlay applied
# on top of an already-curated catalog — it does not touch T-012's aggregation nor
# T-015's curation/search — so it merges cleanly alongside concurrent work on this
# file and can be unit-tested directly.
#
# The base per-connection label comes from
# :func:`selfai_ui.audio.connections.connection_display_label`; a base label may
# collide across two like-configured connections, so
# :func:`disambiguate_source_labels` makes each *shown* label unique before it is
# attached — guaranteeing R4 AC2 holds on the shown source alone, not merely on
# the underlying id.
# ---------------------------------------------------------------------------


def disambiguate_source_labels(labels_by_id: Any) -> dict:
    """Make a ``{connection_id: base_label}`` map yield a UNIQUE label per id (R4 AC2).

    ``labels_by_id`` maps each source connection id to its human-readable base
    label (from :func:`connection_display_label`). Two connections of the same
    type and model share a base label, which would make two voices from different
    connections show the *same* source and blur the very distinction R4 exists to
    preserve. For every base label carried by more than one connection, the
    connection id is appended (``"Self-hosted TTS (m) #<id>"``) so the shown source
    is distinct per connection; a label unique to a single connection is left
    untouched for readability. Pure and total: a non-dict input yields ``{}``.
    """
    labels = labels_by_id if isinstance(labels_by_id, dict) else {}

    counts: dict[str, int] = {}
    for label in labels.values():
        if isinstance(label, str):
            counts[label] = counts.get(label, 0) + 1

    resolved: dict[str, str] = {}
    for cid, label in labels.items():
        if not isinstance(label, str) or not label:
            # No usable base label -> fall back to the bare id as the shown source.
            resolved[cid] = str(cid)
            continue
        if counts.get(label, 0) > 1:
            resolved[cid] = f"{label} #{cid}"
        else:
            resolved[cid] = label
    return resolved


def label_voice_sources(catalog: Any, source_labels: Any) -> dict:
    """Attach a human-readable ``source_connection_label`` to each voice (R4 AC1).

    ``catalog`` is a curated/aggregated catalog (``{"voices": [entry, ...]}`` where
    each entry carries a ``source_connection_id``). ``source_labels`` is the
    ``{connection_id: shown_label}`` map (typically the output of
    :func:`disambiguate_source_labels`). Every entry gains a
    ``source_connection_label`` set from that map, so the admin curation surface can
    *show* which connection each voice came from (R4 AC1). When a voice's source id
    is absent from the map (e.g. a source with no derivable label), the bare
    ``source_connection_id`` is used as the shown label so the field is never blank
    and two differently-sourced voices remain distinguishable (R4 AC2).

    Pure: each entry is copied before annotation, so the input catalog is not
    mutated. The ``source_connection_id`` is preserved untouched alongside the new
    label — the id stays the authoritative identity, the label is display polish.
    """
    labels = source_labels if isinstance(source_labels, dict) else {}

    out: list[dict] = []
    for entry in catalog.get("voices", []) if isinstance(catalog, dict) else []:
        entry = dict(entry)
        sid = entry.get("source_connection_id")
        shown = labels.get(sid)
        entry["source_connection_label"] = (
            shown if isinstance(shown, str) and shown else sid
        )
        out.append(entry)

    return {"voices": out}


# ---------------------------------------------------------------------------
# R4: Hosted-provider real voice catalogs, selected by declared type (T-013)
#
# cavekit-audio-voice-catalog.md R4 (Hosted-Provider Catalogs Are Real Too).
#
# The self-hosted path above (T-010) builds a catalog from one uniform backend
# response. The three HOSTED provider types do NOT share a response shape:
#
#   * HOSTED_GENERAL (an OpenAI-compatible TTS provider) exposes a fixed,
#     published voice set and no live listing endpoint;
#   * HOSTED_SPECIALIZED_A (ElevenLabs) returns richly-labelled voice objects
#     under a ``voices`` wrapper, with gender nested under ``labels``;
#   * HOSTED_SPECIALIZED_B (Azure) returns a flat list of
#     ``ShortName``/``DisplayName``/``Locale``/``Gender`` rows.
#
# Because their shapes differ, the fetch/parse *strategy* (the normalizer) must
# be chosen per provider. R4's core contract is that this choice is made from the
# connection's DECLARED type (``AudioConnectionType``), never guessed from a flat
# engine string nor sniffed from the response body — replacing the prior
# ``routers/audio.py`` ``get_available_voices`` heuristic that branched on the
# ambient ``TTS_ENGINE`` string. :func:`hosted_fetch_strategy` below is a pure,
# total mapping from the three hosted enum members to their provider normalizer;
# a non-hosted (or unknown) type raises rather than falling through to any
# default, so no heuristic guess is possible (R4 AC2). Each of the three types
# therefore resolves to its own provider's real list (R4 AC3), built only from
# what that provider reported (R4 AC1) — there is no shared placeholder set.
#
# Kept as a self-contained block (its own import below) so it merges cleanly
# alongside the concurrent multi-connection aggregation work (T-012) on this same
# file; nothing above this line is modified.
# ---------------------------------------------------------------------------

from selfai_ui.audio.connections import AudioConnectionType  # noqa: E402

# OpenAI's TTS voice set is fixed and published — the provider exposes no listing
# endpoint, so its *real* catalog is exactly this enumerable set. This is the
# actual set the provider offers (not a shared placeholder used when the backend
# is unknown): it is carried over unchanged from the ``routers/audio.py`` openai
# branch, and is used only for the HOSTED_GENERAL strategy, never as a default
# for any other type.
_OPENAI_VOICES: tuple[tuple[str, str], ...] = (
    ("alloy", "Alloy"),
    ("echo", "Echo"),
    ("fable", "Fable"),
    ("onyx", "Onyx"),
    ("nova", "Nova"),
    ("shimmer", "Shimmer"),
)


def _entries_from_generic_rows(rows: list) -> list[VoiceCatalogEntry]:
    """Normalize rows using the same tolerant parse as the self-hosted path.

    Reuses the module's existing per-row helpers (``_row_voice_id`` /
    ``_display_name`` / ``_language_of`` / ``_gender_of``) so a hosted provider
    that happens to speak the generic id/name/language/gender shape is parsed
    identically to a self-hosted backend. Rows without a usable id are skipped,
    never fabricated.
    """
    entries: list[VoiceCatalogEntry] = []
    for row in rows:
        voice_id = _row_voice_id(row)
        if not voice_id:
            continue
        entries.append(
            VoiceCatalogEntry(
                id=voice_id,
                name=_display_name(row, voice_id),
                language=_language_of(row),
                gender=_gender_of(row),
            )
        )
    return entries


def _normalize_openai(voices_raw: Any) -> list[VoiceCatalogEntry]:
    """HOSTED_GENERAL (OpenAI-compatible): the provider's fixed published voices.

    OpenAI's TTS API has no live voice-listing endpoint, so its real catalog is
    the fixed set it publishes. Should a compatible gateway nonetheless return a
    list, that real response is honored instead of the fixed set — the fixed set
    is the fallback for a provider with no listing endpoint, not a placeholder
    that overrides real data.
    """
    rows = _as_list(voices_raw)
    if rows:
        return _entries_from_generic_rows(rows)
    return [VoiceCatalogEntry(id=vid, name=name) for vid, name in _OPENAI_VOICES]


def _normalize_elevenlabs(voices_raw: Any) -> list[VoiceCatalogEntry]:
    """HOSTED_SPECIALIZED_A (ElevenLabs): real voices from the provider's list.

    ElevenLabs returns ``{"voices": [{"voice_id", "name", "labels": {...}}, ...]}``
    where gender (and sometimes language) live nested under ``labels``. The id is
    ``voice_id``; the name is the provider-supplied ``name`` (falling back to the
    id so a voice is never a bare identifier — R2 AC4). A voice absent from the
    provider's response never appears (R4 AC1). If the provider reports nothing
    (fetch failed -> ``None``), the catalog is empty — voices are not fabricated.
    """
    entries: list[VoiceCatalogEntry] = []
    for row in _as_list(voices_raw):
        if not isinstance(row, dict):
            # Tolerate a bare id string as a degenerate row.
            voice_id = _row_voice_id(row)
            if voice_id:
                entries.append(VoiceCatalogEntry(id=voice_id, name=voice_id))
            continue
        voice_id = row.get("voice_id") or row.get("id")
        if not isinstance(voice_id, str) or not voice_id:
            continue
        labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
        name = row.get("name")
        language = row.get("language") or row.get("locale") or labels.get("language")
        gender = labels.get("gender") or row.get("gender")
        entries.append(
            VoiceCatalogEntry(
                id=voice_id,
                name=name if isinstance(name, str) and name else voice_id,
                language=language if isinstance(language, str) and language else None,
                gender=gender if isinstance(gender, str) and gender else None,
            )
        )
    return entries


def _normalize_azure(voices_raw: Any) -> list[VoiceCatalogEntry]:
    """HOSTED_SPECIALIZED_B (Azure): real voices from the region's voice list.

    Azure returns a flat list of rows keyed with capitalized attribute names:
    ``ShortName`` (the id used on the wire), ``DisplayName``, ``Locale``,
    ``Gender``. The human name mirrors the prior ``routers/audio.py`` azure
    branch — ``"{DisplayName} ({ShortName})"`` — carried over unchanged. A row
    without a ``ShortName`` is skipped, never fabricated; an empty/absent provider
    response yields an empty catalog.
    """
    entries: list[VoiceCatalogEntry] = []
    for row in _as_list(voices_raw):
        if not isinstance(row, dict):
            continue
        short = row.get("ShortName") or row.get("short_name")
        if not isinstance(short, str) or not short:
            continue
        display = row.get("DisplayName") or row.get("LocalName")
        name = f"{display} ({short})" if isinstance(display, str) and display else short
        locale = row.get("Locale") or row.get("locale")
        gender = row.get("Gender") or row.get("gender")
        entries.append(
            VoiceCatalogEntry(
                id=short,
                name=name,
                language=locale if isinstance(locale, str) and locale else None,
                gender=gender if isinstance(gender, str) and gender else None,
            )
        )
    return entries


# The pure, total dispatch from a declared hosted type to its provider
# normalizer. Keyed on the enum member itself — the *declared type* — so strategy
# selection can never be a guess derived from the response body or an ambient
# engine string (R4 AC2). The two self-hosted types are deliberately absent: they
# take the T-010 self-hosted path (:func:`build_voice_catalog`), not this one.
_HOSTED_STRATEGIES = {
    AudioConnectionType.HOSTED_GENERAL: _normalize_openai,
    AudioConnectionType.HOSTED_SPECIALIZED_A: _normalize_elevenlabs,
    AudioConnectionType.HOSTED_SPECIALIZED_B: _normalize_azure,
}


def is_hosted_type(connection_type: Any) -> bool:
    """Whether a connection type has a hosted-provider voice-catalog strategy."""
    return connection_type in _HOSTED_STRATEGIES


def hosted_fetch_strategy(connection_type: Any):
    """Return the voice-catalog normalizer for a hosted connection type (R4 AC2).

    The strategy is keyed strictly off the DECLARED ``AudioConnectionType`` — it
    never inspects a response body or a flat engine string. A non-hosted type (the
    two self-hosted types) or any non-member raises ``ValueError`` rather than
    falling through to a default, so a provider is never inferred heuristically.
    """
    try:
        strategy = _HOSTED_STRATEGIES.get(connection_type)
    except TypeError:  # unhashable / wholly unexpected input
        strategy = None
    if strategy is None:
        raise ValueError(
            f"{connection_type!r} is not a hosted-provider connection type; "
            "hosted voice-catalog fetch is defined only for the three hosted "
            "types (HOSTED_GENERAL, HOSTED_SPECIALIZED_A, HOSTED_SPECIALIZED_B)"
        )
    return strategy


def build_hosted_voice_catalog(connection_type: Any, voices_raw: Any) -> dict:
    """Build a hosted provider's real voice catalog, per its declared type (R4).

    ``connection_type`` is the connection's DECLARED :class:`AudioConnectionType`;
    ``voices_raw`` is the raw response that provider's own backend returned for its
    voice listing. The normalizer is selected from ``connection_type`` alone
    (R4 AC2) — the response body never influences which provider we parse as. The
    returned catalog contains exactly the voices present in that provider's
    response (deduplicated, order-preserving), each carrying the R2 metadata
    (name always present; language/gender when the provider reports them), so:

    * a hosted connection returns that provider's actual voice list, not a
      placeholder (R4 AC1);
    * each of the three hosted types resolves to its own provider's real list,
      because each maps to a distinct normalizer over a distinct response shape
      (R4 AC3).

    Returns ``{"voices": [entry, ...]}`` in the same shape as
    :func:`build_voice_catalog`, so both the self-hosted and hosted paths present
    an identical catalog contract to downstream readers (aggregation, curation,
    the end-user picker).
    """
    strategy = hosted_fetch_strategy(connection_type)

    entries: list[VoiceCatalogEntry] = []
    seen: set[str] = set()
    for entry in strategy(voices_raw):
        if entry.id in seen:
            continue
        seen.add(entry.id)
        entries.append(entry)

    return {"voices": [entry.model_dump() for entry in entries]}
