# test_anthropic_messages.py
#
# Route-level cover for POST /v1/messages and GET /v1/models (self.ai#112) -- the
# inbound Anthropic surface. The block-by-block conversion is covered in
# tests/test_anthropic_inbound.py; what is tested here is the wiring the converters
# cannot see: that an Anthropic client's `x-api-key` authenticates, that the request
# reaches the ordinary chat-completions path in OpenAI shape, and that failures come
# back in the Anthropic error envelope rather than self.ai's.

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from starlette.responses import StreamingResponse

MODEL_ID = "test-local-model"


@pytest.fixture
def registered_model(test_app):
    """Put a model in the live catalog so the endpoint's own lookup succeeds."""
    original = test_app.state.MODELS
    test_app.state.MODELS = {MODEL_ID: {"id": MODEL_ID, "name": "Test Local Model", "created": 1_700_000_000}}
    yield MODEL_ID
    test_app.state.MODELS = original


def openai_completion(content="hello", finish_reason="stop"):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL_ID,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def messages_body(**overrides):
    body = {"model": MODEL_ID, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


##########################################
# Auth
##########################################


def test_unauthenticated_request_is_rejected(client, registered_model):
    assert client.post("/v1/messages", json=messages_body()).status_code == 403


def test_x_api_key_header_authenticates(client, test_user, db_session, registered_model):
    """The header an Anthropic client actually sends.

    Anthropic SDKs send `x-api-key`, not `Authorization: Bearer`. Before this the
    header was ignored entirely, so a correctly-configured client got a 403 with no
    indication that the credential had simply not been looked at.
    """
    api_key = "sk-anthropic-inbound-test"
    db_session.execute(text("UPDATE [user] SET api_key = :k WHERE id = :id"), {"k": api_key, "id": test_user["id"]})
    db_session.commit()

    with patch("selfai_ui.main.chat_completion", new=AsyncMock(return_value=openai_completion())):
        response = client.post("/v1/messages", json=messages_body(), headers={"x-api-key": api_key})

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "hello"


def test_bearer_token_still_works(authenticated_user, registered_model):
    with patch("selfai_ui.main.chat_completion", new=AsyncMock(return_value=openai_completion())):
        response = authenticated_user.post("/v1/messages", json=messages_body())

    assert response.status_code == 200


##########################################
# Request validation and error envelope
##########################################


def test_unknown_model_is_a_404_in_the_anthropic_envelope(authenticated_user, registered_model):
    response = authenticated_user.post("/v1/messages", json=messages_body(model="nope"))

    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_found_error"
    assert "nope" in body["error"]["message"]


def test_missing_max_tokens_is_rejected(authenticated_user, registered_model):
    # Required on Anthropic, optional on OpenAI -- without this check the conversion
    # succeeds and the client gets a reply it considers invalid.
    body = messages_body()
    del body["max_tokens"]
    response = authenticated_user.post("/v1/messages", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_missing_model_is_rejected(authenticated_user, registered_model):
    body = messages_body()
    del body["model"]
    response = authenticated_user.post("/v1/messages", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_downstream_http_errors_keep_their_status_and_structured_detail(authenticated_user, registered_model):
    """A GPU-window refusal must stay readable through the translation.

    self.ai#103 is the cautionary case: a structured 503 body collapsed into a bare
    string and became undiagnosable. The Anthropic envelope has no field for it, so
    the dict is carried through under `detail` rather than flattened away.
    """
    from fastapi import HTTPException

    detail = {"detail": "GPU dedicated to the active training window", "gpu_locked_by": "training"}
    with patch(
        "selfai_ui.main.chat_completion",
        new=AsyncMock(side_effect=HTTPException(status_code=503, detail=detail)),
    ):
        response = authenticated_user.post("/v1/messages", json=messages_body())

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["message"] == "GPU dedicated to the active training window"
    assert body["error"]["detail"]["gpu_locked_by"] == "training"


##########################################
# Delegation and response shape
##########################################


def test_request_reaches_chat_completion_in_openai_shape(authenticated_user, registered_model):
    """The endpoint converts and delegates; it does not re-implement the pipeline.

    This is also how per-user model access control, the GPU-window checkpoint, and
    the persistence path apply to this surface -- they are chat_completion's job, so
    what has to be verified here is that the call actually goes through it.
    """
    mock = AsyncMock(return_value=openai_completion())
    with patch("selfai_ui.main.chat_completion", new=mock):
        authenticated_user.post(
            "/v1/messages",
            json=messages_body(system="be terse", stop_sequences=["END"], temperature=0.5),
        )

    payload = mock.await_args.args[1]
    assert payload["model"] == MODEL_ID
    assert payload["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    assert payload["max_tokens"] == 64
    assert payload["stop"] == ["END"]
    assert payload["temperature"] == 0.5
    assert payload["stream"] is False


def test_non_streaming_response_is_an_anthropic_message(authenticated_user, registered_model):
    with patch("selfai_ui.main.chat_completion", new=AsyncMock(return_value=openai_completion())):
        response = authenticated_user.post("/v1/messages", json=messages_body())

    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["id"].startswith("msg_")
    assert body["model"] == MODEL_ID
    assert body["content"] == [{"type": "text", "text": "hello"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 2}


def test_streaming_response_is_anthropic_sse(authenticated_user, registered_model):
    async def openai_stream():
        for chunk in (
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}], "model": MODEL_ID},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "model": MODEL_ID},
        ):
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    upstream = StreamingResponse(openai_stream(), media_type="text/event-stream")
    with patch("selfai_ui.main.chat_completion", new=AsyncMock(return_value=upstream)):
        response = authenticated_user.post("/v1/messages", json=messages_body(stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    event_names = [
        line.removeprefix("event:").strip() for line in response.text.split("\n") if line.startswith("event:")
    ]
    assert event_names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # The model the client asked for, not whatever the pipeline resolved internally.
    assert '"model": "test-local-model"' in response.text


##########################################
# Model listing
##########################################


def test_v1_models_returns_the_anthropic_listing_shape(authenticated_user, registered_model):
    with patch(
        "selfai_ui.main.get_models",
        new=AsyncMock(return_value={"data": [{"id": MODEL_ID, "name": "Test Local Model", "created": 1_700_000_000}]}),
    ):
        response = authenticated_user.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    assert body["first_id"] == MODEL_ID
    assert body["last_id"] == MODEL_ID
    assert body["data"] == [
        {
            "type": "model",
            "id": MODEL_ID,
            "display_name": "Test Local Model",
            "created_at": "2023-11-14T22:13:20Z",
        }
    ]
