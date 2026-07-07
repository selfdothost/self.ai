import logging
from typing import Optional

import aiohttp

from fastapi import Depends, HTTPException, Request, APIRouter
from pydantic import BaseModel

from selfai_ui.utils.auth import get_admin_user
from selfai_ui.env import SRC_LOG_LEVELS, AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST
from selfai_ui.constants import ERROR_MESSAGES

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("LANGUAGE_EVAL", logging.INFO))

router = APIRouter()


##########################################
# Connection verification
##########################################


class ConnectionVerificationForm(BaseModel):
    url: str


@router.post("/verify")
async def verify_connection(
    form_data: ConnectionVerificationForm, user=Depends(get_admin_user)
):
    url = form_data.url.rstrip("/")
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    ) as session:
        try:
            async with session.get(f"{url}/health") as r:
                if r.status != 200:
                    raise Exception(f"HTTP Error: {r.status}")
                data = await r.json()
                return data
        except aiohttp.ClientError as e:
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(status_code=500, detail="Self.AI UI: language-eval Connection Error")
        except Exception as e:
            log.exception(f"Connection error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Self.AI UI: {str(e)}")


##########################################
# Configuration
##########################################


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_LANGUAGE_EVAL_API": request.app.state.config.ENABLE_LANGUAGE_EVAL_API,
        "LANGUAGE_EVAL_BASE_URLS": request.app.state.config.LANGUAGE_EVAL_BASE_URLS,
    }


class LanguageEvalConfigForm(BaseModel):
    ENABLE_LANGUAGE_EVAL_API: Optional[bool] = None
    LANGUAGE_EVAL_BASE_URLS: list[str]


@router.post("/config/update")
async def update_config(
    request: Request, form_data: LanguageEvalConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.ENABLE_LANGUAGE_EVAL_API = form_data.ENABLE_LANGUAGE_EVAL_API
    request.app.state.config.LANGUAGE_EVAL_BASE_URLS = form_data.LANGUAGE_EVAL_BASE_URLS
    return {
        "ENABLE_LANGUAGE_EVAL_API": request.app.state.config.ENABLE_LANGUAGE_EVAL_API,
        "LANGUAGE_EVAL_BASE_URLS": request.app.state.config.LANGUAGE_EVAL_BASE_URLS,
    }
