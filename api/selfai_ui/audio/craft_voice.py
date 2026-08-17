"""Crafted (Workshop) voices as chat-selectable TTS voices.

A "crafted voice" lives in the Voices table: a set of sample clips plus a Sound
Studio node graph. Kokoro/engine voices are named (``af_bella``); a crafted voice
is reference-based (Chatterbox) and self.speak is deliberately stateless, so it
cannot be selected by name through the OpenAI ``/audio/speech`` proxy.

This module bridges the two so a crafted voice can be attached to a Workspace
Model and spoken in chat:

* its stable selectable id is ``craft:<voice_uuid>`` — namespaced so the
  ``/speech`` router can tell it apart from an engine voice id;
* :func:`resolve_blend_recipe` reads the voice's saved graph to decide which
  sample clips at which weights make up the voice (the same Shape/Blend the
  Workshop Preview uses), falling back to an equal blend of every attached clip;
* :func:`synthesize_via_speak` runs that recipe through self.speak's
  ``/api/voices/blend`` (or ``/preview`` for one clip) — the same connector path
  the Workshop Preview node uses — and returns the encoded audio.
"""

import logging

import httpx
from fastapi import HTTPException, Request

from selfai_ui.models.files import Files
from selfai_ui.models.voices import VoiceFiles
from selfai_ui.storage.provider import Storage
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)

# Namespace for a crafted-voice selectable id. Engine voice ids (af_bella, …) never
# contain a colon, so this prefix is an unambiguous discriminator.
CRAFT_PREFIX = "craft:"


def is_craft_voice(voice_id) -> bool:
    """True if ``voice_id`` names a crafted Workshop voice (``craft:<uuid>``)."""
    return isinstance(voice_id, str) and voice_id.startswith(CRAFT_PREFIX)


def craft_voice_uuid(voice_id: str) -> str:
    """The bare voice uuid from a ``craft:<uuid>`` selectable id."""
    return voice_id[len(CRAFT_PREFIX):]


def resolve_blend_recipe(voice) -> "list[tuple[str, float]]":
    """Decide the [(file_id, weight), …] recipe for a crafted voice.

    Reads the voice's saved node graph the way the Workshop Preview does:
    the Shape ("clone") node's connected Voice Sample nodes are the clips, in
    edge order, each carrying the file id the user picked. With exactly two, the
    Shape's ``blend`` slider weights them (0 = first clip, 1 = second); more than
    two (a single slider can't express it) or no blend → equal weights.

    Fallbacks that keep chat playback working when the graph is absent or
    unwired: if no Shape/sample nodes resolve, blend EVERY attached sample file
    equally. Returns ``[]`` only when the voice has no samples at all.
    """
    graph = getattr(voice, "graph", None) or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    shape = next((n for n in nodes if n.get("type") == "clone"), None)
    if shape:
        picked: list[str] = []
        for e in edges:
            if e.get("target") == shape.get("id"):
                src = next((n for n in nodes if n.get("id") == e.get("source")), None)
                values = ((src or {}).get("data") or {}).get("values") or {}
                f = values.get("file")
                if isinstance(f, str) and f:
                    picked.append(f)
        if len(picked) >= 2:
            raw = ((shape.get("data") or {}).get("values") or {}).get("blend")
            blend = 0.5 if not isinstance(raw, (int, float)) else max(0.0, min(1.0, float(raw)))
            weights = [1 - blend, blend] if len(picked) == 2 else [1.0] * len(picked)
            return list(zip(picked, weights))
        if len(picked) == 1:
            return [(picked[0], 1.0)]

    # No usable graph recipe — equal blend of every attached sample.
    file_ids = VoiceFiles.get_file_ids_by_voice_id(voice.id)
    return [(fid, 1.0) for fid in file_ids]


def _read_sample(file_id: str):
    """Resolve a file id to (filename, bytes, content_type), or None if unusable."""
    f = Files.get_file_by_id(file_id)
    if not f or not f.path:
        return None
    try:
        local_path = Storage.get_file(f.path)
        with open(local_path, "rb") as fh:
            data = fh.read()
    except Exception as e:
        log.warning("craft voice: could not read sample %s (%r)", file_id, e)
        return None
    if not data:
        return None
    name = f.filename or (f.meta or {}).get("name") or "sample.wav"
    content_type = (f.meta or {}).get("content_type") or "audio/wav"
    return (name, data, content_type)


async def synthesize_via_speak(
    request: Request,
    references: "list[tuple[str, float]]",
    text: str,
    response_format: str = "mp3",
    exaggeration=None,
    cfg_weight=None,
) -> "tuple[bytes, str]":
    """Synthesise ``text`` in a crafted voice via self.speak and return
    (audio_bytes, media_type).

    ``references`` is the [(file_id, weight)] recipe. 2+ clips go to
    ``/api/voices/blend`` (weighted speaker-embedding interpolation); one clip
    goes to ``/api/voices/preview`` (plain clone). Ticket-gated audio:synthesize,
    stateless on self.speak's side. Raises HTTPException on any failure.
    """
    samples = [(_read_sample(fid), w) for fid, w in references]
    samples = [(s, w) for s, w in samples if s is not None]
    if not samples:
        raise HTTPException(status_code=400, detail="This voice has no usable samples.")

    control = str(request.app.state.config.TTS_CONTROL_BASE_URL or "").rstrip("/")
    if not control:
        raise HTTPException(status_code=503, detail="The TTS control endpoint is not configured.")

    data = {"text": text, "response_format": response_format}
    if exaggeration is not None:
        data["exaggeration"] = str(exaggeration)
    if cfg_weight is not None:
        data["cfg_weight"] = str(cfg_weight)

    if len(samples) >= 2:
        upload = [("reference", (n, b, ct)) for (n, b, ct), _ in samples]
        data["weights"] = [str(w) for _, w in samples]
        endpoint = f"{control}/api/voices/blend"
    else:
        (n, b, ct), _ = samples[0]
        upload = {"reference": (n, b, ct)}
        endpoint = f"{control}/api/voices/preview"

    headers = {TICKET_HEADER: mint_service_ticket("self.speak", "audio:synthesize")}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0)) as client:
            resp = await client.post(endpoint, files=upload, data=data, headers=headers)
    except httpx.HTTPError as e:
        log.warning("craft voice: self.speak unreachable (%r)", e)
        raise HTTPException(status_code=502, detail="The voice engine is unreachable.")
    if resp.status_code == 503:
        raise HTTPException(status_code=503, detail="The Chatterbox voice engine is not available.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Voice synthesis failed ({resp.status_code}).")

    return resp.content, resp.headers.get("content-type", f"audio/{response_format}")
