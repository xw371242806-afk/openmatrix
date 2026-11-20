import asyncio
import json
import unittest
from dataclasses import dataclass

import agent
from agent.orchestrator import InterruptToken, ResearchOrchestrator, RetryPolicy
from agent.planner import Plan, PlanStep, RuleBasedPlanner, validate_plan_tools
from agent.research_run import ResearchRunController, ResearchRunManager
from agent.state_machine import RunPhase, StepStatus
from agent.tools import (
    AGENT_TOOL_ALLOWLIST,
    MATRIX_CELL_TOOLS,
    McpToolGateway,
    ToolResult,
    ToolSpec,
    is_allowed_tool,
)


def run_coro(coro):
    return asyncio.run(coro)


@dataclass
class MockToolGateway:
    """In-memory ToolGateway test double. No external API is ever touched."""

    responses: dict[str, str | list[str]] = None
    errors: dict[str, str | list[str]] = None
    calls: list[tuple[str, dict]] = None

    def __post_init__(self):
        self.responses = self.responses or {}
        self.errors = self.errors or {}
        self.calls = self.calls or []

    async def list_tools(self):
        return [ToolSpec(name=name) for name in sorted(AGENT_TOOL_ALLOWLIST)]

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        error = self.errors.get(name)
        response = self.responses.get(name, "{}")
        if error is not None:
            if isinstance(error, list):
                first = error.pop(0)
                return ToolResult(text="", error=first) if error else ToolResult(text="{}", error=first)
            return ToolResult(text="", error=error)
        if isinstance(response, list):
            return ToolResult(text=response.pop(0))
        return ToolResult(text=response)


class PlannerTemplateTests(unittest.TestCase):
    def test_pages_template_orders_retrieve_summarize_verify(self):
        plan = RuleBasedPlanner().plan(
            "r1",
            "What is revenue?",
            {"pages": ["pg1 text"], "question": "What is revenue?", "quote": "revenue was 1m"},
        )
        self.assertEqual([s.tool for s in plan.steps], ["retrieve_context", "summarize_context", "verify_quote"])

    def test_documents_template_uses_analyze_documents(self):
        plan = RuleBasedPlanner().plan("r2", "q", {"documents": [{"name": "a", "pages": ["x"]}], "question": "q"})
        self.assertEqual([s.tool for s in plan.steps], ["analyze_documents"])

    def test_url_template_uses_fetch_context(self):
        plan = RuleBasedPlanner().plan("r3", "q", {"url": "https://example.com", "question": "q"})
        self.assertEqual([s.tool for s in plan.steps], ["fetch_context"])

    def test_sec_template_uses_sec_tools(self):
        plan = RuleBasedPlanner().plan("r4", "q", {"ticker": "AAPL", "fact_names": ["Revenues"]})
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["sec_search", "sec_get_filing", "sec_get_xbrl_facts"])
        # step-output binding wires sec_search.filings[0].filing_url into sec_get_filing
        filing_step = plan.steps[1]
        self.assertEqual("sec_get_filing", filing_step.tool)
        self.assertEqual(
            {"step": "r4:sec_search", "field": "filings.0.filing_url"},
            filing_step.input_bindings["filing_url"],
        )

    def test_akshare_template(self):
        plan = RuleBasedPlanner().plan("r5", "q", {"code": "600519"})
        self.assertEqual([s.tool for s in plan.steps], ["ak_stock_quote", "ak_financials"])

    def test_fred_template(self):
        plan = RuleBasedPlanner().plan("r6", "q", {"series_id": "GDP"})
        self.assertEqual([s.tool for s in plan.steps], ["macro_fred_lookup"])

    def test_kpi_template(self):
        plan = RuleBasedPlanner().plan(
            "r8", "q", {"pages": ["pg1"], "kpi_names": ["revenue", "headcount"]}
        )
        tools = [s.tool for s in plan.steps]
        self.assertIn("retrieve_context", tools)
        self.assertIn("extract_kpis", tools)

    def test_financials_template(self):
        plan = RuleBasedPlanner().plan(
            "r9", "q", {"financials": [{"value": 1}, {"value": 2}], "operation": "sum"}
        )
        self.assertEqual([s.tool for s in plan.steps], ["compute_financials"])

    def test_empty_context_raises(self):
        from agent.planner import PlanningError

        with self.assertRaises(PlanningError):
            RuleBasedPlanner().plan("r7", "q", {})

    def test_planner_never_emits_matrix_cell_tools(self):
        cases = [
            {"pages": ["x"], "question": "q"},
            {"pages": ["x"], "kpi_names": ["revenue"]},
            {"financials": [{"value": 1}]},
            {"documents": [{"name": "a", "pages": ["x"]}], "question": "q"},
            {"url": "https://example.com", "question": "q"},
            {"ticker": "AAPL"},
            {"code": "600519"},
            {"series_id": "GDP"},
        ]
        for context in cases:
            plan = RuleBasedPlanner().plan("r", "q", context)
            for step in plan.steps:
                self.assertNotIn(step.tool, MATRIX_CELL_TOOLS, step.tool)
                self.assertIn(step.tool, AGENT_TOOL_ALLOWLIST, step.tool)


class AllowlistTests(unittest.TestCase):
    def test_allowlist_excludes_matrix_cell_tools(self):
        for tool in MATRIX_CELL_TOOLS:
            self.assertNotIn(tool, AGENT_TOOL_ALLOWLIST)
            self.assertFalse(is_allowed_tool(tool))

    def test_allowlist_has_sixteen_analysis_tools(self):
        self.assertEqual(
            len(AGENT_TOOL_ALLOWLIST),
            16,
            f"expected 16 analysis tools, got {sorted(AGENT_TOOL_ALLOWLIST)}",
        )

    def test_validate_plan_tools_flags_disallowed(self):
        bad = Plan(
            research_id="r",
            objective="q",
            steps=[PlanStep(step_id="s1", tool="export_matrix_report", arguments={})],
        )
        violations = validate_plan_tools(bad)
        self.assertEqual(len(violations), 1)

    def test_validate_plan_tools_accepts_clean_plan(self):
        plan = RuleBasedPlanner().plan("r", "q", {"pages": ["x"], "question": "q"})
        self.assertEqual(validate_plan_tools(plan), [])


class OrchestratorHappyPathTests(unittest.TestCase):
    def test_successful_run_completes_and_records_outputs(self):
        gateway = MockToolGateway(
            responses={
                "retrieve_context": json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]}),
                "summarize_context": json.dumps({"summary": "revenue was 1m", "source_chunks": [], "full_document": False}),
                "verify_quote": json.dumps({"citation_status": "verified_exact", "page": 1}),
            }
        )
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=2, backoff_seconds=0))

        result = run_coro(orchestrator.run("r1", "What is revenue?", {"pages": ["pg1"], "question": "What is revenue?", "quote": "revenue was 1m"}))

        self.assertEqual(result.phase, RunPhase.COMPLETED)
        self.assertIsNone(result.error)
        self.assertEqual(set(result.steps), {"r1:retrieve", "r1:summarize", "r1:verify"})
        for record in result.steps.values():
            self.assertEqual(record.status, StepStatus.SUCCEEDED)
            self.assertEqual(record.attempts, 1)
        self.assertEqual([c[0] for c in gateway.calls], ["retrieve_context", "summarize_context", "verify_quote"])
        self.assertIn("revenue was 1m", result.conclusion["outputs"]["r1:summarize"])

    def test_optional_step_failure_degrades_gracefully(self):
        gateway = MockToolGateway(
            responses={
                "retrieve_context": json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]}),
            },
            errors={
                # summarize is optional (required=False) in the pages template
                "summarize_context": "Error: boom",
            },
        )
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=2, backoff_seconds=0))

        result = run_coro(orchestrator.run("r2", "q", {"pages": ["pg1"], "question": "q"}))

        self.assertEqual(result.phase, RunPhase.COMPLETED)
        self.assertEqual(result.steps["r2:retrieve"].status, StepStatus.SUCCEEDED)
        self.assertEqual(result.steps["r2:summarize"].status, StepStatus.FAILED)

    def test_required_step_failure_fails_run(self):
        gateway = MockToolGateway(errors={"retrieve_context": "Error: fatal"})
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        result = run_coro(orchestrator.run("r3", "q", {"pages": ["pg1"], "question": "q"}))

        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.steps["r3:retrieve"].status, StepStatus.FAILED)

    def test_retry_transient_error_then_succeeds(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": []})},
            errors={"retrieve_context": ["Error: timed out", None]},
        )
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=3, backoff_seconds=0))

        result = run_coro(orchestrator.run("r4", "q", {"pages": ["pg1"], "question": "q"}))

        self.assertEqual(result.phase, RunPhase.COMPLETED)
        self.assertEqual(result.steps["r4:retrieve"].status, StepStatus.SUCCEEDED)
        self.assertEqual(result.steps["r4:retrieve"].attempts, 2)

    def test_red_line_plan_is_rejected_before_execution(self):
        bad_plan = Plan(
            research_id="r5",
            objective="q",
            steps=[PlanStep(step_id="s1", tool="export_matrix_report", arguments={"run_id": "x"})],
        )
        gateway = MockToolGateway()
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        result = run_coro(orchestrator.run("r5", "q", plan=bad_plan))

        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertIn("red line", result.error)
        self.assertEqual(gateway.calls, [])


class InterruptTests(unittest.TestCase):
    def test_cancel_between_steps_stops_run(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": []})},
            errors={"summarize_context": "Error: boom"},
        )
        token = InterruptToken()
        token.pause()
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        async def drive():
            task = asyncio.create_task(
                orchestrator.run("r6", "q", {"pages": ["pg1"], "question": "q"}, token=token)
            )
            await asyncio.sleep(0.05)
            token.cancel()
            return await task

        result = run_coro(drive())

        self.assertEqual(result.phase, RunPhase.CANCELLED)

    def test_pause_and_resume(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": []})}
        )
        token = InterruptToken()
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        async def drive():
            token.pause()
            task = asyncio.create_task(
                orchestrator.run("r7", "q", {"pages": ["pg1"], "question": "q"}, token=token)
            )
            await asyncio.sleep(0.1)
            self.assertTrue(token.paused)
            token.resume()
            return await task

        result = run_coro(drive())
        self.assertEqual(result.phase, RunPhase.COMPLETED)


class ResearchRunModeTests(unittest.TestCase):
    def test_controller_end_to_end(self):
        gateway = MockToolGateway(
            responses={
                "retrieve_context": json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]}),
                "summarize_context": json.dumps({"summary": "s", "source_chunks": [], "full_document": False}),
            }
        )
        controller = ResearchRunController(
            "r100", "What is revenue?", {"pages": ["pg1"], "question": "What is revenue?"},
            gateway=gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
        )

        async def drive_controller():
            controller.start()
            return await controller.wait()

        final = run_coro(drive_controller())
        self.assertEqual(final.phase, RunPhase.COMPLETED)
        status = controller.status()
        self.assertEqual(status["research_id"], "r100")
        self.assertEqual(len(status["steps"]), 2)

    def test_manager_cancel(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": []})},
            errors={"summarize_context": "Error: boom"},
        )
        manager = ResearchRunManager(gateway=gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0))

        async def drive_manager():
            controller = manager.start("r101", "q", {"pages": ["pg1"], "question": "q"})
            self.assertIs(manager.get("r101"), controller)
            self.assertIsNotNone(manager.status("r101"))
            # Pause first so the run is guaranteed to still be live, then cancel.
            self.assertTrue(manager.pause("r101"))
            self.assertTrue(manager.cancel("r101"))
            final = await manager.result("r101")
            self.assertEqual(final.phase, RunPhase.CANCELLED)

        run_coro(drive_manager())
        self.assertIn("r101", manager.list())

    def test_research_run_entrypoint(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]})}
        )

        async def drive_entry():
            controller = agent.research_run(
                "r102", "q", {"pages": ["pg1"], "question": "q"},
                gateway=gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
            )
            self.assertEqual(controller.research_id, "r102")
            return await controller.wait()

        final = run_coro(drive_entry())
        self.assertEqual(final.phase, RunPhase.COMPLETED)


class McpGatewayProtocolTests(unittest.TestCase):
    def test_mcp_gateway_lists_fifty_tools(self):
        # list_tools is in-process and touches no external API.
        gateway = McpToolGateway()

        specs = run_coro(gateway.list_tools())

        self.assertEqual(len(specs), 52)
        names = {s.name for s in specs}
        self.assertTrue(MATRIX_CELL_TOOLS.issubset(names))
        self.assertTrue(AGENT_TOOL_ALLOWLIST.issubset(names))

    def test_mcp_gateway_rejects_disallowed_tool_without_calling(self):
        gateway = McpToolGateway()

        result = run_coro(gateway.call_tool("export_matrix_report", {"run_id": "x"}))

        self.assertFalse(result.ok)
        self.assertIn("allowlist", result.error or "")


class BlockingToolGateway:
    """ToolGateway whose call_tool never returns until cancelled."""

    def __init__(self):
        self.calls = 0

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments):
        self.calls += 1
        await asyncio.Event().wait()


class P1ErrorHandlingTests(unittest.TestCase):
    def test_planner_error_transitions_to_failed(self):
        # Empty context makes the built-in planner raise PlanningError; the run
        # must land in a terminal FAILED state instead of escaping the task.
        gateway = MockToolGateway()
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        result = run_coro(orchestrator.run("r300", "q", {}))

        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertIsNotNone(result.error)

    def test_gateway_exception_transitions_to_failed(self):
        class RaisingGateway(MockToolGateway):
            async def call_tool(self, name, arguments):
                self.calls.append((name, dict(arguments)))
                raise RuntimeError("boom")

        gateway = RaisingGateway()
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        result = run_coro(orchestrator.run("r301", "q", {"pages": ["pg1"], "question": "q"}))

        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(len(gateway.calls), 1)

    def test_retry_policy_max_attempts_caps_calls(self):
        # Step default is 3 attempts, but a strict policy of 1 must win.
        gateway = MockToolGateway(errors={"retrieve_context": "Error: timed out"})
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=1, backoff_seconds=0))

        result = run_coro(orchestrator.run("r302", "q", {"pages": ["pg1"], "question": "q"}))

        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertEqual(len(gateway.calls), 1)

    def test_non_retryable_error_does_not_loop(self):
        # A permanent tool error must stop after the first attempt even when the
        # attempt budget is large.
        gateway = MockToolGateway(errors={"retrieve_context": "Error: fatal"})
        orchestrator = ResearchOrchestrator(gateway, RetryPolicy(max_attempts=5, backoff_seconds=0))

        result = run_coro(orchestrator.run("r303", "q", {"pages": ["pg1"], "question": "q"}))

        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertEqual(len(gateway.calls), 1)


class P1RunRegistryTests(unittest.TestCase):
    def test_duplicate_start_returns_existing_controller(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]})}
        )
        manager = ResearchRunManager(gateway=gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0))

        async def drive():
            first = manager.start("r400", "q", {"pages": ["pg1"], "question": "q"})
            second = manager.start("r400", "q", {"pages": ["pg1"], "question": "q"})
            self.assertIs(second, first)
            final = await manager.result("r400")
            return final, first

        final, first = run_coro(drive())
        self.assertEqual(final.phase, RunPhase.COMPLETED)
        self.assertIs(manager.get("r400"), first)

    def test_restart_after_terminal_replaces_controller(self):
        gateway = MockToolGateway(
            responses={"retrieve_context": json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]})}
        )
        manager = ResearchRunManager(gateway=gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0))

        async def drive():
            first = manager.start("r401", "q", {"pages": ["pg1"], "question": "q"})
            await manager.result("r401")
            second = manager.start("r401", "q", {"pages": ["pg1"], "question": "q"})
            self.assertIsNot(second, first)
            final = await manager.result("r401")
            return final

        final = run_coro(drive())
        self.assertEqual(final.phase, RunPhase.COMPLETED)


class P1InterruptTests(unittest.TestCase):
    def test_cancel_interrupts_in_flight_call(self):
        gateway = BlockingToolGateway()
        controller = ResearchRunController(
            "r500", "q", {"pages": ["pg1"], "question": "q"},
            gateway=gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
        )

        async def drive():
            controller.start()
            await asyncio.sleep(0.05)
            self.assertTrue(controller.cancel())
            final = await asyncio.wait_for(controller.wait(), timeout=5)
            return final

        final = run_coro(drive())
        self.assertEqual(final.phase, RunPhase.CANCELLED)

    def test_cancel_interrupts_retry_backoff(self):
        # A long backoff must not delay cancellation: cancel() wakes the wait.
        gateway = MockToolGateway(errors={"retrieve_context": "Error: timed out"})
        controller = ResearchRunController(
            "r501", "q", {"pages": ["pg1"], "question": "q"},
            gateway=gateway,
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=5),
        )

        async def drive():
            controller.start()
            await asyncio.sleep(0.05)
            self.assertTrue(controller.cancel())
            final = await asyncio.wait_for(controller.wait(), timeout=5)
            return final

        final = run_coro(drive())
        self.assertEqual(final.phase, RunPhase.CANCELLED)

    def test_call_timeout_turns_into_failed_run(self):
        gateway = BlockingToolGateway()
        orchestrator = ResearchOrchestrator(
            gateway, RetryPolicy(max_attempts=1, backoff_seconds=0, call_timeout_seconds=0.05)
        )

        async def drive():
            return await asyncio.wait_for(
                orchestrator.run("r502", "q", {"pages": ["pg1"], "question": "q"}),
                timeout=5,
            )

        result = run_coro(drive())
        self.assertEqual(result.phase, RunPhase.FAILED)
        self.assertIn("timed out", result.error or "")
        self.assertEqual(gateway.calls, 1)


class StepOutputBindingTests(unittest.TestCase):
    """A1: step-output bindings resolve upstream JSON fields into later args."""

    def test_field_binding_resolves_from_prior_output(self):
        from agent.orchestrator import _dig

        payload = {
            "ticker": "AAPL",
            "filings": [{"form": "10-K", "filing_url": "https://sec.gov/x"}],
        }
        self.assertEqual("AAPL", _dig(payload, "ticker"))
        self.assertEqual("https://sec.gov/x", _dig(payload, "filings.0.filing_url"))
        self.assertIsNone(_dig(payload, "filings.1.filing_url"))
        self.assertIsNone(_dig(payload, "nope"))

    def test_orchestrator_resolves_bindings_before_call(self):
        from agent.orchestrator import ResearchOrchestrator
        from agent.planner import PlanStep
        from agent.state_machine import ResearchRun, StepStatus

        run = ResearchRun("r-bind", "q", {})
        run.steps = {
            "r-bind:search": type(
                "R",
                (),
                {
                    "status": StepStatus.SUCCEEDED,
                    "tool": "sec_search",
                    "output": '{"filings": [{"filing_url": "https://sec.gov/f"}]}',
                },
            )(),
        }
        step = PlanStep(
            step_id="r-bind:filing",
            tool="sec_get_filing",
            input_bindings={"filing_url": {"step": "r-bind:search", "field": "filings.0.filing_url"}},
        )
        args = ResearchOrchestrator._resolve_bindings(run, step)
        self.assertEqual("https://sec.gov/f", args.get("filing_url"))

    def test_binding_missing_upstream_resolves_none(self):
        from agent.orchestrator import ResearchOrchestrator
        from agent.planner import PlanStep
        from agent.state_machine import ResearchRun, StepStatus

        run = ResearchRun("r-bind2", "q", {})
        # upstream step FAILED -> binding resolves to None
        run.steps = {
            "r-bind2:search": type("R", (), {"status": StepStatus.FAILED, "output": None})(),
        }
        step = PlanStep(
            step_id="r-bind2:filing",
            tool="sec_get_filing",
            input_bindings={"filing_url": {"step": "r-bind2:search", "field": "filings.0.filing_url"}},
        )
        args = ResearchOrchestrator._resolve_bindings(run, step)
        self.assertIsNone(args.get("filing_url"))

    def test_template_binding_substitutes_dotted_path(self):
        from agent.orchestrator import ResearchOrchestrator, _template_format
        from agent.planner import PlanStep
        from agent.state_machine import ResearchRun, StepStatus

        payload = {"filings": [{"accession": "000123", "primary_document": "doc.htm"}]}
        out = _template_format(
            "https://sec.gov/Archives/{filings.0.accession}/{filings.0.primary_document}",
            payload,
        )
        self.assertEqual("https://sec.gov/Archives/000123/doc.htm", out)

        run = ResearchRun("r-tpl", "q", {})
        run.steps = {
            "r-tpl:search": type(
                "R",
                (),
                {
                    "status": StepStatus.SUCCEEDED,
                    "tool": "sec_search",
                    "output": '{"filings": [{"accession": "000123", "primary_document": "doc.htm"}]}',
                },
            )(),
        }
        step = PlanStep(
            step_id="r-tpl:filing",
            tool="sec_get_filing",
            input_bindings={
                "filing_url": {
                    "step": "r-tpl:search",
                    "template": "https://sec.gov/Archives/{filings.0.accession}/{filings.0.primary_document}",
                }
            },
        )
        args = ResearchOrchestrator._resolve_bindings(run, step)
        self.assertEqual("https://sec.gov/Archives/000123/doc.htm", args.get("filing_url"))

    def test_template_binding_missing_field_resolves_none(self):
        from agent.orchestrator import ResearchOrchestrator
        from agent.planner import PlanStep
        from agent.state_machine import ResearchRun, StepStatus

        run = ResearchRun("r-tpl2", "q", {})
        run.steps = {
            "r-tpl2:search": type(
                "R",
                (),
                {"status": StepStatus.SUCCEEDED, "tool": "sec_search", "output": '{"filings": []}'},
            )(),
        }
        step = PlanStep(
            step_id="r-tpl2:filing",
            tool="sec_get_filing",
            input_bindings={
                "filing_url": {
                    "step": "r-tpl2:search",
                    "template": "https://sec.gov/Archives/{filings.0.accession}/{filings.0.primary_document}",
                }
            },
        )
        args = ResearchOrchestrator._resolve_bindings(run, step)
        self.assertIsNone(args.get("filing_url"))


if __name__ == "__main__":
    unittest.main()
