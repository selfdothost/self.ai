"""self.ai#91 delivery: hf_task templating, manual sync, and withdrawal on delete.

The properties here are about a two-system truth: the catalog row and the
harness's stored copy. What matters is that the row never claims more than what
actually happened.
"""

import httpx
import pytest
import respx
import yaml

from selfai_ui.models.custom_evals import CustomEvals
from selfai_ui.routers.evaluations import (
    CODE_EVAL_API_URL,
    LANGUAGE_EVAL_API_URL,
    _eval_tasks_cache,
)
from selfai_ui.utils.eval_task_template import (
    TemplateError,
    render_hf_task_yaml,
    validate_hf_task_definition,
)

BASE = "/api/v1/evaluations/custom"

GOOD_HF_DEF = {
    "dataset_path": "allenai/sciq",
    "output_type": "multiple_choice",
    "test_split": "test",
    "doc_to_text": "Question: {{question}}\nAnswer:",
    "doc_to_target": 3,
    "doc_to_choice": "{{[distractor1, distractor2, distractor3, correct_answer]}}",
}


@pytest.fixture(autouse=True)
def _clear_cache():
    _eval_tasks_cache.clear()
    yield
    _eval_tasks_cache.clear()


def _create(client, name="my_task", kind="hf_task", definition=None):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(return_value=httpx.Response(200, json=[]))
        resp = client.post(
            BASE,
            json={
                "name": name,
                "eval_type": "language-eval",
                "kind": kind,
                "definition": definition if definition is not None else dict(GOOD_HF_DEF),
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ─── templating ────────────────────────────────────────────────────────


@pytest.mark.tier1
def test_rendered_yaml_is_plain_data_and_round_trips_under_safe_load():
    """The harness accepts configs with yaml.safe_load, which refuses custom
    tags — so anything we emit must survive it."""
    out = render_hf_task_yaml("my_task", GOOD_HF_DEF, description="a description")
    parsed = yaml.safe_load(out)
    assert parsed["task"] == "my_task"
    assert parsed["dataset_path"] == "allenai/sciq"
    assert parsed["output_type"] == "multiple_choice"
    assert parsed["doc_to_choice"]
    # Defaults matched to the vendored sciq.yaml.
    assert [m["metric"] for m in parsed["metric_list"]] == ["acc", "acc_norm"]
    assert parsed["metadata"]["description"] == "a description"


@pytest.mark.tier1
def test_generate_until_gets_exact_match_by_default():
    out = render_hf_task_yaml(
        "gen_task",
        {
            "dataset_path": "openai/gsm8k",
            "output_type": "generate_until",
            "test_split": "test",
            "doc_to_text": "Question: {{question}}\nAnswer:",
            "doc_to_target": "{{answer}}",
        },
    )
    parsed = yaml.safe_load(out)
    assert [m["metric"] for m in parsed["metric_list"]] == ["exact_match"]
    assert "doc_to_choice" not in parsed


@pytest.mark.tier1
@pytest.mark.parametrize(
    "missing,expected",
    [
        ("dataset_path", "dataset_path"),
        ("doc_to_text", "doc_to_text"),
        ("doc_to_target", "doc_to_target"),
        ("doc_to_choice", "doc_to_choice"),
        ("test_split", "test_split"),
    ],
)
def test_definitions_missing_a_scoring_field_are_refused(missing, expected):
    """A task that renders but scores the wrong thing is worse than one that
    refuses to render — its number looks real."""
    d = dict(GOOD_HF_DEF)
    d.pop(missing)
    with pytest.raises(TemplateError) as e:
        validate_hf_task_definition(d)
    assert expected in str(e.value)


@pytest.mark.tier1
def test_unknown_output_type_is_refused():
    d = dict(GOOD_HF_DEF, output_type="loglikelihood_rolling")
    with pytest.raises(TemplateError):
        validate_hf_task_definition(d)


@pytest.mark.tier1
def test_rendered_endpoint_shows_what_would_be_sent(authenticated_admin):
    row = _create(authenticated_admin, name="preview_me")
    resp = authenticated_admin.get(f"{BASE}/{row['id']}/rendered")
    assert resp.status_code == 200
    assert yaml.safe_load(resp.json()["yaml"])["task"] == "preview_me"


# ─── delivery ──────────────────────────────────────────────────────────


@pytest.mark.tier1
def test_sync_pushes_the_rendered_yaml_and_marks_synced(authenticated_admin):
    row = _create(authenticated_admin, name="push_me")
    captured = {}

    def _cb(request):
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"name": "push_me", "category": "other"})

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(side_effect=_cb)
        resp = authenticated_admin.post(f"{BASE}/{row['id']}/sync")

    assert resp.status_code == 200
    assert resp.json()["sync_status"] == "synced"
    assert resp.json()["synced_at"] is not None
    # The harness is sent finished YAML — it never learns about HF refs.
    assert "task: push_me" in captured["body"]


@pytest.mark.tier1
def test_sync_failure_records_the_harness_complaint_verbatim(authenticated_admin):
    """The harness knows why it refused; relaying 'sync failed' would throw that
    away."""
    row = _create(authenticated_admin, name="refused")

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(
            return_value=httpx.Response(400, json={"detail": "Task config uses a custom YAML tag"})
        )
        resp = authenticated_admin.post(f"{BASE}/{row['id']}/sync")

    assert resp.status_code == 400
    stored = CustomEvals.get_by_id(row["id"])
    assert stored.sync_status == "failed"
    assert "custom YAML tag" in stored.sync_error


@pytest.mark.tier1
def test_unreachable_harness_marks_failed_not_synced(authenticated_admin):
    row = _create(authenticated_admin, name="unreachable")

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = authenticated_admin.post(f"{BASE}/{row['id']}/sync")

    assert resp.status_code == 502
    assert CustomEvals.get_by_id(row["id"]).sync_status == "failed"


# ─── code-eval delivery (self.code-eval#6/#7) ──────────────────────────
#
# A different shape from the language-eval push above, on purpose: code-eval
# takes no task config, only a language token, and it — not api-core — decides
# whether the token is honourable.


def _create_code(client, name="zig_lang", definition=None):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(return_value=httpx.Response(200, json=[]))
        router.get(f"{CODE_EVAL_API_URL}/api/tasks").mock(return_value=httpx.Response(200, json=[]))
        resp = client.post(
            BASE,
            json={
                "name": name,
                "eval_type": "code-eval",
                "kind": "code_language",
                "definition": definition if definition is not None else {"language": "zig"},
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.tier1
def test_code_eval_sync_registers_the_language(authenticated_admin):
    created = _create_code(authenticated_admin, name="zig_ok")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{CODE_EVAL_API_URL}/api/languages").mock(
            return_value=httpx.Response(
                201,
                json={"language": "zig", "executor": True, "dataset": True,
                      "runnable": True, "task": "multiple-zig"},
            )
        )
        resp = authenticated_admin.post(f"{BASE}/{created['id']}/sync")

    assert resp.status_code == 200, resp.text
    assert CustomEvals.get_by_id(created["id"]).sync_status == "synced"
    # The token travels, not the row name — they are different strings and
    # sending the name would register a language nobody asked for.
    assert route.calls.last.request.content == b'{"language":"zig"}'


@pytest.mark.tier1
def test_an_unrunnable_language_is_refused_with_the_harness_report(authenticated_admin):
    """The `python` case that motivated the dataset check: every execution check
    passes and only the dataset one fails. The reason must survive intact —
    "no executor" and "no dataset config" send an admin to different repos."""
    created = _create_code(authenticated_admin, name="py_lang", definition={"language": "python"})
    report = {
        "language": "python", "executor": True, "dataset": False, "runnable": False,
        "detail": "nuprl/MultiPL-E has no 'humaneval-python' config, so multiple-python "
                  "would fail at dataset load even though the harness could execute it.",
    }
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{CODE_EVAL_API_URL}/api/languages").mock(
            return_value=httpx.Response(422, json={"detail": report})
        )
        resp = authenticated_admin.post(f"{BASE}/{created['id']}/sync")

    assert resp.status_code == 400
    assert "humaneval-python" in resp.text
    assert CustomEvals.get_by_id(created["id"]).sync_status == "failed"


@pytest.mark.tier1
def test_a_harness_that_cannot_read_its_builtins_is_a_503_not_a_bad_request(authenticated_admin):
    """The harness fails closed when it cannot tell whether a token shadows a
    built-in. That is the harness being unavailable for the decision, not the
    admin asking for something wrong, and the status code has to say which."""
    created = _create_code(authenticated_admin, name="unknown_builtins")
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{CODE_EVAL_API_URL}/api/languages").mock(
            return_value=httpx.Response(
                503, json={"detail": "Cannot read the built-in language list right now"}
            )
        )
        resp = authenticated_admin.post(f"{BASE}/{created['id']}/sync")

    assert resp.status_code == 503
    assert CustomEvals.get_by_id(created["id"]).sync_status == "failed"


@pytest.mark.tier1
def test_an_unreachable_code_eval_harness_does_not_mark_the_row_synced(authenticated_admin):
    created = _create_code(authenticated_admin, name="unreachable_lang")
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{CODE_EVAL_API_URL}/api/languages").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        resp = authenticated_admin.post(f"{BASE}/{created['id']}/sync")

    assert resp.status_code == 502
    assert CustomEvals.get_by_id(created["id"]).sync_status == "failed"


@pytest.mark.tier1
def test_deleting_a_synced_code_eval_withdraws_the_language_first(authenticated_admin):
    """Otherwise the harness keeps generating a multiple-{lang} task that
    nothing in self.ai remembers."""
    created = _create_code(authenticated_admin, name="withdraw_lang")
    CustomEvals.set_sync_state(created["id"], "synced")
    with respx.mock(assert_all_called=False) as router:
        route = router.delete(f"{CODE_EVAL_API_URL}/api/languages/zig").mock(
            return_value=httpx.Response(200, json={"deleted": "zig"})
        )
        resp = authenticated_admin.delete(f"{BASE}/{created['id']}")

    assert resp.status_code == 200
    assert route.called
    assert CustomEvals.get_by_id(created["id"]) is None


@pytest.mark.tier1
def test_a_failed_code_eval_withdrawal_keeps_the_record(authenticated_admin):
    """A record dropped while the harness copy survives is an orphan nobody can
    find their way back to — keep it visible and retryable instead."""
    created = _create_code(authenticated_admin, name="stuck_lang")
    CustomEvals.set_sync_state(created["id"], "synced")
    with respx.mock(assert_all_called=False) as router:
        router.delete(f"{CODE_EVAL_API_URL}/api/languages/zig").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        resp = authenticated_admin.delete(f"{BASE}/{created['id']}")

    assert resp.status_code == 502
    assert CustomEvals.get_by_id(created["id"]) is not None
    assert CustomEvals.get_by_id(created["id"]).sync_status == "failed"


@pytest.mark.tier1
def test_a_404_withdrawal_is_the_desired_end_state(authenticated_admin):
    """The harness already does not have it. Blocking the delete forever on
    that would be worse than completing it."""
    created = _create_code(authenticated_admin, name="already_gone")
    CustomEvals.set_sync_state(created["id"], "synced")
    with respx.mock(assert_all_called=False) as router:
        router.delete(f"{CODE_EVAL_API_URL}/api/languages/zig").mock(
            return_value=httpx.Response(404, json={"detail": "'zig' is not a registered language"})
        )
        resp = authenticated_admin.delete(f"{BASE}/{created['id']}")

    assert resp.status_code == 200
    assert CustomEvals.get_by_id(created["id"]) is None


@pytest.mark.tier1
def test_an_unsynced_code_eval_row_deletes_without_calling_the_harness(authenticated_admin):
    created = _create_code(authenticated_admin, name="never_synced")
    with respx.mock(assert_all_called=False) as router:
        route = router.delete(f"{CODE_EVAL_API_URL}/api/languages/zig")
        resp = authenticated_admin.delete(f"{BASE}/{created['id']}")

    assert resp.status_code == 200
    assert not route.called


# ─── withdrawal ────────────────────────────────────────────────────────


@pytest.mark.tier1
def test_delete_withdraws_from_the_harness_first(authenticated_admin):
    row = _create(authenticated_admin, name="withdraw_me")
    CustomEvals.set_sync_state(row["id"], "synced")

    with respx.mock(assert_all_called=True) as router:
        route = router.delete(f"{LANGUAGE_EVAL_API_URL}/api/tasks/withdraw_me").mock(
            return_value=httpx.Response(200, json={"deleted": "withdraw_me"})
        )
        resp = authenticated_admin.delete(f"{BASE}/{row['id']}")

    assert resp.status_code == 200
    assert route.called
    assert CustomEvals.get_by_id(row["id"]) is None


@pytest.mark.tier1
def test_failed_withdrawal_keeps_the_record(authenticated_admin):
    """Dropping the record while the harness still has the task would orphan a
    runnable task nothing remembers."""
    row = _create(authenticated_admin, name="stuck")
    CustomEvals.set_sync_state(row["id"], "synced")

    with respx.mock(assert_all_called=True) as router:
        router.delete(f"{LANGUAGE_EVAL_API_URL}/api/tasks/stuck").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = authenticated_admin.delete(f"{BASE}/{row['id']}")

    assert resp.status_code == 502
    still = CustomEvals.get_by_id(row["id"])
    assert still is not None
    assert still.sync_status == "failed"


@pytest.mark.tier1
def test_never_synced_row_deletes_without_calling_the_harness(authenticated_admin):
    row = _create(authenticated_admin, name="never_pushed")
    with respx.mock(assert_all_called=False) as router:
        route = router.delete(f"{LANGUAGE_EVAL_API_URL}/api/tasks/never_pushed")
        resp = authenticated_admin.delete(f"{BASE}/{row['id']}")
    assert resp.status_code == 200
    assert not route.called


@pytest.mark.tier1
def test_harness_404_on_withdrawal_is_treated_as_done(authenticated_admin):
    """The desired end state is 'the harness has no such task'. A 404 already
    satisfies it, so blocking the delete forever would be wrong."""
    row = _create(authenticated_admin, name="already_gone")
    CustomEvals.set_sync_state(row["id"], "synced")

    with respx.mock(assert_all_called=True) as router:
        router.delete(f"{LANGUAGE_EVAL_API_URL}/api/tasks/already_gone").mock(
            return_value=httpx.Response(404, json={"detail": "No custom task"})
        )
        resp = authenticated_admin.delete(f"{BASE}/{row['id']}")

    assert resp.status_code == 200
    assert CustomEvals.get_by_id(row["id"]) is None
