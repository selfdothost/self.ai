import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.chats import Chats
from selfai_ui.models.folders import (
    FolderForm,
    FolderModel,
    FolderPresetModel,
    Folders,
)
from selfai_ui.utils import folder_presets
from selfai_ui.utils.auth import get_verified_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


router = APIRouter()


############################
# Get Folders
############################


@router.get("/", response_model=list[FolderModel])
async def get_folders(user=Depends(get_verified_user)):
    folders = Folders.get_folders_by_user_id(user.id)

    return [
        {
            **folder.model_dump(),
            "items": {
                "chats": [
                    {"title": chat.title, "id": chat.id}
                    for chat in Chats.get_chats_by_folder_id_and_user_id(folder.id, user.id)
                ]
            },
        }
        for folder in folders
    ]


############################
# Create Folder
############################


@router.post("/")
def create_folder(form_data: FolderForm, user=Depends(get_verified_user)):
    folder = Folders.get_folder_by_parent_id_and_user_id_and_name(None, user.id, form_data.name)

    if folder:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Folder already exists"),
        )

    try:
        folder = Folders.insert_new_folder(user.id, form_data.name)
        return folder
    except Exception as e:
        log.exception(e)
        log.error("Error creating folder")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error creating folder"),
        )


############################
# Get Folders By Id
############################


@router.get("/{id}", response_model=Optional[FolderModel])
async def get_folder_by_id(id: str, user=Depends(get_verified_user)):
    folder = Folders.get_folder_by_id_and_user_id(id, user.id)
    if folder:
        return folder
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Update Folder Name By Id
############################


# The three named preset fields (cavekit R1) that this update path persists into
# the folder's ``meta`` (cavekit R2). Any of them appearing in the request marks
# the update as carrying a preset; a plain rename (name only) carries none.
PRESET_FIELDS = {"default_model_id", "tool_ids", "knowledge_ids"}


@router.post("/{id}/update")
async def update_folder_name_by_id(id: str, form_data: FolderForm, user=Depends(get_verified_user)):
    folder = Folders.get_folder_by_id_and_user_id(id, user.id)
    if not folder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    # Preset presence gate — cavekit R2. The three preset fields ride in on this
    # same pre-existing update path (no new endpoint) as extra fields on the
    # form. A plain rename-only update carries NONE of them, so ``has_preset`` is
    # False and the meta-write path below is skipped entirely — the stored
    # ``meta["preset"]`` is never touched or cleared (R2 AC4: merge-on-omit,
    # never replace). FolderPresetModel tolerates and round-trips unrecognized
    # keys (R1 forward-compat).
    extra = form_data.model_extra or {}
    has_preset = bool(PRESET_FIELDS & set(extra))
    preset = None

    if has_preset:
        # cavekit R3: validate every reference the preset carries (default
        # model, tools, knowledge) against a real record accessible to the
        # writer BEFORE any database write happens. A non-empty unresolved list
        # rejects the WHOLE update atomically (R3 AC4 — nothing persists, not
        # even a concurrent rename), and the error detail stays generic so it
        # never reveals which reference failed nor whether an existing record
        # was merely inaccessible vs. nonexistent (R3 no-existence-leak).
        preset_model = FolderPresetModel(**extra)
        if folder_presets.unresolved_preset_references(preset_model, user):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating folder"),
            )
        preset = preset_model.model_dump()

    # Rename — unchanged behavior. Only run the duplicate-name check and the name
    # write when the name actually changes; a preset-only update re-submits the
    # folder's current name, which must not be treated as a self-collision.
    if form_data.name != folder.name:
        existing_folder = Folders.get_folder_by_parent_id_and_user_id_and_name(
            folder.parent_id, user.id, form_data.name
        )
        if existing_folder:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Folder already exists"),
            )

        try:
            folder = Folders.update_folder_name_by_id_and_user_id(id, user.id, form_data.name)
        except Exception as e:
            log.exception(e)
            log.error(f"Error updating folder: {id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating folder"),
            )

    # Preset persist — cavekit R2. Only reached once every reference has
    # resolved. Persist the validated, typed preset into the folder's existing
    # ``meta`` JSON column (no migration). An update omitting the preset fields
    # never reaches here, so its stored preset is left untouched.
    if has_preset:
        try:
            updated = Folders.update_folder_meta_by_id_and_user_id(id, user.id, {"preset": preset})
            if updated is not None:
                folder = updated
        except Exception as e:
            log.exception(e)
            log.error(f"Error updating folder preset: {id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating folder"),
            )

    return folder


############################
# Update Folder Parent Id By Id
############################


class FolderParentIdForm(BaseModel):
    parent_id: Optional[str] = None


@router.post("/{id}/update/parent")
async def update_folder_parent_id_by_id(id: str, form_data: FolderParentIdForm, user=Depends(get_verified_user)):
    folder = Folders.get_folder_by_id_and_user_id(id, user.id)
    if folder:
        existing_folder = Folders.get_folder_by_parent_id_and_user_id_and_name(
            form_data.parent_id, user.id, folder.name
        )

        if existing_folder:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Folder already exists"),
            )

        try:
            folder = Folders.update_folder_parent_id_by_id_and_user_id(id, user.id, form_data.parent_id)
            return folder
        except Exception as e:
            log.exception(e)
            log.error(f"Error updating folder: {id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating folder"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Update Folder Is Expanded By Id
############################


class FolderIsExpandedForm(BaseModel):
    is_expanded: bool


@router.post("/{id}/update/expanded")
async def update_folder_is_expanded_by_id(id: str, form_data: FolderIsExpandedForm, user=Depends(get_verified_user)):
    folder = Folders.get_folder_by_id_and_user_id(id, user.id)
    if folder:
        try:
            folder = Folders.update_folder_is_expanded_by_id_and_user_id(id, user.id, form_data.is_expanded)
            return folder
        except Exception as e:
            log.exception(e)
            log.error(f"Error updating folder: {id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating folder"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Delete Folder By Id
############################


@router.delete("/{id}")
async def delete_folder_by_id(id: str, user=Depends(get_verified_user)):
    folder = Folders.get_folder_by_id_and_user_id(id, user.id)
    if folder:
        try:
            result = Folders.delete_folder_by_id_and_user_id(id, user.id)
            if result:
                return result
            else:
                raise Exception("Error deleting folder")
        except Exception as e:
            log.exception(e)
            log.error(f"Error deleting folder: {id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error deleting folder"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
