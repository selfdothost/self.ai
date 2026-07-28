"""The reference mod's small in-process state store.

This is the substrate for R1's coherent state chain: one place the tool writes,
the namespace streams from, and the route reads back. It is a reference
implementation, not production infrastructure -- a single module-level instance
holding the last handle a tool invocation produced is exactly enough to prove
"tool writes state -> namespace streams it -> route reads it" as one unit.

The state is a `{task_id, status}` record -- the async-handle shape the tool
pins in R2 (the pattern self.crew#141 confirmed). Later tasks plug in without
redesigning this file:

  * T-003 (register_tools): the handler calls `record(task_id, status)` and
    returns the record.
  * T-004 (register_ws): the namespace streams `record` / `latest()` to a
    subscribed client via the facade's `emit_to_user`.
  * T-005 (register_routers): the route returns `snapshot()`, so a client that
    never subscribed can still observe the tool's state change.

No import from the application package lives here -- the store is pure stdlib,
so it stays trivially inside the facade boundary (R7).

Cavekit: cavekit-mods-reference-implementation.md R1 -- T-002
"""

from __future__ import annotations

import threading


class ReferenceState:
    """The last handle the tool produced, plus every handle seen this process.

    Deliberately tiny. `latest` is what the namespace streams and the route
    reads; `records` is kept so a later test can assert more than one write is
    observable. A lock guards the two writes because a namespace emit and an
    HTTP read can race a tool call across threads, and a torn read would be a
    confusing failure to debug in a reference artifact.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._records: dict[str, dict] = {}

    def record(self, task_id: str, status: str) -> dict:
        """Write one `{task_id, status}` handle and return it.

        Returns the stored record so a caller (the tool handler) can hand the
        very same value back to the model without re-deriving it.
        """
        entry = {"task_id": task_id, "status": status}
        with self._lock:
            self._latest = entry
            self._records[task_id] = entry
        return entry

    def latest(self) -> dict | None:
        """The most recently recorded handle, or None before any tool ran."""
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def snapshot(self) -> dict:
        """Everything a route needs to report the current state.

        A plain dict so a route can return it as a JSON body directly.
        """
        with self._lock:
            return {
                "latest": dict(self._latest) if self._latest is not None else None,
                "count": len(self._records),
            }

    def reset(self) -> None:
        """Clear all state. For test isolation and lifecycle shutdown."""
        with self._lock:
            self._latest = None
            self._records = {}


#: The single process-wide store. Imported by the entrypoint and, in later
#: tasks, written by the tool and read by the route and namespace.
STATE = ReferenceState()
