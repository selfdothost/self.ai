import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.files import Files
from selfai_ui.models.voices import (
    VoiceFiles,
    VoiceForm,
    VoiceResponse,
    Voices,
    VoiceUserResponse,
)
from selfai_ui.storage.provider import Storage
from selfai_ui.utils.access_control import has_access, has_permission
from selfai_ui.utils.auth import get_verified_user
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


# The Voices Workspace surface (Sound Studio, Phase 1). A voice is a first-class
# RBAC'd asset: the access model + the endpoint shape are a faithful copy of
# Knowledge (routers/knowledge.py), so the same AccessControl UI / has_access()
# apply and self.chat's $lib/apis/voices mirrors $lib/apis/knowledge. All routes
# are session/ticket-authed via get_verified_user.


def _voice_with_files(voice) -> VoiceResponse:
    file_ids = VoiceFiles.get_file_ids_by_voice_id(voice.id)
    files = Files.get_file_metadatas_by_ids(file_ids) if file_ids else []
    return VoiceResponse(**voice.model_dump(), files=files)


############################
# GetVoices (read-accessible) / GetVoiceList (write-accessible)
############################


@router.get("/", response_model=list[VoiceUserResponse])
async def get_voices(user=Depends(get_verified_user)):
    if user.role == "admin":
        voices = Voices.get_voices()
    else:
        voices = Voices.get_voices_by_user_id(user.id, "read")

    result = []
    for voice in voices:
        file_ids = VoiceFiles.get_file_ids_by_voice_id(voice.id)
        files = Files.get_file_metadatas_by_ids(file_ids) if file_ids else []
        result.append(VoiceUserResponse(**voice.model_dump(), files=files))
    return result


@router.get("/list", response_model=list[VoiceUserResponse])
async def get_voice_list(user=Depends(get_verified_user)):
    if user.role == "admin":
        voices = Voices.get_voices()
    else:
        voices = Voices.get_voices_by_user_id(user.id, "write")

    result = []
    for voice in voices:
        file_ids = VoiceFiles.get_file_ids_by_voice_id(voice.id)
        files = Files.get_file_metadatas_by_ids(file_ids) if file_ids else []
        result.append(VoiceUserResponse(**voice.model_dump(), files=files))
    return result


############################
# CreateNewVoice
############################


@router.post("/create", response_model=Optional[VoiceResponse])
async def create_new_voice(request: Request, form_data: VoiceForm, user=Depends(get_verified_user)):
    if user.role != "admin" and not has_permission(
        user.id, "studio.voices", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    voice = Voices.insert_new_voice(user.id, form_data)
    if not voice:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(),
        )
    return _voice_with_files(voice)


############################
# GetVoiceById
############################


@router.get("/{id}", response_model=Optional[VoiceResponse])
async def get_voice_by_id(id: str, user=Depends(get_verified_user)):
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    if not (user.role == "admin" or voice.user_id == user.id or has_access(user.id, "read", voice.access_control)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    return _voice_with_files(voice)


############################
# UpdateVoiceById (name / description / access_control)
############################


@router.post("/{id}/update", response_model=Optional[VoiceResponse])
async def update_voice_by_id(id: str, form_data: VoiceForm, user=Depends(get_verified_user)):
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    if not (user.role == "admin" or voice.user_id == user.id or has_access(user.id, "write", voice.access_control)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    updated = Voices.update_voice_by_id(id=id, form_data=form_data)
    if not updated:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())
    return _voice_with_files(updated)


############################
# UpdateVoiceGraphById (the pipeline node-graph)
############################


class GraphForm(BaseModel):
    graph: dict


@router.post("/{id}/graph/update", response_model=Optional[VoiceResponse])
async def update_voice_graph_by_id(id: str, form_data: GraphForm, user=Depends(get_verified_user)):
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    if not (user.role == "admin" or voice.user_id == user.id or has_access(user.id, "write", voice.access_control)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    updated = Voices.update_voice_graph_by_id(id=id, graph=form_data.graph)
    if not updated:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())
    return _voice_with_files(updated)


############################
# Voice sample files (attach / detach)
############################


class FileIdForm(BaseModel):
    file_id: str


@router.post("/{id}/file/add", response_model=Optional[VoiceResponse])
def add_file_to_voice_by_id(id: str, form_data: FileIdForm, user=Depends(get_verified_user)):
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    if not (user.role == "admin" or voice.user_id == user.id or has_access(user.id, "write", voice.access_control)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    file = Files.get_file_by_id(form_data.file_id)
    if not file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    # NOTE: the audio-type constraint is enforced client-side (Sound Studio Files
    # tab, Phase 1 R3); server-side audio validation is a hardening follow-up.
    if not VoiceFiles.add_file_to_voice(id, form_data.file_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())
    return _voice_with_files(Voices.get_voice_by_id(id=id))


@router.post("/{id}/file/remove", response_model=Optional[VoiceResponse])
def remove_file_from_voice_by_id(id: str, form_data: FileIdForm, user=Depends(get_verified_user)):
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    if not (user.role == "admin" or voice.user_id == user.id or has_access(user.id, "write", voice.access_control)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    VoiceFiles.remove_file_from_voice(id, form_data.file_id)
    return _voice_with_files(Voices.get_voice_by_id(id=id))


############################
# DeleteVoiceById
############################


@router.delete("/{id}/delete", response_model=bool)
async def delete_voice_by_id(id: str, user=Depends(get_verified_user)):
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    if not (user.role == "admin" or voice.user_id == user.id or has_access(user.id, "write", voice.access_control)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    # voice_file join rows cascade via the FK ON DELETE CASCADE; the graph field
    # is on the voice row itself, so it goes with the record — no orphaned rows.
    return Voices.delete_voice_by_id(id=id)


############################
# Voice Workshop — Preview (connector to self.speak / Chatterbox)
############################


class VoiceBlendRef(BaseModel):
    """One weighted sample in a blended preview. `weight` is the pull toward this
    clip's voice identity (renormalised across refs by self.speak)."""

    file_id: str
    weight: float = 1.0


class VoicePreviewForm(BaseModel):
    """Preview a crafted voice. `text` is spoken in the voice; the levers map to
    Chatterbox's controls (Expressiveness → exaggeration, Character → cfg_weight).

    Two modes:
      * single-sample — `file_id` picks which sample to seed from (omitted → the
        voice's first sample); proxied to self.speak `/api/voices/preview`.
      * BLEND — `references` (2+ weighted samples) interpolates a NEW voice from
        several clips; proxied to self.speak `/api/voices/blend`. Takes precedence
        over `file_id` when present. This is the Voice Workshop multi-sample path.
    """

    text: str
    file_id: Optional[str] = None
    references: Optional[list[VoiceBlendRef]] = None
    exaggeration: Optional[float] = None
    cfg_weight: Optional[float] = None


@router.post("/{id}/preview")
async def preview_voice_by_id(
    id: str, form_data: VoicePreviewForm, request: Request, user=Depends(get_verified_user)
):
    """Voice Workshop Preview — synthesise `text` in the crafted voice and return
    the audio. Fetches the voice's sample clip, mints a self.speak service ticket,
    and proxies to self.speak's `POST /api/voices/preview` with the artist's levers.
    This is the connector that makes the Preview node produce real sound (Chatterbox
    live on self-speak-gpu). Returns the encoded audio (wav) on 200.
    """
    voice = Voices.get_voice_by_id(id=id)
    if not voice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    if not (
        user.role == "admin"
        or voice.user_id == user.id
        or has_access(user.id, "read", voice.access_control)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    text = (form_data.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Type a line to preview the voice.")

    def _read_sample(fid: str) -> tuple[str, bytes, str]:
        """Resolve a file id to (filename, bytes, content_type) or raise."""
        f = Files.get_file_by_id(fid)
        if not f or not f.path:
            raise HTTPException(status_code=404, detail="Voice sample not found.")
        try:
            local_path = Storage.get_file(f.path)
            with open(local_path, "rb") as fh:
                data_bytes = fh.read()
        except Exception as e:
            log.warning("voice preview %s: could not read sample %s (%r)", id, fid, e)
            raise HTTPException(status_code=500, detail="Could not read the voice sample.")
        if not data_bytes:
            raise HTTPException(status_code=400, detail="A voice sample is empty.")
        return (
            f.filename or "sample.wav",
            data_bytes,
            (f.meta or {}).get("content_type") or "audio/wav",
        )

    # Build the reference list. Blend mode (2+ weighted samples) wins; otherwise a
    # single sample (the requested file, else the voice's first).
    refs = [r for r in (form_data.references or []) if r.file_id]
    if len(refs) >= 2:
        samples = [(_read_sample(r.file_id), r.weight) for r in refs]
    else:
        single = refs[0].file_id if refs else form_data.file_id
        if not single:
            ids = VoiceFiles.get_file_ids_by_voice_id(id)
            if not ids:
                raise HTTPException(status_code=400, detail="Add a voice sample before previewing.")
            single = ids[0]
        samples = [(_read_sample(single), 1.0)]

    control = str(request.app.state.config.TTS_CONTROL_BASE_URL or "").rstrip("/")
    if not control:
        raise HTTPException(status_code=503, detail="The TTS control endpoint is not configured.")

    data = {"text": text, "response_format": "wav"}
    if form_data.exaggeration is not None:
        data["exaggeration"] = str(form_data.exaggeration)
    if form_data.cfg_weight is not None:
        data["cfg_weight"] = str(form_data.cfg_weight)

    if len(samples) >= 2:
        # BLEND: repeated `reference` files + parallel `weights` → self.speak /blend.
        upload = [("reference", (fn, b, ct)) for (fn, b, ct), _ in samples]
        data["weights"] = [str(w) for _, w in samples]
        endpoint = f"{control}/api/voices/blend"
    else:
        (fn, b, ct), _ = samples[0]
        upload = {"reference": (fn, b, ct)}
        endpoint = f"{control}/api/voices/preview"

    headers = {TICKET_HEADER: mint_service_ticket("self.speak", "audio:synthesize")}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0)) as client:
            resp = await client.post(endpoint, files=upload, data=data, headers=headers)
    except httpx.HTTPError as e:
        log.warning("voice preview %s: self.speak unreachable (%r)", id, e)
        raise HTTPException(status_code=502, detail="The voice engine is unreachable.")
    if resp.status_code == 503:
        raise HTTPException(status_code=503, detail="The Chatterbox voice engine is not available.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Preview failed ({resp.status_code}).")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "audio/wav"),
        headers={"Cache-Control": "no-store"},
    )
