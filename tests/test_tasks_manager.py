"""Tests for P4-3 agent.tasks (TaskManager)."""

import asyncio
import time
import unittest

from agent.tasks import TaskManager


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = TaskManager()

    def tearDown(self):
        self.manager.shutdown()

    def test_submit_and_success(self):
        async def op(payload):
            await asyncio.sleep(0.1)
            return {"doubled": payload["value"] * 2}

        task_id = self.manager.submit("op", {"value": 21}, op)
        # poll until succeeded
        status = None
        for _ in range(50):
            status = self.manager.status(task_id)
            if status and status["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual("succeeded", status["status"])
        result = self.manager.result(task_id)
        self.assertEqual(42, result["result"]["doubled"])

    def test_failure_recorded(self):
        async def boom(payload):
            raise RuntimeError("kaboom")

        task_id = self.manager.submit("op", None, boom)
        status = None
        for _ in range(50):
            status = self.manager.status(task_id)
            if status and status["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual("failed", status["status"])
        self.assertIn("kaboom", status["error"])

    def test_status_unknown(self):
        self.assertIsNone(self.manager.status("nope"))

    def test_cancel_queued(self):
        async def slow(payload):
            await asyncio.sleep(10)

        task_id = self.manager.submit("op", None, slow)
        self.assertTrue(self.manager.cancel(task_id))
        status = self.manager.status(task_id)
        self.assertEqual("cancelled", status["status"])

    def test_cancel_really_cancels_execution(self):
        """The callable receives CancelledError; status stays cancelled after the
        original operation duration; no post-cancel side effects."""
        import asyncio as _asyncio

        cancelled_event = _asyncio.Event()
        side_effect = []

        async def slow(payload):
            try:
                await _asyncio.sleep(1.0)
                side_effect.append("completed")
            except _asyncio.CancelledError:
                cancelled_event.set()
                raise

        task_id = self.manager.submit("op", None, slow)
        # ensure the callable has actually started before cancelling so the
        # CancelledError path is exercised deterministically.
        time.sleep(0.2)
        self.assertTrue(self.manager.cancel(task_id))
        # give the loop time to deliver the cancellation
        time.sleep(0.5)
        self.assertTrue(cancelled_event.is_set(), "callable must receive CancelledError")
        self.assertEqual([], side_effect, "post-cancel side effect must not occur")
        status = self.manager.status(task_id)
        self.assertEqual("cancelled", status["status"])
        # wait past the original duration: still cancelled, never succeeded
        time.sleep(1.2)
        self.assertEqual("cancelled", self.manager.status(task_id)["status"])

    def test_cancel_before_execution_prevents_run(self):
        """Cancelling a queued task prevents the callable from starting."""
        import asyncio as _asyncio

        ran = []

        async def slow(payload):
            ran.append("ran")
            await _asyncio.sleep(0.2)

        # use a shared event to hold the loop from running the task yet
        gate = _asyncio.Event()

        async def gated(payload):
            await gate.wait()
            ran.append("ran")

        task_id = self.manager.submit("op", None, gated)
        self.assertTrue(self.manager.cancel(task_id))
        time.sleep(0.5)
        self.assertEqual([], ran, "callable must not run after queued-cancel")
        self.assertEqual("cancelled", self.manager.status(task_id)["status"])

    def test_cancel_unknown(self):
        self.assertFalse(self.manager.cancel("nope"))

    def test_cancel_finished_false(self):
        async def quick(payload):
            return "done"

        task_id = self.manager.submit("op", None, quick)
        time.sleep(0.3)
        self.assertFalse(self.manager.cancel(task_id))

    def test_list(self):
        async def quick(payload):
            return "done"

        task_id = self.manager.submit("op", None, quick)
        time.sleep(0.3)
        self.assertIn(task_id, [t["task_id"] for t in self.manager.list()])

    def test_concurrent_default_task_manager_initialization(self):
        from concurrent.futures import ThreadPoolExecutor

        import agent.tasks as tasks_mod

        if tasks_mod._default_manager is not None:
            tasks_mod._default_manager.shutdown()
            tasks_mod._default_manager = None

        async def quick_work(payload):
            await asyncio.sleep(0.01)
            return payload["idx"]

        def worker(idx):
            mgr = tasks_mod.default_task_manager()
            tid = mgr.submit("concurrent", {"idx": idx}, quick_work)
            return mgr, tid

        try:
            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(worker, range(20)))

            primary = tasks_mod.default_task_manager()
            for mgr, tid in results:
                self.assertIs(mgr, primary)
                status = None
                for _ in range(50):
                    status = primary.status(tid)
                    if status and status["status"] in ("succeeded", "failed"):
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(status)
                self.assertEqual("succeeded", status["status"])
                res = primary.result(tid)
                self.assertIsNotNone(res)
                self.assertIsNotNone(res.get("result"))
        finally:
            if tasks_mod._default_manager is not None:
                tasks_mod._default_manager.shutdown()
                tasks_mod._default_manager = None


if __name__ == "__main__":
    unittest.main()
