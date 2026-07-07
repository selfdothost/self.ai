"""T-316: Benchmarks router tests — list + update."""

import pytest


@pytest.mark.tier0
def test_list_benchmarks(authenticated_admin, seeded_benchmarks):
    """Alembic migration seeds default benchmarks."""
    resp = authenticated_admin.get("/api/benchmarks")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Migration seeds known benchmarks
    assert len(body) >= 1


@pytest.mark.tier0
def test_update_benchmark_not_found(authenticated_admin):
    resp = authenticated_admin.put(
        "/api/benchmarks/nonexistent-benchmark",
        json={"max_duration_minutes": 60, "notes": "test"},
    )
    assert resp.status_code == 404


@pytest.mark.tier0
def test_update_benchmark_max_duration(authenticated_admin, seeded_benchmarks):
    listing = authenticated_admin.get("/api/benchmarks").json()
    if not listing:
        pytest.skip("No seeded benchmarks to update")
    bench_id = listing[0]["id"]

    resp = authenticated_admin.put(
        f"/api/benchmarks/{bench_id}",
        json={"max_duration_minutes": 120, "notes": "updated"},
    )
    assert resp.status_code == 200
    assert resp.json()["max_duration_minutes"] == 120


@pytest.mark.tier0
def test_user_cannot_list_benchmarks(authenticated_user):
    resp = authenticated_user.get("/api/benchmarks")
    assert resp.status_code in (401, 403)
