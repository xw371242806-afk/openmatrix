"""Scheduled / continuous research (H7).

Adds a cron-expression scheduler in front of ``research_run`` so research can
re-run on a cadence without blocking the MCP server. Pure standard library;
no external scheduler dependency (a full task queue is deferred to P4/P7).

Design:

- ``parse_cron(expr)`` -> ``CronSpec`` supporting the standard 5-field
  ``minute hour day-of-month month day-of-week`` syntax (``*``, ``*/n``,
  ``a-b``, ``a,b,c`` and lists). Field 4 accepts ``0``/``6`` for Sunday.
- ``CronSpec.matches(dt)`` -> whether a given datetime matches.
- ``ScheduleManager`` keeps registered schedules in a dict and runs a daemon
  loop thread (one tick per minute) that fires the callback for every due
  schedule. A schedule that is still running (previous research run not
  terminal) is skipped on the next tick to avoid overlapping runs.

The manager is importable and testable without starting the loop thread.
"""

from __future__ import annotations

import re
import contextlib
import json
import os
import sqlite3
from pathlib import Path
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

MINUTE_FIELD = 0
HOUR_FIELD = 1
DOM_FIELD = 2
MONTH_FIELD = 3
DOW_FIELD = 4

_TOKEN_RE = re.compile(r"^\*$|^\*/\d+$|^\d+(-\d+)?(,\d+(-\d+)?)*$")


def _expand_field(value: str, minimum: int, maximum: int) -> set[int] | None:
    """Expand one cron field into the set of matching values.

    Returns None for invalid syntax; ``*`` becomes the full range.
    """
    value = value.strip()
    if not _TOKEN_RE.match(value):
        return None
    if value == "*":
        return set(range(minimum, maximum + 1))
    if value.startswith("*/"):
        step = value[2:]
        if not step.isdigit() or int(step) < 1:
            return None
        step = int(step)
        return set(range(minimum, maximum + 1, step))
    if "," in value:
        out: set[int] = set()
        for part in value.split(","):
            expanded = _expand_field(part, minimum, maximum)
            if expanded is None:
                return None
            out |= expanded
        return out
    if "-" in value:
        lo_s, _, hi_s = value.partition("-")
        if not lo_s.isdigit() or not hi_s.isdigit():
            return None
        lo, hi = int(lo_s), int(hi_s)
        if lo > hi:
            return None
        if lo < minimum or hi > maximum:
            return None
        return set(range(lo, hi + 1))
    if value.isdigit():
        n = int(value)
        if n < minimum or n > maximum:
            return None
        return {n}
    return None


@dataclass(frozen=True)
class CronSpec:
    """A parsed 5-field cron expression."""

    minutes: frozenset[int]
    hours: frozenset[int]
    dom: frozenset[int]
    months: frozenset[int]
    dow: frozenset[int]
    dom_restricted: bool = False
    dow_restricted: bool = False

    @property
    def raw(self) -> str:
        parts = []
        for values in (self.minutes, self.hours, self.dom, self.months, self.dow):
            if not values:
                parts.append("?")
            elif len(values) == 60:
                parts.append("*")
            elif len(values) == 24:
                parts.append("*")
            elif len(values) == 31:
                parts.append("*")
            elif len(values) == 12:
                parts.append("*")
            elif len(values) == 7:
                parts.append("*")
            else:
                parts.append(",".join(str(v) for v in sorted(values)))
        return " ".join(parts)

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes:
            return False
        if dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False

        dom_match = dt.day in self.dom
        dow = dt.weekday()  # Monday=0 .. Sunday=6
        dow_match = dow in self.dow

        # Cron spec: when BOTH day-of-month and day-of-week are restricted
        # (not '*'), a match on EITHER is sufficient (OR semantics).
        if self.dom_restricted and self.dow_restricted:
            return dom_match or dow_match
        return dom_match and dow_match


def parse_cron(expr: str) -> CronSpec | None:
    """Parse a 5-field cron expression, or None if invalid.

    Field 4 (day-of-week) accepts 0..7 where 0/7 are Sunday. Field 3
    (day-of-month) uses 1..31.
    """
    if not expr or not isinstance(expr, str):
        return None
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    minutes = _expand_field(parts[0], 0, 59)
    hours = _expand_field(parts[1], 0, 23)
    dom = _expand_field(parts[2], 1, 31)
    months = _expand_field(parts[3], 1, 12)
    dow_raw = _expand_field(parts[4], 0, 7)
    if minutes is None or hours is None or dom is None or months is None or dow_raw is None:
        return None
    # Convert cron day-of-week (0/7 = Sunday, 1..6 = Monday..Saturday) to
    # Python weekday numbering (Monday=0 .. Sunday=6): (value - 1) % 7.
    dow_python = set()
    for value in dow_raw:
        if value == 0 or value == 7:
            dow_python.add(6)
        else:
            dow_python.add((value - 1) % 7)
    return CronSpec(
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        dom=frozenset(dom),
        months=frozenset(months),
        dow=frozenset(dow_python),
        dom_restricted=(parts[2] != "*"),
        dow_restricted=(parts[4] != "*"),
    )


@dataclass
class ScheduleEntry:
    """A registered schedule."""

    schedule_id: str
    cron: str
    objective: str
    research_id: str
    context: dict[str, Any] | None = None
    spec: CronSpec | None = None
    last_fired: str | None = None
    next_due: str | None = None
    last_error: str | None = None
    paused: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "cron": self.cron,
            "objective": self.objective,
            "research_id": self.research_id,
            "last_fired": self.last_fired,
            "next_due": self.next_due,
            "last_error": self.last_error,
            "paused": self.paused,
        }


def _format(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class ScheduleManager:
    """Registry + optional background ticker for cron schedules.

    ``start_ticker()`` launches a daemon thread that checks schedules every
    ``tick_seconds``. Each tick, due schedules (cron matches the current
    minute) are handed to ``callback(schedule_id, entry)``. Callback runs
    synchronously in the ticker thread; a long-running callback will delay the
    next tick but never overlaps itself because each entry only fires when its
    last launch is not in-flight (``_in_flight``).
    """

    def __init__(
        self,
        callback: Callable[[str, ScheduleEntry], None] | None = None,
        tick_seconds: int = 60,
        db_path: str | Path | None = None,
    ) -> None:
        self._callback = callback
        self._tick_seconds = tick_seconds
        self._db_path = str(db_path) if db_path else None
        self.persistent = self._db_path is not None
        self._entries: dict[str, ScheduleEntry] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._init_persistence()
        self._load_persistence()


    def _db_connect(self):
        # A fresh connection per operation avoids sqlite thread affinity errors.
        return sqlite3.connect(self._db_path, timeout=10)

    def _init_persistence(self) -> None:
        if not self._db_path:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._db_connect()) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id TEXT PRIMARY KEY, cron TEXT NOT NULL,
                    objective TEXT NOT NULL, research_id TEXT NOT NULL,
                    context_json TEXT, last_fired TEXT, next_due TEXT,
                    last_error TEXT, paused INTEGER NOT NULL DEFAULT 0
                )"""
            )
            conn.commit()

    def _load_persistence(self) -> None:
        if not self._db_path:
            return
        with contextlib.closing(self._db_connect()) as conn:
            rows = conn.execute(
                "SELECT schedule_id, cron, objective, research_id, context_json, "
                "last_fired, next_due, last_error, paused FROM schedules"
            ).fetchall()
        for row in rows:
            schedule_id, cron, objective, research_id, context_json, last_fired, next_due, last_error, paused = row
            spec = parse_cron(cron)
            if spec is None:
                continue
            try:
                context = json.loads(context_json) if context_json else None
            except (TypeError, ValueError):
                context = None
            self._entries[schedule_id] = ScheduleEntry(
                schedule_id=schedule_id, cron=cron, objective=objective,
                research_id=research_id, context=context, spec=spec,
                last_fired=last_fired, next_due=next_due,
                last_error=last_error, paused=bool(paused),
            )

    def _save_entry(self, entry: ScheduleEntry) -> None:
        if not self._db_path:
            return
        with contextlib.closing(self._db_connect()) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO schedules
                (schedule_id, cron, objective, research_id, context_json,
                 last_fired, next_due, last_error, paused)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry.schedule_id, entry.cron, entry.objective, entry.research_id,
                 json.dumps(entry.context, ensure_ascii=False, sort_keys=True)
                 if entry.context is not None else None,
                 entry.last_fired, entry.next_due, entry.last_error, int(entry.paused)),
            )
            conn.commit()

    def _delete_entry(self, schedule_id: str) -> None:
        if not self._db_path:
            return
        with contextlib.closing(self._db_connect()) as conn:
            conn.execute("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
            conn.commit()

    def register(
        self,
        schedule_id: str,
        cron: str,
        objective: str,
        research_id: str,
        context: dict[str, Any] | None = None,
    ) -> ScheduleEntry | None:
        spec = parse_cron(cron)
        if spec is None:
            return None
        entry = ScheduleEntry(
            schedule_id=schedule_id,
            cron=cron,
            objective=objective,
            research_id=research_id,
            context=context,
            spec=spec,
        )
        with self._lock:
            if schedule_id in self._entries:
                return None
            self._entries[schedule_id] = entry
            try:
                self._save_entry(entry)
            except Exception:
                del self._entries[schedule_id]
                raise
        return entry

    def unregister(self, schedule_id: str) -> bool:
        with self._lock:
            if schedule_id not in self._entries:
                return False
            entry = self._entries.pop(schedule_id)
            try:
                self._delete_entry(schedule_id)
            except Exception:
                self._entries[schedule_id] = entry
                raise
        return True

    def pause(self, schedule_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(schedule_id)
            if entry is None:
                return False
            previous = entry.paused
            entry.paused = True
            try:
                self._save_entry(entry)
            except Exception:
                entry.paused = previous
                raise
        return True

    def resume(self, schedule_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(schedule_id)
            if entry is None:
                return False
            previous = entry.paused
            entry.paused = False
            try:
                self._save_entry(entry)
            except Exception:
                entry.paused = previous
                raise
        return True

    def get(self, schedule_id: str) -> ScheduleEntry | None:
        with self._lock:
            return self._entries.get(schedule_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e.summary() for e in self._entries.values()]

    def tick(self, now: datetime | None = None) -> list[str]:
        """Check all schedules once against ``now``; fire due ones.

        Returns the list of schedule_ids that fired this tick. In-flight
        schedules (a research run started previously and not yet finished,
        tracked via ``mark_done``) are skipped. Callbacks run in their own
        daemon thread so a long-running research run never stalls the ticker.
        """
        now = now or datetime.now()
        now_str = _format(now)
        fired: list[str] = []
        to_fire: list[ScheduleEntry] = []
        with self._lock:
            for entry in list(self._entries.values()):
                if entry.paused:
                    continue
                if entry.spec is None or not entry.spec.matches(now):
                    continue
                if (
                    now_str is not None
                    and entry.last_fired is not None
                    and entry.last_fired[:16] == now_str[:16]
                ):
                    continue
                if entry.schedule_id in self._in_flight:
                    continue
                previous = (entry.last_fired, entry.next_due, entry.last_error)
                self._in_flight.add(entry.schedule_id)
                entry.last_fired = now_str
                entry.next_due = None
                entry.last_error = None
                try:
                    self._save_entry(entry)
                except Exception:
                    entry.last_fired, entry.next_due, entry.last_error = previous
                    self._in_flight.discard(entry.schedule_id)
                    continue
                fired.append(entry.schedule_id)
                to_fire.append(entry)

        def run_callback(entry: ScheduleEntry) -> None:
            try:
                if self._callback is not None:
                    self._callback(entry.schedule_id, entry)
            except Exception as exc:  # noqa: BLE001 - keep the ticker alive
                with self._lock:
                    if self._entries.get(entry.schedule_id) is entry:
                        entry.last_error = f"Error: {exc}"
                        try:
                            self._save_entry(entry)
                        except Exception:
                            pass
            finally:
                self.mark_done(entry.schedule_id)

        for entry in to_fire:
            threading.Thread(
                target=run_callback,
                args=(entry,),
                name=f"schedule-cb-{entry.schedule_id}",
                daemon=True,
            ).start()

        return fired

    def mark_done(self, schedule_id: str) -> None:
        with self._lock:
            self._in_flight.discard(schedule_id)

    def start_ticker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._ticker_loop,
            name="schedule-manager-ticker",
            daemon=True,
        )
        self._thread.start()

    def _ticker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never crash the ticker
                pass
            self._stop.wait(self._tick_seconds)

    def stop_ticker(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


_default_manager: ScheduleManager | None = None


def _schedule_persistence_path() -> str | None:
    raw = os.getenv("OPENMATRIX_SCHEDULES_PERSIST", "true").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return None
    data_dir = Path(os.getenv("OPENMATRIX_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".openmatrix")))
    return str(data_dir / "schedules.sqlite3")


def default_manager(
    callback: Callable[[str, ScheduleEntry], None] | None = None,
) -> ScheduleManager:
    """Return the persistent default manager; explicit ScheduleManager remains in-memory."""
    global _default_manager
    if _default_manager is None:
        _default_manager = ScheduleManager(callback=callback, db_path=_schedule_persistence_path())
    elif callback is not None and _default_manager._callback is None:  # noqa: SLF001
        _default_manager._callback = callback  # noqa: SLF001
    return _default_manager
