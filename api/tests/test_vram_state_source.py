"""ControlBaseVramStateSource tests (R6 poller gap-fix, Color 2b).

The generic single-control-base state source the poller uses for self.speak and
self.sketch (whose control base is a single config string, unlike llamolotl's
list). Drives the defensive parse with an injected ``client_factory`` and a fake
response — no network, no GPU, no real ticket secret (``mint_service_ticket`` is
monkeypatched). Mirrors ``test_vram_llamolotl_state_source``'s posture: every
non-integer-held shape resolves to ``None`` (poller skips), a real integer (incl.
0 for no_gpu) is returned, and an unconfigured base makes NO HTTP call at all.

Async cases run via ``asyncio.run(...)`` inside sync tests, matching
``test_vram_llamolotl_state_source.py`` (the repo registers no pytest-asyncio and
runs under ``--strict-markers``).
"""

import asyncio
import types

import pytest

from selfai_ui.utils.vram_state_source import ControlBaseVramStateSource


class _FakeResp:
    def __init__(self, status_code, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """Async-context ``httpx.AsyncClient`` stand-in returning a preset response
    (or raising a preset exception) from ``.get()``."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        if self._exc is not None:
            raise self._exc
        return self._resp


def _app_state(base="http://self-sketch:8188", attr="SKETCH_CONTROL_BASE_URL"):
    cfg = types.SimpleNamespace(**{attr: base})
    return types.SimpleNamespace(config=cfg)


def _source(monkeypatch, resp=None, exc=None, app_state=None, attr="SKETCH_CONTROL_BASE_URL"):
    # Never mint a real ticket in a unit test.
    monkeypatch.setattr(
        "selfai_ui.utils.vram_state_source.mint_service_ticket",
        lambda audience, scope, *a, **k: "test-ticket",
    )
    client = _FakeClient(resp=resp, exc=exc)
    src = ControlBaseVramStateSource(
        app_state=app_state if app_state is not None else _app_state(attr=attr),
        config_attr=attr,
        audience="self.sketch",
        client_factory=lambda timeout: client,
    )
    return src, client


def _read(src):
    return asyncio.run(src.read_held_bytes("self.sketch"))


def test_ok_returns_integer_held(monkeypatch):
    src, client = _source(monkeypatch, resp=_FakeResp(200, {"status": "ok", "held_bytes": 12345}))
    assert _read(src) == 12345
    assert client.calls and client.calls[0][0].endswith("/api/system/vram-state")


def test_no_gpu_relays_zero(monkeypatch):
    # status=no_gpu carries a REAL integer 0 (CPU mode) — relayed, not skipped.
    src, _ = _source(monkeypatch, resp=_FakeResp(200, {"status": "no_gpu", "held_bytes": 0}))
    assert _read(src) == 0


def test_unreachable_null_held_is_none(monkeypatch):
    # status=unreachable carries held=null — no integer -> None -> poller skips.
    src, _ = _source(monkeypatch, resp=_FakeResp(200, {"status": "unreachable", "held_bytes": None}))
    assert _read(src) is None


def test_llamolotl_style_field_name(monkeypatch):
    src, _ = _source(monkeypatch, resp=_FakeResp(200, {"held_vram_bytes": 777}))
    assert _read(src) == 777


def test_bool_held_excluded(monkeypatch):
    # bool is an int subclass — must NOT be read as 1.
    src, _ = _source(monkeypatch, resp=_FakeResp(200, {"held_bytes": True}))
    assert _read(src) is None


def test_non_2xx_is_none(monkeypatch):
    src, _ = _source(monkeypatch, resp=_FakeResp(503, {"held_bytes": 5}))
    assert _read(src) is None


def test_undecodable_body_is_none(monkeypatch):
    src, _ = _source(monkeypatch, resp=_FakeResp(200, raise_json=True))
    assert _read(src) is None


def test_non_object_body_is_none(monkeypatch):
    src, _ = _source(monkeypatch, resp=_FakeResp(200, ["not", "a", "dict"]))
    assert _read(src) is None


def test_connection_error_is_none(monkeypatch):
    src, _ = _source(monkeypatch, exc=RuntimeError("connection refused"))
    assert _read(src) is None


def test_unconfigured_base_makes_no_http(monkeypatch):
    # Blank control base -> None, and .get() is never called.
    src, client = _source(
        monkeypatch, resp=_FakeResp(200, {"held_bytes": 5}), app_state=_app_state(base="  ")
    )
    assert _read(src) is None
    assert client.calls == []


def test_missing_config_attr_is_none(monkeypatch):
    src, client = _source(
        monkeypatch,
        resp=_FakeResp(200, {"held_bytes": 5}),
        app_state=types.SimpleNamespace(config=types.SimpleNamespace()),
    )
    assert _read(src) is None
    assert client.calls == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
