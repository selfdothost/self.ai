"""`stop_task()` success reporting (self.ai#98).

Every successful Stop used to answer `{"status": false, "message": "Failed to
stop task ..."}`. The cancel worked every time -- measured 4/4 against the live
instance, with the partial assistant message persisted and the task gone from
GET /api/tasks -- only the reply said otherwise.

The cause is a mismatch between how success was DETECTED and how the real
coroutine BEHAVES. `stop_task()` returned success only from an
`except CancelledError` branch, but the chat generation coroutine
(`post_response_handler()`) catches CancelledError itself to emit
`task-cancelled` and persist the partial message, then returns normally -- so
nothing propagated and the failure line was returned instead.

The test that matters most here is therefore
`test_task_that_swallows_cancellation_reports_success`: it models the coroutine
the app actually runs, rather than the textbook one. The pre-existing suite
(test_task_cancel_hooks.py) only ever used `_forever()`, which propagates -- so
it reported success and could not have caught this.

Conventions follow test_task_cancel_hooks.py: async bodies run via
`asyncio.run()` rather than `@pytest.mark.asyncio`, because pytest-asyncio is
not a dependency here and a bare asyncio marker silently SKIPS instead of
failing.
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
    """Propagates CancelledError -- the textbook coroutine."""
    await asyncio.sleep(3600)


async def _swallows_cancellation(record):
    """Models post_response_handler(): catches CancelledError, does its cleanup,
    and RETURNS NORMALLY. This is the shape that produced the bug."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        record.append("cleanup ran")
        # deliberately does not re-raise -- exactly like the real handler
        return "finished cleanly"


async def _fails_while_cancelling():
    """Ends by raising something other than CancelledError."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise RuntimeError("cleanup blew up")


class TestSuccessReporting:
    def test_task_that_swallows_cancellation_reports_success(self):
        """THE REGRESSION. Against the old code this returned
        `{"status": False, "message": "Failed to stop task ..."}` while having
        cancelled the task perfectly."""
        record = []

        async def run():
            task_id, _ = tasks_mod.create_task(_swallows_cancellation(record))
            await asyncio.sleep(0)  # let it reach the await
            return await tasks_mod.stop_task(task_id), task_id

        res, task_id = asyncio.run(run())

        assert record == ["cleanup ran"], "the coroutine's cleanup did not run"
        assert res["status"] is True
        assert "successfully stopped" in res["message"]
        assert "Failed" not in res["message"]

    def test_task_that_propagates_cancellation_still_reports_success(self):
        """The path that already worked, pinned so the fix does not trade one
        for the other."""

        async def run():
            task_id, _ = tasks_mod.create_task(_forever())
            await asyncio.sleep(0)
            return await tasks_mod.stop_task(task_id)

        res = asyncio.run(run())

        assert res["status"] is True

    def test_task_that_fails_while_cancelling_still_reports_stopped(self):
        """The user asked for it to stop and it stopped. How its cleanup ended
        is a log line, not a failure to report back to the caller."""

        async def run():
            task_id, _ = tasks_mod.create_task(_fails_while_cancelling())
            await asyncio.sleep(0)
            return await tasks_mod.stop_task(task_id)

        res = asyncio.run(run())

        assert res["status"] is True

    def test_unknown_task_still_raises(self):
        """Unchanged: the endpoint turns this into a 404, and that is the one
        genuine 'cannot stop it' case."""

        async def run():
            return await tasks_mod.stop_task("no-such-task")

        with pytest.raises(ValueError, match="not found"):
            asyncio.run(run())


class TestRegistryCleanup:
    def test_swallowing_task_is_removed_from_the_registry(self):
        """The old code only popped the task inside the CancelledError branch,
        so a swallowing task was reported as failed AND could be left behind on
        that path. Registry state is what GET /api/tasks reports."""

        async def run():
            task_id, _ = tasks_mod.create_task(_swallows_cancellation([]))
            await asyncio.sleep(0)
            await tasks_mod.stop_task(task_id)
            return task_id

        task_id = asyncio.run(run())

        assert task_id not in tasks_mod.tasks
        assert task_id not in tasks_mod.task_cancel_hooks


class TestCallerCancellationIsNotSwallowed:
    def test_cancelled_error_that_is_not_the_tasks_is_re_raised(self):
        """`except CancelledError: pass` is dangerous inside a coroutine that
        can itself be cancelled: if the CancelledError belongs to OUR caller
        rather than to the task, swallowing it strands that caller. The guard
        re-raises whenever the task did not itself end cancelled.

        Driven with a stub rather than a real uncancellable task ON PURPOSE. The
        obvious version of this test -- a coroutine that loops forever ignoring
        cancellation -- deadlocks `asyncio.run()` at shutdown, because the loop
        cancels leftover tasks and waits for them, and that task never dies. A
        hanging test in CI is worse than the coverage it buys."""

        class _RaisesCancelledButIsNotCancelled:
            """Awaiting raises CancelledError while `.cancelled()` stays False —
            i.e. the exception came from somewhere other than this task."""

            def cancel(self):
                return True

            def cancelled(self):
                return False

            def __await__(self):
                async def _raise():
                    raise asyncio.CancelledError()

                return _raise().__await__()

        async def run():
            task_id = "stub-task"
            tasks_mod.tasks[task_id] = _RaisesCancelledButIsNotCancelled()
            return await tasks_mod.stop_task(task_id)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())

    def test_cancelled_error_from_the_task_itself_is_absorbed(self):
        """The mirror case: `.cancelled()` is True, so the CancelledError is the
        task's own and must NOT propagate -- it is the normal success path."""

        class _ProperlyCancelled:
            def cancel(self):
                return True

            def cancelled(self):
                return True

            def __await__(self):
                async def _raise():
                    raise asyncio.CancelledError()

                return _raise().__await__()

        async def run():
            task_id = "stub-task"
            tasks_mod.tasks[task_id] = _ProperlyCancelled()
            return await tasks_mod.stop_task(task_id)

        res = asyncio.run(run())

        assert res["status"] is True
