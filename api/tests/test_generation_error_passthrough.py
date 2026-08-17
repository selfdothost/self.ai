"""Generation-endpoint HTTPException passthrough (self.ai#35).

``/api/chat/completions`` and ``/api/completions`` wrapped EVERY downstream
exception as ``HTTPException(400, detail=str(e))``. A Starlette
``HTTPException`` is an ``Exception``, so a handler that had already chosen a
status and a machine-readable detail body got flattened into a 400 whose detail
is a Python repr:

    400 {"detail": "503: {'detail': 'GPU dedicated to the active training
         window ...', 'gpu_locked_by': 'training'}"}

That is what the GPU-window lease checkpoint (``utils/lease_admission``, #35)
answers with on the path self.chat actually uses. Measured live 2026-08-04
against an active training window: ``/llamolotl/chat/completions`` (no
re-wrap) answered 503 with the structured body, both ``/api`` endpoints
answered 400 with the string above.

These tests pin the contract from the caller's side: an HTTPException raised
downstream reaches the client with its status AND its detail intact, while a
non-HTTP exception still becomes a 400 carrying ``str(e)``.
"""

import pytest
from fastapi import HTTPException

import selfai_ui.main as main

# The refusal lease_admission actually builds for a training window.
GPU_REFUSAL_DETAIL = {
    "detail": (
        "GPU dedicated to the active training window — local inference "
        "temporarily unavailable"
    ),
    "gpu_locked_by": "training",
}

MODEL_ID = "test-local-model"


@pytest.fixture
def stub_models(test_app):
    """Populate app.state.MODELS so the handlers skip their get_all_models()
    refresh (which would reach for real upstreams)."""
    previous = getattr(test_app.state, "MODELS", None)
    test_app.state.MODELS = {MODEL_ID: {"id": MODEL_ID, "name": MODEL_ID, "owned_by": "llamolotl"}}
    yield
    test_app.state.MODELS = previous


def _payload():
    return {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}], "stream": False}


# ---- /api/chat/completions ------------------------------------------------


def test_chat_completions_preserves_gpu_window_refusal(
    monkeypatch, authenticated_admin, stub_models
):
    """The #35 case: a 503 + dict detail from the lease checkpoint survives."""

    async def _payload_passthrough(request, form_data, metadata, user, model):
        return form_data, []

    async def _refuse(request, form_data, user):
        raise HTTPException(status_code=503, detail=GPU_REFUSAL_DETAIL)

    monkeypatch.setattr(main, "process_chat_payload", _payload_passthrough)
    monkeypatch.setattr(main, "generate_chat_completion_with_tools", _refuse)

    res = authenticated_admin.post("/api/chat/completions", json=_payload())

    assert res.status_code == 503
    # The structured body, not a Python repr embedded in a string -- a caller
    # must be able to read gpu_locked_by to tell "GPU policy" from "down".
    assert res.json()["detail"] == GPU_REFUSAL_DETAIL


def test_chat_completions_preserves_status_from_payload_stage(
    monkeypatch, authenticated_admin, stub_models
):
    """The other wrapper: an HTTPException from the process_chat_payload stage
    (e.g. check_model_access's 403) keeps its status too."""

    async def _deny(request, form_data, metadata, user, model):
        raise HTTPException(status_code=403, detail="Model not found")

    monkeypatch.setattr(main, "process_chat_payload", _deny)

    res = authenticated_admin.post("/api/chat/completions", json=_payload())

    assert res.status_code == 403
    assert res.json()["detail"] == "Model not found"


def test_chat_completions_still_wraps_non_http_exceptions(
    monkeypatch, authenticated_admin, stub_models
):
    """Regression guard: the 400 + str(e) behaviour is unchanged for everything
    that is NOT an HTTPException."""

    async def _payload_passthrough(request, form_data, metadata, user, model):
        return form_data, []

    async def _boom(request, form_data, user):
        raise ValueError("upstream exploded")

    monkeypatch.setattr(main, "process_chat_payload", _payload_passthrough)
    monkeypatch.setattr(main, "generate_chat_completion_with_tools", _boom)

    res = authenticated_admin.post("/api/chat/completions", json=_payload())

    assert res.status_code == 400
    assert res.json()["detail"] == "upstream exploded"


# ---- /api/completions -----------------------------------------------------


def test_completions_preserves_gpu_window_refusal(
    monkeypatch, authenticated_admin, stub_models
):
    """lease_admission guards generate_completion too, so this endpoint
    flattened the same 503."""

    async def _refuse(request, form_data, user):
        raise HTTPException(status_code=503, detail=GPU_REFUSAL_DETAIL)

    monkeypatch.setattr(main, "completion_handler", _refuse)

    res = authenticated_admin.post(
        "/api/completions", json={"model": MODEL_ID, "prompt": "hi", "stream": False}
    )

    assert res.status_code == 503
    assert res.json()["detail"] == GPU_REFUSAL_DETAIL


def test_completions_still_wraps_non_http_exceptions(
    monkeypatch, authenticated_admin, stub_models
):
    async def _boom(request, form_data, user):
        raise ValueError("upstream exploded")

    monkeypatch.setattr(main, "completion_handler", _boom)

    res = authenticated_admin.post(
        "/api/completions", json={"model": MODEL_ID, "prompt": "hi", "stream": False}
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "upstream exploded"
