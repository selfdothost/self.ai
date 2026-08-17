"""self.ai#91: the custom-eval catalog.

The properties worth pinning are the ones that keep the catalog honest about a
distributed system it does not control:

* a registration may not shadow a built-in task name, because the harness
  resolves by name and a shadow silently changes what a benchmark *means*,
* that check fails CLOSED — if the harness is unreachable we cannot know
  whether the name is taken, so the registration is refused rather than admitted
  on a guess,
* a new row is `pending`, never `synced`: the API pod cannot write into the
  harness's storage (all PVCs are RWO local-path), so registration and delivery
  are separate facts.
"""

import httpx
import pytest
import respx

from selfai_ui.routers.evaluations import (
    CODE_EVAL_API_URL,
    LANGUAGE_EVAL_API_URL,
    _eval_tasks_cache,
)

BASE = "/api/v1/evaluations/custom"


@pytest.fixture(autouse=True)
def _clear_task_cache():
    _eval_tasks_cache.clear()
    yield
    _eval_tasks_cache.clear()


def _mock_builtins(router, eval_type, names):
    url = LANGUAGE_EVAL_API_URL if eval_type == "language-eval" else CODE_EVAL_API_URL
    router.get(f"{url}/api/tasks").mock(
        return_value=httpx.Response(200, json=[{"name": n, "category": "other"} for n in names])
    )


def _yaml_form(name="my_custom_task", **kw):
    form = {
        "name": name,
        "eval_type": "language-eval",
        "kind": "raw_yaml",
        "definition": {"yaml": f"task: {name}\ndataset_path: acme/things\n"},
    }
    form.update(kw)
    return form


@pytest.mark.tier1
def test_create_then_list_round_trip(authenticated_admin):
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", ["hellaswag", "mmlu"])
        created = authenticated_admin.post(BASE, json=_yaml_form())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "my_custom_task"
    assert body["owner_id"]

    listed = authenticated_admin.get(f"{BASE}?eval_type=language-eval")
    assert listed.status_code == 200
    # Membership, not exact equality: the table is shared across the test
    # session, so asserting the whole listing would couple this test to every
    # other file that registers an eval.
    assert "my_custom_task" in [r["name"] for r in listed.json()]


@pytest.mark.tier1
def test_new_registration_is_pending_not_synced(authenticated_admin):
    """The harness has not been told yet — saying otherwise would be a lie the
    admin acts on."""
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        resp = authenticated_admin.post(BASE, json=_yaml_form(name="pending_check"))
    assert resp.status_code == 201
    assert resp.json()["sync_status"] == "pending"
    assert resp.json()["synced_at"] is None


@pytest.mark.tier1
def test_cannot_shadow_a_builtin_task_name(authenticated_admin):
    with respx.mock(assert_all_called=True) as router:
        _mock_builtins(router, "language-eval", ["hellaswag", "mmlu"])
        resp = authenticated_admin.post(BASE, json=_yaml_form(name="hellaswag"))
    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"]


@pytest.mark.tier1
def test_shadow_check_fails_closed_when_harness_unreachable(authenticated_admin):
    """If we cannot list the built-ins we cannot know the name is free. Refusing
    is recoverable; admitting a shadowing name is not."""
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/tasks").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = authenticated_admin.post(BASE, json=_yaml_form(name="unknowable"))
    assert resp.status_code == 502

    # And nothing was written.
    listed = authenticated_admin.get(f"{BASE}?eval_type=language-eval")
    assert [r["name"] for r in listed.json() if r["name"] == "unknowable"] == []


@pytest.mark.tier1
def test_duplicate_name_within_an_eval_type_is_rejected(authenticated_admin):
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        first = authenticated_admin.post(BASE, json=_yaml_form(name="dupe"))
        second = authenticated_admin.post(BASE, json=_yaml_form(name="dupe"))
    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.tier1
def test_same_name_is_allowed_across_eval_types(authenticated_admin):
    """The two harnesses have separate task namespaces."""
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        _mock_builtins(router, "code-eval", [])
        lang = authenticated_admin.post(BASE, json=_yaml_form(name="shared_name"))
        code = authenticated_admin.post(
            BASE,
            json={
                "name": "shared_name",
                "eval_type": "code-eval",
                "kind": "code_language",
                "definition": {"language": "zig"},
            },
        )
    assert lang.status_code == 201
    assert code.status_code == 201


@pytest.mark.tier1
@pytest.mark.parametrize(
    "bad_name",
    ["", "Has-Upper", "has space", "../escape", "-leading-dash", "a" * 200],
)
def test_invalid_names_are_rejected(authenticated_admin, bad_name):
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        resp = authenticated_admin.post(BASE, json=_yaml_form(name=bad_name))
    assert resp.status_code == 400


@pytest.mark.tier1
def test_kind_must_match_the_harness(authenticated_admin):
    """A code_language definition is meaningless to language-eval."""
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        resp = authenticated_admin.post(
            BASE,
            json={
                "name": "mismatched",
                "eval_type": "language-eval",
                "kind": "code_language",
                "definition": {"language": "zig"},
            },
        )
    assert resp.status_code == 400
    assert "not valid for language-eval" in resp.json()["detail"]


@pytest.mark.tier1
def test_empty_definition_is_rejected(authenticated_admin):
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        resp = authenticated_admin.post(BASE, json=_yaml_form(name="nodef", definition={}))
    assert resp.status_code == 400


@pytest.mark.tier1
def test_updating_the_definition_returns_the_row_to_pending(authenticated_admin):
    """The harness holds a copy of the old definition; until a fresh push lands
    the two disagree, and the row must say so."""
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        created = authenticated_admin.post(BASE, json=_yaml_form(name="editme")).json()

    from selfai_ui.models.custom_evals import CustomEvals

    CustomEvals.set_sync_state(created["id"], "synced")
    assert authenticated_admin.get(f"{BASE}/{created['id']}").json()["sync_status"] == "synced"

    updated = authenticated_admin.post(
        f"{BASE}/{created['id']}", json={"definition": {"yaml": "task: editme\ndataset_path: acme/other\n"}}
    )
    assert updated.status_code == 200
    assert updated.json()["sync_status"] == "pending"


@pytest.mark.tier1
def test_description_only_update_does_not_touch_sync_state(authenticated_admin):
    """A label change does not invalidate what the harness holds."""
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        created = authenticated_admin.post(BASE, json=_yaml_form(name="labelonly")).json()

    from selfai_ui.models.custom_evals import CustomEvals

    CustomEvals.set_sync_state(created["id"], "synced")
    updated = authenticated_admin.post(f"{BASE}/{created['id']}", json={"description": "nicer words"})
    assert updated.status_code == 200
    assert updated.json()["sync_status"] == "synced"


@pytest.mark.tier1
def test_delete_removes_the_registration(authenticated_admin):
    with respx.mock(assert_all_called=False) as router:
        _mock_builtins(router, "language-eval", [])
        created = authenticated_admin.post(BASE, json=_yaml_form(name="deleteme")).json()

    assert authenticated_admin.delete(f"{BASE}/{created['id']}").status_code == 200
    assert authenticated_admin.get(f"{BASE}/{created['id']}").status_code == 404


@pytest.mark.tier1
def test_unknown_eval_type_filter_is_rejected(authenticated_admin):
    assert authenticated_admin.get(f"{BASE}?eval_type=speech-eval").status_code == 400


@pytest.mark.tier1
def test_non_admin_cannot_reach_the_catalog(authenticated_user):
    assert authenticated_user.get(BASE).status_code in (401, 403)
    assert authenticated_user.post(BASE, json=_yaml_form(name="nope")).status_code in (401, 403)
