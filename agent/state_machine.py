"""Research-run state machine and event model.

A research run is a finite state machine with well-defined transitions. The
state machine is small, deterministic, and dependency-free so it can be unit
tested in isolation and swapped underneath a LangGraph ``StateGraph`` later if
the optional dependency is ever adopted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunPhase(str, Enum):
    """Top-level lifecycle phase of a research run."""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Allowed transitions. A run may move between the active phases; terminal
# phases (completed / failed / cancelled) are final.
_VALID_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.PENDING: frozenset({RunPhase.PLANNING, RunPhase.CANCELLED}),
    RunPhase.PLANNING: frozenset({RunPhase.RUNNING, RunPhase.FAILED, RunPhase.CANCELLED}),
    RunPhase.RUNNING: frozenset(
        {RunPhase.PAUSED, RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED}
    ),
    RunPhase.PAUSED: frozenset({RunPhase.RUNNING, RunPhase.CANCELLED}),
    RunPhase.COMPLETED: frozenset(),
    RunPhase.FAILED: frozenset(),
    RunPhase.CANCELLED: frozenset(),
}


class StateMachineError(Exception):
    """Raised when an illegal state transition is attempted."""


class StepStatus(str, Enum):
    """Per-step execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class RunEvent:
    """One observable event emitted during a run."""

    phase: str
    step_id: str | None = None
    tool: str | None = None
    message: str = ""


@dataclass
class StepRecord:
    """Record of a single plan step execution (retries included)."""

    step_id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    error: str | None = None
    output: str | dict[str, Any] | None = None


def _output_text(output: object) -> str:
    import json
    if output is None:
        return ''
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)


def _step_evidence(output: object) -> list[dict[str, object]]:
    import json
    if not output:
        return []
    value = output
    if isinstance(output, str):
        try:
            value = json.loads(output)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, dict):
        return []
    items = next((value[key] for key in ('evidence', 'source_chunks', 'chunks', 'hits') if isinstance(value.get(key), list)), [])
    if not isinstance(items, list):
        return []
    keys = ('run_id', 'page', 'document_index', 'cell_id', 'document_name', 'quote', 'source_url', 'chunk_id', 'evidence_id')
    return [{key: item[key] for key in keys if key in item} for item in items[:20] if isinstance(item, dict)]


@dataclass
class ResearchRun:
    """Mutable state of a research run.

    ``events`` grows monotonically and carries the audit trail; ``steps`` are
    keyed by step id so downstream UI / SSE consumers can render progress.
    ``owner`` carries the tenant/user id (P7-2) for isolation.
    """

    research_id: str
    objective: str
    context: dict[str, Any] = field(default_factory=dict)
    owner: str | None = None
    phase: RunPhase = RunPhase.PENDING
    plan: list[Any] = field(default_factory=list)
    steps: dict[str, StepRecord] = field(default_factory=dict)
    events: list[RunEvent] = field(default_factory=list)
    error: str | None = None
    conclusion: Any = None
    created_at: str = ""

    def transition(self, target: RunPhase) -> None:
        """Validate and apply a state transition, recording an event."""
        allowed = _VALID_TRANSITIONS[self.phase]
        if target not in allowed:
            raise StateMachineError(
                f"illegal transition {self.phase.value!r} -> {target.value!r}"
            )
        self.phase = target
        self.events.append(RunEvent(phase=target.value, message=f"phase {target.value}"))

    def step_event(self, step_id: str, tool: str, message: str) -> None:
        self.events.append(
            RunEvent(phase=self.phase.value, step_id=step_id, tool=tool, message=message)
        )

    def add_step(self, record: StepRecord) -> None:
        self.steps[record.step_id] = record

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-serializable snapshot of the run."""
        return {
            "research_id": self.research_id,
            "objective": self.objective,
            "owner": self.owner,
            "phase": self.phase.value,
            "error": self.error,
            "conclusion": self.conclusion,
            "steps": [
                {
                    "step_id": s.step_id,
                    "tool": s.tool,
                    "status": s.status.value,
                    "attempts": s.attempts,
                    "error": s.error,
                    "output_preview": _output_text(s.output)[:4000] or None,
                    "evidence": _step_evidence(s.output),
                }
                for s in self.steps.values()
            ],
        }
