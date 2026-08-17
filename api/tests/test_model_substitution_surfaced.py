"""An eval-window model substitution is announced in the response BODY (self.ai#35).

During an eval window the lease checkpoint serves the already-loaded eval model
instead of the one the user asked for (T-016). That fact was carried ONLY in an
`X-Selfai-Model-Substituted` response header, which nothing ever read: the chat
path is consumed server-side and relayed over a websocket, so a response header
never reaches the browser at all. The user asked for model X, got model Y's
answer with no indication, and the chat was saved against X.

Worse, setting that header CHANGED THE RETURN TYPE. `send_post_request` returned
a plain dict normally but a `JSONResponse` whenever `extra_headers` was set --
i.e. only when a substitution had happened -- and `process_chat_response`'s
non-streaming branch does `if "selected_model_id" in response`, which raises
`TypeError: argument of type 'JSONResponse' is not iterable` on a Response
object. So a non-streaming chat request that got substituted blew up instead of
answering.

The fix carries the fact in the body, on both paths, reusing `selected_model_id`
-- the existing "a different model actually answered" channel built for arena
models, already plumbed through the SSE reader, the response middleware and the
client -- plus a `model_substitution` object for the part `selected_model_id`
cannot express: WHY. An arena pick and an eval substitution both leave
served != requested.

WHAT IS STUBBED
---------------
* The upstream llama.cpp call, via `aioresponses`. Everything inside
  `send_post_request` is production code.
* `middleware.get_event_emitter` -- the websocket side-channel, which needs a
  live socket session and a persisted chat row.

WHAT THIS LEAVES UNCOVERED
--------------------------
* The STREAMING middleware branch's persistence. Its `selected_model_id` handler
  sits inside a nested async generator over a live upstream body; the injected
  event's shape is asserted here at the source instead, and the non-streaming
  branch (identical logic) is driven end to end.
* Whether `evaluate_admission` decides to substitute at all -- that is
  `test_lease_admission.py`'s job. These tests start from the decision.
"""

import asyncio
import json

from selfai_ui.routers.llamolotl import send_post_request
from tests.mocks.external_services import aioresponses_strict

UPSTREAM = "http://fake-llamolotl:8080/v1/chat/completions"

REQUESTED = "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL"
SERVED = "Qwen2.5-Coder-32B-Instruct-Q4_K_M"

SUBSTITUTION = {"requested": REQUESTED, "served": SERVED, "reason": "eval_window"}

UPSTREAM_BODY = {
    "id": "chatcmpl-1",
    "model": SERVED,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
}


def _post_non_streaming(**kwargs):
    with aioresponses_strict() as m:
        m.post(UPSTREAM, status=200, payload=UPSTREAM_BODY)
        return asyncio.run(send_post_request(url=UPSTREAM, payload="{}", stream=False, **kwargs))


def _post_streaming(body, **kwargs):
    """POST and drain the body in ONE event loop.

    The streaming response wraps a live aiohttp stream, so collecting it in a
    second asyncio.run() reads from a connection the first loop already closed
    (ClientConnectionError). Returns (chunks, headers).
    """

    async def _run():
        response = await send_post_request(url=UPSTREAM, payload="{}", stream=True, **kwargs)
        chunks = [chunk async for chunk in response.body_iterator]
        return chunks, response.headers

    with aioresponses_strict() as m:
        m.post(UPSTREAM, status=200, body=body)
        return asyncio.run(_run())


# ---- non-streaming --------------------------------------------------------


def test_non_streaming_substitution_returns_a_plain_dict():
    """The regression that mattered: `in` on the result must not raise.

    This is the exact operation process_chat_response performs, and it is what
    a JSONResponse turned into a TypeError.
    """
    res = _post_non_streaming(substitution=SUBSTITUTION)

    assert isinstance(res, dict)
    assert "selected_model_id" in res  # would TypeError on a JSONResponse


def test_non_streaming_substitution_announces_served_model_and_reason():
    res = _post_non_streaming(substitution=SUBSTITUTION)

    # Reuses the existing arena channel, so the message is stored against the
    # model that actually answered.
    assert res["selected_model_id"] == SERVED
    # ...and carries the part that channel cannot express.
    assert res["model_substitution"] == SUBSTITUTION
    # The upstream answer is relayed untouched alongside it.
    assert res["choices"] == UPSTREAM_BODY["choices"]


def test_non_streaming_without_substitution_adds_nothing():
    res = _post_non_streaming(substitution=None)

    assert res == UPSTREAM_BODY
    assert "selected_model_id" not in res
    assert "model_substitution" not in res


def test_non_streaming_return_type_does_not_depend_on_extra_headers():
    """The original defect, pinned without reference to the new parameter.

    `extra_headers` used to flip the non-streaming return from a dict to a
    JSONResponse. Because it was set ONLY on a substitution, substituting was
    exactly what broke the consumer -- and a `substitution=`-free test proves
    the type switch itself is gone, not merely that a new argument exists.
    """
    with_headers = _post_non_streaming(
        extra_headers={"X-Selfai-Model-Substituted": REQUESTED}
    )
    without_headers = _post_non_streaming()

    assert isinstance(with_headers, dict)
    assert type(with_headers) is type(without_headers)
    assert "selected_model_id" in with_headers or with_headers == UPSTREAM_BODY


# ---- streaming ------------------------------------------------------------


def test_streaming_substitution_is_announced_as_the_first_event():
    upstream = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    chunks, _ = _post_streaming(upstream, substitution=SUBSTITUTION)
    first = chunks[0].decode()

    assert first.startswith("data: ")
    announced = json.loads(first[len("data: ") :].strip())
    assert announced["selected_model_id"] == SERVED
    assert announced["model_substitution"] == SUBSTITUTION

    # Upstream's own bytes follow, unaltered -- the announcement is prepended,
    # never a rewrite of the model's output.
    assert b"".join(chunks[1:]) == upstream


def test_streaming_without_substitution_relays_upstream_unaltered():
    upstream = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    chunks, _ = _post_streaming(upstream, substitution=None)

    assert b"".join(chunks) == upstream


def test_streaming_substitution_still_sets_the_header_for_api_callers():
    """The header is kept -- a direct (non-browser) API caller can still read it.
    The body is what the chat path needs; the header costs nothing to keep."""
    _, headers = _post_streaming(
        b"data: [DONE]\n\n",
        substitution=SUBSTITUTION,
        extra_headers={"X-Selfai-Model-Substituted": REQUESTED},
    )

    assert headers["X-Selfai-Model-Substituted"] == REQUESTED


# ---- middleware persistence ----------------------------------------------


def _drive_process_chat_response(monkeypatch, test_app, test_admin, response, requested):
    """Run the non-streaming branch of process_chat_response and return whatever
    it persisted onto the message."""
    import types

    from selfai_ui.models.users import Users
    from selfai_ui.utils import middleware

    captured = {}

    def _capture(chat_id, message_id, payload):
        captured.update(payload)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(middleware.Chats, "upsert_message_to_chat_by_id_and_message_id", _capture)
    monkeypatch.setattr(middleware, "get_event_emitter", lambda *a, **k: _noop)

    asyncio.run(
        middleware.process_chat_response(
            types.SimpleNamespace(app=test_app),
            response,
            {"model": requested},
            Users.get_user_by_id(test_admin["id"]),
            [],
            {"session_id": "s", "chat_id": "c", "message_id": "m"},
            None,
        )
    )
    return captured


def test_non_streaming_response_persists_substitution_on_the_message(
    monkeypatch, test_app, test_admin
):
    """The stored chat message records BOTH which model answered and why, so the
    substitution can still explain itself when the chat is reopened."""
    captured = _drive_process_chat_response(
        monkeypatch,
        test_app,
        test_admin,
        {**UPSTREAM_BODY, "selected_model_id": SERVED, "model_substitution": SUBSTITUTION},
        REQUESTED,
    )

    assert captured["selectedModelId"] == SERVED
    assert captured["modelSubstitution"] == SUBSTITUTION


def test_arena_pick_persists_no_substitution_reason(monkeypatch, test_app, test_admin):
    """An arena pick sets selected_model_id too. It must NOT acquire a
    substitution reason it does not have -- that is the whole point of carrying
    the reason separately rather than inferring it from served != requested."""
    captured = _drive_process_chat_response(
        monkeypatch,
        test_app,
        test_admin,
        {**UPSTREAM_BODY, "selected_model_id": SERVED},
        "arena-model",
    )

    assert captured["selectedModelId"] == SERVED
    assert "modelSubstitution" not in captured
