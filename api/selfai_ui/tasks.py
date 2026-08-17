# tasks.py
import asyncio
import logging
from typing import Awaitable, Callable, Dict
from uuid import uuid4

log = logging.getLogger(__name__)

# A dictionary to keep track of active tasks
tasks: Dict[str, asyncio.Task] = {}

# Optional per-task "tell the upstream to stop too" callbacks (self.ai#39).
#
# Cancelling the asyncio task only drops OUR connection to the backend. For
# llama.cpp that is a *connection-drop* cancel: the slot keeps generating until
# the server's own polling notices the dead socket, so tokens are still produced
# for a request nobody is reading (the incident on #39 measured 252 of them, and
# a ~3x skew on an unrelated benchmark from the resulting contention).
#
# A hook lets the layer that actually knows how to reach the backend register an
# explicit release, without teaching this module anything about llamolotl.
task_cancel_hooks: Dict[str, Callable[[], Awaitable[None]]] = {}

# Upper bound on a hook. The point of the hook is to free the GPU slot *sooner*
# than the backend's own polling would; if it cannot do that quickly it has no
# value left, and it must never delay the cancellation the user asked for.
CANCEL_HOOK_TIMEOUT_SECONDS = 5.0


def cleanup_task(task_id: str):
    """
    Remove a completed or canceled task from the global `tasks` dictionary.
    """
    tasks.pop(task_id, None)  # Remove the task if it exists
    task_cancel_hooks.pop(task_id, None)


def create_task(coroutine):
    """
    Create a new asyncio task and add it to the global task dictionary.
    """
    task_id = str(uuid4())  # Generate a unique ID for the task
    task = asyncio.create_task(coroutine)  # Create the task

    # Add a done callback for cleanup
    task.add_done_callback(lambda t: cleanup_task(task_id))

    tasks[task_id] = task
    return task_id, task


def register_cancel_hook(task_id: str, hook: Callable[[], Awaitable[None]]):
    """
    Register a coroutine to run when `task_id` is stopped, before the asyncio
    task itself is cancelled.

    Used to send an explicit stop to whatever backend is generating, so the
    resource is released on a push rather than on the backend noticing our
    dropped connection. Registration is optional: a task with no hook keeps the
    original drop-the-connection behaviour exactly.

    The hook is best-effort. It is awaited with a timeout and every exception is
    swallowed -- a backend that is unreachable, slow, or does not support an
    explicit cancel must never stop the user's Stop from working.
    """
    task_cancel_hooks[task_id] = hook


async def _run_cancel_hook(task_id: str):
    hook = task_cancel_hooks.pop(task_id, None)
    if hook is None:
        return

    try:
        await asyncio.wait_for(hook(), timeout=CANCEL_HOOK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning(
            f"cancel hook for task {task_id} timed out after "
            f"{CANCEL_HOOK_TIMEOUT_SECONDS}s; falling back to connection-drop cancel"
        )
    except Exception as e:
        log.warning(
            f"cancel hook for task {task_id} failed ({type(e).__name__}: {e}); "
            "falling back to connection-drop cancel"
        )


def get_task(task_id: str):
    """
    Retrieve a task by its task ID.
    """
    return tasks.get(task_id)


def list_tasks():
    """
    List all currently active task IDs.
    """
    return list(tasks.keys())


async def stop_task(task_id: str):
    """
    Cancel a running task and remove it from the global task list.
    """
    task = tasks.get(task_id)
    if not task:
        raise ValueError(f"Task with ID {task_id} not found.")

    # Tell the backend first, while our connection is still open. On llama.cpp
    # the CANCEL task handler sends a final response before releasing the slot,
    # so the stream we are about to abandon terminates cleanly rather than being
    # torn out from under the server.
    await _run_cancel_hook(task_id)

    task.cancel()  # Request task cancellation

    # Success is decided by observed state, NOT by whether CancelledError
    # propagated out of the coroutine (self.ai#98).
    #
    # The old code returned success only from an `except CancelledError` branch.
    # But the chat generation coroutine -- post_response_handler() -- catches
    # CancelledError ITSELF to emit `task-cancelled` and persist the partial
    # message, then returns normally. So `await task` completed without raising,
    # the success branch was never reached, and EVERY successful stop answered
    # `{"status": false, "message": "Failed to stop task ..."}` while having
    # cancelled the task perfectly. A task that cancels correctly was
    # indistinguishable here from one that ignored the cancel.
    #
    # Fixing it on the other side -- re-raising from post_response_handler --
    # was considered and rejected: the line immediately after that handler's
    # except block runs `await response.background()`, which re-raising would
    # skip. The swallow is load-bearing there.
    #
    # Once `await task` returns or raises, the task is finished by definition;
    # that is what awaiting it means. There is no longer a reachable "failed to
    # stop" state to report, so the failure return is gone rather than left as
    # dead code.
    try:
        await task  # Wait for the task to handle the cancellation
    except asyncio.CancelledError:
        # Either the task propagated the cancellation (the textbook path), or
        # OUR OWN caller was cancelled while we waited. Those must not be
        # conflated: if the task did not itself end cancelled, this exception
        # belongs to the caller and swallowing it would break its cancellation.
        if not task.cancelled():
            raise
    except Exception as e:
        # The coroutine ended by failing rather than by cancelling. The stop
        # still did what the user asked -- the task is over and gone -- so this
        # is a log line, not a failure to report back.
        log.warning(
            f"task {task_id} raised {type(e).__name__} while being cancelled "
            f"({e}); it is stopped either way"
        )

    tasks.pop(task_id, None)  # Remove it from the dictionary
    return {"status": True, "message": f"Task {task_id} successfully stopped."}
