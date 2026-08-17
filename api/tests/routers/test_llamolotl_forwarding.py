"""
T-R03 (part 2): Llamolotl proxy forwarding.

Llamolotl router uses aiohttp for all upstream calls. Tests use
aioresponses for mocking.
"""

import pytest

from tests.mocks.external_services import aioresponses_strict


@pytest.mark.tier1
def test_llamolotl_verify_success(authenticated_admin):
    """POST /llamolotl/verify probes upstream /health and returns the body."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/health",
            status=200,
            payload={"status": "healthy", "version": "0.1.0"},
        )
        resp = authenticated_admin.post("/llamolotl/verify", json={"url": target, "key": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"


@pytest.mark.tier1
def test_llamolotl_verify_upstream_error_becomes_500(authenticated_admin):
    """Upstream non-200 is caught and converted to 500."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(f"{target}/health", status=503, payload={"error": "unavailable"})
        resp = authenticated_admin.post("/llamolotl/verify", json={"url": target, "key": ""})
    assert resp.status_code == 500


@pytest.mark.tier1
def test_llamolotl_verify_with_bearer_key(authenticated_admin):
    """When a key is provided, the Authorization: Bearer header is forwarded
    to the upstream /health endpoint.

    Uses aioresponses `callback=` to capture the request and assert on
    the exact forwarded Authorization header.
    """
    target = "http://self-llamolotl-auth:8080"
    captured_headers = {}

    def capture_callback(url, **kwargs):
        from aioresponses import CallbackResult

        captured_headers.update(kwargs.get("headers") or {})
        return CallbackResult(status=200, payload={"status": "ok"})

    with aioresponses_strict() as m:
        m.get(f"{target}/health", callback=capture_callback)
        resp = authenticated_admin.post(
            "/llamolotl/verify",
            json={"url": target, "key": "secret-key-123"},
        )

    assert resp.status_code == 200
    # Assert the Authorization header was forwarded with the bearer key
    auth_header = captured_headers.get("Authorization", "")
    assert auth_header == "Bearer secret-key-123", (
        f"Expected 'Bearer secret-key-123' forwarded, got: {auth_header!r}. "
        f"All captured headers: {captured_headers}"
    )


@pytest.mark.tier1
def test_llamolotl_verify_unmocked_url_fails(authenticated_admin):
    """Without a mock, connection error returns 500 with specific detail."""
    resp = authenticated_admin.post(
        "/llamolotl/verify",
        json={"url": "http://nonexistent.invalid.test:9999", "key": ""},
    )
    # Router converts aiohttp.ClientError → 500 with detail containing
    # "Self.AI UI:" prefix per llamolotl.py:273-277
    assert resp.status_code == 500, f"Expected 500 connection error, got {resp.status_code}: " f"{resp.text[:200]}"


@pytest.mark.tier1
def test_llamolotl_config_update_persists(authenticated_admin):
    """Config update round-trip with current config succeeds."""
    current = authenticated_admin.get("/llamolotl/config").json()
    resp = authenticated_admin.post("/llamolotl/config/update", json=current)
    assert resp.status_code == 200, f"Config round-trip returned {resp.status_code}: {resp.text[:200]}"


@pytest.mark.tier1
def test_reload_presets_asks_the_router_to_re_read(authenticated_admin):
    """A models-preset edit is inert until the router re-reads it, and ?reload=1 is that ask.

    Without the flag the call is an ordinary model list and the ConfigMap change
    stays invisible until llama-server restarts — which evicts every resident
    model to change one number.
    """
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/v1/models?reload=1",
            status=200,
            payload={
                "data": [
                    {"id": "gemma", "status": {"value": "unloaded", "preset": "[gemma]\nctx-size = 16384\n"}}
                ]
            },
        )
        resp = authenticated_admin.post("/llamolotl/models/reload-presets")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "gemma"


@pytest.mark.tier1
def test_reload_presets_is_admin_only(authenticated_user):
    """Re-reading server config off disk is an operator action, not a user one."""
    resp = authenticated_user.post("/llamolotl/models/reload-presets")
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_slots_forwards_the_model_param(authenticated_user):
    """self.ai#101: the router is multi-process (one child llama-server per
    model), so /slots must be addressed to a child. The proxy dropped `model`,
    so every call came back 400 'model name is missing from the request' and
    nothing above the router could see in-flight generation — the input the
    drain-before-swap loader reads."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/slots?model=gemma-4",
            status=200,
            payload=[{"id": 0, "is_processing": True}],
        )
        resp = authenticated_user.get("/llamolotl/slots?model=gemma-4")
    assert resp.status_code == 200
    assert resp.json()[0]["is_processing"] is True


@pytest.mark.tier1
def test_slots_without_a_model_does_not_invent_one(authenticated_user):
    """aioresponses_strict fails the test on any unregistered URL, so
    registering only the bare /slots proves no model was guessed onto it.
    Guessing would report another model's slots as this one's."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(f"{target}/slots", status=200, payload=[])
        resp = authenticated_user.get("/llamolotl/slots")
    assert resp.status_code == 200


####################
# GET /llamolotl/residency — the proactive loader's single read
####################


def _slot(is_processing):
    """A slot as llama.cpp actually returns it — the full sampler block is what
    makes proxying /slots to the browser wrong, so keep a representative shape."""
    return {"id": 0, "n_ctx": 16384, "is_processing": is_processing, "params": {"top_k": 64}}


@pytest.mark.tier1
def test_residency_reports_busy_slot_counts_for_loaded_models(authenticated_user):
    """The swap gate: 'is anything generating right now'. Reduced to a count
    here because the panel needs a number, not ~1.5 KB of sampler params."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/v1/models",
            status=200,
            payload={"data": [{"id": "gemma", "status": {"value": "loaded"}}]},
        )
        m.get(
            f"{target}/slots?model=gemma",
            status=200,
            payload=[_slot(True), _slot(False), _slot(False)],
        )
        resp = authenticated_user.get("/llamolotl/residency")

    assert resp.status_code == 200
    model = resp.json()["models"][0]
    assert model["slots"] == {"busy": 1, "total": 3}


@pytest.mark.tier1
def test_residency_does_not_poll_slots_for_an_unloaded_model(authenticated_user):
    """An unloaded model has no child llama-server to ask, so /slots for it is a
    400 from the router rather than a zero. aioresponses_strict fails the test on
    any unregistered URL, so leaving /slots unregistered proves it was not hit."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/v1/models",
            status=200,
            payload={"data": [{"id": "glm", "status": {"value": "unloaded"}}]},
        )
        resp = authenticated_user.get("/llamolotl/residency")

    assert resp.status_code == 200
    assert resp.json()["models"][0].get("slots") is None


@pytest.mark.tier1
def test_residency_reports_unknown_slots_as_null_not_zero(authenticated_user):
    """'No requests in flight' is a green light for a swap; 'we could not find
    out' is not. Collapsing the two would evict a model mid-generation."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/v1/models",
            status=200,
            payload={"data": [{"id": "gemma", "status": {"value": "loaded"}}]},
        )
        m.get(
            f"{target}/slots?model=gemma",
            status=200,
            payload={"error": {"code": 400, "message": "model name is missing from the request"}},
        )
        resp = authenticated_user.get("/llamolotl/residency")

    assert resp.status_code == 200
    assert resp.json()["models"][0]["slots"] is None


@pytest.mark.tier1
def test_residency_reports_what_llamolotl_could_free_itself(authenticated_user):
    """Free capacity alone does not explain why a swap is admissible: the model
    that does not fit in `free_bytes` fits in free + what we evict (#114). The
    panel shows both so a refusal reconstructs to real arithmetic."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(f"{target}/v1/models", status=200, payload={"data": []})
        resp = authenticated_user.get("/llamolotl/residency")

    assert resp.status_code == 200
    capacity = resp.json()["capacity"]
    assert set(capacity) == {"total_bytes", "free_bytes", "llamolotl_held_bytes"}
    assert all(isinstance(v, int) for v in capacity.values())


####################
# self.ai#103 — the upstream's error body must survive
####################


@pytest.mark.tier1
def test_router_capacity_refusal_reaches_the_caller():
    """The bug: aiohttp's raise_for_status() calls response.release(), which
    discards the payload -- so the old `await r.json()` in the except branch
    could never succeed and always fell through to the ClientResponseError
    repr. A precise capacity answer became "503, message='Service
    Unavailable'", which reads like the backend is down rather than 29 MiB
    short. The body has to be read BEFORE anything releases it.

    Exercised against send_post_request directly: that is where the release
    happens, and the chat route rejects an unresolvable model before it ever
    gets here."""
    import asyncio

    from fastapi import HTTPException

    from selfai_ui.routers.llamolotl import send_post_request

    target = "http://self-llamolotl:8080/v1/chat/completions"
    upstream = (
        "model name=Qwen3-Coder-Next-UD-Q3_K_XL does not fit in VRAM: needs an "
        "estimated 22784 MiB but only 22755 MiB is free, and no more resident "
        "models are safe to evict"
    )
    with aioresponses_strict() as m:
        m.post(
            target,
            status=503,
            payload={"error": {"code": 503, "message": upstream, "type": "unavailable_error"}},
        )
        try:
            asyncio.run(send_post_request(url=target, payload="{}", stream=False))
            raise AssertionError("expected the refusal to raise")
        except HTTPException as e:
            exc = e

    assert exc.status_code == 503
    assert "22784 MiB" in exc.detail and "22755 MiB" in exc.detail
    assert "Service Unavailable" not in exc.detail


@pytest.mark.tier1
def test_the_upstream_status_is_preserved_not_flattened_to_500():
    """A 503 is retryable and a 400 is not; collapsing either into 500 tells the
    caller the wrong thing about whether to try again."""
    import asyncio

    from fastapi import HTTPException

    from selfai_ui.routers.llamolotl import send_post_request

    target = "http://self-llamolotl:8080/v1/chat/completions"
    with aioresponses_strict() as m:
        m.post(
            target,
            status=400,
            payload={"error": {"code": 400, "message": "bad model", "type": "invalid_request_error"}},
        )
        try:
            asyncio.run(send_post_request(url=target, payload="{}", stream=False))
            raise AssertionError("expected the refusal to raise")
        except HTTPException as e:
            exc = e
    assert exc.status_code == 400
    assert "bad model" in exc.detail


@pytest.mark.tier1
def test_a_streaming_refusal_is_reported_not_streamed():
    """The old code called raise_for_status() before the stream branch, so a
    streaming request hit the same body loss. Streaming is the default for
    chat, which is the path a user actually takes."""
    import asyncio

    from fastapi import HTTPException

    from selfai_ui.routers.llamolotl import send_post_request

    target = "http://self-llamolotl:8080/v1/chat/completions"
    with aioresponses_strict() as m:
        m.post(
            target,
            status=503,
            payload={"error": {"code": 503, "message": "needs an estimated 22784 MiB", "type": "unavailable_error"}},
        )
        try:
            asyncio.run(send_post_request(url=target, payload="{}", stream=True))
            raise AssertionError("expected the refusal to raise")
        except HTTPException as e:
            exc = e
    assert exc.status_code == 503
    assert "22784 MiB" in exc.detail


@pytest.mark.tier1
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": {"message": "needs 100 MiB"}}, "needs 100 MiB"),
        ({"error": "flat string form"}, "flat string form"),
        ({"detail": "our own route's shape"}, "our own route's shape"),
        ("<html>502 Bad Gateway</html>", "<html>502 Bad Gateway</html>"),
        ({}, None),
        (None, None),
    ],
)
def test_router_error_detail_reads_every_upstream_shape(body, expected):
    """llama.cpp, our own routers, and an intermediary proxy each answer with a
    different shape. Returning a STRING (not the dict) is deliberate: callers
    render `detail` into a toast, and a dict shows "[object Object]" — the same
    message loss by another route."""
    from selfai_ui.routers.llamolotl import router_error_detail

    assert router_error_detail(body) == expected


####################
# split models list once, not once per shard
####################


@pytest.mark.tier1
def test_a_shard_is_hidden_when_the_whole_model_is_listed():
    """The router discovers /models/GLM-4.5-Air-UD-Q4_K_XL/ as one model AND its
    first shard file as another, so the same weights appeared twice in the
    picker — and only the consolidated entry matches the preset section, so the
    selectable duplicate was the one with NO vram-footprint declaration."""
    from selfai_ui.routers.llamolotl import drop_shard_duplicates

    kept = drop_shard_duplicates(
        [
            {"id": "GLM-4.5-Air-UD-Q4_K_XL"},
            {"id": "GLM-4.5-Air-UD-Q4_K_XL-00001-of-00002"},
            {"id": "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL"},
        ]
    )
    assert [m["id"] for m in kept] == [
        "GLM-4.5-Air-UD-Q4_K_XL",
        "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL",
    ]


@pytest.mark.tier1
def test_an_orphan_shard_is_kept():
    """With no consolidated sibling listed, that entry is the only way to reach
    those weights. Hiding it would remove a working model rather than a
    duplicate — the same mistake being fixed, pointed the other way."""
    from selfai_ui.routers.llamolotl import drop_shard_duplicates

    kept = drop_shard_duplicates([{"id": "SomeModel-00001-of-00003"}])
    assert [m["id"] for m in kept] == ["SomeModel-00001-of-00003"]


@pytest.mark.tier1
def test_a_normal_model_name_is_never_mistaken_for_a_shard():
    """The suffix has to be llama.cpp's own -NNNNN-of-NNNNN split naming. A
    model whose name merely contains digits or dashes must survive."""
    from selfai_ui.routers.llamolotl import drop_shard_duplicates

    ids = [
        "Qwen2.5-Coder-32B-Instruct-Q4_K_M",
        "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL",
        "Qwen3-Reranker-0.6B-q8_0",
        "model-1-of-2",  # not 5-digit padded — not the split convention
    ]
    kept = drop_shard_duplicates([{"id": i} for i in ids])
    assert [m["id"] for m in kept] == ids


@pytest.mark.tier1
def test_the_trailing_gguf_form_is_recognised_too():
    """Some entries arrive still carrying the extension. That variant is
    exactly what made the Models row unmatchable, so it must not be the one
    shape the filter misses."""
    from selfai_ui.routers.llamolotl import drop_shard_duplicates

    kept = drop_shard_duplicates(
        [{"id": "Base-Model"}, {"id": "Base-Model-00002-of-00002.gguf"}]
    )
    assert [m["id"] for m in kept] == ["Base-Model"]
