"""
T-227 + T-228: File upload security tests.

Validates server-side enforcement of upload constraints:
- Size limits (413 on oversized)
- Path traversal prevention
- MIME type validation (415 on mismatch)
- Null byte injection rejected
"""

import io
import pytest


UPLOAD_PATH = "/api/v1/files/"


# ---------------------------------------------------------------------------
# T-227: Size limit enforcement
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_upload_without_auth_rejected(client):
    """File upload requires authentication."""
    resp = client.post(
        UPLOAD_PATH,
        files={"file": ("test.txt", b"hello", "text/plain")},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
@pytest.mark.security
def test_upload_small_file_accepted(authenticated_user, test_app):
    """A file under the size limit is accepted (or fails gracefully post-size-check)."""
    # Set a generous size limit for this test
    test_app.state.config.FILE_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
    resp = authenticated_user.post(
        UPLOAD_PATH,
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    # Upload itself should not fail on size (may fail on processing,
    # that's ok — we only care that size check passes)
    assert resp.status_code != 413


@pytest.mark.tier0
@pytest.mark.security
def test_upload_oversized_file_returns_413(authenticated_user, test_app):
    """A file exceeding FILE_MAX_SIZE returns 413."""
    test_app.state.config.FILE_MAX_SIZE = 100  # bytes
    big_content = b"A" * 500
    resp = authenticated_user.post(
        UPLOAD_PATH,
        files={"file": ("big.bin", big_content, "application/octet-stream")},
    )
    assert resp.status_code == 413, (
        f"Expected 413 for oversized upload, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# T-228: Path traversal, null bytes, MIME
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_upload_path_traversal_sanitized(authenticated_user, test_app):
    """Filenames with path traversal sequences do not escape upload dir."""
    test_app.state.config.FILE_MAX_SIZE = None  # no limit for this test
    resp = authenticated_user.post(
        UPLOAD_PATH,
        files={
            "file": (
                "../../../etc/passwd",
                b"harmless",
                "text/plain",
            )
        },
    )
    # Must not return a path containing traversal sequences
    if resp.status_code == 200:
        body = resp.json()
        path = body.get("meta", {}).get("path", body.get("path", ""))
        assert ".." not in path, (
            f"Upload path contains traversal sequence: {path}"
        )


@pytest.mark.tier0
@pytest.mark.security
def test_upload_null_byte_filename_handled(authenticated_user, test_app):
    """Null bytes in filename must be stripped, not truncate the filename."""
    test_app.state.config.FILE_MAX_SIZE = None
    resp = authenticated_user.post(
        UPLOAD_PATH,
        files={
            "file": (
                "safe.jpg\x00.php",
                b"\xff\xd8\xff\xe0content",
                "image/jpeg",
            )
        },
    )
    # Either rejected or filename sanitized. If stored, the filename
    # must NOT contain a null byte or its URL-encoded variants.
    if resp.status_code == 200:
        body = resp.json()
        filename = body.get("filename", body.get("meta", {}).get("name", ""))
        assert "\x00" not in filename, "Raw null byte found in stored filename"
        assert "%00" not in filename, "URL-encoded null byte found in stored filename"
        assert "%2500" not in filename, "Double-encoded null byte found in stored filename"


@pytest.mark.tier0
@pytest.mark.security
def test_upload_blocked_mime_rejected(authenticated_user, test_app):
    """An executable MIME type is rejected with 415."""
    test_app.state.config.FILE_MAX_SIZE = None
    resp = authenticated_user.post(
        UPLOAD_PATH,
        files={
            "file": (
                "malware.bin",
                b"\x7fELFtest",
                "application/x-executable",
            )
        },
    )
    assert resp.status_code == 415, (
        f"Expected 415 for executable MIME, got {resp.status_code}"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_upload_allowlist_blocks_non_allowed(authenticated_user, test_app):
    """When allowlist is configured, non-allowed types return 415."""
    test_app.state.config.FILE_MAX_SIZE = None
    test_app.state.config.FILE_UPLOAD_MIME_ALLOWLIST = ["text/plain"]
    try:
        resp = authenticated_user.post(
            UPLOAD_PATH,
            files={
                "file": (
                    "document.pdf",
                    b"%PDF-1.4content",
                    "application/pdf",
                )
            },
        )
        assert resp.status_code == 415, (
            f"PDF should be rejected when allowlist is text/plain only, "
            f"got {resp.status_code}"
        )
    finally:
        test_app.state.config.FILE_UPLOAD_MIME_ALLOWLIST = []
