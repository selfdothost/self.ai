"""Model-lines router — self.ai#131, cavekit-model-versioning.md R1/R8/R10.

Two things are pinned here that are easy to get wrong and invisible when wrong:
access (a line is readable by its owner, by an admin, and by `access_control`,
and by nobody else) and the switched-off path (every route refuses *specifically*
— never a 500, never an empty success that reads as "this line has no history").
"""

import pytest

from selfai_ui.models.model_versions import (
    ModelLineForm,
    ModelLines,
    ModelVersionForm,
    ModelVersions,
)

BASE = "/api/v1/model-lines"


@pytest.fixture
def corpus_on(test_app):
    original = test_app.state.config.ENABLE_SELF_CORPUS
    test_app.state.config.ENABLE_SELF_CORPUS = True
    yield test_app
    test_app.state.config.ENABLE_SELF_CORPUS = original


@pytest.fixture
def corpus_off(test_app):
    original = test_app.state.config.ENABLE_SELF_CORPUS
    test_app.state.config.ENABLE_SELF_CORPUS = False
    yield test_app
    test_app.state.config.ENABLE_SELF_CORPUS = original


def _line(user_id, name="gemma-line", access_control=None):
    return ModelLines.insert_new_line(
        user_id,
        ModelLineForm(name=name, corpus_repo="selfai-line-x", access_control=access_control),
    )


def _versions(line_id, count=2):
    made = []
    parent = None
    for i in range(count):
        version = ModelVersions.insert_new_version(
            line_id,
            ModelVersionForm(
                kind="base" if i == 0 else "adapter",
                corpus_commit_id=f"commit-{i}",
                parent_version_id=parent,
                artifact_ref=f"artifact-{i}.gguf",
            ),
        )
        parent = version.id
        made.append(version)
    return made


####################
# R1 — lines and their history
####################


@pytest.mark.tier0
def test_owner_sees_their_line_with_history_in_sequence_order(corpus_on, authenticated_user, test_user):
    line = _line(test_user["id"])
    versions = _versions(line.id, count=3)

    resp = authenticated_user.get(f"{BASE}/line", params={"id": line.id})

    assert resp.status_code == 200
    body = resp.json()
    assert [v["id"] for v in body["versions"]] == [v.id for v in versions]
    assert [v["sequence"] for v in body["versions"]] == [1, 2, 3]


@pytest.mark.tier0
def test_listing_excludes_another_users_private_line(corpus_on, authenticated_user, test_user, test_admin):
    mine = _line(test_user["id"], name="mine")
    _line(test_admin["id"], name="theirs", access_control={})

    body = authenticated_user.get(f"{BASE}/").json()

    assert [line["id"] for line in body] == [mine.id]


@pytest.mark.tier0
def test_admin_sees_every_line(corpus_on, authenticated_admin, test_user):
    _line(test_user["id"], name="someone-elses", access_control={})
    body = authenticated_admin.get(f"{BASE}/").json()
    assert len(body) == 1


@pytest.mark.tier0
def test_reading_another_users_private_line_is_refused(corpus_on, authenticated_user, test_admin):
    line = _line(test_admin["id"], access_control={})
    resp = authenticated_user.get(f"{BASE}/line", params={"id": line.id})
    assert resp.status_code == 401


@pytest.mark.tier0
def test_missing_line_is_404(corpus_on, authenticated_user):
    assert authenticated_user.get(f"{BASE}/line", params={"id": "ghost"}).status_code == 404


@pytest.mark.tier0
def test_create_line_derives_its_corpus_repo(corpus_on, authenticated_admin):
    resp = authenticated_admin.post(f"{BASE}/create", json={"name": "new-line"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["corpus_repo"] == f"selfai-line-{body['id']}"


@pytest.mark.tier0
def test_create_line_needs_the_models_permission(corpus_on, user_without_workspace_permissions):
    resp = user_without_workspace_permissions.post(f"{BASE}/create", json={"name": "nope"})
    assert resp.status_code == 401


####################
# R1 — deletion is refused, not cascaded
####################


@pytest.mark.tier0
def test_deleting_a_line_with_versions_conflicts(corpus_on, authenticated_user, test_user):
    line = _line(test_user["id"])
    _versions(line.id, count=1)

    resp = authenticated_user.request("DELETE", f"{BASE}/line/delete", params={"id": line.id})

    assert resp.status_code == 409
    assert ModelLines.get_line_by_id(line.id) is not None


####################
# R8 — revert
####################


@pytest.mark.tier0
def test_setting_the_current_version_moves_the_pointer(corpus_on, authenticated_user, test_user):
    line = _line(test_user["id"])
    versions = _versions(line.id, count=2)
    ModelLines.set_current_version(line.id, versions[1].id)

    resp = authenticated_user.post(
        f"{BASE}/line/current-version",
        params={"id": line.id},
        json={"version_id": versions[0].id},
    )

    assert resp.status_code == 200
    assert resp.json()["current_version_id"] == versions[0].id
    assert len(ModelVersions.get_versions_by_line(line.id)) == 2


@pytest.mark.tier0
def test_setting_a_foreign_version_is_a_400(corpus_on, authenticated_user, test_user):
    line_a = _line(test_user["id"], name="a")
    line_b = _line(test_user["id"], name="b")
    foreign = _versions(line_a.id, count=1)[0]

    resp = authenticated_user.post(
        f"{BASE}/line/current-version",
        params={"id": line_b.id},
        json={"version_id": foreign.id},
    )

    assert resp.status_code == 400


####################
# R2/R8 — provenance and resolution over the wire
####################


@pytest.mark.tier0
def test_provenance_walks_newest_first(corpus_on, authenticated_user, test_user):
    line = _line(test_user["id"])
    versions = _versions(line.id, count=3)

    body = authenticated_user.get(f"{BASE}/version/provenance", params={"id": versions[-1].id}).json()

    assert [v["id"] for v in body] == [v.id for v in reversed(versions)]


@pytest.mark.tier0
def test_resolve_returns_the_base_alongside_an_adapter(corpus_on, authenticated_user, test_user):
    line = _line(test_user["id"])
    versions = _versions(line.id, count=2)

    body = authenticated_user.get(f"{BASE}/version/resolve", params={"id": versions[1].id}).json()

    assert body["artifact_ref"] == "artifact-1.gguf"
    assert body["base_artifact_ref"] == "artifact-0.gguf"


@pytest.mark.tier0
def test_resolving_a_version_with_no_artifact_conflicts(corpus_on, authenticated_user, test_user):
    line = _line(test_user["id"])
    version = ModelVersions.insert_new_version(
        line.id, ModelVersionForm(kind="base", corpus_commit_id="c1", artifact_ref=None)
    )

    resp = authenticated_user.get(f"{BASE}/version/resolve", params={"id": version.id})

    assert resp.status_code == 409
    assert version.id in resp.json()["detail"]


####################
# R10 — switched off refuses specifically
####################


@pytest.mark.tier0
@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/", {}),
        ("post", "/create", {"json": {"name": "x"}}),
        ("get", "/line", {"params": {"id": "any"}}),
        ("get", "/version", {"params": {"id": "any"}}),
        ("get", "/version/provenance", {"params": {"id": "any"}}),
        ("get", "/version/resolve", {"params": {"id": "any"}}),
        ("post", "/line/current-version", {"params": {"id": "any"}, "json": {"version_id": "v"}}),
        ("delete", "/line/delete", {"params": {"id": "any"}}),
    ],
)
def test_every_route_refuses_when_corpus_is_off(corpus_off, authenticated_admin, method, path, kwargs):
    resp = authenticated_admin.request(method.upper(), f"{BASE}{path}", **kwargs)

    assert resp.status_code == 503, f"{method} {path} did not refuse"
    assert "ENABLE_SELF_CORPUS" in resp.json()["detail"]


@pytest.mark.tier0
def test_switched_off_does_not_return_an_empty_success(corpus_off, authenticated_user, test_user):
    """The failure mode this guards: a 200 with [] reads as "no history"."""
    line = _line(test_user["id"])
    _versions(line.id, count=2)

    resp = authenticated_user.get(f"{BASE}/line", params={"id": line.id})

    assert resp.status_code != 200


####################
# R6 — publish is separately permissioned
####################


@pytest.fixture
def permissions(test_app):
    """Set the permission blob for a test, restoring it afterwards."""
    original = test_app.state.config.USER_PERMISSIONS

    def _set(**studio):
        test_app.state.config.USER_PERMISSIONS = {"studio": {"models": True, "training": True, **studio}}

    yield _set
    test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier0
def test_training_permission_does_not_imply_publish(corpus_on, authenticated_user, test_user, permissions):
    """Fitting an adapter costs an adapter; publishing costs several GB."""
    permissions(publish=False)
    line = _line(test_user["id"])
    _versions(line.id, count=2)

    resp = authenticated_user.post(
        f"{BASE}/line/publish", params={"id": line.id}, json={"output_name": "v2.gguf"}
    )

    assert resp.status_code == 401


@pytest.mark.tier0
def test_publish_permission_admits(corpus_on, authenticated_user, test_user, permissions, monkeypatch):
    from selfai_ui.routers import model_versions as router_module

    permissions(publish=True)
    line = _line(test_user["id"])
    versions = _versions(line.id, count=2)

    async def _created(app_state, line_id, *, user_id, form_data):
        from selfai_ui.models.model_versions import PublishJobs

        return PublishJobs.insert_new_job(line_id, user_id, versions[0].id, [versions[1].id], form_data)

    monkeypatch.setattr(router_module, "create_publish_job", _created)

    resp = authenticated_user.post(
        f"{BASE}/line/publish", params={"id": line.id}, json={"output_name": "v2.gguf"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_version_id"] == versions[0].id
    # Enqueued, not performed: the merge waits for a window and a VRAM lease.
    assert body["status"] == "queued"


@pytest.mark.tier0
def test_an_empty_publish_is_a_409_not_a_500(corpus_on, authenticated_user, test_user, permissions):
    permissions(publish=True)
    line = _line(test_user["id"])
    _versions(line.id, count=1)  # a base and no adapters

    resp = authenticated_user.post(
        f"{BASE}/line/publish", params={"id": line.id}, json={"output_name": "v2.gguf"}
    )

    assert resp.status_code == 409
    assert "nothing to publish" in resp.json()["detail"]


@pytest.mark.tier0
def test_preview_needs_only_read(corpus_on, authenticated_user, test_user, permissions):
    """Looking at what a publish would merge is not itself a publish."""
    permissions(publish=False)
    line = _line(test_user["id"])
    versions = _versions(line.id, count=2)

    body = authenticated_user.get(f"{BASE}/line/publish/preview", params={"id": line.id}).json()

    assert body["base_version_id"] == versions[0].id
    assert body["adapter_version_ids"] == [versions[1].id]
    assert body["publishable"] is True
