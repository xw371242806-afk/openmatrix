"""Research orchestrator: the step loop that executes a plan.

This is the minimal self-built orchestration engine (LangGraph stays an
optional future swap). It walks the ordered plan, calls each tool through the
``ToolGateway``, applies bounded retry, and degrades gracefully when an optional
step fails. The loop is cooperative: it checks the ``InterruptToken`` between
steps so a long run can be paused or cancelled from another task/thread.

Red line: every step tool is validated against the allowlist before execution,
so even a hand-built plan can never invoke ``matrix_cells`` export tools.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from agent.planner import Plan, PlanStep, validate_plan_tools
from agent.state_machine import (
    ResearchRun,
    RunPhase,
    StepRecord,
    StepStatus,
    StateMachineError,
)
from agent.tools import ToolGateway, ToolResult, is_allowed_tool


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy applied to transient tool failures."""

    max_attempts: int = 3
    backoff_seconds: float = 0.5
    retry_network_only: bool = True
    # Per-call timeout in seconds. When set, an in-flight tool call that does
    # not finish within the budget is aborted and surfaced as a retryable
    # "timed out" error. None means no deadline (cancellation still wins).
    call_timeout_seconds: float | None = None


class InterruptToken:
    """Cooperative pause / cancel signal shared between controller and loop.

    ``cancel()`` sets a plain flag *and* wakes an ``asyncio.Event`` so that an
    in-flight tool call or retry backoff can observe cancellation promptly
    instead of waiting for the next cooperative step boundary.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._paused = asyncio.Event()
        self._paused.set()  # not paused by default
        self._cancelled_event = asyncio.Event()

    def cancel(self) -> None:
        self._cancelled = True
        self._paused.set()  # wake a paused loop so it can observe cancellation
        self._cancelled_event.set()  # wake an in-flight call / backoff

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    @property
    def cancelled_event(self) -> asyncio.Event:
        """Event set exactly once when ``cancel()`` is called."""
        return self._cancelled_event

    async def wait_if_paused(self) -> None:
        if self._cancelled:
            return
        while not self._paused.is_set():
            await asyncio.sleep(0.05)

    async def wait_backoff(self, seconds: float) -> bool:
        """Wait for a retry backoff, waking early on cancellation.

        Returns ``True`` when the run was cancelled during the wait, so the
        caller can stop retrying instead of sleeping past cancellation.
        """
        if seconds <= 0:
            return False
        try:
            await asyncio.wait_for(self._cancelled_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return self._cancelled


class ResearchOrchestrator:
    """Executes a research plan as an interruptible step loop."""

    def __init__(self, gateway: ToolGateway, retry_policy: RetryPolicy | None = None) -> None:
        self._gateway = gateway
        self._retry = retry_policy or RetryPolicy()

    async def run(
        self,
        research_id: str,
        objective: str,
        context: dict[str, Any] | None = None,
        plan: Plan | None = None,
        token: InterruptToken | None = None,
        run: ResearchRun | None = None,
    ) -> ResearchRun:
        run = run or ResearchRun(
            research_id=research_id,
            objective=objective,
            context=context or {},
        )
        if run.research_id != research_id:
            raise ValueError("provided run.research_id does not match research_id")
        token = token or InterruptToken()

        try:
            run.transition(RunPhase.PLANNING)
            plan = plan or self._plan_for(research_id, objective, run.context)
            if not plan.steps:
                run.error = "plan produced no steps"
                run.transition(RunPhase.FAILED)
                return run

            violations = validate_plan_tools(plan)
            if violations:
                run.error = f"red line: plan references disallowed tools: {violations}"
                run.transition(RunPhase.FAILED)
                return run

            run.plan = plan.steps
            run.transition(RunPhase.RUNNING)

            for step in plan.steps:
                await token.wait_if_paused()
                if token.cancelled:
                    run.transition(RunPhase.CANCELLED)
                    return run

                record = await self._execute_step(run, step, token)
                run.add_step(record)
                run.step_event(step.step_id, step.tool, record.status.value)

                if token.cancelled:
                    run.transition(RunPhase.CANCELLED)
                    return run

                if record.status == StepStatus.FAILED and step.required:
                    run.error = record.error or f"required step {step.step_id} failed"
                    run.transition(RunPhase.FAILED)
                    return run

            run.conclusion = self._build_conclusion(run, plan)
            run.transition(RunPhase.COMPLETED)
            return run
        except StateMachineError as exc:
            run.error = str(exc)
            if run.phase in (RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED):
                return run
            try:
                run.transition(RunPhase.FAILED)
            except StateMachineError:
                pass
            return run
        except asyncio.CancelledError:
            # External task.cancel(): record the terminal state, then re-raise so
            # the asyncio.Task actually observes the cancellation.
            run.error = "research run task cancelled externally"
            if run.phase not in (RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED):
                try:
                    run.transition(RunPhase.CANCELLED)
                except StateMachineError:
                    pass
            raise
        except Exception as exc:
            # Any unexpected planning / execution / conclusion error must land in
            # a terminal FAILED state (with a captured error) instead of escaping
            # the background task and leaving the run stuck in planning/running.
            run.error = f"run failed: {exc}"
            if run.phase not in (RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED):
                try:
                    run.transition(RunPhase.FAILED)
                except StateMachineError:
                    pass
            return run

    def _plan_for(self, research_id: str, objective: str, context: dict[str, Any]) -> Plan:
        from agent.planner import RuleBasedPlanner

        return RuleBasedPlanner().plan(research_id, objective, context)

    async def _execute_step(
        self, run: ResearchRun, step: PlanStep, token: InterruptToken
    ) -> StepRecord:
        arguments = self._resolve_bindings(run, step)
        record = StepRecord(
            step_id=step.step_id,
            tool=step.tool,
            arguments=arguments,
            status=StepStatus.RUNNING,
        )

        if not is_allowed_tool(step.tool):
            record.status = StepStatus.FAILED
            record.error = f"tool {step.tool!r} is outside the agent allowlist (red line)"
            return record

        # The effective attempt budget is bounded by both the step-level default
        # and the RetryPolicy so a strict policy can never be exceeded.
        max_attempts = max(1, min(step.max_attempts, self._retry.max_attempts))
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            record.attempts = attempt
            result = await self._call_with_interrupt(step, token, arguments)
            if result is None:  # cancellation won the race against the call
                record.status = StepStatus.FAILED
                record.error = "cancelled"
                return record

            if result.ok:
                record.output = result.text
                record.status = StepStatus.SUCCEEDED
                return record

            last_error = result.error or "tool returned an error"
            run.step_event(
                step.step_id,
                step.tool,
                f"attempt {attempt}/{max_attempts} failed: {last_error}",
            )
            # Permanent (non-retryable) failures must stop immediately instead of
            # being hammered repeatedly.
            if not self._should_retry(last_error):
                break
            if attempt < max_attempts:
                if await token.wait_backoff(self._retry.backoff_seconds):
                    record.status = StepStatus.FAILED
                    record.error = "cancelled during retry backoff"
                    return record

        record.status = StepStatus.FAILED
        record.error = last_error
        return record

    @staticmethod
    def _resolve_bindings(run: ResearchRun, step: PlanStep) -> dict[str, Any]:
        """Resolve step-output bindings into concrete arguments.

        For each ``input_bindings[arg]``, look up the prior step's SUCCEEDED
        output, parse it as JSON, and either read a dotted path (``field``) or
        substitute dotted-path placeholders ``{filings.0.accession}`` in a
        ``template``. Missing upstream steps / fields resolve to ``None`` (the
        tool then reports its own error). Upstream tools must be inside the
        agent allowlist (red line).
        """
        import json as _json

        from agent.tools import is_allowed_tool

        arguments = dict(step.arguments)
        for arg_name, binding in (step.input_bindings or {}).items():
            upstream = run.steps.get(binding.get("step", ""))
            if upstream is None or upstream.status != StepStatus.SUCCEEDED or not upstream.output:
                arguments[arg_name] = None
                continue
            if not is_allowed_tool(upstream.tool):
                arguments[arg_name] = None
                continue
            try:
                payload = _json.loads(upstream.output)
            except Exception:
                arguments[arg_name] = None
                continue
            field = binding.get("field")
            if field is not None:
                arguments[arg_name] = _dig(payload, field)
                continue
            template = binding.get("template")
            if template is not None:
                try:
                    arguments[arg_name] = _template_format(template, payload)
                except Exception:
                    arguments[arg_name] = None
        return arguments

    async     def _call_with_interrupt(
        self, step: PlanStep, token: InterruptToken, arguments: dict[str, Any] | None = None
    ) -> ToolResult | None:
        """Invoke a tool, racing the call against cancellation / timeout.

        Returns ``None`` when cancellation wins so the step loop moves the run
        straight to CANCELLED. Tool-level failures and timeouts come back as a
        ``ToolResult`` so the retry policy can decide whether to retry.
        """
        if token.cancelled:
            return None

        call_task = asyncio.create_task(
            self._gateway.call_tool(step.tool, dict(arguments if arguments is not None else step.arguments))
        )
        cancel_task = asyncio.create_task(token.cancelled_event.wait())

        try:
            done, _pending = await asyncio.wait(
                {call_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=self._retry.call_timeout_seconds,
            )
        except BaseException:
            # Cancellation of the outer task (or any other interrupt) must still
            # reap the sub-tasks so nothing leaks a pending task.
            for task in (call_task, cancel_task):
                task.cancel()
            await asyncio.gather(call_task, cancel_task, return_exceptions=True)
            raise
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass

        if call_task in done:
            try:
                return call_task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return ToolResult(text="", error=f"tool call failed: {exc}")

        # Cancellation won the race against a still-running tool call.
        if token.cancelled:
            call_task.cancel()
            try:
                await call_task
            except asyncio.CancelledError:
                pass
            return None

        # The per-call timeout expired before the tool returned.
        call_task.cancel()
        try:
            await call_task
        except asyncio.CancelledError:
            pass
        return ToolResult(text="", error="tool call timed out")

    def _should_retry(self, error: str) -> bool:
        """Decide whether a failure is worth retrying.

        By default only network/transport-style errors are retried; a tool-level
        error like ``Error: matrix run ... not found`` is permanent. Setting
        ``retry_network_only=False`` retries any failure (useful for flaky
        upstream data sources).
        """
        if not self._retry.retry_network_only:
            return True
        lowered = error.lower()
        return any(marker in lowered for marker in ("timed out", "timeout", "connection", "url error", "rate limit", "429", "502", "503"))

    @staticmethod
    def _build_conclusion(run: ResearchRun, plan: Plan) -> dict[str, Any]:
        """Assemble a structured conclusion from the succeeded steps' outputs."""
        outputs: dict[str, str] = {}
        for record in run.steps.values():
            if record.status == StepStatus.SUCCEEDED and record.output:
                outputs[record.step_id] = record.output
        return {
            "research_id": run.research_id,
            "objective": run.objective,
            "steps": [
                {
                    "step_id": s.step_id,
                    "tool": s.tool,
                    "status": s.status.value,
                    "description": next(
                        (p.description for p in plan.steps if p.step_id == s.step_id),
                        "",
                    ),
                }
                for s in run.steps.values()
            ],
            "outputs": outputs,
            "evidence": _evidence_notes(outputs),
        }


def _evidence_notes(outputs: dict[str, str]) -> list[str]:
    """Short human-readable notes summarizing the collected evidence."""
    notes = []
    for step_id, text in outputs.items():
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if "chunks" in data:
                    notes.append(f"{step_id}: {len(data.get('chunks') or [])} chunks selected")
                elif "summary" in data and data["summary"]:
                    notes.append(f"{step_id}: summary present")
                elif "citation_status" in data:
                    notes.append(f"{step_id}: citation_status={data['citation_status']}")
        except (json.JSONDecodeError, TypeError):
            pass
    if not notes:
        notes.append("no structured evidence parsed; raw tool outputs captured")
    return notes


def _dig(payload: Any, path: str) -> Any:
    """Read a dotted JSON path (e.g. ``filings.0.filing_url``) from a payload.

    List indices are decimal integers; missing keys/indices return None.
    """
    if payload is None:
        return None
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _template_format(template: str, payload: Any) -> str:
    """Substitute dotted-path placeholders like ``{filings.0.accession}``.

    Any placeholder whose path is missing resolves the whole template to
    ``None`` (returned by the caller).
    """
    import re as _re

    def _repl(match: _re.Match) -> str:
        value = _dig(payload, match.group(1))
        if value is None:
            raise KeyError(match.group(1))
        return str(value)

    return _re.sub(r"\{([^{}]+)\}", _repl, template)
