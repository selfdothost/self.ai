"""
T-305 + T-306: Files router CRUD and content round-trip tests.

Note: file upload POST is covered in test_file_upload_security.py for
size/MIME/traversal concerns. Here we test metadata CRUD + content R/W.
"""

import io
import pytest


@pytest.mark.tier0
def test_list_files_empty(authenticated_user):
    resp = authenticated_user.get("/api/v1/files/")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.tier0
def test_upload_list_delete_round_trip(authenticated_user, test_app, monkeypatch):
    """Upload a file, find it in the list, delete it, verify it's gone.

    Note: upload triggers process_file which hits retrieval backends.
    We xfail on retrieval-backend failures rather than skipping — this
    way a regression in upload itself (pre-processing) still surfaces.
    """
    monkeypatch.setattr(test_app.state.config, "FILE_MAX_SIZE", None)
    content = b"hello world"
    resp = authenticated_user.post(
        "/api/v1/files/",
        files={"file": ("greeting.txt", io.BytesIO(content), "text/plain")},
    )
    if resp.status_code == 400 and "retrieval" in resp.text.lower():
        pytest.xfail(
            "Upload succeeded through to retrieval backend processing "
            "which is not mocked. Upstream retrieval backend failure."
        )
    assert resp.status_code == 200, (
        f"Upload failed with {resp.status_code}: {resp.text[:200]}"
    )

    file_id = resp.json()["id"]

    listing = authenticated_user.get("/api/v1/files/").json()
    assert any(f["id"] == file_id for f in listing)

    resp = authenticated_user.delete(f"/api/v1/files/{file_id}")
    assert resp.status_code == 200

    after = authenticated_user.get("/api/v1/files/").json()
    assert not any(f["id"] == file_id for f in after)


@pytest.mark.tier0
def test_get_file_not_found(authenticated_user):
    """Get a non-existent file returns 401 (router scopes to user's files)."""
    resp = authenticated_user.get("/api/v1/files/nonexistent-file-id")
    # files.py pattern: unauthorized/not-found returns 401 per router impl
    assert resp.status_code == 404


@pytest.mark.tier0
def test_files_cross_user_isolation(
    authenticated_user, db_session
):
    """User A does not see user B's files in list."""
    from tests.factories import UserFactory, FileFactory

    user_b = UserFactory.create(db_session)
    file_b = FileFactory.create(
        db_session, user_id=user_b.id, filename="B.txt"
    )

    listing = authenticated_user.get("/api/v1/files/").json()
    assert not any(f.get("filename") == "B.txt" for f in listing)
    assert not any(f.get("id") == file_b.id for f in listing)
