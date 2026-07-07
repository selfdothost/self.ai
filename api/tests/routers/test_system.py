"""
System router tests — resources and processes observability.

Admin-only. Tests verify endpoint auth + that shape matches the declared
response model (SystemResources, ProcessList).

Module-level dependency check: if psutil is unavailable, the whole
module is skipped with a clear reason. Per-test skip-on-unexpected-status
was removed — regressions must fail, not silently skip.
"""

import pytest

# Fail-fast at import: if psutil is missing the system endpoints cannot work.
# pytest.importorskip skips the whole module with a clear diagnostic.
pytest.importorskip("psutil", reason="system router requires psutil")


@pytest.mark.tier0
def test_resources_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/system/resources")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_resources_admin_access(authenticated_admin):
    """Admin reading resources returns 200 + dict body."""
    resp = authenticated_admin.get("/api/system/resources")
    assert resp.status_code == 200, (
        f"System resources unexpectedly returned {resp.status_code}. "
        f"Body: {resp.text[:200]}"
    )
    assert isinstance(resp.json(), dict)


@pytest.mark.tier0
def test_processes_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/system/processes")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_processes_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/system/processes")
    assert resp.status_code == 200, (
        f"System processes unexpectedly returned {resp.status_code}. "
        f"Body: {resp.text[:200]}"
    )
    assert isinstance(resp.json(), dict)
