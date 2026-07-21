import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, Boolean, Column, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, get_db
from selfai_ui.models.chats import Chats

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# Folder DB Schema
####################


class Folder(Base):
    __tablename__ = "folder"
    id = Column(Text, primary_key=True)
    parent_id = Column(Text, nullable=True)
    user_id = Column(Text)
    name = Column(Text)
    items = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)
    is_expanded = Column(Boolean, default=False)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class FolderPresetModel(BaseModel):
    """Typed shape of a chat folder's preset configuration.

    A folder's preset carries retrieval defaults that a chat created inside the
    folder is seeded from (by a separate, future consumer). All three fields are
    optional: a folder with none of them set carries no preset.

    ``extra="allow"`` provides forward compatibility — future preset attributes
    can be carried without a breaking schema change, and unrecognized keys are
    round-tripped intact rather than being dropped or rejected. Mirrors the
    ``ModelMeta`` pattern in ``models/models.py``.
    """

    default_model_id: Optional[str] = None
    """Reference (id) to the folder's default model, if any."""

    tool_ids: Optional[list[str]] = None
    """References (ids) to the folder's default tools, if any."""

    knowledge_ids: Optional[list[str]] = None
    """References (ids) to the folder's attached knowledge — a single list
    covering both knowledge bases and datasets (a dataset is a knowledge
    record), not two separate lists."""

    model_config = ConfigDict(extra="allow")


class FolderModel(BaseModel):
    id: str
    parent_id: Optional[str] = None
    user_id: str
    name: str
    items: Optional[dict] = None
    meta: Optional[dict] = None
    is_expanded: bool = False
    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class FolderForm(BaseModel):
    name: str
    model_config = ConfigDict(extra="allow")


class FolderTable:
    def insert_new_folder(self, user_id: str, name: str, parent_id: Optional[str] = None) -> Optional[FolderModel]:
        with get_db() as db:
            id = str(uuid.uuid4())
            folder = FolderModel(
                **{
                    "id": id,
                    "user_id": user_id,
                    "name": name,
                    "parent_id": parent_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )
            try:
                result = Folder(**folder.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                if result:
                    return FolderModel.model_validate(result)
                else:
                    return None
            except Exception as e:
                print(e)
                return None

    def get_folder_by_id_and_user_id(self, id: str, user_id: str) -> Optional[FolderModel]:
        try:
            with get_db() as db:
                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()

                if not folder:
                    return None

                return FolderModel.model_validate(folder)
        except Exception:
            return None

    def get_children_folders_by_id_and_user_id(self, id: str, user_id: str) -> Optional[FolderModel]:
        try:
            with get_db() as db:
                folders = []

                def get_children(folder):
                    children = self.get_folders_by_parent_id_and_user_id(folder.id, user_id)
                    for child in children:
                        get_children(child)
                        folders.append(child)

                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()
                if not folder:
                    return None

                get_children(folder)
                return folders
        except Exception:
            return None

    def get_folders_by_user_id(self, user_id: str) -> list[FolderModel]:
        with get_db() as db:
            return [FolderModel.model_validate(folder) for folder in db.query(Folder).filter_by(user_id=user_id).all()]

    def get_folder_by_parent_id_and_user_id_and_name(
        self, parent_id: Optional[str], user_id: str, name: str
    ) -> Optional[FolderModel]:
        try:
            with get_db() as db:
                # Check if folder exists
                folder = (
                    db.query(Folder)
                    .filter_by(parent_id=parent_id, user_id=user_id)
                    .filter(Folder.name.ilike(name))
                    .first()
                )

                if not folder:
                    return None

                return FolderModel.model_validate(folder)
        except Exception as e:
            log.error(f"get_folder_by_parent_id_and_user_id_and_name: {e}")
            return None

    def get_folders_by_parent_id_and_user_id(self, parent_id: Optional[str], user_id: str) -> list[FolderModel]:
        with get_db() as db:
            return [
                FolderModel.model_validate(folder)
                for folder in db.query(Folder).filter_by(parent_id=parent_id, user_id=user_id).all()
            ]

    def update_folder_parent_id_by_id_and_user_id(
        self,
        id: str,
        user_id: str,
        parent_id: str,
    ) -> Optional[FolderModel]:
        try:
            with get_db() as db:
                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()

                if not folder:
                    return None

                folder.parent_id = parent_id
                folder.updated_at = int(time.time())

                db.commit()

                return FolderModel.model_validate(folder)
        except Exception as e:
            log.error(f"update_folder: {e}")
            return

    def update_folder_name_by_id_and_user_id(self, id: str, user_id: str, name: str) -> Optional[FolderModel]:
        try:
            with get_db() as db:
                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()

                if not folder:
                    return None

                existing_folder = (
                    db.query(Folder).filter_by(name=name, parent_id=folder.parent_id, user_id=user_id).first()
                )

                if existing_folder:
                    return None

                folder.name = name
                folder.updated_at = int(time.time())

                db.commit()

                return FolderModel.model_validate(folder)
        except Exception as e:
            log.error(f"update_folder: {e}")
            return

    def update_folder_is_expanded_by_id_and_user_id(
        self, id: str, user_id: str, is_expanded: bool
    ) -> Optional[FolderModel]:
        try:
            with get_db() as db:
                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()

                if not folder:
                    return None

                folder.is_expanded = is_expanded
                folder.updated_at = int(time.time())

                db.commit()

                return FolderModel.model_validate(folder)
        except Exception as e:
            log.error(f"update_folder: {e}")
            return

    def update_folder_meta_by_id_and_user_id(
        self, id: str, user_id: str, meta: dict
    ) -> Optional[FolderModel]:
        """Persist folder metadata (e.g. the preset) into the folder's existing
        ``meta`` JSON column — cavekit-chat-folders-rag-config.md R2.

        The provided ``meta`` mapping is shallow-merged into the folder's
        existing ``meta`` at the top level: keys present in ``meta`` are set (or
        replaced), keys absent from ``meta`` are left untouched. This is the
        generic top-level primitive; field-level merge *within* the preset (an
        update that omits some preset fields) is a separate concern (T-004).

        ``meta`` is already a JSON column on the ``folder`` table, so no schema
        migration is required (R2 AC3). Reassigning ``folder.meta`` to a new dict
        (rather than mutating in place) is what makes SQLAlchemy detect the
        change on a plain ``JSON`` column.
        """
        try:
            with get_db() as db:
                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()

                if not folder:
                    return None

                folder.meta = {**(folder.meta or {}), **(meta or {})}
                folder.updated_at = int(time.time())

                db.commit()

                return FolderModel.model_validate(folder)
        except Exception as e:
            log.error(f"update_folder_meta: {e}")
            return None

    def delete_folder_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            with get_db() as db:
                folder = db.query(Folder).filter_by(id=id, user_id=user_id).first()
                if not folder:
                    return False

                # Delete all chats in the folder
                Chats.delete_chats_by_user_id_and_folder_id(user_id, folder.id)

                # Delete all children folders
                def delete_children(folder):
                    folder_children = self.get_folders_by_parent_id_and_user_id(folder.id, user_id)
                    for folder_child in folder_children:
                        Chats.delete_chats_by_user_id_and_folder_id(user_id, folder_child.id)
                        delete_children(folder_child)

                        folder = db.query(Folder).filter_by(id=folder_child.id).first()
                        db.delete(folder)
                        db.commit()

                delete_children(folder)
                db.delete(folder)
                db.commit()
                return True
        except Exception as e:
            log.error(f"delete_folder: {e}")
            return False


Folders = FolderTable()
