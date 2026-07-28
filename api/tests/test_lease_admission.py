"""Chat/API lease admission checkpoint tests (Decision 6 / R3 — T-018, T-020, T-021).

Drives ``evaluate_admission`` directly with the four state reads it makes
(active window, exclusive holder, eval-running, loaded model) monkeypatched — no
DB, no network. Covers:

  * T-018 — training/curator window -> REFUSE; registry exclusive lease -> REFUSE;
    eval window + a different fresh loaded model -> SUBSTITUTE; the same model,
    no eval window, and an eval-only window -> PROCEED.
  * T-020 — startup race: eval running but no model ever polled (None) -> PROCEED
    (eval fails open); and recovery once a fresh model is reported.
  * T-021 — staleness: fresh -> substitute, stale -> passthrough, recovered ->
    substitute again; and the window-read-error fail-CLOSED path.
"""

import time
import types

import pytest

from selfai_ui.models.eval_jobs import EvalJobs
from selfai_ui.models.vram_leases import STALE_THRESHOLD_SECONDS, VramLeases
from selfai_ui.utils.lease_admission import AdmissionAction, evaluate_admission


def _window(*job_types):
    """A JobWindowWithSlots-shaped fake with the given slot job_types."""
    return types.SimpleNamespace(
        slots=[types.SimpleNamespace(job_type=jt) for jt in job_types]
    )


def _setup(
    monkeypatch,
    window=None,
    window_raises=False,
    exclusive=None,
    eval_running=False,
    loaded=(None, None),
):
    """Patch the four reads evaluate_admission makes. Defaults = fully idle."""
    import selfai_ui.models.job_windows as jw

    if window_raises:
        def _raise():
            raise RuntimeError("job_window table unreadable")
        monkeypatch.setattr(jw.JobWindows, "get_active_window", _raise)
    else:
        monkeypatch.setattr(jw.JobWindows, "get_active_window", lambda: window)

    monkeypatch.setattr(VramLeases, "active_exclusive_holder", lambda: exclusive)
    monkeypatch.setattr(
        EvalJobs,
        "get_jobs_by_status",
        lambda status: ([object()] if (eval_running and status == "running") else []),
    )
    monkeypatch.setattr(VramLeases, "loaded_model", lambda consumer_id: loaded)


# ---- T-018: refuse / substitute / passthrough -----------------------------


def test_passthrough_when_idle(monkeypatch):
    _setup(monkeypatch)
    d = evaluate_admission(None, "modelA")
    assert d.action == AdmissionAction.PROCEED


@pytest.mark.parametrize("job_type", ["training", "curator"])
def test_exclusive_window_refuses(monkeypatch, job_type):
    _setup(monkeypatch, window=_window(job_type))
    d = evaluate_admission(None, "modelA")
    assert d.action == AdmissionAction.REFUSE
    assert d.refuse_status == 503
    assert d.refuse_detail["gpu_locked_by"] == job_type


def test_eval_only_window_is_not_a_lockout(monkeypatch):
    # An eval-only window is NOT VRAM-exclusive — no lockout from the window path.
    _setup(monkeypatch, window=_window("code-eval"))
    d = evaluate_admission(None, "modelA")
    assert d.action == AdmissionAction.PROCEED


def test_registry_exclusive_holder_refuses(monkeypatch):
    holder = types.SimpleNamespace(consumer_id="self.training")
    _setup(monkeypatch, exclusive=holder)
    d = evaluate_admission(None, "modelA")
    assert d.action == AdmissionAction.REFUSE
    assert d.refuse_detail["gpu_locked_by"] == "self.training"


def test_eval_substitute_fresh_different_model(monkeypatch):
    _setup(monkeypatch, eval_running=True, loaded=("loadedX", int(time.time())))
    d = evaluate_admission(None, "requestedY")
    assert d.action == AdmissionAction.SUBSTITUTE
    assert d.substitute_model == "loadedX"
    assert d.requested_model == "requestedY"


def test_eval_same_model_passthrough(monkeypatch):
    _setup(monkeypatch, eval_running=True, loaded=("modelA", int(time.time())))
    d = evaluate_admission(None, "modelA")
    assert d.action == AdmissionAction.PROCEED


def test_no_eval_window_no_substitution(monkeypatch):
    # A model is loaded but no eval window is active — no substitution.
    _setup(monkeypatch, eval_running=False, loaded=("loadedX", int(time.time())))
    d = evaluate_admission(None, "requestedY")
    assert d.action == AdmissionAction.PROCEED


# ---- T-020: startup race + recovery ---------------------------------------


def test_startup_no_loaded_model_fails_open(monkeypatch):
    # Eval running but nothing ever polled (None) -> passthrough (fail open).
    _setup(monkeypatch, eval_running=True, loaded=(None, None))
    d = evaluate_admission(None, "requestedY")
    assert d.action == AdmissionAction.PROCEED


def test_recovers_to_substitute_once_fresh_model_reported(monkeypatch):
    _setup(monkeypatch, eval_running=True, loaded=(None, None))
    assert evaluate_admission(None, "requestedY").action == AdmissionAction.PROCEED
    # A fresh report arrives -> substitution resumes immediately.
    _setup(monkeypatch, eval_running=True, loaded=("loadedX", int(time.time())))
    assert evaluate_admission(None, "requestedY").action == AdmissionAction.SUBSTITUTE


# ---- T-021: staleness (fresh / stale / recovered) + fail-closed ------------


def test_stale_loaded_model_fails_open(monkeypatch):
    stale_ts = int(time.time()) - (STALE_THRESHOLD_SECONDS + 60)
    _setup(monkeypatch, eval_running=True, loaded=("loadedX", stale_ts))
    d = evaluate_admission(None, "requestedY")
    assert d.action == AdmissionAction.PROCEED  # no substitution on a stale read


def test_stale_then_fresh_recovers(monkeypatch):
    stale_ts = int(time.time()) - (STALE_THRESHOLD_SECONDS + 60)
    _setup(monkeypatch, eval_running=True, loaded=("loadedX", stale_ts))
    assert evaluate_admission(None, "requestedY").action == AdmissionAction.PROCEED
    _setup(monkeypatch, eval_running=True, loaded=("loadedX", int(time.time())))
    assert evaluate_admission(None, "requestedY").action == AdmissionAction.SUBSTITUTE


def test_window_read_error_fails_closed(monkeypatch):
    # T-019: an unreadable window state refuses (fail closed), distinct from a lock.
    _setup(monkeypatch, window_raises=True)
    d = evaluate_admission(None, "modelA")
    assert d.action == AdmissionAction.REFUSE
    assert d.refuse_status == 503
    assert d.refuse_detail.get("gpu_status_unknown") is True
