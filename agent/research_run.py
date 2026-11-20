"""Interruptible research-run execution mode.

``research_run(research_id, objective, context)`` is the server-side entrypoint
that runs the agent orchestration as an observable, interruptible task. The
caller gets a ``ResearchRunController`` handle and can poll ``status()``,
``pause()``, ``resume()``, ``cancel()`` and collect the final ``result()``
without blocking on a long tool chain. This mirrors the MCP Tasks API pattern
(``tasks/get`` / ``cancel`` / ``result``) at the in-process level.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.orchestrator import InterruptToken, ResearchOrchestrator, RetryPolicy
from agent.state_machine import ResearchRun, RunPhase
from agent.tools import ToolGateway, default_gateway


class ResearchRunController:
    """Handle to an interruptible research-run task.

    ``start()`` must be called inside a running event loop (e.g. within an async
    FastAPI route or ``asyncio.run``). After start, ``status`` reflects the live
    ``ResearchRun``; ``cancel``/``pause``/``resume`` steer the cooperative loop.
    """

    def __init__(
        self,
        research_id: str,
        objective: str,
        context: dict[str, Any] | None = None,
        owner: str | None = None,
        gateway: ToolGateway | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.research_id = research_id
        self.objective = objective
        self.context = context or {}
        self.owner = owner
        self._gateway = gateway or default_gateway()
        self._retry = retry_policy or RetryPolicy()
        self._token = InterruptToken()
        self._orchestrator = ResearchOrchestrator(self._gateway, self._retry)
        self._task: asyncio.Task | None = None
        self._run: ResearchRun | None = None

    def start(self) -> "ResearchRunController":
        """Launch the run in a background task. Idempotent."""
        if self._task is None or self._task.done():
            self._run = ResearchRun(
                research_id=self.research_id,
                objective=self.objective,
                context=self.context,
                owner=self.owner,
            )
            self._task = asyncio.create_task(self._drive())
        return self

    async def _drive(self) -> ResearchRun:
        assert self._run is not None
        await self._orchestrator.run(
            self.research_id,
            self.objective,
            self.context,
            token=self._token,
            run=self._run,
        )
        return self._run

    def status(self) -> dict[str, Any]:
        """JSON-serializable snapshot of the current run state."""
        if self._run is not None:
            return self._run.summary()
        return {
            "research_id": self.research_id,
            "objective": self.objective,
            "owner": self.owner,
            "phase": "pending",
            "error": None,
            "steps": [],
        }

    def pause(self) -> bool:
        """Pause the run: request a cooperative stop and transition PAUSED now.

        The phase immediately becomes ``PAUSED`` when the run is in RUNNING
        (observable via ``status()``); the loop halts at the next step boundary
        (``token.wait_if_paused``). A request while PENDING/PLANNING only sets
        the token; the loop transitions to RUNNING and then stops at the first
        boundary.
        """
        if self._terminal():
            return False
        self._token.pause()
        if self._run is not None and self._run.phase == RunPhase.RUNNING:
            self._run.transition(RunPhase.PAUSED)
        return True

    def resume(self) -> bool:
        if not self._token.paused:
            return False
        self._token.resume()
        if self._run is not None and self._run.phase == RunPhase.PAUSED:
            self._run.transition(RunPhase.RUNNING)
        return True

    def cancel(self) -> bool:
        """Request cancellation; the loop exits at the next boundary."""
        if self._terminal():
            return False
        self._token.cancel()
        return True

    def _terminal(self) -> bool:
        return self._run is not None and self._run.phase in (
            RunPhase.COMPLETED,
            RunPhase.FAILED,
            RunPhase.CANCELLED,
        )

    def is_terminal(self) -> bool:
        """True when the run has reached a terminal phase (or not yet started)."""
        return self._terminal()

    async def wait(self) -> ResearchRun:
        """Await completion/cancellation and return the final run."""
        if self._task is None:
            self.start()
        assert self._task is not None
        return await self._task

    async def result(self) -> ResearchRun | None:
        """Return the final run once finished, otherwise None."""
        if self._task is None:
            return None
        if self._task.done():
            return self._run
        return None


class ResearchRunManager:
    """In-process registry of interruptible research runs keyed by research_id.

    Reuses the existing ``TaskQueueAdapter``-style naming (enqueue/status/cancel/
    result) so the agent surface is consistent with the P2-4 task queue.
    """

    def __init__(
        self,
        gateway: ToolGateway | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._retry = retry_policy or RetryPolicy()
        self._runs: dict[str, ResearchRunController] = {}

    def start(
        self,
        research_id: str,
        objective: str,
        context: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> ResearchRunController:
        # Re-entrant safety: a live controller for the same id is returned as-is
        # so a duplicate start can never orphan the background task. Only a
        # finished run is replaced by a brand-new controller.
        existing = self._runs.get(research_id)
        if existing is not None and not existing.is_terminal():
            if owner is not None and existing.owner != owner:
                raise ValueError(f"research run {research_id!r} belongs to another owner")
            return existing
        controller = ResearchRunController(
            research_id=research_id,
            objective=objective,
            context=context,
            owner=owner,
            gateway=self._gateway,
            retry_policy=self._retry,
        )
        self._runs[research_id] = controller
        return controller.start()

    def get(self, research_id: str, owner: str | None = None) -> ResearchRunController | None:
        controller = self._runs.get(research_id)
        if controller is None:
            return None
        if owner is not None and controller.owner != owner:
            return None
        return controller

    def status(self, research_id: str, owner: str | None = None) -> dict[str, Any] | None:
        controller = self.get(research_id, owner=owner)
        return controller.status() if controller else None

    def cancel(self, research_id: str, owner: str | None = None) -> bool:
        controller = self.get(research_id, owner=owner)
        return controller.cancel() if controller else False

    def pause(self, research_id: str, owner: str | None = None) -> bool:
        controller = self.get(research_id, owner=owner)
        return controller.pause() if controller else False

    def resume(self, research_id: str, owner: str | None = None) -> bool:
        controller = self.get(research_id, owner=owner)
        return controller.resume() if controller else False

    async def result(self, research_id: str, owner: str | None = None) -> ResearchRun | None:
        controller = self.get(research_id, owner=owner)
        if controller is None:
            return None
        await controller.wait()
        return await controller.result()

    def list(self, owner: str | None = None) -> list[str]:
        if owner is None:
            return list(self._runs)
        return [
            rid
            for rid, controller in self._runs.items()
            if controller.owner == owner
        ]


_default_manager: ResearchRunManager | None = None


def research_run(
    research_id: str,
    objective: str,
    context: dict[str, Any] | None = None,
    *,
    gateway: ToolGateway | None = None,
    retry_policy: RetryPolicy | None = None,
) -> ResearchRunController:
    """Start an interruptible research run and return its controller.

    Registers the run with the module-level manager so ``status``/``cancel`` /
    ``pause`` / ``resume`` can be addressed by ``research_id`` from anywhere in
    the process.
    """
    global _default_manager
    if _default_manager is None:
        _default_manager = ResearchRunManager(gateway=gateway, retry_policy=retry_policy)
    else:
        if gateway is not None:
            _default_manager._gateway = gateway  # noqa: SLF001
        if retry_policy is not None:
            _default_manager._retry = retry_policy  # noqa: SLF001
    # Route through the manager so duplicate live ids are never orphaned and the
    # registry stays the single source of truth for status/cancel/result.
    return _default_manager.start(research_id, objective, context)
