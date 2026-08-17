"""Voice-catalog router — self-hosted TTS (self.speak) real voice list.

R1 (Real Catalog Per Self-Hosted Connection) of ``cavekit-audio-voice-catalog.md``:
a self-hosted TTS connection's voice list reflects that connection's *actual*
available voices, not a fixed or placeholder set.

This is the TTS analog of ``routers/transcribe.py``'s model listing. The catalog
is fetched from the self-hosted TTS backend's *control* port
(``AUDIO_TTS_CONTROL_BASE_URL``), separate from the OpenAI-compatible *serving*
endpoint used for synthesis requests — the same serving/control split
self.llamolotl and the STT transcribe router use.

The endpoint fetches the connection's own voice list and normalizes it via
``audio/voice_catalog.py``. Because the catalog is built only from what the
addressed backend returns, pointing at a different self-hosted TTS backend yields
that backend's own voices (R1 AC2), and no voice absent from the backend's
response is ever added (R1 AC3).

The admin-facing ``GET /voices`` shows the full catalog including disabled
voices. Enabled-only end-user filtering (R5, T-014) adds two routes here that
reuse the same fetch: ``GET /voices/enabled`` (verified-user, returns only the
voices an admin has left enabled) and ``POST /voices/{voice_id}/enabled`` (admin,
toggles a voice's enabled state). The curation map itself is the admin-set
``AUDIO_TTS_ENABLED_VOICES`` PersistentConfig, mirroring the transcribe router's
``AUDIO_STT_ENABLED_MODELS``.

Voice metadata (R2) is carried on each entry (T-011). Multi-connection
aggregation (R3, T-012) adds ``GET /voices/aggregated``: one combined catalog
merging every saved self-hosted TTS connection, each voice attributed to its
source connection. Hosted-provider catalogs (R4) remain a separate task
(T-013) and are absent here.

The admin voice-curation surface (cavekit-audio-voice-picker R1, T-015) adds
``GET /voices/curation``: the same source-attributed aggregation with each voice
carrying an ``enabled`` flag and an optional ``search`` filter, layered via
``curate_voice_catalog`` — the voices analog of the transcribe router's
``GET /models`` curation view. Its toggle is the existing
``POST /voices/{voice_id}/enabled``, so the curation list and the toggle share
one ``AUDIO_TTS_ENABLED_VOICES`` map and a toggle persists across reload.

The end-user voice picker (cavekit-audio-voice-picker R2, T-016) adds
``GET /voices/selectable``: the enabled-only subset of the SAME aggregated
catalog the admin curates (``_aggregate_catalog`` + ``filter_enabled_voices``),
verified-user gated — the voices analog of the transcribe router's
``GET /models/selectable``. It differs from T-014's single-connection
``GET /voices/enabled`` by filtering the multi-connection aggregate, so the
end-user picker offers exactly the enabled subset of what admin curation shows,
holding R2's enable/disable invariant across every saved self-hosted TTS
connection.
"""

import logging
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from selfai_ui.audio.connections import (
    AudioConnectionStore,
    AudioConnectionType,
    connection_display_label,
)
from selfai_ui.audio.craft_voice import CRAFT_PREFIX
from selfai_ui.audio.voice_catalog import (
    aggregate_voice_catalogs,
    build_voice_catalog,
    curate_voice_catalog,
    disambiguate_source_labels,
    filter_enabled_voices,
    label_voice_sources,
    resolve_tts_control_url,
)
from selfai_ui.env import (
    AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST,
    SRC_LOG_LEVELS,
)
from selfai_ui.models.voices import Voices
from selfai_ui.utils.auth import get_admin_user, get_verified_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("AUDIO", logging.INFO))

router = APIRouter()


def get_tts_control_url(request: Request) -> str:
    """Resolve the self-hosted TTS control-port base URL, or 400 if unset.

    A 400 (not 500) because an absent control URL means "no self-hosted TTS
    backend is configured" — a client/config condition, not a server fault.
    Mirrors the transcribe router's guard on an unconfigured backend.
    """
    control_url = getattr(request.app.state.config, "TTS_CONTROL_BASE_URL", "") or ""
    control_url = control_url.strip()
    if not control_url:
        raise HTTPException(
            status_code=400,
            detail="No self-hosted TTS backend is configured",
        )
    return control_url.rstrip("/")


async def _send_get(url: str, key: Optional[str] = None):
    """GET the backend's voice list, returning parsed JSON or None on failure."""
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers={**({"Authorization": f"Bearer {key}"} if key else {})},
            ) as response:
                if response.status >= 400:
                    log.warning("voice-catalog GET %s -> HTTP %s", url, response.status)
                    return None
                return await response.json()
    except Exception as e:
        log.warning("voice-catalog GET %s failed: %s", url, e)
        return None


async def _fetch_catalog(request: Request) -> dict:
    """Fetch and normalize the self-hosted TTS connection's real voice catalog.

    Shared by the admin (full) and end-user (enabled-only) reads so both see the
    same underlying voice set; only the end-user path narrows it. Raises 400 if
    no backend is configured and 502 if it is unreachable.
    """
    control_url = get_tts_control_url(request)
    key = getattr(request.app.state.config, "TTS_OPENAI_API_KEY", None) or None

    voices_raw = await _send_get(f"{control_url}/api/voices", key)

    if voices_raw is None:
        raise HTTPException(
            status_code=502,
            detail="Self.AI UI: could not reach the self-hosted TTS backend",
        )

    return build_voice_catalog(voices_raw)


def _enabled_map(request: Request) -> dict:
    """Return a copy of the admin-set voice-id -> enabled map from config."""
    raw = getattr(request.app.state.config, "TTS_ENABLED_VOICES", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _source_labels(request: Request) -> dict:
    """Return a ``{connection_id: shown_label}`` map for the curation surface (R4).

    Derives a human-readable label for every saved connection
    (:func:`connection_display_label`) and disambiguates any that collide
    (:func:`disambiguate_source_labels`), so the admin curation list can *show*
    which connection each voice came from — with a meaningful, per-connection
    distinct source rather than the opaque connection id (cavekit-audio-voice-picker
    R4, T-018). The bare id remains carried on each voice as the stable identity.
    """
    store = _load_connection_store(request)
    base = {conn.id: connection_display_label(conn) for conn in store.list()}
    return disambiguate_source_labels(base)


@router.get("/voices")
async def list_self_hosted_voices(request: Request, user=Depends(get_admin_user)):
    """Return the self-hosted TTS connection's real voice catalog (admin view).

    The catalog reflects exactly the voices the addressed backend reports — no
    placeholder or shared default set (R1 AC1/AC2/AC3). This is the admin-facing
    view: it includes every voice regardless of enabled state, so a disabled
    voice stays visible for curation (R5 AC2).
    """
    return await _fetch_catalog(request)


def _load_connection_store(request: Request) -> AudioConnectionStore:
    """Rebuild the saved audio-connection store from persisted config.

    Reads ``AUDIO_CONNECTION_CONFIGS`` off ``app.state.config`` (wired in
    ``main.py``, populated by the T-006 migration) and rebuilds the store, so the
    aggregation endpoint sees exactly the set of connections the operator saved.
    A missing/malformed value yields an empty store rather than raising.
    """
    raw = getattr(request.app.state.config, "AUDIO_CONNECTION_CONFIGS", None)
    return AudioConnectionStore.from_serializable(raw if isinstance(raw, dict) else {})


async def _aggregate_catalog(request: Request) -> dict:
    """Fetch and merge every saved self-hosted TTS connection's real catalog.

    Shared by the raw aggregation view (``GET /voices/aggregated``, R3) and the
    admin curation surface (``GET /voices/curation``, cavekit-audio-voice-picker
    R1) so both see exactly the same merged, source-attributed voice set; only the
    curation path layers the enabled overlay and search on top.

    Each connection's fetch address is resolved by :func:`resolve_tts_control_url`:
    a per-connection address field if one is present, else the shared
    ``AUDIO_TTS_CONTROL_BASE_URL``. A connection with no resolvable address is
    skipped; a reachable-but-empty or unreachable connection simply contributes no
    voices, so one bad connection never fails the whole aggregate.
    """
    store = _load_connection_store(request)
    tts_connections = store.list_by_type(AudioConnectionType.SELF_HOSTED_TTS)

    default_control = (
        getattr(request.app.state.config, "TTS_CONTROL_BASE_URL", "") or ""
    ).strip()
    key = getattr(request.app.state.config, "TTS_OPENAI_API_KEY", None) or None

    sources = []
    for connection in tts_connections:
        control_url = resolve_tts_control_url(connection.fields, default_control)
        if not control_url:
            # No per-connection address and no shared fallback -> unaddressable.
            continue
        voices_raw = await _send_get(f"{control_url.rstrip('/')}/api/voices", key)
        # voices_raw may be None (unreachable) -> that source contributes nothing.
        sources.append((connection.id, voices_raw))

    return aggregate_voice_catalogs(sources)


@router.get("/voices/aggregated")
async def list_aggregated_voices(request: Request, user=Depends(get_admin_user)):
    """Return one combined catalog merging every self-hosted TTS connection (R3).

    Aggregates the real voice catalogs of *all* saved self-hosted TTS connections
    into a single list, each voice carrying the id of the connection it came from
    (``source_connection_id``). So the combined catalog contains voices from both
    (every) connection (AC1), each is attributed to its source (AC2), and two
    similarly-named voices from different connections stay distinguishable by that
    source (AC3).

    This is the raw R3 merge with no curation overlay; the admin curation surface
    that layers per-voice enabled state and search on top of the same merge is
    ``GET /voices/curation`` (cavekit-audio-voice-picker R1). Admin-gated, like the
    full ``GET /voices`` catalog this composes.

    Hosted-provider connections are not aggregated here — their real catalogs are
    T-013 (R4); the merge itself is source-agnostic and will accept them once they
    exist.
    """
    return await _aggregate_catalog(request)


@router.get("/voices/curation")
async def list_voice_curation(
    request: Request,
    search: Optional[str] = None,
    user=Depends(get_admin_user),
):
    """Return the admin voice-curation surface (cavekit-audio-voice-picker R1).

    The admin-facing list of every voice in the aggregated catalog, each carrying
    an ``enabled`` flag controlling whether end users may select it — the voices
    analog of the transcribe router's ``GET /models`` curation view. Built by
    layering :func:`curate_voice_catalog`'s enabled overlay + search on top of the
    same source-attributed merge ``GET /voices/aggregated`` exposes:

    * every voice in the aggregated catalog is listed, INCLUDING those currently
      disabled — the overlay annotates enabled state rather than filtering, so a
      disabled voice stays visible for curation (R1 AC1). This is the deliberate
      admin-side counterpart to ``GET /voices/enabled``'s end-user filter.
    * each listed voice carries an ``enabled`` bool read from the admin-set
      ``AUDIO_TTS_ENABLED_VOICES`` map; the toggle that sets it is
      ``POST /voices/{voice_id}/enabled`` (R1 AC2), and because both the toggle and
      this list read/write that PersistentConfig map, a toggle persists and is
      reflected on the next load of this surface (R1 AC4).
    * an optional ``search`` query param narrows the list by id, name, language,
      gender or source connection (case-insensitive), consistent with the
      established text-model curation search (R1 AC3).

    Each entry keeps its ``source_connection_id`` AND gains a human-readable
    ``source_connection_label`` (cavekit-audio-voice-picker R4, T-018): the admin
    surface shows a meaningful source per voice, so two similarly-named voices from
    different backends are distinguishable by their shown source, not only by an
    opaque connection id (R4 AC1/AC2). The label is resolved from the saved
    connections via :func:`_source_labels` and layered on with
    :func:`label_voice_sources` after curation, leaving the id — the authoritative
    identity — untouched. Admin-gated, like the aggregated catalog it composes.
    """
    catalog = await _aggregate_catalog(request)
    curated = curate_voice_catalog(catalog, _enabled_map(request), search)
    return label_voice_sources(curated, _source_labels(request))


@router.get("/voices/enabled")
async def list_enabled_voices(request: Request, user=Depends(get_verified_user)):
    """Return only the voices an admin has enabled — the end-user catalog (R5).

    The same real catalog as ``GET /voices``, narrowed by the admin-set
    ``AUDIO_TTS_ENABLED_VOICES`` curation map: a voice an admin has disabled is
    omitted (R5 AC1), and enabling/disabling a voice adds/removes it here with no
    other change (R5 AC3). Verified-user (not admin) gated: this is the picker
    catalog every end user reads.
    """
    catalog = await _fetch_catalog(request)
    return filter_enabled_voices(catalog, _enabled_map(request))


# ---------------------------------------------------------------------------
# End-user voice picker honoring admin curation (cavekit-audio-voice-picker R2,
# T-016)
# ---------------------------------------------------------------------------
#
# ``GET /voices/enabled`` (T-014, cavekit-audio-voice-catalog R5) already filters
# the enabled-only set, but over a SINGLE self-hosted TTS connection's catalog
# (``_fetch_catalog``). The admin curation surface an operator actually toggles
# against — ``GET /voices/curation`` (T-015) — is built over the AGGREGATED,
# multi-connection catalog (``_aggregate_catalog``). Those two catalogs differ
# whenever more than one self-hosted TTS connection is saved: a voice that lives
# only on a second connection can be enabled in admin curation yet never appears
# in the single-connection ``/voices/enabled`` view.
#
# R2's invariant is that the end-user picker offers EXACTLY the enabled subset of
# what the admin curated — "enabling a voice in admin curation makes it available
# in the end-user picker; disabling it removes it" (R2 AC3). To hold that across
# multi-connection deployments, the end-user picker must filter the SAME
# aggregated catalog the admin curation surface presents. This mirrors the
# transcribe precedent exactly: T-031's end-user ``GET /models/selectable``
# filters the same ``build_model_listing`` that the admin ``GET /models``
# curation view annotates — end-user = enabled subset of precisely what the admin
# sees.
#
# ``GET /voices/selectable`` is that endpoint: it runs the same
# ``_aggregate_catalog`` merge ``/voices/curation`` uses, then applies T-014's
# pure ``filter_enabled_voices`` (kept — never a placeholder set) to return only
# the admin-enabled voices, each still carrying its ``source_connection_id`` (the
# filter copies extra entry fields through). It is a clean, additive endpoint kept
# separate from T-014's ``/voices/enabled`` and the concurrent T-018 curation-
# display work so the diff merges cleanly.


@router.get("/voices/selectable")
async def list_selectable_voices(request: Request, user=Depends(get_verified_user)):
    """List the voices a plain end user may select — the picker catalog (R2).

    The end-user counterpart to the admin ``GET /voices/curation`` surface: it
    runs the SAME source-attributed multi-connection aggregation
    (``_aggregate_catalog``) the admin curates against, then narrows it to only
    the voices an admin has enabled (``AUDIO_TTS_ENABLED_VOICES`` id -> True) via
    the pure :func:`filter_enabled_voices`. So the end-user picker offers exactly
    the enabled subset of the catalog the admin curated:

    * only admin-enabled voices are returned — a voice an admin disabled, or never
      enabled, is absent, so it is not selectable in the end-user picker
      (R2 AC1/AC2);
    * because both this endpoint and the admin curation surface read the one
      ``_aggregate_catalog`` merge and the one ``AUDIO_TTS_ENABLED_VOICES`` map,
      enabling a voice in admin curation makes it appear here and disabling it
      removes it, with no other change (R2 AC3) — and that invariant now holds for
      a voice on ANY saved self-hosted TTS connection, not only the default one
      (unlike the single-connection ``GET /voices/enabled``).

    Each returned voice keeps its ``source_connection_id`` and R2 metadata, so the
    picker can present real voice data (T-017 builds on this). Verified-user (not
    admin) gated: choosing a voice is a personal-settings affordance, not a
    management action — the same posture as the transcribe ``GET /models/selectable``.
    """
    catalog = await _aggregate_catalog(request)
    result = filter_enabled_voices(catalog, _enabled_map(request))

    # Append the user's crafted Workshop voices (owner + read-shared) as selectable
    # TTS voices, namespaced ``craft:<id>`` so the /speech router routes them to
    # Chatterbox. These are the USER's own voices, not admin-curated engine voices,
    # so they bypass the AUDIO_TTS_ENABLED_VOICES enablement gate above. Lets a
    # crafted/blended voice be attached to a Workspace Model and spoken in chat.
    try:
        for v in Voices.get_voices_by_user_id(user.id, "read"):
            result["voices"].append(
                {"id": f"{CRAFT_PREFIX}{v.id}", "name": v.name, "source": "workshop"}
            )
    except Exception as e:  # never let crafted-voice listing break the engine catalog
        log.warning("selectable: could not append crafted voices (%r)", e)

    return result


class VoiceEnabledForm(BaseModel):
    """Body for the enable/disable toggle: the desired enabled state."""

    enabled: bool


@router.post("/voices/{voice_id}/enabled")
async def set_voice_enabled(
    voice_id: str,
    form_data: VoiceEnabledForm,
    request: Request,
    user=Depends(get_admin_user),
):
    """Set whether end users may select the given voice (R5, admin-only).

    Persists an admin-set ``id -> enabled`` flag in ``AUDIO_TTS_ENABLED_VOICES``,
    mirroring the transcribe router's ``/models/{id}/enabled`` toggle. Enabling a
    voice makes it appear in ``GET /voices/enabled``; disabling it removes it
    (R5 AC3). The flag is stored by voice id and survives reload via
    PersistentConfig. The toggle does not require the TTS backend to be reachable:
    it records intent against the id the admin is curating.
    """
    config = request.app.state.config
    enabled_map = _enabled_map(request)
    enabled_map[voice_id] = form_data.enabled
    # Reassign a fresh dict so PersistentConfig replaces its value and persists.
    config.TTS_ENABLED_VOICES = enabled_map

    return {"id": voice_id, "enabled": form_data.enabled}
