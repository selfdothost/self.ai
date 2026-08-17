from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from selfai_ui.config import ENABLE_PERSISTENT_CONFIG, BannerModel, get_config, save_config
from selfai_ui.utils.auth import get_admin_user, get_verified_user
from selfai_ui.utils.config_redaction import redact_config

router = APIRouter()


############################
# ImportConfig
############################


class ImportConfigForm(BaseModel):
    config: dict


@router.post("/import", response_model=dict)
async def import_config(form_data: ImportConfigForm, user=Depends(get_admin_user)):
    """Replace the stored config blob.

    Refused outright when `ENABLE_PERSISTENT_CONFIG` is False (self.ai#95). With
    the flag off, env/vault is authoritative: `PersistentConfig.update()` will
    not adopt the imported value and boot will not read it either, so the import
    could only ever write a blob that nothing reads — while still parking
    whatever it contained in the config table. Refusing says that out loud
    instead of returning 200 for an operation that did nothing.
    """
    if not ENABLE_PERSISTENT_CONFIG:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "ENABLE_PERSISTENT_CONFIG is False — configuration is managed by the deployment "
                "manifests and environment, so an imported config would not take effect. "
                "Change the manifest instead."
            ),
        )
    save_config(form_data.config)
    return redact_config(get_config())


############################
# ExportConfig
############################


@router.get("/export", response_model=dict)
async def export_config(user=Depends(get_admin_user)):
    """Export the stored config blob, with credentials redacted.

    Previously returned `get_config()` verbatim, so any admin could download
    every live API key, OAuth client secret, LDAP bind password and the
    self.corpus credentials as plaintext JSON (self.ai#95). Redaction applies
    regardless of `ENABLE_PERSISTENT_CONFIG` — gating the writes stops the table
    accumulating new secrets but does not clean what is already in it, and a
    deployment running with persistent config on persists them by design.
    """
    return redact_config(get_config())


############################
# SetDefaultModels
############################
class ModelsConfigForm(BaseModel):
    DEFAULT_MODELS: Optional[str]
    MODEL_ORDER_LIST: Optional[list[str]]


@router.get("/models", response_model=ModelsConfigForm)
async def get_models_config(request: Request, user=Depends(get_admin_user)):
    return {
        "DEFAULT_MODELS": request.app.state.config.DEFAULT_MODELS,
        "MODEL_ORDER_LIST": request.app.state.config.MODEL_ORDER_LIST,
    }


@router.post("/models", response_model=ModelsConfigForm)
async def set_models_config(request: Request, form_data: ModelsConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.DEFAULT_MODELS = form_data.DEFAULT_MODELS
    request.app.state.config.MODEL_ORDER_LIST = form_data.MODEL_ORDER_LIST
    return {
        "DEFAULT_MODELS": request.app.state.config.DEFAULT_MODELS,
        "MODEL_ORDER_LIST": request.app.state.config.MODEL_ORDER_LIST,
    }


class PromptSuggestion(BaseModel):
    title: list[str]
    content: str


class SetDefaultSuggestionsForm(BaseModel):
    suggestions: list[PromptSuggestion]


@router.post("/suggestions", response_model=list[PromptSuggestion])
async def set_default_suggestions(
    request: Request,
    form_data: SetDefaultSuggestionsForm,
    user=Depends(get_admin_user),
):
    data = form_data.model_dump()
    request.app.state.config.DEFAULT_PROMPT_SUGGESTIONS = data["suggestions"]
    return request.app.state.config.DEFAULT_PROMPT_SUGGESTIONS


############################
# SetBanners
############################


class SetBannersForm(BaseModel):
    banners: list[BannerModel]


@router.post("/banners", response_model=list[BannerModel])
async def set_banners(
    request: Request,
    form_data: SetBannersForm,
    user=Depends(get_admin_user),
):
    data = form_data.model_dump()
    request.app.state.config.BANNERS = data["banners"]
    return request.app.state.config.BANNERS


@router.get("/banners", response_model=list[BannerModel])
async def get_banners(
    request: Request,
    user=Depends(get_verified_user),
):
    return request.app.state.config.BANNERS
