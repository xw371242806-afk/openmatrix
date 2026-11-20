"""Tests for P4-3 MCP task tools (task_submit/status/cancel/result + document_add async)."""

import asyncio
import json
import os
import tempfile
import time
import unittest

import apikeys  # noqa: F401 - ensure env isolation helpers run
import library
import agent.tasks as tasks_mod
from mcp_server import (
    document_add,
    project_create,
    task_cancel,
    task_result,
    task_status,
    task_submit,
)


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["OPENMATRIX_LIBRARY_ROOT"] = _tmp.name
    library._default_store = None
    tasks_mod._default_manager = None


def tearDownModule():
    import asyncio

    if tasks_mod._default_manager is not None:
        tasks_mod._default_manager.shutdown()
        tasks_mod._default_manager = None
    if library._default_store is not None:
        asyncio.run(library._default_store.close())
        library._default_store = None
    os.environ.pop("OPENMATRIX_LIBRARY_ROOT", None)
    _tmp.cleanup()


def wait_status(manager, task_id, max_wait=5.0):
    deadline = time.time() + max_wait
    status = None
    while time.time() < deadline:
        status = manager.status(task_id)
        if status and status["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.1)
    return status


class TaskMcpP4Tests(unittest.TestCase):
    def test_task_submit_document_index(self):
        proj = json.loads(project_create("Idx", None))
        doc = json.loads(document_add(proj["id"], "a.txt", "Revenue was 125 million dollars.", index=False))
        # submit index as a background task
        out = json.loads(task_submit("document_add", {"doc_id": doc["id"]}))
        self.assertTrue(out["task_id"].startswith("t-"))
        status = wait_status(tasks_mod.default_task_manager(), out["task_id"])
        self.assertEqual("succeeded", status["status"])
        res = json.loads(asyncio.run(task_result(out["task_id"])))
        self.assertEqual("indexed", res["result"]["status"])

    def test_task_submit_unknown_operation(self):
        self.assertTrue(task_submit("nope").startswith("Error:"))

    def test_task_status_unknown(self):
        self.assertTrue(task_status("nope").startswith("Error:"))

    def test_task_cancel_running(self):
        async def slow(payload):
            await asyncio.sleep(30)

        import asyncio

        manager = tasks_mod.default_task_manager()
        task_id = manager.submit("slow", None, slow)
        out = json.loads(task_cancel(task_id))
        self.assertTrue(out["cancelled"])
        self.assertEqual("cancelled", manager.status(task_id)["status"])

    def test_task_cancel_unknown(self):
        self.assertTrue(task_cancel("nope").startswith("Error:"))

    def test_document_add_async_mode(self):
        proj = json.loads(project_create("Async", None))
        out = json.loads(document_add(proj["id"], "b.txt", "Capex reached 9 million.", index=True, async_mode=True))
        self.assertTrue(out["async"])
        self.assertTrue(out["task_id"].startswith("t-"))
        status = wait_status(tasks_mod.default_task_manager(), out["task_id"])
        self.assertEqual("succeeded", status["status"])


if __name__ == "__main__":
    unittest.main()
