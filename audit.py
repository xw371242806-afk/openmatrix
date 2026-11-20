"""B2 append-only audit log (activity feed single-user edition).

Records notable server events (project/document/API-key operations, research
starts) so ``audit_list`` can surface a recent-activity feed. Backed by an
async single-writer SQLite store, consistent with the other stores.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

SCHEMA = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT,
        detail TEXT
    )
"""


def audit_db_path(db_path: Path | str | None = None) -> Path | str:
    if db_path is not None:
        p = Path(db_path)
        if p.suffix:
            return p
        return p / "audit.db"
    env = os.environ.get("OPENMATRIX_AUDIT_DB")
    if env:
        return Path(env)
    env_root = os.environ.get("OPENMATRIX_LIBRARY_ROOT")
    if env_root:
        return Path(env_root) / "audit.db"
    return Path(".openmatrix") / "audit.db"


class AuditLogStore:
    """Append-only async audit log."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = audit_db_path(db_path)
        self._conn: Any | None = None
        self._open_lock: asyncio.Lock | None = None

    async def _ensure_open(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._conn is None:
                import aiosqlite

                db = Path(self._db_path)
                db.parent.mkdir(parents=True, exist_ok=True)
                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.executescript(SCHEMA)
                await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def append(self, event: str, actor: str | None = None, detail: str | None = None) -> None:
        conn = await self._ensure_open()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        await conn.execute(
            "INSERT INTO audit_log (ts, event, actor, detail) VALUES (?, ?, ?, ?)",
            (ts, event, actor, detail),
        )
        await conn.commit()

    async def recent(self, limit: int = 20) -> list[dict]:
        conn = await self._ensure_open()
        cur = await conn.execute(
            "SELECT id, ts, event, actor, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


_default_audit: AuditLogStore | None = None


def default_audit_store(db_path: Path | str | None = None) -> AuditLogStore:
    global _default_audit
    if _default_audit is None:
        _default_audit = AuditLogStore(db_path=db_path)
    return _default_audit
