"""Periodic /models integrity sweep -- self.ai/self.ai#38.

Covers utils/model_integrity.py directly (no HTTP client needed for
sweep_once/_index_by_stem) plus the admin-facing GET /llamolotl/api/integrity
endpoint that surfaces the last sweep's findings.
"""

import asyncio
from types import SimpleNamespace

import pytest

from selfai_ui.utils.model_integrity import (
    MIN_SANE_GGUF_SIZE_BYTES,
    _index_by_stem,
    sweep_once,
)
from tests.mocks.external_services import aioresponses_strict

BASE_URL = "http://self-llamolotl:8080"
CONTROL_URL = "http://self-llamolotl:8093"


def _app_state(base_urls=None, control_urls=None, api_configs=None, enable=True):
    cfg = SimpleNamespace(
        ENABLE_LLAMOLOTL_API=enable,
        LLAMOLOTL_BASE_URLS=base_urls if base_urls is not None else [BASE_URL],
        LLAMOLOTL_CONTROL_BASE_URLS=control_urls if control_urls is not None else [CONTROL_URL],
        LLAMOLOTL_API_CONFIGS=api_configs if api_configs is not None else {},
    )
    return SimpleNamespace(config=cfg)


def test_index_by_stem_strips_extension_and_dirs():
    available = [
        {"name": "nomic-embed-text-v1.5.Q5_K_M.gguf", "size": 5000},
        {"name": "subdir/GLM-4.5-Air-q8_0.gguf", "size": 9000},
    ]
    index = _index_by_stem(available)
    assert set(index) == {"nomic-embed-text-v1.5.Q5_K_M", "GLM-4.5-Air-q8_0"}
    assert index["GLM-4.5-Air-q8_0"]["size"] == 9000


@pytest.mark.tier1
def test_sweep_flags_missing_backing_file():
    """A model known to llama-server with no matching entry in
    /api/models/available is reported as 'missing' -- the exact
    self.ai/self.ai#38 root-cause scenario (preset-configured, never pulled)."""
    with aioresponses_strict() as m:
        m.get(
            f"{BASE_URL}/v1/models",
            payload={"data": [{"id": "nomic-embed-text-v1.5.Q5_K_M", "object": "model"}]},
        )
        m.get(f"{CONTROL_URL}/api/models/available", payload=[])

        findings = asyncio.run(sweep_once(_app_state()))

    assert BASE_URL in findings
    assert len(findings[BASE_URL]) == 1
    issue = findings[BASE_URL][0]
    assert issue["model_id"] == "nomic-embed-text-v1.5.Q5_K_M"
    assert issue["issue"] == "missing"


@pytest.mark.tier1
def test_sweep_flags_undersized_file():
    """A backing file that exists but is far below any real GGUF's size
    (truncated/interrupted pull) is reported as 'undersized'."""
    with aioresponses_strict() as m:
        m.get(
            f"{BASE_URL}/v1/models",
            payload={"data": [{"id": "GLM-4.5-Air-q8_0", "object": "model"}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            payload=[{"name": "GLM-4.5-Air-q8_0.gguf", "size": 512}],
        )

        findings = asyncio.run(sweep_once(_app_state()))

    assert BASE_URL in findings
    issue = findings[BASE_URL][0]
    assert issue["issue"] == "undersized"
    assert issue["size_bytes"] == 512
    assert issue["min_sane_size_bytes"] == MIN_SANE_GGUF_SIZE_BYTES


@pytest.mark.tier1
def test_sweep_clean_when_file_present_and_sane():
    with aioresponses_strict() as m:
        m.get(
            f"{BASE_URL}/v1/models",
            payload={"data": [{"id": "GLM-4.5-Air-q8_0", "object": "model"}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            payload=[{"name": "GLM-4.5-Air-q8_0.gguf", "size": MIN_SANE_GGUF_SIZE_BYTES * 100}],
        )

        findings = asyncio.run(sweep_once(_app_state()))

    assert findings == {}


def test_sweep_noop_when_llamolotl_disabled():
    findings = asyncio.run(sweep_once(_app_state(enable=False)))
    assert findings == {}


@pytest.mark.tier1
def test_sweep_skips_cycle_on_unreachable_backend():
    """A connection failure must not be reported as every model being
    'missing' -- that would be a false positive flood on a transient
    network blip. sweep_once should skip the cycle for that backend."""
    with aioresponses_strict() as m:
        m.get(f"{BASE_URL}/v1/models", exception=ConnectionError("boom"))
        m.get(f"{CONTROL_URL}/api/models/available", payload=[])

        findings = asyncio.run(sweep_once(_app_state()))

    assert findings == {}


@pytest.mark.tier1
def test_integrity_endpoint_surfaces_last_sweep_findings(authenticated_admin):
    """GET /llamolotl/api/integrity returns whatever sweep_once last wrote
    to app.state.MODEL_INTEGRITY_WARNINGS -- the admin-UI-facing surface
    for this sweep's findings."""
    resp = authenticated_admin.get("/llamolotl/api/integrity")
    assert resp.status_code == 200
    body = resp.json()
    assert "warnings" in body
    assert isinstance(body["warnings"], dict)
