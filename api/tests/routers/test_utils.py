"""Utils router tests — gravatar, code format, markdown, pdf, admin downloads."""

import pytest


@pytest.mark.tier0
def test_gravatar_public(client):
    """Gravatar endpoint is public (email is a weak secret)."""
    resp = client.get("/api/v1/utils/gravatar?email=test@example.com")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_code_format_requires_auth(client):
    resp = client.post(
        "/api/v1/utils/code/format", json={"code": "x=1"}
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_code_format_valid_python(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/utils/code/format",
        json={"code": "x=1;y=2"},
    )
    assert resp.status_code == 200
    assert "code" in resp.json()


@pytest.mark.tier0
def test_code_format_invalid_python_rejected(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/utils/code/format",
        json={"code": "def broken("},  # unclosed paren
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_markdown_requires_auth(client):
    resp = client.post("/api/v1/utils/markdown", json={"md": "# hi"})
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_markdown_valid_input(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/utils/markdown",
        json={"md": "# Hello\n\nWorld"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "html" in body
    assert "<h1>" in body["html"]


@pytest.mark.tier0
def test_pdf_requires_auth(client):
    resp = client.post(
        "/api/v1/utils/pdf",
        json={"title": "t", "messages": []},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_db_download_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/utils/db/download")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_litellm_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/utils/litellm/config")
    assert resp.status_code in (401, 403)
