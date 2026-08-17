"""Cancel-hook behaviour on task stop (self.ai#39).

Cancelling the asyncio task only closes self.ai's socket to the backend. For
llama.cpp that is a connection-drop cancel: the slot keeps generating until the
server's own polling notices, which is what produced the 252-tokens-after-the-
client-left incident on #39. The hook is how a caller sends an explicit release
first.

These cover the contract the streaming path depends on, with no network:
the hook runs *before* cancellation, and it can never prevent the stop.

Async bodies run via `asyncio.run()` rather than `@pytest.mark.asyncio` —
pytest-asyncio is not a dependency here, and a bare asyncio marker would
silently skip instead of fail (same reasoning as test_anthropic_conversion.py).
"""

import asyncio

import pytest

from selfai_ui import tasks as tasks_mod


@pytest.fixture(autouse=True)
def _clean_registries():
    tasks_mod.tasks.clear()
    tasks_mod.task_cancel_hooks.clear()
    yield
    tasks_mod.tasks.clear()
    tasks_mod.task_cancel_hooks.clear()


async def _forever():
    await asyncio.sleep(3600)


def test_hook_runs_before_task_is_cancelled():
    """Ordering is the whole point: the backend must be told while our
    connection is still open, so it can end the stream cleanly rather than
    having it torn out from under it."""
    order = []

    async def run():
        task_id, task = tasks_mod.create_task(_forever())

        async def hook():
            order.append("hook")
            assert not task.cancelled(), "hook ran after the task was already cancelled"

        tasks_mod.register_cancel_hook(task_id, hook)

        res = await tasks_mod.stop_task(task_id)
        order.append("stopped")
        return res

    res = asyncio.run(run())

    assert res["status"] is True
    assert order == ["hook", "stopped"]


def test_stop_still_works_when_no_hook_registered():
    """External gateways register no hook. They must keep the original
    drop-the-connection behaviour, unchanged."""

    async def run():
        task_id, _ = tasks_mod.create_task(_forever())
        return await tasks_mod.stop_task(task_id)

    assert asyncio.run(run())["status"] is True


def test_failing_hook_does_not_block_the_stop():
    """A backend that is down, or that does not support explicit cancel, must
    never stop the user's Stop from working."""

    async def run():
        task_id, _ = tasks_mod.create_task(_forever())

        async def boom():
            raise RuntimeError("llamolotl unreachable")

        tasks_mod.register_cancel_hook(task_id, boom)
        return await tasks_mod.stop_task(task_id)

    assert asyncio.run(run())["status"] is True


def test_hanging_hook_is_bounded_and_still_stops(monkeypatch):
    """A hook that never returns must not hold the request open. Its only value
    is freeing the slot sooner than polling would; past that it is pure delay."""
    monkeypatch.setattr(tasks_mod, "CANCEL_HOOK_TIMEOUT_SECONDS", 0.05)

    async def run():
        task_id, _ = tasks_mod.create_task(_forever())

        async def hang():
            await asyncio.sleep(3600)

        tasks_mod.register_cancel_hook(task_id, hang)

        loop = asyncio.get_running_loop()
        started = loop.time()
        res = await tasks_mod.stop_task(task_id)
        return res, loop.time() - started

    res, elapsed = asyncio.run(run())

    assert res["status"] is True
    assert elapsed < 1.0, f"stop took {elapsed:.2f}s — the hook timeout did not bound it"


def test_hook_is_dropped_when_the_task_completes_on_its_own():
    """The common case is a generation that finishes normally. Its hook must not
    linger in the registry — these are per-request and would otherwise grow
    without bound."""

    async def run():
        async def quick():
            return "done"

        async def noop():
            return None

        task_id, task = tasks_mod.create_task(quick())
        tasks_mod.register_cancel_hook(task_id, noop)

        await task
        await asyncio.sleep(0)  # let the done-callback run
        return task_id

    task_id = asyncio.run(run())

    assert task_id not in tasks_mod.task_cancel_hooks
    assert task_id not in tasks_mod.tasks
