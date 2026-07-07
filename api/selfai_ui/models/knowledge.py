import json
import logging
import time
from typing import Optional
import uuid

from selfai_ui.internal.db import Base, get_db
from selfai_ui.env import SRC_LOG_LEVELS

from selfai_ui.models.files import FileMetadataResponse
from selfai_ui.models.users import Users, UserResponse


from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, ForeignKey, Index, String, Text, JSON

from selfai_ui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

####################
# Knowledge DB Schema
####################


class Knowledge(Base):
    __tablename__ = "knowledge"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    name = Column(Text)
    description = Column(Text)

    data = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)

    access_control = Column(JSON, nullable=True)  # Controls data access levels.
    # Defines access control rules for this entry.
    # - `None`: Public access, available to all users with the "user" role.
    # - `{}`: Private access, restricted exclusively to the owner.
    # - Custom permissions: Specific access control for reading and writing;
    #   Can specify group or user-level restrictions:
    #   {
    #      "read": {
    #          "group_ids": ["group_id1", "group_id2"],
    #          "user_ids":  ["user_id1", "user_id2"]
    #      },
    #      "write": {
    #          "group_ids": ["group_id1", "group_id2"],
    #          "user_ids":  ["user_id1", "user_id2"]
    #      }
    #   }

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    name: str
    description: str

    data: Optional[dict] = None
    meta: Optional[dict] = None

    access_control: Optional[dict] = None

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


####################
# Forms
####################


class KnowledgeUserModel(KnowledgeModel):
    user: Optional[UserResponse] = None


class KnowledgeResponse(KnowledgeModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class KnowledgeUserResponse(KnowledgeUserModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class KnowledgeForm(BaseModel):
    name: str
    description: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None


class KnowledgeTable:
    def insert_new_knowledge(
        self, user_id: str, form_data: KnowledgeForm
    ) -> Optional[KnowledgeModel]:
        with get_db() as db:
            knowledge = KnowledgeModel(
                **{
                    **form_data.model_dump(),
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )

            try:
                result = Knowledge(**knowledge.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                if result:
                    return KnowledgeModel.model_validate(result)
                else:
                    return None
            except Exception:
                return None

    def get_knowledge_bases(self) -> list[KnowledgeUserModel]:
        with get_db() as db:
            knowledge_bases = []
            for knowledge in (
                db.query(Knowledge).order_by(Knowledge.updated_at.desc()).all()
            ):
                user = Users.get_user_by_id(knowledge.user_id)
                knowledge_bases.append(
                    KnowledgeUserModel.model_validate(
                        {
                            **KnowledgeModel.model_validate(knowledge).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return knowledge_bases

    def get_knowledge_bases_by_user_id(
        self, user_id: str, permission: str = "write"
    ) -> list[KnowledgeUserModel]:
        knowledge_bases = self.get_knowledge_bases()
        return [
            knowledge_base
            for knowledge_base in knowledge_bases
            if knowledge_base.user_id == user_id
            or has_access(user_id, permission, knowledge_base.access_control)
        ]

    def get_knowledge_by_id(self, id: str) -> Optional[KnowledgeModel]:
        try:
            with get_db() as db:
                knowledge = db.query(Knowledge).filter_by(id=id).first()
                return KnowledgeModel.model_validate(knowledge) if knowledge else None
        except Exception:
            return None

    def update_knowledge_by_id(
        self, id: str, form_data: KnowledgeForm, overwrite: bool = False
    ) -> Optional[KnowledgeModel]:
        try:
            with get_db() as db:
                knowledge = self.get_knowledge_by_id(id=id)
                db.query(Knowledge).filter_by(id=id).update(
                    {
                        **form_data.model_dump(),
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_knowledge_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def update_knowledge_data_by_id(
        self, id: str, data: dict
    ) -> Optional[KnowledgeModel]:
        try:
            with get_db() as db:
                knowledge = self.get_knowledge_by_id(id=id)
                db.query(Knowledge).filter_by(id=id).update(
                    {
                        "data": data,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_knowledge_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def delete_knowledge_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Knowledge).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def delete_all_knowledge(self) -> bool:
        with get_db() as db:
            try:
                db.query(Knowledge).delete()
                db.commit()

                return True
            except Exception:
                return False


Knowledges = KnowledgeTable()


####################
# KnowledgeFile Join Table
####################


class KnowledgeFile(Base):
    __tablename__ = "knowledge_file"

    knowledge_id = Column(
        Text,
        ForeignKey("knowledge.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_id = Column(
        String,
        ForeignKey("file.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at = Column(BigInteger)

    __table_args__ = (
        Index("ix_knowledge_file_file_id", "file_id"),
    )


class KnowledgeFilesTable:
    def add_file_to_knowledge(self, knowledge_id: str, file_id: str) -> bool:
        with get_db() as db:
            try:
                existing = (
                    db.query(KnowledgeFile)
                    .filter_by(knowledge_id=knowledge_id, file_id=file_id)
                    .first()
                )
                if existing:
                    return True
                db.add(
                    KnowledgeFile(
                        knowledge_id=knowledge_id,
                        file_id=file_id,
                        created_at=int(time.time()),
                    )
                )
                db.commit()
                return True
            except Exception as e:
                log.exception(e)
                return False

    def remove_file_from_knowledge(self, knowledge_id: str, file_id: str) -> bool:
        with get_db() as db:
            try:
                db.query(KnowledgeFile).filter_by(
                    knowledge_id=knowledge_id, file_id=file_id
                ).delete()
                db.commit()
                return True
            except Exception:
                return False

    def get_file_ids_by_knowledge_id(self, knowledge_id: str) -> list[str]:
        with get_db() as db:
            rows = (
                db.query(KnowledgeFile.file_id)
                .filter_by(knowledge_id=knowledge_id)
                .order_by(KnowledgeFile.created_at.desc())
                .all()
            )
            return [row.file_id for row in rows]

    def remove_all_files_from_knowledge(self, knowledge_id: str) -> bool:
        with get_db() as db:
            try:
                db.query(KnowledgeFile).filter_by(
                    knowledge_id=knowledge_id
                ).delete()
                db.commit()
                return True
            except Exception:
                return False

    def get_knowledge_id_for_file(self, file_id: str) -> Optional[str]:
        with get_db() as db:
            row = (
                db.query(KnowledgeFile.knowledge_id)
                .filter_by(file_id=file_id)
                .first()
            )
            return row.knowledge_id if row else None


KnowledgeFiles = KnowledgeFilesTable()
