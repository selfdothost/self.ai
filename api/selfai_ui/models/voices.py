import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Index, String, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, get_db
from selfai_ui.models.files import FileMetadataResponse
from selfai_ui.models.users import UserResponse, Users
from selfai_ui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

####################
# Voice DB Schema
####################
#
# A "voice" is a first-class, RBAC'd Workspace asset (Sound Studio, Phase 1). It
# is the container the user builds on the Voices canvas: it owns a node-graph
# pipeline (`graph`) and a set of attached sample/reference audio files (the
# `voice_file` join table). The access model is a faithful copy of Knowledge
# (models/knowledge.py) so the same AccessControl UI + has_access() apply.


class Voice(Base):
    __tablename__ = "voice"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    name = Column(Text)
    description = Column(Text)

    # The node-graph pipeline that builds this voice — a STRUCTURED field on the
    # record ({nodes, edges, ...}), not a serialized-file hack. One voice owns one
    # graph (Sound Studio container-update model).
    graph = Column(JSON, nullable=True)

    # Provenance / consent / engine metadata (populated in later phases).
    meta = Column(JSON, nullable=True)

    access_control = Column(JSON, nullable=True)  # Controls data access levels.
    # Mirrors Knowledge's convention:
    # - `None`: Public access, available to all users with the "user" role.
    # - `{}`: Private access, restricted exclusively to the owner.
    # - Custom: {"read": {"group_ids": [...], "user_ids": [...]},
    #            "write": {"group_ids": [...], "user_ids": [...]}}

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class VoiceModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    name: str
    description: str

    graph: Optional[dict] = None
    meta: Optional[dict] = None

    access_control: Optional[dict] = None

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


####################
# Forms
####################


class VoiceUserModel(VoiceModel):
    user: Optional[UserResponse] = None


class VoiceResponse(VoiceModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class VoiceUserResponse(VoiceUserModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class VoiceForm(BaseModel):
    # Creation is deliberately minimal — a name is enough; building happens on the
    # detail canvas. description/graph are optional so name-only create works.
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    graph: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None


class VoiceTable:
    def insert_new_voice(self, user_id: str, form_data: VoiceForm) -> Optional[VoiceModel]:
        with get_db() as db:
            voice = VoiceModel(
                **{
                    **form_data.model_dump(),
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )

            try:
                result = Voice(**voice.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                if result:
                    return VoiceModel.model_validate(result)
                else:
                    return None
            except Exception:
                return None

    def get_voices(self) -> list[VoiceUserModel]:
        with get_db() as db:
            voices = []
            for voice in db.query(Voice).order_by(Voice.updated_at.desc()).all():
                user = Users.get_user_by_id(voice.user_id)
                voices.append(
                    VoiceUserModel.model_validate(
                        {
                            **VoiceModel.model_validate(voice).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return voices

    def get_voices_by_user_id(self, user_id: str, permission: str = "write") -> list[VoiceUserModel]:
        voices = self.get_voices()
        return [
            voice
            for voice in voices
            if voice.user_id == user_id or has_access(user_id, permission, voice.access_control)
        ]

    def get_voice_by_id(self, id: str) -> Optional[VoiceModel]:
        try:
            with get_db() as db:
                voice = db.query(Voice).filter_by(id=id).first()
                return VoiceModel.model_validate(voice) if voice else None
        except Exception:
            return None

    def update_voice_by_id(self, id: str, form_data: VoiceForm) -> Optional[VoiceModel]:
        try:
            with get_db() as db:
                db.query(Voice).filter_by(id=id).update(
                    {
                        **form_data.model_dump(),
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_voice_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def update_voice_graph_by_id(self, id: str, graph: dict) -> Optional[VoiceModel]:
        try:
            with get_db() as db:
                db.query(Voice).filter_by(id=id).update(
                    {
                        "graph": graph,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_voice_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def update_voice_meta_by_id(self, id: str, meta: dict) -> Optional[VoiceModel]:
        try:
            with get_db() as db:
                db.query(Voice).filter_by(id=id).update(
                    {
                        "meta": meta,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_voice_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def delete_voice_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                # voice_file rows cascade via the FK ON DELETE CASCADE.
                db.query(Voice).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def delete_all_voices(self) -> bool:
        with get_db() as db:
            try:
                db.query(Voice).delete()
                db.commit()
                return True
            except Exception:
                return False


Voices = VoiceTable()


####################
# VoiceFile Join Table (sample/reference audio attached to a voice)
####################


class VoiceFile(Base):
    __tablename__ = "voice_file"

    voice_id = Column(
        Text,
        ForeignKey("voice.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_id = Column(
        String,
        ForeignKey("file.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at = Column(BigInteger)

    __table_args__ = (Index("ix_voice_file_file_id", "file_id"),)


class VoiceFilesTable:
    def add_file_to_voice(self, voice_id: str, file_id: str) -> bool:
        with get_db() as db:
            try:
                existing = db.query(VoiceFile).filter_by(voice_id=voice_id, file_id=file_id).first()
                if existing:
                    return True
                db.add(
                    VoiceFile(
                        voice_id=voice_id,
                        file_id=file_id,
                        created_at=int(time.time()),
                    )
                )
                db.commit()
                return True
            except Exception as e:
                log.exception(e)
                return False

    def remove_file_from_voice(self, voice_id: str, file_id: str) -> bool:
        with get_db() as db:
            try:
                db.query(VoiceFile).filter_by(voice_id=voice_id, file_id=file_id).delete()
                db.commit()
                return True
            except Exception:
                return False

    def get_file_ids_by_voice_id(self, voice_id: str) -> list[str]:
        with get_db() as db:
            rows = (
                db.query(VoiceFile.file_id)
                .filter_by(voice_id=voice_id)
                .order_by(VoiceFile.created_at.desc())
                .all()
            )
            return [row.file_id for row in rows]

    def remove_all_files_from_voice(self, voice_id: str) -> bool:
        with get_db() as db:
            try:
                db.query(VoiceFile).filter_by(voice_id=voice_id).delete()
                db.commit()
                return True
            except Exception:
                return False

    def get_voice_id_for_file(self, file_id: str) -> Optional[str]:
        with get_db() as db:
            row = db.query(VoiceFile.voice_id).filter_by(file_id=file_id).first()
            return row.voice_id if row else None


VoiceFiles = VoiceFilesTable()
