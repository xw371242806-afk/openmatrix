"""Tests for H7 agent.scheduler (cron parsing + ScheduleManager)."""

import threading
import unittest
import tempfile
from pathlib import Path
from agent.scheduler import ScheduleManager
from datetime import datetime

from agent.scheduler import CronSpec, ScheduleManager, parse_cron


class ParseCronTests(unittest.TestCase):
    """5-field cron expression parsing."""

    def test_parse_all_stars(self):
        spec = parse_cron("* * * * *")
        self.assertIsNotNone(spec)
        self.assertIn(30, spec.minutes)
        self.assertIn(12, spec.hours)

    def test_parse_specific_fields(self):
        spec = parse_cron("0 9 15 1 *")
        self.assertEqual({0}, set(spec.minutes))
        self.assertEqual({9}, set(spec.hours))
        self.assertEqual({15}, set(spec.dom))
        self.assertEqual({1}, set(spec.months))
        self.assertTrue(spec.matches(datetime(2026, 1, 15, 9, 0)))

    def test_parse_step(self):
        spec = parse_cron("*/15 * * * *")
        self.assertEqual({0, 15, 30, 45}, set(spec.minutes))

    def test_parse_range_and_list(self):
        spec = parse_cron("1-5,10 * * * *")
        self.assertEqual({1, 2, 3, 4, 5, 10}, set(spec.minutes))

    def test_parse_invalid(self):
        self.assertIsNone(parse_cron(""))
        self.assertIsNone(parse_cron("* * * *"))
        self.assertIsNone(parse_cron("61 * * * *"))
        self.assertIsNone(parse_cron("* 24 * * *"))
        self.assertIsNone(parse_cron("* * 32 * *"))
        self.assertIsNone(parse_cron("* * * 13 *"))
        self.assertIsNone(parse_cron("* * * * 8"))
        self.assertIsNone(parse_cron("not a cron"))

    def test_dow_sunday_alias(self):
        # 0 and 7 both mean Sunday
        spec0 = parse_cron("* * * * 0")
        spec7 = parse_cron("* * * * 7")
        self.assertIsNotNone(spec0)
        self.assertIsNotNone(spec7)
        sunday = datetime(2026, 8, 16)  # 2026-08-16 is a Sunday
        self.assertTrue(spec0.matches(sunday))
        self.assertTrue(spec7.matches(sunday))

    def test_dow_mapping_weekday(self):
        # cron 1..6 -> Monday..Saturday
        for cron_dow, weekday_dt in (
            (1, datetime(2026, 8, 17)),  # Monday
            (2, datetime(2026, 8, 18)),  # Tuesday
            (3, datetime(2026, 8, 19)),  # Wednesday
            (4, datetime(2026, 8, 20)),  # Thursday
            (5, datetime(2026, 8, 21)),  # Friday
            (6, datetime(2026, 8, 22)),  # Saturday
        ):
            spec = parse_cron(f"* * * * {cron_dow}")
            self.assertIsNotNone(spec, f"cron DOW {cron_dow}")
            self.assertTrue(spec.matches(weekday_dt), f"cron DOW {cron_dow} should match {weekday_dt}")

    def test_dow_wildcard_matches_monday(self):
        spec = parse_cron("* * * * *")
        self.assertIsNotNone(spec)
        monday = datetime(2026, 8, 17)  # Monday
        self.assertTrue(spec.matches(monday))

    def test_dow_wrong_day_does_not_match(self):
        spec = parse_cron("* * * * 1")  # Monday only
        self.assertIsNotNone(spec)
        tuesday = datetime(2026, 8, 18)
        sunday = datetime(2026, 8, 16)
        self.assertFalse(spec.matches(tuesday))
        self.assertFalse(spec.matches(sunday))

    def test_matches_negative(self):
        spec = parse_cron("0 9 15 1 *")
        self.assertFalse(spec.matches(datetime(2026, 2, 15, 9, 0)))
        self.assertFalse(spec.matches(datetime(2026, 1, 14, 9, 0)))
        self.assertFalse(spec.matches(datetime(2026, 1, 15, 8, 0)))

    def test_dom_dow_or_semantics(self):
        # cron spec: when BOTH day-of-month and day-of-week are restricted,
        # a match on EITHER fires (OR). 2026-08-15 is a Saturday, 2026-08-17 a Monday.
        spec = parse_cron("0 9 15 * 1")
        self.assertTrue(spec.matches(datetime(2026, 8, 15, 9, 0)))  # DOM 15
        self.assertTrue(spec.matches(datetime(2026, 8, 17, 9, 0)))  # Monday
        self.assertFalse(spec.matches(datetime(2026, 8, 16, 9, 0)))  # Sunday, neither


class ScheduleManagerTests(unittest.TestCase):
    """Registration, unregister, tick firing and in-flight guard."""

    def setUp(self):
        self.fired = []
        self.lock = threading.Lock()
        self.manager = ScheduleManager(callback=self._cb, tick_seconds=1)

    def _cb(self, schedule_id, entry):
        with self.lock:
            self.fired.append((schedule_id, entry.last_fired))

    def test_default_manager_callback_installed_late(self):
        # Regression: calling list first (callback=None) must not strand the
        # callback installed later by schedule_research.
        import agent.scheduler as sched

        sched._default_manager = None
        sched.default_manager()  # creates singleton WITHOUT callback
        manager = sched.default_manager(sched_cb := lambda sid, e: None)
        self.assertIsNotNone(manager._callback)  # callback retroactively installed
        sched._default_manager = None  # reset for other tests

    def test_default_manager_callback_not_overwritten(self):
        import agent.scheduler as sched

        sched._default_manager = None
        first_cb = lambda sid, e: None
        sched.default_manager(first_cb)
        manager = sched.default_manager(second_cb := lambda sid, e: None)
        self.assertIs(first_cb, manager._callback)  # existing callback kept
        sched._default_manager = None

    def test_register_and_list(self):
        entry = self.manager.register("s1", "*/5 * * * *", "objective", "rid-1")
        self.assertIsNotNone(entry)
        self.assertEqual(1, len(self.manager.list()))
        self.assertEqual("s1", self.manager.list()[0]["schedule_id"])

    def test_register_duplicate_rejected(self):
        self.assertIsNotNone(self.manager.register("s1", "*/5 * * * *", "obj", "rid"))
        self.assertIsNone(self.manager.register("s1", "0 0 * * *", "obj2", "rid2"))

    def test_register_invalid_cron(self):
        self.assertIsNone(self.manager.register("s1", "bad cron", "obj", "rid"))

    def test_unregister(self):
        self.manager.register("s1", "*/5 * * * *", "obj", "rid")
        self.assertTrue(self.manager.unregister("s1"))
        self.assertFalse(self.manager.unregister("s1"))
        self.assertEqual([], self.manager.list())

    def test_pause_resume(self):
        self.manager.register("s1", "* * * * *", "obj", "rid")
        self.assertTrue(self.manager.pause("s1"))
        self.assertEqual([], self.manager.tick(datetime(2026, 8, 14, 12, 30)))
        self.assertTrue(self.manager.resume("s1"))
        fired = self.manager.tick(datetime(2026, 8, 14, 12, 31))
        self.assertEqual(["s1"], fired)

    def test_tick_fires_on_match(self):
        import time

        self.manager.register("s1", "* * * * *", "obj", "rid")
        fired = self.manager.tick(datetime(2026, 8, 14, 12, 30))
        self.assertEqual(["s1"], fired)
        # callback runs in a spawned daemon thread; wait for it
        for _ in range(20):
            with self.lock:
                if len(self.fired) > 0:
                    break
            time.sleep(0.1)
        with self.lock:
            self.assertEqual(1, len(self.fired))
            self.assertEqual("s1", self.fired[0][0])

    def test_tick_skips_no_match(self):
        self.manager.register("s1", "0 9 * * *", "obj", "rid")
        self.assertEqual([], self.manager.tick(datetime(2026, 8, 14, 12, 30)))
        with self.lock:
            self.assertEqual([], self.fired)

    def test_callback_error_recorded(self):
        import time

        def boom(schedule_id, entry):
            raise RuntimeError("boom")

        m = ScheduleManager(callback=boom, tick_seconds=1)
        m.register("s1", "* * * * *", "obj", "rid")
        m.tick(datetime(2026, 8, 14, 12, 30))
        for _ in range(20):
            if m.get("s1").last_error is not None:
                break
            time.sleep(0.1)
        self.assertEqual("Error: boom", m.get("s1").last_error)

    def test_start_stop_ticker(self):
        self.manager.register("s1", "* * * * *", "obj", "rid")
        self.manager.start_ticker()
        # Let it spin a couple of ticks
        import time

        time.sleep(2.5)
        self.manager.stop_ticker()
        with self.lock:
            self.assertGreaterEqual(len(self.fired), 1)

    def test_in_flight_guard_bypassed_via_reregister(self):
        import time

        evt = threading.Event()

        def blocking_cb(sid, entry):
            evt.wait()
            with self.lock:
                self.fired.append(sid)

        m = ScheduleManager(callback=blocking_cb, tick_seconds=1)
        m.register("s1", "* * * * *", "obj", "rid")

        m.tick(datetime(2026, 8, 14, 12, 30))
        self.assertIn("s1", m._in_flight)

        m.unregister("s1")
        m.register("s1", "* * * * *", "obj", "rid")

        # Second tick should skip since s1 is still in-flight (guard survives unregister)
        m.tick(datetime(2026, 8, 14, 12, 31))

        evt.set()
        for _ in range(20):
            if "s1" not in m._in_flight:
                break
            time.sleep(0.1)

        with self.lock:
            self.assertEqual(1, len(self.fired))

        # Third tick fires since flight cleared
        m.tick(datetime(2026, 8, 14, 12, 32))
        for _ in range(20):
            with self.lock:
                if len(self.fired) == 2:
                    break
            time.sleep(0.1)
        with self.lock:
            self.assertEqual(2, len(self.fired))

    def test_same_minute_deduplication(self):
        import time

        m = ScheduleManager(callback=self._cb, tick_seconds=1)
        m.register("s1", "* * * * *", "obj", "rid")

        m.tick(datetime(2026, 8, 14, 12, 30, 0))

        for _ in range(20):
            if "s1" not in m._in_flight:
                break
            time.sleep(0.1)

        # Fire again in the same minute -> must be deduplicated
        m.tick(datetime(2026, 8, 14, 12, 30, 45))

        with self.lock:
            self.assertEqual(1, len(self.fired))


class ScheduleCallbackLoopTests(unittest.TestCase):
    """Integration: _schedule_callback runs research_run inside a live event loop."""

    def test_research_run_invoked_with_running_loop(self):
        """research_run must be called with a running event loop; .wait() must complete."""
        import asyncio
        import types

        from mcp_server import _schedule_callback

        loop_was_running = []

        async def fake_wait():
            pass

        def fake_research_run(research_id, objective, context):
            try:
                asyncio.get_running_loop()
                loop_was_running.append(True)
            except RuntimeError:
                loop_was_running.append(False)
            controller = types.SimpleNamespace(wait=fake_wait)
            return controller

        import mcp_server as ms

        original = ms.research_run
        ms.research_run = fake_research_run
        try:
            entry = types.SimpleNamespace(
                research_id="rid-test",
                objective="test objective",
                context=None,
            )
            _schedule_callback("sid-test", entry)
        finally:
            ms.research_run = original

        self.assertEqual([True], loop_was_running, "research_run must be called with a running event loop")

    def test_schedule_callback_wait_completes(self):
        """_schedule_callback must not raise when research_run and .wait() succeed."""
        import asyncio
        import types

        from mcp_server import _schedule_callback

        completed = []

        async def fake_wait():
            completed.append(True)

        def fake_research_run(research_id, objective, context):
            return types.SimpleNamespace(wait=fake_wait)

        import mcp_server as ms

        original = ms.research_run
        ms.research_run = fake_research_run
        try:
            entry = types.SimpleNamespace(
                research_id="rid-test",
                objective="test objective",
                context={"key": "val"},
            )
            _schedule_callback("sid-test", entry)
        finally:
            ms.research_run = original

        self.assertEqual([True], completed, ".wait() must have been awaited to completion")


class U4SchedulePersistenceTests(unittest.TestCase):
    def test_restart_preserves_pause_and_json_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "schedules.sqlite3"
            first = ScheduleManager(db_path=db)
            context = {"source": "restart", "nested": {"ok": True}}
            self.assertIsNotNone(first.register("s1", "* * * * *", "objective", "r1", context))
            self.assertTrue(first.pause("s1"))
            second = ScheduleManager(db_path=db)
            entry = second.get("s1")
            self.assertIsNotNone(entry)
            self.assertTrue(entry.paused)
            self.assertEqual(context, entry.context)

    def test_delete_and_resume_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "schedules.sqlite3"
            manager = ScheduleManager(db_path=db)
            self.assertIsNotNone(manager.register("s1", "* * * * *", "objective", "r1", {"a": 1}))
            self.assertTrue(manager.pause("s1"))
            self.assertTrue(manager.resume("s1"))
            self.assertFalse(ScheduleManager(db_path=db).get("s1").paused)
            self.assertTrue(manager.unregister("s1"))
            self.assertIsNone(ScheduleManager(db_path=db).get("s1"))

    def test_tick_state_and_next_manager_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "schedules.sqlite3"
            fired = []
            manager = ScheduleManager(callback=lambda sid, entry: fired.append(sid), db_path=db)
            manager.register("s1", "* * * * *", "objective", "r1", {"x": [1]})
            self.assertEqual(["s1"], manager.tick(datetime(2026, 1, 1, 0, 0)))
            manager.mark_done("s1")
            restored = ScheduleManager(db_path=db)
            entry = restored.get("s1")
            self.assertEqual("2026-01-01T00:00:00", entry.last_fired)
            self.assertEqual({"x": [1]}, entry.context)
            self.assertEqual([], restored.tick(datetime(2026, 1, 1, 0, 0)))
            self.assertEqual(["s1"], restored.tick(datetime(2026, 1, 1, 0, 1)))

    def test_callback_constructor_remains_in_memory(self):
        manager = ScheduleManager(lambda *_: None)
        self.assertFalse(manager.persistent)

if __name__ == "__main__":
    unittest.main()
