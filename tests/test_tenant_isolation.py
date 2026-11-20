"""Tests for P7-2: tenant/owner isolation on research runs."""

import asyncio
import json
import unittest

from agent.orchestrator import RetryPolicy
from agent.research_run import ResearchRunManager
from agent.state_machine import RunPhase
from agent.tools import ToolResult


def run_coro(coro):
    return asyncio.run(coro)


class FakeGateway:
    async def list_tools(self):
        return [type("T", (), {"name": "retrieve_context"})()]

    async def call_tool(self, name, arguments):
        await asyncio.sleep(0.01)
        return ToolResult(text=json.dumps({"chunks": [{"chunk_id": "c1", "page": 1, "text": "x"}]}))


class OwnerIsolationTests(unittest.TestCase):
    """ResearchRunManager owner scoping."""

    def setUp(self):
        self.manager = ResearchRunManager(
            gateway=FakeGateway(), retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0)
        )

    def test_start_records_owner(self):
        async def drive():
            controller = self.manager.start("r-a1", "q", {"pages": ["p"]}, owner="tenant-1")
            await asyncio.sleep(0.02)
            return controller

        controller = run_coro(drive())
        self.assertEqual("tenant-1", controller.owner)
        self.assertEqual("tenant-1", controller.status()["owner"])

    def test_status_scoped_by_owner(self):
        async def drive():
            self.manager.start("r-a2", "q", {"pages": ["p"]}, owner="tenant-1")
            await asyncio.sleep(0.02)
            return (
                self.manager.status("r-a2", owner="tenant-1"),
                self.manager.status("r-a2", owner="tenant-2"),
            )

        mine, other = run_coro(drive())
        self.assertIsNotNone(mine)
        self.assertIsNone(other)

    def test_cancel_scoped_by_owner(self):
        async def drive():
            self.manager.start("r-a3", "q", {"pages": ["p"]}, owner="tenant-1")
            await asyncio.sleep(0.02)
            self.assertFalse(self.manager.cancel("r-a3", owner="tenant-2"))
            self.assertTrue(self.manager.cancel("r-a3", owner="tenant-1"))
            final = await self.manager.result("r-a3", owner="tenant-1")
            self.assertEqual(RunPhase.CANCELLED, final.phase)

        run_coro(drive())

    def test_list_filters_by_owner(self):
        async def drive():
            self.manager.start("r-a4", "q", {"pages": ["p"]}, owner="tenant-1")
            self.manager.start("r-a5", "q", {"pages": ["p"]}, owner="tenant-2")
            self.manager.start("r-a6", "q", {"pages": ["p"]})
            await asyncio.sleep(0.02)
            return self.manager

        manager = run_coro(drive())
        self.assertEqual({"r-a4"}, set(manager.list(owner="tenant-1")))
        self.assertEqual({"r-a5"}, set(manager.list(owner="tenant-2")))
        self.assertEqual(3, len(manager.list()))

    def test_result_scoped_by_owner(self):
        async def drive():
            self.manager.start("r-a7", "q", {"pages": ["p"]}, owner="tenant-1")
            await asyncio.sleep(0.02)
            self.assertIsNone(await self.manager.result("r-a7", owner="tenant-2"))
            final = await self.manager.result("r-a7", owner="tenant-1")
            self.assertEqual(RunPhase.COMPLETED, final.phase)

        run_coro(drive())

    def test_scoped_caller_cannot_see_unowned_run(self):
        async def drive():
            self.manager.start("r-a8", "q", {"pages": ["p"]})  # no owner
            await asyncio.sleep(0.02)
            return (
                self.manager.status("r-a8", owner="tenant-1"),
                self.manager.cancel("r-a8", owner="tenant-1"),
                self.manager.list(owner="tenant-1"),
            )

        status, cancelled, listed = run_coro(drive())
        self.assertIsNone(status)          # unowned run invisible to scoped caller
        self.assertFalse(cancelled)        # cannot cancel it either
        self.assertEqual([], listed)       # not listed
        # unscoped caller still sees it (compat path)
        self.assertIsNotNone(self.manager.status("r-a8"))


class TenantMcpTests(unittest.TestCase):
    """MCP task_* tools honor owner scoping."""

    def setUp(self):
        import importlib

        from mcp_server import task_cancel, task_list, task_status, task_submit

        self.task_submit = task_submit
        self.task_status = task_status
        self.task_cancel = task_cancel
        self.task_list = task_list
        mod = importlib.import_module("agent.research_run")
        self.manager = ResearchRunManager(
            gateway=FakeGateway(), retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0)
        )
        mod._default_manager = self.manager

    def tearDown(self):
        import importlib

        importlib.import_module("agent.research_run")._default_manager = None

    def test_submit_with_owner_then_status_scoped(self):
        async def drive():
            out = json.loads(self.task_submit(
                "research_run", {"research_id": "m1", "objective": "q", "owner": "tenant-1"}
            ))
            await asyncio.sleep(0.01)
            return out, self.task_status("m1", owner="tenant-1"), self.task_status("m1", owner="tenant-2")

        out, mine, other = run_coro(drive())
        self.assertEqual("m1", out["task_id"])
        status = json.loads(mine)
        self.assertEqual("tenant-1", status["owner"])
        self.assertTrue(other.startswith("Error:"))

    def test_cancel_scoped(self):
        async def drive():
            self.task_submit("research_run", {"research_id": "m2", "objective": "q", "owner": "tenant-1"})
            # cancel immediately while the run is still live
            mine = self.task_cancel("m2", owner="tenant-1")
            other = self.task_cancel("m2", owner="tenant-2")
            return mine, other

        mine, other = run_coro(drive())
        self.assertTrue(mine.startswith('{"'), mine[:60])
        self.assertTrue(other.startswith("Error:"), other[:60])

    def test_list_scoped(self):
        async def drive():
            self.task_submit("research_run", {"research_id": "m3", "objective": "q", "owner": "tenant-1"})
            self.task_submit("research_run", {"research_id": "m4", "objective": "q", "owner": "tenant-2"})
            await asyncio.sleep(0.01)
            return self.task_list(owner="tenant-1")

        data1 = json.loads(run_coro(drive()))
        ids1 = [t["research_id"] for t in data1["tasks"] if t["kind"] == "research_run"]
        self.assertIn("m3", ids1)
        self.assertNotIn("m4", ids1)


if __name__ == "__main__":
    unittest.main()
