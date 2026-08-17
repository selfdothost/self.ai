"""_register_publish_vram_consumer() config-guard tests (self.ai#136).

Style follows tests/test_vram_speak_registration.py: the hook reads its env via
a local ``from selfai_ui.env import ...`` at call time, so it is driven by
monkeypatching ``selfai_ui.env.PUBLISH_VRAM_*``, with ``VramLeases.register``
faked to capture the form without a DB.

THIS FILE EXISTS BECAUSE ITS ABSENCE SHIPPED A GAP. `#136` added
``_acquire_publish_card`` / ``_release_publish_card`` and wired them into
dispatch, with tests that patched the acquire — so they proved the ORDER was
right and never that a real broker is consulted. Nothing registered
``self.publish``, so ``_publish_is_lease_consumer()`` was permanently False and
the acquire short-circuited in production while every test passed. Same lesson
as test_startup_vram_wiring.py: a capability only ever injected in tests is one
nobody has checked is plugged in.
"""

import pytest

from selfai_ui.main import _register_publish_vram_consumer
from selfai_ui.utils.gpu_queue import PUBLISH_AUDIENCE


def _capture_register(monkeypatch):
    calls = []

    def fake_register(form):
        calls.append(form)
        return None

    monkeypatch.setattr("selfai_ui.models.vram_leases.VramLeases.register", fake_register)
    return calls


def test_configured_registers_the_publish_consumer(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", "25757220864")
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_LEASE_PRIORITY", 8)
    calls = _capture_register(monkeypatch)

    _register_publish_vram_consumer()

    assert len(calls) == 1
    form = calls[0]
    assert form.consumer_id == "self.publish"
    assert form.total_capacity_bytes == 25757220864
    assert form.priority == 8


def test_it_registers_the_id_gpu_queue_actually_acquires(monkeypatch):
    """The registration and the acquire must name the same consumer.

    If these drift, `_publish_is_lease_consumer()` looks up an id nothing
    registered and reports False forever — which is precisely the failure this
    file was written for, and it would be silent.
    """
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", "25757220864")
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_LEASE_PRIORITY", 8)
    calls = _capture_register(monkeypatch)

    _register_publish_vram_consumer()

    assert calls[0].consumer_id == PUBLISH_AUDIENCE


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_unset_or_blank_registers_nothing_and_does_not_raise(monkeypatch, raw):
    """Unconfigured is the state every deployment was in before this hook."""
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", raw)
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_LEASE_PRIORITY", 8)
    calls = _capture_register(monkeypatch)

    _register_publish_vram_consumer()

    assert calls == []


def test_a_non_integer_capacity_registers_nothing_and_does_not_raise(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", "twenty-four gigs")
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_LEASE_PRIORITY", 8)
    calls = _capture_register(monkeypatch)

    _register_publish_vram_consumer()

    assert calls == []


def test_a_failing_registry_never_escapes_the_lifespan(monkeypatch):
    """self.ai must boot on a deployment where the registry write fails."""

    def explode(form):
        raise RuntimeError("yard-pg is unhappy")

    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", "25757220864")
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_LEASE_PRIORITY", 8)
    monkeypatch.setattr("selfai_ui.models.vram_leases.VramLeases.register", explode)

    _register_publish_vram_consumer()  # must not raise


def test_the_hook_is_actually_called_at_startup():
    """The whole point: a hook nobody calls is the gap this closes."""
    import inspect

    from selfai_ui.main import lifespan

    assert "_register_publish_vram_consumer()" in inspect.getsource(lifespan)


def test_publish_is_in_the_trusted_consumer_table(monkeypatch):
    """The HTTP boundary defers to this table, and it must agree with the hook.

    `vram_consumer_config` is core's trusted view of consumer POLICY — capacity
    and priority are operator config, not a consumer's self-report. A consumer
    the hook registers but this table does not know is one whose policy could
    be overwritten by whatever a caller posted.
    """
    from selfai_ui.utils.vram_consumer_config import trusted_consumer_config

    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", "25757220864")
    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_LEASE_PRIORITY", 8)

    config = trusted_consumer_config()

    assert "self.publish" in config
    assert config["self.publish"]["capacity"] == 25757220864
    assert config["self.publish"]["priority"] == 8


def test_publish_is_never_force_reap_eligible(monkeypatch):
    """Reaping "the publish" would mean killing llamolotl.

    The merge runs in a pipeline subprocess inside llamolotl's pod, so the pod
    identity belongs to llamolotl's row, under its own selector.
    """
    from selfai_ui.utils.vram_consumer_config import trusted_consumer_config

    monkeypatch.setattr("selfai_ui.env.PUBLISH_VRAM_CAPACITY_BYTES", "25757220864")

    entry = trusted_consumer_config()["self.publish"]

    assert not entry.get("namespace")
    assert not entry.get("selector")
