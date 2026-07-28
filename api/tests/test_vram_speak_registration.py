"""_register_speak_vram_consumer() config-guard tests
(cavekit-vram-speak-consumer R3 / T-006).

The lifespan hook reads its env via a local ``from selfai_ui.env import ...`` at
call time, so we drive it by monkeypatching ``selfai_ui.env.SPEAK_VRAM_*`` and
mock ``VramLeases.register`` to capture the ``VramConsumerRegisterForm`` without
a DB. No network, no GPU, no real registry.

Cases (mirroring R1-AC2/AC3/AC4/AC5):
  * configured -> register called once with the self.speak consumer, capacity,
    and priority from env.
  * unset/blank -> register NOT called, no exception (R1-AC3).
  * non-integer -> register NOT called, no exception (R1-AC4).
  * registry raises -> the hook swallows it, no exception escapes (R1-AC5).

NOTE: this imports ``selfai_ui.main`` (where the hook lives), which pulls the
full app import graph — it runs under the repo's full CI env, not a minimal
transport-only venv.
"""

import pytest

from selfai_ui.main import _register_speak_vram_consumer


def _capture_register(monkeypatch):
    """Replace VramLeases.register with a capturing fake; return the calls list."""
    calls = []

    def fake_register(form):
        calls.append(form)
        return None

    monkeypatch.setattr(
        "selfai_ui.models.vram_leases.VramLeases.register", fake_register
    )
    return calls


def test_configured_registers_speak_consumer(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_CAPACITY_BYTES", "25757220864")
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_LEASE_PRIORITY", 5)
    calls = _capture_register(monkeypatch)

    _register_speak_vram_consumer()

    assert len(calls) == 1
    form = calls[0]
    assert form.consumer_id == "self.speak"
    assert form.total_capacity_bytes == 25757220864
    assert form.priority == 5


def test_unset_capacity_registers_nothing_no_raise(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_CAPACITY_BYTES", "")
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_LEASE_PRIORITY", 0)
    calls = _capture_register(monkeypatch)

    # Must not raise.
    _register_speak_vram_consumer()

    assert calls == []


def test_blank_whitespace_capacity_registers_nothing_no_raise(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_CAPACITY_BYTES", "   ")
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_LEASE_PRIORITY", 0)
    calls = _capture_register(monkeypatch)

    _register_speak_vram_consumer()

    assert calls == []


def test_non_integer_capacity_registers_nothing_no_raise(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_CAPACITY_BYTES", "twelve")
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_LEASE_PRIORITY", 5)
    calls = _capture_register(monkeypatch)

    _register_speak_vram_consumer()

    assert calls == []


def test_registry_error_is_swallowed_no_raise(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_CAPACITY_BYTES", "25757220864")
    monkeypatch.setattr("selfai_ui.env.SPEAK_VRAM_LEASE_PRIORITY", 5)

    def boom(form):
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(
        "selfai_ui.models.vram_leases.VramLeases.register", boom
    )

    # Best-effort at boot: the registry error must not escape the hook.
    _register_speak_vram_consumer()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
