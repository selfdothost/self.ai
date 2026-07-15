"""
T-307 + T-308: Knowledge bases router CRUD and file membership tests.

Knowledge creation requires admin role or workspace.knowledge permission.
We use authenticated_admin for create/delete tests.
"""

import pytest


@pytest.mark.tier0
def test_list_knowledge_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/knowledge/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_knowledge_base(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={
            "name": "Test KB",
            "description": "A knowledge base for tests",
            "access_control": None,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Test KB"


@pytest.mark.tier0
def test_get_knowledge_by_id(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Specific KB", "description": "test"},
    ).json()
    kb_id = created["id"]

    resp = authenticated_admin.get(f"/api/v1/knowledge/{kb_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == kb_id
    assert body["name"] == "Specific KB"
    assert "files" in body  # KnowledgeFilesResponse includes files list


@pytest.mark.tier0
def test_update_knowledge(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Old Name", "description": "old desc"},
    ).json()
    kb_id = created["id"]

    resp = authenticated_admin.post(
        f"/api/v1/knowledge/{kb_id}/update",
        json={"name": "New Name", "description": "new desc"},
    )
    assert resp.status_code == 200
    refetch = authenticated_admin.get(f"/api/v1/knowledge/{kb_id}").json()
    assert refetch["name"] == "New Name"


@pytest.mark.tier0
def test_delete_knowledge(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Delete KB", "description": ""},
    ).json()
    kb_id = created["id"]

    resp = authenticated_admin.delete(f"/api/v1/knowledge/{kb_id}/delete")
    assert resp.status_code == 200
    # Refetch should return not-found
    refetch = authenticated_admin.get(f"/api/v1/knowledge/{kb_id}")
    # 200 with null, or 4xx
    if refetch.status_code == 200:
        assert refetch.json() is None


@pytest.mark.tier0
def test_knowledge_get_not_found(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/knowledge/bogus-id-12345")
    # 200 null or 404
    if resp.status_code == 200:
        assert resp.json() is None
    else:
        assert resp.status_code in (401, 404)


@pytest.mark.tier0
def test_knowledge_creation_denied_for_user_without_permission(
    user_without_workspace_permissions,
):
    """Regular user without workspace.knowledge permission is denied."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/knowledge/create",
        json={"name": "Denied KB", "description": ""},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# T-R18: File membership (add/remove/reset) error paths
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_add_file_to_nonexistent_kb_rejected(authenticated_admin):
    """Adding a file to a KB that doesn't exist returns 400 NOT_FOUND."""
    resp = authenticated_admin.post(
        "/api/v1/knowledge/nonexistent-kb-id/file/add",
        json={"file_id": "some-file-id"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_add_nonexistent_file_to_kb_rejected(authenticated_admin):
    """Adding a nonexistent file to an existing KB returns 400 NOT_FOUND."""
    kb = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Member KB", "description": ""},
    ).json()
    resp = authenticated_admin.post(
        f"/api/v1/knowledge/{kb['id']}/file/add",
        json={"file_id": "nonexistent-file-id"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_remove_file_from_nonexistent_kb_rejected(authenticated_admin):
    """Removing a file from a nonexistent KB returns 400 NOT_FOUND."""
    resp = authenticated_admin.post(
        "/api/v1/knowledge/nonexistent-kb-id/file/remove",
        json={"file_id": "some-file-id"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_reset_nonexistent_kb_rejected(authenticated_admin):
    """Resetting a nonexistent KB returns 400 NOT_FOUND."""
    resp = authenticated_admin.post("/api/v1/knowledge/nonexistent-kb-id/reset")
    assert resp.status_code == 400


@pytest.mark.tier0
def test_reset_existing_kb_clears_membership(authenticated_admin):
    """Resetting an existing KB succeeds (empty KB stays empty)."""
    kb = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Reset Me", "description": ""},
    ).json()
    resp = authenticated_admin.post(f"/api/v1/knowledge/{kb['id']}/reset")
    # Router returns 200 with updated KB (no files)
    assert resp.status_code == 200


@pytest.mark.tier0
def test_user_cannot_add_file_to_other_users_kb(authenticated_user, db_session):
    """User A cannot add a file to user B's knowledge base."""
    from tests.factories import KnowledgeFactory, UserFactory

    user_b = UserFactory.create(db_session)
    kb_b = KnowledgeFactory.create(db_session, user_id=user_b.id, name="B's KB")
    resp = authenticated_user.post(
        f"/api/v1/knowledge/{kb_b.id}/file/add",
        json={"file_id": "any-file"},
    )
    # Router raises 400 ACCESS_PROHIBITED when caller is not owner/admin
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# iter_files_by_knowledge_id: streaming export pagination
#
# KBs now regularly carry thousands to millions of documents. The old
# get_file_ids_by_knowledge_id() + Files.get_files_by_ids() pairing loaded
# every file's full extracted content into one Python list before writing a
# single JSONL line — these tests exercise the batched replacement.
# ---------------------------------------------------------------------------


def _make_kb_with_files(db_session, file_count, user_id=None):
    from selfai_ui.models.knowledge import KnowledgeFiles
    from tests.factories import FileFactory, KnowledgeFactory

    kb = KnowledgeFactory.create(db_session, user_id=user_id or "test-user")
    files = [
        FileFactory.create(
            db_session,
            filename=f"doc-{i}.txt",
            data={"content": f"content-{i}"},
        )
        for i in range(file_count)
    ]
    for f in files:
        KnowledgeFiles.add_file_to_knowledge(kb.id, f.id)
    return kb, files


@pytest.mark.tier0
def test_iter_files_by_knowledge_id_empty_kb(db_session):
    """A KB with no files yields no batches at all."""
    from selfai_ui.models.knowledge import KnowledgeFiles
    from tests.factories import KnowledgeFactory

    kb = KnowledgeFactory.create(db_session, user_id="test-user")
    batches = list(KnowledgeFiles.iter_files_by_knowledge_id(kb.id))
    assert batches == []


@pytest.mark.tier0
def test_iter_files_by_knowledge_id_paginates_across_batches(db_session):
    """5 files with batch_size=2 come back as batches of [2, 2, 1] and
    cover every file exactly once, with no batch exceeding batch_size."""
    from selfai_ui.models.knowledge import KnowledgeFiles

    kb, files = _make_kb_with_files(db_session, file_count=5)

    batches = list(
        KnowledgeFiles.iter_files_by_knowledge_id(kb.id, batch_size=2)
    )
    assert [len(b) for b in batches] == [2, 2, 1]

    seen_ids = [f.id for batch in batches for f in batch]
    assert sorted(seen_ids) == sorted(f.id for f in files)
    assert len(seen_ids) == len(set(seen_ids))  # no duplicates across batches


@pytest.mark.tier0
def test_iter_files_by_knowledge_id_only_this_kb(db_session):
    """Files belonging to a different KB are never yielded."""
    from selfai_ui.models.knowledge import KnowledgeFiles

    kb_a, files_a = _make_kb_with_files(db_session, file_count=2)
    kb_b, _files_b = _make_kb_with_files(db_session, file_count=3)

    batches = list(KnowledgeFiles.iter_files_by_knowledge_id(kb_a.id))
    seen_ids = [f.id for batch in batches for f in batch]
    assert sorted(seen_ids) == sorted(f.id for f in files_a)


@pytest.mark.tier0
def test_prepare_curator_input_exports_all_files(authenticated_admin, db_session):
    """The /prepare-input endpoint's JSONL export covers every file in the
    KB via the batched iter_files_by_knowledge_id path (batch-boundary
    behavior itself is covered by the model-level tests above)."""
    kb, files = _make_kb_with_files(db_session, file_count=5)

    resp = authenticated_admin.post(f"/api/v1/knowledge/{kb.id}/prepare-input")

    assert resp.status_code == 200
    assert resp.json()["file_count"] == len(files)
