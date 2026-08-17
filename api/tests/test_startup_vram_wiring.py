"""Startup wiring of the VRAM lease broker (self.ai#75).

THIS FILE EXISTS BECAUSE ITS ABSENCE SHIPPED A BUG. `set_reaper()` had zero
callers anywhere in the repo, so `VramBroker._reaper` was permanently None and
the entire R5 force-reap escalation was unreachable in production — while the
Deployment carried a pods:delete ServiceAccount for it, self.llamolotl was
registered with a pod identity, and the boot log said "force-reap armed". Every
unit test passed, because every one of them injected a reaper directly
(`VramBrokerImpl(reaper=fake)`) and so never exercised the wiring.

The lesson generalises past the reaper: nothing pinned that the lifespan installs
the release transport or starts the poller either. A capability that is only ever
injected in tests is a capability nobody has checked is plugged in. These tests
pin the plug.

Style follows tests/test_vram_speak_registration.py: the hooks read env via a
local import at call time, so they are driven with monkeypatch. Importing
selfai_ui.main pulls the full app graph, so this runs under the repo's CI env.
"""

import inspect

from selfai_ui.main import _install_pod_reaper, lifespan
from selfai_ui.utils.vram_broker import VramBrokerImpl


class _Broker(VramBrokerImpl):
    """A throwaway broker so a test never mutates the module-level singleton."""


def _patch_broker(monkeypatch):
    broker = _Broker()
    monkeypatch.setattr("selfai_ui.utils.vram_broker.VramBroker", broker)
    return broker


# ---------------------------------------------------------------------------
# The wiring itself
# ---------------------------------------------------------------------------


def test_lifespan_installs_every_broker_capability():
    """The regression guard for #75, kept deliberately structural.

    A functional assertion would need the whole app to boot; what actually went
    wrong was simpler than that — a call that was never written. So assert the
    lifespan body reaches for each capability. If someone deletes one of these
    calls, this fails; that is exactly the failure that went unnoticed."""
    # lifespan is @asynccontextmanager-wrapped; unwrap so getsource reads the
    # body rather than contextlib's helper.
    src = inspect.getsource(getattr(lifespan, "__wrapped__", lifespan))
    assert "_install_speak_release_transport(" in src, "release transport not installed"
    assert "_run_vram_poller(" in src, "R6 state poller not started"
    assert "_install_pod_reaper(" in src, "R5 force-reap capability not installed"


def test_configured_pod_identity_arms_the_reaper_and_its_target(monkeypatch):
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_NAMESPACE", "self-ai")
    monkeypatch.setattr(
        "selfai_ui.env.LLAMOLOTL_K8S_POD_SELECTOR", "app.kubernetes.io/name=self-llamolotl"
    )
    broker = _patch_broker(monkeypatch)

    _install_pod_reaper()

    assert broker._reaper is not None, "reaper was not installed"
    assert broker._reap_target_for("self.llamolotl") == (
        "self-ai",
        "app.kubernetes.io/name=self-llamolotl",
    )


def test_unconfigured_pod_identity_leaves_r5_inert(monkeypatch):
    """No env identity -> no reaper AND no targets. R5 stays exactly as it was
    before this wiring landed: escalation skipped, nothing reapable."""
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_NAMESPACE", "")
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_POD_SELECTOR", "")
    broker = _patch_broker(monkeypatch)

    _install_pod_reaper()

    assert broker._reaper is None
    assert broker._reap_target_for("self.llamolotl") is None


def test_half_configured_pod_identity_leaves_r5_inert(monkeypatch):
    """A namespace with no selector (or vice versa) is not a target. Half a
    coordinate is not somewhere to aim a pod deletion."""
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_NAMESPACE", "self-ai")
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_POD_SELECTOR", "   ")
    broker = _patch_broker(monkeypatch)

    _install_pod_reaper()

    assert broker._reaper is None
    assert broker._reap_target_for("self.llamolotl") is None


def test_wiring_failure_never_escapes_the_lifespan(monkeypatch):
    """Best-effort, like every neighbouring startup hook: a broken reaper import
    must not stop the app coming up. Core serving chat matters more than R5."""
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_NAMESPACE", "self-ai")
    monkeypatch.setattr("selfai_ui.env.LLAMOLOTL_K8S_POD_SELECTOR", "app=x")

    def _boom(*_a, **_kw):
        raise RuntimeError("k8s client unavailable")

    monkeypatch.setattr("selfai_ui.utils.vram_k8s.KubernetesPodReaper", _boom)
    broker = _patch_broker(monkeypatch)

    _install_pod_reaper()  # must not raise

    assert broker._reaper is None
