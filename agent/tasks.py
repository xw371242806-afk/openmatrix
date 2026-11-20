"""P4-3 generic in-process task execution for long operations.

Wraps arbitrary async callables as observable, interruptible tasks so MCP
tools can enqueue long work (document indexing, batch parsing, rendering) and
the caller polls status/result instead of blocking. Pure standard library.

Design:

- ``TaskManager.submit(operation, payload, fn)`` enqueues ``fn(payload)`` as an
  asyncio task and returns a task_id.
- ``status(task_id)`` -> JSON snapshot {task_id, operation, status, created_at,
  started_at, finished_at, error}.
- ``result(task_id)`` -> final payload or ``None`` while pending.
- ``cancel(task_id)`` -> True when the task was live and cancelled.
- A task is cancelled by cancelling its asyncio.Task; the wrapped callable
  should be cancellation-aware (it runs on the manager's event loop).

The manager owns one event loop in a dedicated thread so MCP handlers
(which may be sync or async) can submit without blocking.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Awaitable, Callable

TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class TaskManager:
    """In-process registry + executor for long-running async operations."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._started = threading.Event()

    # ---- lifecycle ----

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                self._loop = loop
                self._thread = threading.Thread(
                    target=self._run_loop,
                    args=(loop,),
                    name="task-manager-loop",
                    daemon=True,
                )
                self._thread.start()
                self._started.wait(timeout=5)
            assert self._loop is not None
            return self._loop

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        self._started.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    # ---- submit / query ----

    def submit(
        self,
        operation: str,
        payload: dict[str, Any] | None,
        fn: Callable[[dict[str, Any] | None], Awaitable[Any]],
    ) -> str:
        task_id = "t-" + uuid.uuid4().hex[:12]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        record = {
            "task_id": task_id,
            "operation": operation,
            "status": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        with self._lock:
            self._tasks[task_id] = record
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._run(task_id, payload, fn), loop)
        with self._lock:
            record["_future"] = future
        return task_id

    async def _run(
        self,
        task_id: str,
        payload: dict[str, Any] | None,
        fn: Callable[[dict[str, Any] | None], Awaitable[Any]],
    ) -> None:
        with self._lock:
            record = self._tasks[task_id]
            if record["status"] == "cancelled":
                return
            record["status"] = "running"
            record["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            result = await fn(payload)
            with self._lock:
                current = self._tasks[task_id]
                # A cancellation may have landed while the callable ran.
                if current["status"] != "cancelled":
                    current["status"] = "succeeded"
                    current["result"] = result
                    current["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        except asyncio.CancelledError:
            with self._lock:
                current = self._tasks[task_id]
                current["status"] = "cancelled"
                current["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception as exc:  # noqa: BLE001 - surface as failed
            with self._lock:
                current = self._tasks[task_id]
                if current["status"] != "cancelled":
                    current["status"] = "failed"
                    current["error"] = str(exc)
                    current["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    def status(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            return {k: v for k, v in record.items() if k not in ("result", "_future")}

    def result(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            out = {k: v for k, v in record.items() if k not in ("result", "_future")}
            out["result"] = record.get("result")
            return out

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued/running task (cancels actual execution).

        Returns True when the task was live and cancellation was requested.
        The underlying coroutine receives ``asyncio.CancelledError``; state is
        set to ``cancelled`` and later completion cannot overwrite it.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return False
            if record["status"] not in ("queued", "running"):
                return False
            record["status"] = "cancelled"
            record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            future = record.get("_future")
        if future is not None:
            # thread-safe: schedule the cancel on the manager's event loop
            future.cancel()
        return True

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for tid in self._tasks:
                record = self._tasks[tid]
                out.append({k: v for k, v in record.items() if k not in ("result", "_future")})
            return out

    def shutdown(self) -> None:
        """Stop the background loop and wait for the thread to exit."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)


_default_manager: TaskManager | None = None
_default_manager_lock = threading.Lock()


def default_task_manager() -> TaskManager:
    """Return the module-level default TaskManager (created lazily).

    Initialization is guarded by a module-level lock so concurrent callers
    always share one manager (and one background loop thread).
    """
    global _default_manager
    if _default_manager is None:
        with _default_manager_lock:
            if _default_manager is None:
                _default_manager = TaskManager()
    return _default_manager
