"""Tests for optimization wave 1: /api/v1/schedules REST + /api/v1/model-router."""

import unittest
import warnings
from datetime import datetime
from threading import Event
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
    category=UserWarning,
)

from fastapi.testclient import TestClient

from agent import scheduler
from agent.scheduler import ScheduleManager
from main import _schedule_research_callback, app


class SchedulesRestTests(unittest.TestCase):
    def setUp(self):
        # Each REST test owns a fresh persistent store; never clear user jobs.
        temporary = TemporaryDirectory(prefix='schedule-rest-test-')
        self.addCleanup(temporary.cleanup)
        manager = ScheduleManager(db_path=Path(temporary.name) / 'schedules.sqlite3')
        self.addCleanup(manager.stop_ticker)
        singleton_patch = patch.object(scheduler, '_default_manager', manager)
        singleton_patch.start()
        self.addCleanup(singleton_patch.stop)
        self.client = TestClient(app)

    def test_create_list_delete_schedule_roundtrip(self):
        resp = self.client.post(
            "/api/v1/schedules",
            json={
                "schedule_id": "sched-rest-1",
                "cron": "*/5 * * * *",
                "objective": "monitor filings",
                "research_id": "res-1",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["schedule_id"], "sched-rest-1")
        self.assertEqual(body["status"], "registered")

        listing = self.client.get("/api/v1/schedules").json()["schedules"]
        ids = [s["schedule_id"] for s in listing]
        self.assertIn("sched-rest-1", ids)

        deleted = self.client.delete("/api/v1/schedules/sched-rest-1")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["status"], "cancelled")

    def test_create_duplicate_returns_400(self):
        payload = {
            "schedule_id": "sched-dup",
            "cron": "*/5 * * * *",
            "objective": "o",
            "research_id": "r",
        }
        first = self.client.post("/api/v1/schedules", json=payload)
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/v1/schedules", json=payload)
        self.assertEqual(second.status_code, 400)

    def test_create_invalid_cron_returns_400(self):
        resp = self.client.post(
            "/api/v1/schedules",
            json={
                "schedule_id": "sched-bad",
                "cron": "not a cron",
                "objective": "o",
                "research_id": "r",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_fields_return_400(self):
        for payload in (
            {"cron": "", "objective": "o", "research_id": "r"},
            {"cron": "* * * * *", "objective": "", "research_id": "r"},
            {"cron": "* * * * *", "objective": "o", "research_id": ""},
        ):
            resp = self.client.post("/api/v1/schedules", json=payload)
            self.assertEqual(resp.status_code, 400)

    def test_context_must_be_object(self):
        resp = self.client.post(
            "/api/v1/schedules",
            json={
                "schedule_id": "sched-ctx",
                "cron": "* * * * *",
                "objective": "o",
                "research_id": "r",
                "context": ["not", "an", "object"],
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_unknown_returns_404(self):
        resp = self.client.delete("/api/v1/schedules/never-existed")
        self.assertEqual(resp.status_code, 404)


class ModelRouterEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_router_endpoint_shape(self):
        resp = self.client.get("/api/v1/model-router")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("enabled", body)
        self.assertIn("default_model", body)
        for task in ("fast", "chain", "batch"):
            self.assertIn(task, body["tasks"])


class ScheduleResearchCallbackTests(unittest.TestCase):
    def test_manager_calls_research_callback_with_schedule_id_and_entry(self):
        completed = Event()
        calls = []

        class FakeController:
            async def wait(self):
                completed.set()

        def fake_research_run(research_id, objective, context):
            calls.append((research_id, objective, context))
            return FakeController()

        manager = ScheduleManager(callback=_schedule_research_callback, tick_seconds=1)
        manager.register(
            "sched-callback",
            "* * * * *",
            "monitor filings",
            "research-1",
            {"source": "test"},
        )

        with patch("agent.research_run.research_run", side_effect=fake_research_run):
            fired = manager.tick(datetime(2026, 8, 30, 12, 0))
            self.assertTrue(completed.wait(timeout=2))

        self.assertEqual(["sched-callback"], fired)
        self.assertIsNone(manager.get("sched-callback").last_error)
        self.assertEqual(
            [("research-1", "monitor filings", {"source": "test"})],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
