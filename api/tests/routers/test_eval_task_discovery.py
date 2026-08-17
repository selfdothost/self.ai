"""self.ai#89: GET /evaluations/tasks — live benchmark discovery from the harnesses.

Both harnesses have always served GET /api/tasks (scope `tasks:read`) and until
this endpoint landed nothing called either one; the client shipped a hardcoded
array instead. These tests pin the three properties that make the proxy
trustworthy as the picker's source:

* the outbound call carries a service ticket scoped to `tasks:read` for the
  right audience (same contract as the rest of evaluations.py — see
  test_evaluations_ticket_auth.py),
* a transport failure or upstream error surfaces as 502 and never as an empty
  list, because an empty list is a *meaningful* answer from these harnesses
  (a signal raised at import time in a vendored metric has emptied code-eval's
  registry before) — conflating the two would hide an outage,
* the TTL cache serves repeat opens without a second upstream call.
"""

import httpx
import jwt
import pytest
import respx

from selfai_ui.routers.evaluations import (
    CODE_EVAL_API_URL,
    CODE_EVAL_AUDIENCE,
    LANGUAGE_EVAL_API_URL,
    LANGUAGE_EVAL_AUDIENCE,
    _eval_tasks_cache,
)

TEST_SERVICE_AUTH_SECRET = "test-service-auth-secret-not-for-production"


@pytest.fixture(autouse=True)
def _clear_task_cache():
    """The cache is module-global; leaking it across tests would let one test's
    payload satisfy another's assert_all_called mock."""
    _eval_tasks_cache.clear()
    yield
    _eval_tasks_cache.clear()


def _decode_ticket(request: httpx.Request, audience: str) -> dict:
    ticket = request.headers.get("X-Selfai-Ticket")
    assert ticket is not None, f"no X-Selfai-Ticket header sent to {request.url}"
    return jwt.decode(
        ticket,
        TEST_SERVICE_AUTH_SECRET,
        algorithms=["HS256"],
        audience=audience,
    )


@pytest.mark.tier1
def test_code_eval_tasks_forwards_ticket_with_tasks_read_scope(authenticated_admin):
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(
            200,
            json=[
                {"name": "humaneval", "category": "humaneval"},
                {"name": "multiple-rs", "category": "multiple"},
            ],
        )

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(side_effect=_cb)
        resp = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=code-eval")

    assert resp.status_code == 200
    assert captured["claims"]["aud"] == CODE_EVAL_AUDIENCE
    assert "tasks:read" in captured["claims"]["scope"].split()
    # Category travels with each item, so the client can group without a
    # second round trip to /api/tasks/categories.
    assert resp.json() == [
        {"name": "humaneval", "category": "humaneval"},
        {"name": "multiple-rs", "category": "multiple"},
    ]


@pytest.mark.tier1
def test_language_eval_tasks_forwards_ticket_with_tasks_read_scope(authenticated_admin):
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, LANGUAGE_EVAL_AUDIENCE)
        return httpx.Response(200, json=[{"name": "hellaswag", "category": "commonsense"}])

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(side_effect=_cb)
        resp = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=language-eval")

    assert resp.status_code == 200
    assert captured["claims"]["aud"] == LANGUAGE_EVAL_AUDIENCE
    assert "tasks:read" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_default_eval_type_is_code_eval(authenticated_admin):
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(200, json=[{"name": "mbpp", "category": "mbpp"}])
        )
        resp = authenticated_admin.get("/api/v1/evaluations/tasks")
    assert resp.status_code == 200


@pytest.mark.tier1
def test_unknown_eval_type_is_rejected_without_calling_upstream(authenticated_admin):
    """An unrecognised eval_type must not be turned into a URL and fetched."""
    with respx.mock(assert_all_called=False) as router:
        code = router.get(f"{CODE_EVAL_API_URL}/api/tasks")
        lang = router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks")
        resp = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=speech-eval")
    assert resp.status_code == 400
    assert not code.called
    assert not lang.called


@pytest.mark.tier1
def test_unreachable_harness_is_502_not_an_empty_list(authenticated_admin):
    """A transport failure must stay distinguishable from 'the harness has no
    tasks' — otherwise an outage renders as an empty picker."""
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=code-eval")
    assert resp.status_code == 502
    assert resp.json() != []


@pytest.mark.tier1
def test_upstream_non_200_is_502(authenticated_admin):
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(503, json={"detail": "starting up"})
        )
        resp = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=language-eval")
    assert resp.status_code == 502


@pytest.mark.tier1
def test_empty_task_list_is_passed_through_as_a_real_answer(authenticated_admin):
    """A harness that genuinely discovered nothing returns 200 + [] — that is a
    real signal (self.ai code-eval has emptied its registry this way before),
    not an error to be masked."""
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(return_value=httpx.Response(200, json=[]))
        resp = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=code-eval")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.tier1
def test_repeat_call_within_ttl_is_served_from_cache(authenticated_admin):
    """language-eval indexes thousands of vendored YAMLs; re-fetching that on
    every picker open is what the TTL exists to avoid."""
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(200, json=[{"name": "gsm8k", "category": "math"}])
        )
        first = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=language-eval")
        second = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=language-eval")

    assert first.status_code == 200
    assert second.json() == first.json()
    assert route.call_count == 1


@pytest.mark.tier1
def test_cache_is_keyed_per_eval_type(authenticated_admin):
    """A warm code-eval cache must not answer a language-eval request."""
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(200, json=[{"name": "humaneval", "category": "humaneval"}])
        )
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(200, json=[{"name": "mmlu", "category": "knowledge"}])
        )
        code = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=code-eval")
        lang = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=language-eval")

    assert code.json()[0]["name"] == "humaneval"
    assert lang.json()[0]["name"] == "mmlu"


@pytest.mark.tier1
def test_non_admin_cannot_list_tasks(authenticated_user):
    resp = authenticated_user.get("/api/v1/evaluations/tasks?eval_type=code-eval")
    assert resp.status_code in (401, 403)


# ─── GET /evaluations/languages (self.chat#40) ─────────────────────────
#
# MultiPL-E's language set became runtime-extensible in self.code-eval#6/#7, so
# a static client list drifts the same way the task list did — it sat at 14
# entries against 23 built-ins, and a language registered at runtime never
# appeared at all. Same fail-closed contract as /tasks, for a sharper reason:
# `builtin` can never legitimately be empty, so an empty render would be a lie
# rather than merely ambiguous.


@pytest.mark.tier1
def test_languages_forwards_ticket_with_tasks_read_scope(authenticated_admin):
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"builtin": ["rs", "go", "js"], "custom": ["zig"]})

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/languages").mock(side_effect=_cb)
        resp = authenticated_admin.get("/api/v1/evaluations/languages")

    assert resp.status_code == 200
    assert captured["claims"]["aud"] == CODE_EVAL_AUDIENCE
    assert "tasks:read" in captured["claims"]["scope"].split()
    # Built-in and registered stay distinguishable: the client greys or badges
    # them differently, and only a registered one can be withdrawn.
    assert resp.json() == {"builtin": ["rs", "go", "js"], "custom": ["zig"]}


@pytest.mark.tier1
def test_unreachable_harness_is_502_not_an_empty_language_set(authenticated_admin):
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/languages").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        resp = authenticated_admin.get("/api/v1/evaluations/languages")

    assert resp.status_code == 502
    assert "languages" in resp.json()["detail"]


@pytest.mark.tier1
def test_upstream_error_is_502_not_an_empty_language_set(authenticated_admin):
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/languages").mock(
            return_value=httpx.Response(500, json={"detail": "boom"})
        )
        resp = authenticated_admin.get("/api/v1/evaluations/languages")

    assert resp.status_code == 502


@pytest.mark.tier1
def test_languages_are_not_served_from_the_task_cache(authenticated_admin):
    """The task cache is keyed by eval_type and holds a list; languages are a
    dict from a different endpoint. Sharing that cache would serve one shape
    where the other is expected."""
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(200, json=[{"name": "humaneval", "category": "humaneval"}])
        )
        router.get(f"{CODE_EVAL_API_URL}/api/languages").mock(
            return_value=httpx.Response(200, json={"builtin": ["rs"], "custom": []})
        )
        tasks = authenticated_admin.get("/api/v1/evaluations/tasks?eval_type=code-eval")
        langs = authenticated_admin.get("/api/v1/evaluations/languages")

    assert isinstance(tasks.json(), list)
    assert isinstance(langs.json(), dict)
