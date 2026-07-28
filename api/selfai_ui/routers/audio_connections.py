"""Audio connections CRUD router (cavekit-audio-connections.md R1/R2).

The typed-connection domain (``selfai_ui.audio.connections``) supplies the
type-first chooser, the per-type field sets, and a persisted
:class:`AudioConnectionStore`, but the kit deferred the HTTP surface that exposes
that CRUD to the Connections page ("the router endpoints that expose this CRUD
are wired where the audio Connections surface is assembled"). Until it existed
there was no way to *create* a typed audio connection at all — only the one-time
legacy migration could mint them — so a self-hosted STT/TTS connection could not
be brought into being for the voice-catalog / transcribe-model management
surfaces to read.

This router is that surface. It wires the existing domain store to the persisted
``AUDIO_CONNECTION_CONFIGS`` PersistentConfig, mirroring the text-model
multi-connection stores (``OLLAMA_API_CONFIGS`` / ``OPENAI_API_CONFIGS``):

  GET    /types           the type-first chooser payload (the five types +
                          each self-hosted type's management action).
  GET    ""               list every saved connection (secrets masked).
  POST   ""               create a connection of a chosen type with its fields.
  PATCH  /{id}            merge new field values into one connection.
  DELETE /{id}            remove one connection by id.

Admin-gated: creating/curating backend connections is a management action.
Secret fields (hosted-provider API keys) are masked in every response — the
stored value is never echoed back — so listing connections cannot leak a
credential. The self-hosted types carry no secret fields.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from selfai_ui.audio.connections import (
    AudioConnectionStore,
    AudioConnectionType,
    ConnectionNotFoundError,
    SavedAudioConnection,
    UnknownFieldError,
    available_connection_types,
    connection_display_label,
    fields_for,
    get_type_info,
    management_action_for_type,
    serialize_connection_type,
    serialize_management_action,
)
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.auth import get_admin_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("AUDIO", logging.INFO))

router = APIRouter()

# Placeholder shown in place of a secret field's stored value, so a credential is
# never echoed back to a client while the field's presence stays visible.
_SECRET_MASK = "********"


def _load_store(request: Request) -> AudioConnectionStore:
    """Rebuild the connection store from the persisted config."""
    raw = getattr(request.app.state.config, "AUDIO_CONNECTION_CONFIGS", None)
    return AudioConnectionStore.from_serializable(raw if isinstance(raw, dict) else {})


def _save_store(request: Request, store: AudioConnectionStore) -> None:
    """Persist the store back to ``AUDIO_CONNECTION_CONFIGS`` (survives reload).

    Reassigning the attribute drives ``AppConfig.__setattr__`` -> the underlying
    ``PersistentConfig.save()``, the same persist path the transcribe/voice
    curation toggles use.
    """
    request.app.state.config.AUDIO_CONNECTION_CONFIGS = store.to_serializable()


def _mask_fields(connection: SavedAudioConnection) -> dict:
    """Return the connection's field values with secret fields masked."""
    secret_names = {f.name for f in fields_for(connection.type) if f.secret}
    masked: dict = {}
    for name, value in connection.fields.items():
        masked[name] = _SECRET_MASK if name in secret_names and value else value
    return masked


def _serialize(connection: SavedAudioConnection) -> dict:
    """Serialize a saved connection for the Connections surface (secrets masked)."""
    info = get_type_info(connection.type)
    return {
        "id": connection.id,
        "type": connection.type.value,
        "label": connection_display_label(connection),
        "fields": _mask_fields(connection),
        "management_action": serialize_management_action(
            management_action_for_type(info)
        ),
    }


@router.get("/types")
async def list_connection_types(user=Depends(get_admin_user)):
    """Return the type-first chooser payload (R1): the five selectable types."""
    return {
        "types": [serialize_connection_type(info) for info in available_connection_types()]
    }


@router.get("")
async def list_connections(request: Request, user=Depends(get_admin_user)):
    """List every saved audio connection, in insertion order (R2 AC5)."""
    store = _load_store(request)
    return {"connections": [_serialize(c) for c in store.list()]}


class CreateConnectionForm(BaseModel):
    """Body for creating a connection: the chosen type and its field values."""

    type: str
    fields: dict = {}


@router.post("")
async def create_connection(
    form_data: CreateConnectionForm, request: Request, user=Depends(get_admin_user)
):
    """Create a connection of a chosen type with its type-appropriate fields (R1/R2).

    The type must be one of the five :class:`AudioConnectionType` values, and
    every field must belong to that type (an unknown field is a 400) — the store
    validates the field set. A fresh id is minted, so two connections of the same
    type coexist independently (R2 AC1/AC2).
    """
    try:
        conn_type = AudioConnectionType(form_data.type)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"unknown connection type {form_data.type!r}"
        )

    store = _load_store(request)
    try:
        connection = store.add(conn_type, form_data.fields)
    except UnknownFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _save_store(request, store)
    return _serialize(connection)


class UpdateConnectionForm(BaseModel):
    """Body for updating a connection: the field values to merge in."""

    fields: dict


@router.patch("/{connection_id}")
async def update_connection(
    connection_id: str,
    form_data: UpdateConnectionForm,
    request: Request,
    user=Depends(get_admin_user),
):
    """Merge field values into one connection, addressed by id (R2 AC3).

    Only the addressed connection is touched; any sibling — including one of the
    same type — is left unchanged. An unknown field for the connection's type is
    a 400; an unknown id is a 404.
    """
    store = _load_store(request)
    try:
        connection = store.update_fields(connection_id, form_data.fields)
    except ConnectionNotFoundError:
        raise HTTPException(status_code=404, detail="no such audio connection")
    except UnknownFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _save_store(request, store)
    return _serialize(connection)


@router.delete("/{connection_id}")
async def delete_connection(
    connection_id: str, request: Request, user=Depends(get_admin_user)
):
    """Remove one connection by id (R2 AC4); an unknown id is a 404."""
    store = _load_store(request)
    try:
        store.delete(connection_id)
    except ConnectionNotFoundError:
        raise HTTPException(status_code=404, detail="no such audio connection")

    _save_store(request, store)
    return {"id": connection_id, "status": "deleted"}
