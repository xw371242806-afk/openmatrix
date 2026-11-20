"""Tests for P5-1 MCP Tasks handle (tasks_get/cancel/result) + P5-2 PAUSED phase."""

import asyncio
import json
import unittest

from agent.orchestrator import RetryPolicy
from agent.research_run import ResearchRunManager
from agent.state_machine import RunPhase
from agent.tools import ToolResult
from mcp_server import task_cancel, task_result, task_status, task_submit



class SlowFakeGateway:
    """ToolGateway stub that yields control so a run stays observable mid-flight."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []

    async def list_tools(self):
        return [type("TS", (), {"name": "retrieve_context"})()]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        await asyncio.sleep(self.delay)
        if name == "retrieve_context":
            return ToolResult(text=json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "rev"}]}))
        return ToolResult(text="summary")


def run_coro(coro):
    return asyncio.run(coro)


class TasksMcpTests(unittest.TestCase):
    def setUp(self):
        self.gateway = SlowFakeGateway(delay=0.05)
        self.manager = ResearchRunManager(
            gateway=self.gateway, retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0)
        )
        import importlib
        _research_run_mod = importlib.import_module("agent.research_run")
        _research_run_mod._default_manager = self.manager

    def tearDown(self):
        import importlib
        importlib.import_module("agent.research_run")._default_manager = None

    def test_task_status_unknown(self):
        self.assertTrue(task_status("nope").startswith("Error:"))

    def test_task_cancel_unknown(self):
        self.assertTrue(task_cancel("nope").startswith("Error:"))

    def test_task_status_after_run(self):
        async def drive():
            controller = self.manager.start("t1", "q", {"pages": ["pg1"], "question": "q"})
            await controller.wait()
            return controller

        controller = run_coro(drive())
        status = json.loads(task_status("t1"))
        self.assertEqual("t1", status["research_id"])
        self.assertEqual(RunPhase.COMPLETED.value, status["phase"])

    def test_task_cancel_live_run(self):
        async def drive():
            self.manager.start("t2", "q", {"pages": ["pg1"], "question": "q"})
            await asyncio.sleep(0.02)
            # run is mid-flight (slow gateway); pause -> PAUSED observable
            self.assertTrue(self.manager.pause("t2"))
            status = json.loads(task_status("t2"))
            self.assertEqual(RunPhase.PAUSED.value, status["phase"])
            # cancel through the unified MCP interface (routes to research run)
            cancelled = json.loads(task_cancel("t2"))
            self.assertTrue(cancelled["cancelled"])
            final = await self.manager.result("t2")
            self.assertEqual(RunPhase.CANCELLED, final.phase)

        run_coro(drive())

    def test_research_id_rejects_reserved_prefix(self):
        out = task_submit("research_run", {"research_id": "t-reserved", "objective": "q"})
        self.assertTrue(out.startswith("Error:"))
        self.assertIn("t-", out)

    def test_task_result_pending_then_done(self):
        async def drive():
            controller = self.manager.start("t3", "q", {"pages": ["pg1"], "question": "q"})
            await asyncio.sleep(0.02)
            out = json.loads(await task_result("t3"))
            self.assertIn(out["phase"], ("pending", RunPhase.COMPLETED.value))
            await controller.wait()
            out2 = json.loads(await task_result("t3"))
            self.assertEqual(RunPhase.COMPLETED.value, out2["phase"])

        run_coro(drive())

    def test_pause_immediate_phase_transition(self):
        async def drive():
            self.manager.start("t4", "q", {"pages": ["pg1"], "question": "q"})
            await asyncio.sleep(0.02)
            self.assertTrue(self.manager.pause("t4"))
            status = json.loads(task_status("t4"))
            self.assertEqual(RunPhase.PAUSED.value, status["phase"])
            self.assertTrue(self.manager.resume("t4"))
            final = await self.manager.result("t4")
            self.assertEqual(RunPhase.COMPLETED, final.phase)

        run_coro(drive())


if __name__ == "__main__":
    unittest.main()
