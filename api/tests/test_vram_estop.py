"""Focused tests for the system-wide GPU e-stop (``POST /api/vram-leases/release-all``).

Exercises the endpoint's fan-out logic directly (calling ``release_all`` as a
plain coroutine with a fake request/user) rather than through the full-app
TestClient, so it does not need the full app dependency set. The llamolotl
leg's ``unload_all_llamolotl_models`` is stubbed via ``sys.modules`` because
``routers.llamolotl`` imports ``config`` at module load.
"""

import asyncio
import sys
import types

import selfai_ui.routers.vram_leases as vl
from selfai_ui.models.vram_leases import VramConsumerStatus
from selfai_ui.utils.vram_broker import ReleaseOutcome, ReleaseResponse


class _FakeTransport:
    """Records every request_release call and returns a CONFIRMED response."""

    def __init__(self):
        self.calls = []

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds, force=False):
        self.calls.append(
            {
                "consumer_id": consumer_id,
                "amount_bytes": amount_bytes,
                "timeout_seconds": timeout_seconds,
                "force": force,
            }
        )
        return ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=0)


class _FakeSummary:
    def __init__(self, consumers):
        self.consumers = consumers


class _FakeRequest:
    def __init__(self):
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(config=None))


def _status(cid, held=0):
    return VramConsumerStatus(consumer_id=cid, total_capacity_bytes=24 * 1024**3, held_bytes=held)


def _install_llamolotl_stub(monkeypatch, recorder):
    mod = types.ModuleType("selfai_ui.routers.llamolotl")

    async def _fake_unload_all(app_state, url_idx=None):
        recorder.append(app_state)
        return {"unloaded": ["qwen"], "errors": []}

    mod.unload_all_llamolotl_models = _fake_unload_all
    monkeypatch.setitem(sys.modules, "selfai_ui.routers.llamolotl", mod)


def test_release_all_fans_out_force_to_every_non_llamolotl_consumer(monkeypatch):
    transport = _FakeTransport()
    consumers = [
        _status("self.llamolotl", held=10 * 1024**3),
        _status("self.speak", held=2 * 1024**3),
        _status("self.sketch", held=4 * 1024**3),
        _status("self.curator", held=8 * 1024**3),
    ]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: transport)
    llamolotl_calls = []
    _install_llamolotl_stub(monkeypatch, llamolotl_calls)

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    by_id = {r.consumer_id: r for r in summary.results}
    # self.curator is IN scope as of self.ai#88 — it holds up to 24GiB of the
    # shared 4090 for a curation run, which is precisely what an operator presses
    # this button to clear. It needs no special case: its transport is an
    # ordinary ReleaseTransport reached through the same force=True fan-out.
    forced = {c["consumer_id"]: c for c in transport.calls}
    assert set(forced) == {"self.speak", "self.sketch", "self.curator"}
    assert by_id["self.curator"].ok is True
    assert forced["self.curator"]["force"] is True
    assert forced["self.curator"]["amount_bytes"] == 8 * 1024**3
    assert all(c["force"] is True for c in transport.calls)
    assert all(c["timeout_seconds"] == vl._ESTOP_RELEASE_TIMEOUT_SECONDS for c in transport.calls)
    # forceful release target = the consumer's held bytes.
    assert forced["self.speak"]["amount_bytes"] == 2 * 1024**3
    assert forced["self.sketch"]["amount_bytes"] == 4 * 1024**3
    # llamolotl leg went through unload-all-models, not the transport.
    assert len(llamolotl_calls) == 1
    assert by_id["self.llamolotl"].ok is True
    assert by_id["self.speak"].ok is True and by_id["self.sketch"].ok is True


def test_release_all_uses_sentinel_when_held_unknown(monkeypatch):
    transport = _FakeTransport()
    consumers = [_status("self.speak", held=0)]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: transport)
    _install_llamolotl_stub(monkeypatch, [])

    asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    speak = [c for c in transport.calls if c["consumer_id"] == "self.speak"][0]
    assert speak["amount_bytes"] == vl._ESTOP_RELEASE_SENTINEL_BYTES


def test_release_all_one_failure_does_not_abort_others(monkeypatch):
    class _Boom(_FakeTransport):
        async def request_release(self, consumer_id, amount_bytes, timeout_seconds, force=False):
            if consumer_id == "self.speak":
                raise RuntimeError("speak pod unreachable")
            return await super().request_release(consumer_id, amount_bytes, timeout_seconds, force)

    transport = _Boom()
    consumers = [_status("self.speak"), _status("self.sketch")]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: transport)
    _install_llamolotl_stub(monkeypatch, [])

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    by_id = {r.consumer_id: r for r in summary.results}
    assert by_id["self.speak"].ok is False
    assert "error" in by_id["self.speak"].detail
    # sketch still succeeded despite speak blowing up.
    assert by_id["self.sketch"].ok is True


def test_release_all_reports_transport_missing(monkeypatch):
    consumers = [_status("self.speak")]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: None)
    _install_llamolotl_stub(monkeypatch, [])

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))
    speak = {r.consumer_id: r for r in summary.results}["self.speak"]
    assert speak.ok is False


####################
# self.curator (self.ai#88)
####################


def _install_curator_jobs_stub(monkeypatch, running, requeued):
    """Stub selfai_ui.models.curator_jobs so the requeue leg runs without a DB.

    The real module imports the full model stack; the e-stop only needs
    ``get_jobs_by_status`` + ``requeue_for_next_window``."""
    mod = types.ModuleType("selfai_ui.models.curator_jobs")

    class _Jobs:
        @staticmethod
        def get_jobs_by_status(status):
            assert status == "running"
            return running

        @staticmethod
        def requeue_for_next_window(job_id, reason):
            requeued.append((job_id, reason))

    mod.CuratorJobs = _Jobs
    monkeypatch.setitem(sys.modules, "selfai_ui.models.curator_jobs", mod)


def test_curator_reason_survives_onto_the_result(monkeypatch):
    """self.curator is the one consumer that explains itself. An operator who
    just stopped the card needs to read WHAT was killed, not only that something
    was — the reason names the pipeline."""

    class _Explains:
        async def request_release(self, consumer_id, amount_bytes, timeout_seconds, force=False):
            return ReleaseResponse(
                outcome=ReleaseOutcome.CONFIRMED,
                new_held_bytes=0,
                reason="e-stop: terminated 2 running pipeline(s)",
            )

    consumers = [_status("self.curator", held=8 * 1024**3)]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: _Explains())
    _install_llamolotl_stub(monkeypatch, [])
    _install_curator_jobs_stub(monkeypatch, [], [])

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    curator = {r.consumer_id: r for r in summary.results}["self.curator"]
    assert "terminated 2" in curator.detail["reason"]


def test_a_consumer_with_nothing_to_say_omits_reason(monkeypatch):
    """Absent rather than null, so the admin UI can treat presence as
    'there is something worth showing'."""
    consumers = [_status("self.speak", held=1024**3)]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: _FakeTransport())
    _install_llamolotl_stub(monkeypatch, [])

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    speak = {r.consumer_id: r for r in summary.results}["self.speak"]
    assert "reason" not in speak.detail


def test_estopped_curation_runs_are_requeued_for_the_next_window(monkeypatch):
    """Curation work is restartable and often long, so clearing the card must
    not also make an admin rebuild every queued pipeline by hand."""
    running = [types.SimpleNamespace(id="job-a"), types.SimpleNamespace(id="job-b")]
    requeued = []
    consumers = [_status("self.curator", held=8 * 1024**3)]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: _FakeTransport())
    _install_llamolotl_stub(monkeypatch, [])
    _install_curator_jobs_stub(monkeypatch, running, requeued)

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    assert summary.curator_requeued == ["job-a", "job-b"]
    assert [j for j, _ in requeued] == ["job-a", "job-b"]
    assert all("requeued for the next curator window" in r for _, r in requeued)


def test_a_requeue_failure_never_fails_the_estop(monkeypatch):
    """The card being clear is the point. Turning a successful stop into a 500
    over bookkeeping would leave an operator believing the GPU is still held."""
    mod = types.ModuleType("selfai_ui.models.curator_jobs")

    class _Jobs:
        @staticmethod
        def get_jobs_by_status(status):
            return [types.SimpleNamespace(id="job-a")]

        @staticmethod
        def requeue_for_next_window(job_id, reason):
            raise RuntimeError("db is down")

    mod.CuratorJobs = _Jobs
    monkeypatch.setitem(sys.modules, "selfai_ui.models.curator_jobs", mod)

    consumers = [_status("self.curator", held=8 * 1024**3)]
    monkeypatch.setattr(vl.VramLeases, "capacity_summary", staticmethod(lambda: _FakeSummary(consumers)))
    monkeypatch.setattr(vl.VramBroker, "get_transport", lambda: _FakeTransport())
    _install_llamolotl_stub(monkeypatch, [])

    summary = asyncio.run(vl.release_all(request=_FakeRequest(), user=object()))

    assert {r.consumer_id: r for r in summary.results}["self.curator"].ok is True
    assert summary.curator_requeued == []
