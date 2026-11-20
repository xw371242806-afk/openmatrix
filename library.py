"""P4 document-library persistence layer (SQLite metadata + local disk + vectors).

Zero-external-dependency design (per P4-1 decision): metadata lives in an async
single-writer SQLite store (``ProjectLibraryStore``, same pattern as
``AiosqliteResultStore``), raw document content lives on the local disk under
``OPENMATRIX_LIBRARY_ROOT`` (default ``.openmatrix/library``), and vectors are
handled by the existing ``VectorStoreAdapter`` (Qdrant in-memory/local file or
the built-in ``InMemoryVectorStore`` fallback).

Schema (all list/report oriented - never grid/cell, patent guardrail):

- ``projects``: id, name, description, created_at
- ``documents``: id, project_id, filename, content_path, page_count, status,
  created_at (content stored on disk, not in DB)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def library_root(db_path: Path | str | None = None) -> Path:
    """Resolve the P4 library root directory.

    ``db_path`` may be a full path to a SQLite file (its parent is used) or a
    directory. Falls back to ``OPENMATRIX_LIBRARY_ROOT`` env var, then
    ``.openmatrix/library`` under the current working directory.
    """
    if db_path is not None:
        p = Path(db_path)
        if p.suffix:
            return p.parent
        return p
    env = os.environ.get("OPENMATRIX_LIBRARY_ROOT")
    if env:
        return Path(env)
    return Path(".openmatrix") / "library"


def library_db_path(db_path: Path | str | None = None) -> Path | str:
    """Resolve the SQLite database file path for the library store."""
    if db_path is not None:
        p = Path(db_path)
        if p.suffix == ".db" or p.suffix == ".sqlite" or p.suffix == ".sqlite3":
            return p
        return p / "library.db"
    env = os.environ.get("OPENMATRIX_LIBRARY_DB")
    if env:
        return Path(env)
    return library_root(None) / "library.db"


class ProjectLibraryStore:
    """Async single-writer metadata store for projects/documents (P4-1).

    Mirrors ``AiosqliteResultStore``: one background connection, all writes
    serialized through it, ``CREATE TABLE IF NOT EXISTS`` idempotent schema.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content_path TEXT NOT NULL,
            page_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'uploaded',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_documents_project ON documents (project_id);
        CREATE TABLE IF NOT EXISTS document_versions (
            doc_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            filename TEXT,
            content_path TEXT NOT NULL,
            page_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (doc_id, version)
        );
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = library_db_path(db_path)
        self._root = library_root(db_path)
        self._conn: Any | None = None
        self._open_lock: asyncio.Lock | None = None
        self._thread_lock = threading.Lock()
        self._loop_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._locks_guard = threading.Lock()

    def _get_loop_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._locks_guard:
            lock = self._loop_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._loop_locks[loop] = lock
            return lock

    @property
    def root(self) -> Path:
        return self._root

    async def _ensure_open(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._conn is None:
                import aiosqlite

                self._root.mkdir(parents=True, exist_ok=True)
                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.executescript(self.SCHEMA)
                columns = await self._conn.execute("PRAGMA table_info(documents)")
                names = {row[1] for row in await columns.fetchall()}
                if "provenance_json" not in names:
                    try:
                        await self._conn.execute(
                            "ALTER TABLE documents ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'"
                        )
                    except Exception as exc:
                        # A concurrent store may have won the additive migration.
                        if "duplicate column name" not in str(exc).lower():
                            raise
                await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- projects ----

    async def create_project(self, project_id: str, name: str, description: str | None = None) -> dict:
        if not _SAFE_ID_RE.match(project_id):
            raise ValueError(f"invalid project_id {project_id!r}: use [a-zA-Z0-9_-]")
        if not name or not name.strip():
            raise ValueError("name must be non-empty")
        loop_lock = self._get_loop_lock()
        async with loop_lock:
            with self._thread_lock:
                conn = await self._ensure_open()
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                existing = await self.get_project(project_id)
                if existing is not None:
                    raise ValueError(f"project already exists: {project_id}")
                await conn.execute(
                    "INSERT INTO projects (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                    (project_id, name.strip(), description, now),
                )
                await conn.commit()
                return {"id": project_id, "name": name.strip(), "description": description, "created_at": now}

    async def get_project(self, project_id: str) -> dict | None:
        conn = await self._ensure_open()
        cur = await conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_projects(self) -> list[dict]:
        conn = await self._ensure_open()
        cur = await conn.execute("SELECT * FROM projects ORDER BY created_at")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_project(self, project_id: str) -> bool:
        loop_lock = self._get_loop_lock()
        async with loop_lock:
            with self._thread_lock:
                conn = await self._ensure_open()
                existing = await self.get_project(project_id)
                if existing is None:
                    return False
                # remove version history rows for all project documents (files are
                # removed with the project directory below)
                cur = await conn.execute("SELECT id FROM documents WHERE project_id = ?", (project_id,))
                doc_ids = [dict(r)["id"] for r in await cur.fetchall()]
                for doc_id in doc_ids:
                    await conn.execute("DELETE FROM document_versions WHERE doc_id = ?", (doc_id,))
                await conn.execute("DELETE FROM documents WHERE project_id = ?", (project_id,))
                await conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                await conn.commit()
                # best-effort cleanup of the project's disk directory
                proj_dir = self._root / project_id
                if proj_dir.is_dir():
                    import shutil

                    shutil.rmtree(proj_dir, ignore_errors=True)
                return True

    # ---- documents ----

    async def add_document(
        self,
        doc_id: str,
        project_id: str,
        filename: str,
        content: str,
        page_count: int = 0,
        provenance: dict[str, str] | None = None,
    ) -> dict:
        if not _SAFE_ID_RE.match(doc_id):
            raise ValueError(f"invalid doc_id {doc_id!r}: use [a-zA-Z0-9_-]")
        if not _SAFE_ID_RE.match(project_id):
            raise ValueError(f"invalid project_id {project_id!r}: use [a-zA-Z0-9_-]")
        loop_lock = self._get_loop_lock()
        async with loop_lock:
            with self._thread_lock:
                if await self.get_project(project_id) is None:
                    raise ValueError(f"project not found: {project_id}")
                if await self.get_document(doc_id) is not None:
                    raise ValueError(f"document already exists: {doc_id}")
                conn = await self._ensure_open()
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                proj_dir = self._root / project_id
                proj_dir.mkdir(parents=True, exist_ok=True)
                content_path = proj_dir / f"{doc_id}.txt"
                content_path.write_text(content, encoding="utf-8")
                provenance_json = json.dumps(provenance or {}, sort_keys=True)
                await conn.execute(
                    "INSERT INTO documents (id, project_id, filename, content_path, page_count, status, provenance_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'uploaded', ?, ?)",
                    (doc_id, project_id, filename, str(content_path), page_count, provenance_json, now),
                )
                await conn.commit()
                return {
                    "id": doc_id,
                    "project_id": project_id,
                    "filename": filename,
                    "content_path": str(content_path),
                    "page_count": page_count,
                    "status": "uploaded",
                    "provenance": provenance or {},
                    "created_at": now,
                }

    async def get_document(self, doc_id: str) -> dict | None:
        conn = await self._ensure_open()
        cur = await conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        document = dict(row)
        document["provenance"] = json.loads(document.pop("provenance_json", "{}"))
        return document

    async def update_document(
        self,
        doc_id: str,
        filename: str,
        content: str,
        page_count: int = 0,
    ) -> dict:
        """Versioned overwrite of a document (P4.4).

        Snapshots the current content into `document_versions` (v+1) before
        replacing it, so every state is recoverable. Per-store calls are
        serialized by an `asyncio.Lock`; cross-store/process serialization
        comes from `BEGIN IMMEDIATE`. Snapshot files are created exclusively
        (never overwritten), and failures roll back the DB and remove partial
        files.
        """
        if not _SAFE_ID_RE.match(doc_id):
            raise ValueError(f"invalid doc_id {doc_id!r}: use [a-zA-Z0-9_-]")
        loop_lock = self._get_loop_lock()
        async with loop_lock:
            with self._thread_lock:
                conn = None
                snap_created = False
                snap_path: Path | None = None
                tmp_main: Path | None = None
                old_content = None
                content_path: Path | None = None
                try:
                    conn = await self._ensure_open()
                    await conn.execute("BEGIN IMMEDIATE")
                    cur = await conn.execute(
                        "SELECT id, project_id, filename, content_path, page_count, provenance_json, created_at FROM documents WHERE id = ?",
                        (doc_id,),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        raise ValueError(f"document not found: {doc_id}")
                    existing = dict(row)

                    now = time.strftime("%Y-%m-%dT%H:%M:%S")
                    cur = await conn.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM document_versions WHERE doc_id = ?", (doc_id,)
                    )
                    row_v = await cur.fetchone()
                    next_version = (row_v[0] if row_v else 0) + 1

                    proj_dir = self._root / existing["project_id"]
                    proj_dir.mkdir(parents=True, exist_ok=True)
                    snap_path = proj_dir / f"{doc_id}.v{next_version}.txt"
                    old_content = Path(existing["content_path"]).read_text(encoding="utf-8")

                    # exclusive create: never overwrite an existing snapshot
                    fd = os.open(snap_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(old_content)
                    snap_created = True

                    content_path = proj_dir / f"{doc_id}.txt"
                    tmp_main = proj_dir / f".{doc_id}.{uuid.uuid4().hex}.tmp"
                    tmp_main.write_text(content, encoding="utf-8")

                    await conn.execute(
                        "INSERT INTO document_versions (doc_id, version, filename, content_path, page_count, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (doc_id, next_version, existing["filename"], str(snap_path), existing["page_count"], now),
                    )
                    await conn.execute(
                        "UPDATE documents SET filename = ?, content_path = ?, page_count = ?, status = 'uploaded'"
                        " WHERE id = ?",
                        (filename, str(content_path), page_count, doc_id),
                    )
                    tmp_main.replace(content_path)
                    tmp_main = None
                    await conn.commit()
                except BaseException:
                    if conn is not None:
                        try:
                            await conn.rollback()
                        except BaseException:
                            pass
                    if tmp_main is None and snap_created and snap_path is not None and content_path is not None and old_content is not None:
                        try:
                            content_path.write_text(old_content, encoding="utf-8")
                        except BaseException:
                            pass
                    if snap_created and snap_path is not None and snap_path.exists():
                        snap_path.unlink(missing_ok=True)
                    if tmp_main is not None and tmp_main.exists():
                        tmp_main.unlink(missing_ok=True)
                    raise

        return {
            "id": doc_id,
            "project_id": existing["project_id"],
            "filename": filename,
            "content_path": str(content_path),
            "page_count": page_count,
            "status": "uploaded",
            "provenance": json.loads(existing.get("provenance_json", "{}")),
            "created_at": existing["created_at"],
            "version": next_version,
        }

    async def list_versions(self, doc_id: str) -> list[dict]:
        if not _SAFE_ID_RE.match(doc_id):
            raise ValueError(f"invalid doc_id {doc_id!r}: use [a-zA-Z0-9_-]")
        conn = await self._ensure_open()
        cur = await conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,))
        if await cur.fetchone() is None:
            raise ValueError(f"document not found: {doc_id}")
        cur = await conn.execute(
            "SELECT version, filename, content_path, page_count, created_at FROM document_versions"
            " WHERE doc_id = ? ORDER BY version",
            (doc_id,),
        )
        rows = await cur.fetchall()
        documents = []
        for row in rows:
            document = dict(row)
            document["provenance"] = json.loads(document.pop("provenance_json", "{}"))
            documents.append(document)
        return documents

    async def restore_version(self, doc_id: str, version: int) -> dict | None:
        """Restore a document to a prior snapshot (P4.4). Returns new doc state."""
        if not _SAFE_ID_RE.match(doc_id):
            raise ValueError(f"invalid doc_id {doc_id!r}: use [a-zA-Z0-9_-]")
        existing = await self.get_document(doc_id)
        if existing is None:
            raise ValueError(f"document not found: {doc_id}")
        conn = await self._ensure_open()
        cur = await conn.execute(
            "SELECT filename, content_path, page_count FROM document_versions"
            " WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        snap = dict(row)
        content = Path(snap["content_path"]).read_text(encoding="utf-8")
        return await self.update_document(doc_id, snap["filename"], content, page_count=snap["page_count"])

    async def list_documents(self, project_id: str | None = None) -> list[dict]:
        conn = await self._ensure_open()
        if project_id:
            cur = await conn.execute(
                "SELECT * FROM documents WHERE project_id = ? ORDER BY created_at", (project_id,)
            )
        else:
            cur = await conn.execute("SELECT * FROM documents ORDER BY created_at")
        rows = await cur.fetchall()
        documents = []
        for row in rows:
            document = dict(row)
            document["provenance"] = json.loads(document.pop("provenance_json", "{}"))
            documents.append(document)
        return documents

    async def delete_document(self, doc_id: str) -> bool:
        loop_lock = self._get_loop_lock()
        async with loop_lock:
            with self._thread_lock:
                conn = await self._ensure_open()
                existing = await self.get_document(doc_id)
                if existing is None:
                    return False
                # collect version snapshot paths before deleting rows
                cur = await conn.execute(
                    "SELECT content_path FROM document_versions WHERE doc_id = ?", (doc_id,)
                )
                version_paths = [dict(r)["content_path"] for r in await cur.fetchall()]
                await conn.execute("DELETE FROM document_versions WHERE doc_id = ?", (doc_id,))
                await conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                await conn.commit()
                # remove primary + snapshot files (best effort)
                content_path = Path(existing["content_path"])
                try:
                    if content_path.is_file():
                        content_path.unlink()
                except OSError:
                    pass
                for vp in version_paths:
                    try:
                        p = Path(vp)
                        if p.is_file():
                            p.unlink()
                    except OSError:
                        pass
                return True

    async def get_document_content(self, doc_id: str) -> str | None:
        existing = await self.get_document(doc_id)
        if existing is None:
            return None
        content_path = Path(existing["content_path"])
        if not content_path.is_file():
            return None
        return content_path.read_text(encoding="utf-8")

    async def update_document_status(self, doc_id: str, status: str) -> bool:
        conn = await self._ensure_open()
        existing = await self.get_document(doc_id)
        if existing is None:
            return False
        await conn.execute("UPDATE documents SET status = ? WHERE id = ?", (status, doc_id))
        await conn.commit()
        return True


_default_store: Any = None


def default_library_store(db_path: Path | str | None = None) -> Any:
    """Return the document-library store.

    When ``db_path`` is None the configured singleton backend is used (see
    ``storage.get_metadata_store``; env ``OPENMATRIX_METADATA_ENGINE``,
    default SQLite). An explicit ``db_path`` ALWAYS returns a fresh isolated
    store (never the singleton), so tests can rely on isolation.
    """
    global _default_store
    if db_path is not None:
        return ProjectLibraryStore(db_path=db_path)
    if _default_store is None:
        from storage import get_metadata_store

        _default_store = get_metadata_store()
    return _default_store
