import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from selfai_ui.utils.auth import get_admin_user
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.job_windows import (
    JobWindows,
    JobWindowForm,
    JobWindowWithSlots,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

router = APIRouter()


@router.get("", response_model=list[JobWindowWithSlots])
async def list_windows(user=Depends(get_admin_user)):
    return JobWindows.get_all_windows()


@router.post("", response_model=JobWindowWithSlots)
async def create_window(form_data: JobWindowForm, user=Depends(get_admin_user)):
    if form_data.end_at <= form_data.start_at:
        raise HTTPException(status_code=400, detail="end_at must be after start_at")
    window = JobWindows.insert_new_window(form_data)
    if not window:
        raise HTTPException(status_code=500, detail="Failed to create window")
    return window


@router.get("/{id}", response_model=JobWindowWithSlots)
async def get_window(id: str, user=Depends(get_admin_user)):
    window = JobWindows.get_window_by_id(id)
    if not window:
        raise HTTPException(status_code=404, detail="Window not found")
    return window


@router.put("/{id}", response_model=JobWindowWithSlots)
async def update_window(id: str, form_data: JobWindowForm, user=Depends(get_admin_user)):
    existing = JobWindows.get_window_by_id(id)
    if not existing:
        raise HTTPException(status_code=404, detail="Window not found")
    if form_data.end_at <= form_data.start_at:
        raise HTTPException(status_code=400, detail="end_at must be after start_at")

    update_data = form_data.model_dump()
    # slots need to be serialised as plain dicts for the model layer
    update_data["slots"] = [s.model_dump() for s in form_data.slots]

    window = JobWindows.update_window(id, update_data)
    if not window:
        raise HTTPException(status_code=500, detail="Failed to update window")
    return window


@router.delete("/{id}")
async def delete_window(id: str, user=Depends(get_admin_user)):
    window = JobWindows.get_window_by_id(id)
    if not window:
        raise HTTPException(status_code=404, detail="Window not found")

    # Reject delete on an active window
    now = int(time.time())
    if window.start_at <= now <= window.end_at and window.enabled:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active window. Disable it first or wait for it to complete.",
        )

    success = JobWindows.delete_window(id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete window")
    return {"message": "Window deleted"}
