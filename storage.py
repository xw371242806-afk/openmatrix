"""P4.1 production storage abstraction (metadata + object store).

Defines narrow Protocols over the document library:

- ``MetadataStore``: project/document/version CRUD. The default implementation is
  ``ProjectLibraryStore`` (SQLite + local disk); a ``PostgresMetadataStore``
  (psycopg async, optional dependency) is provided for production deployments.
- ``ObjectStore``: content blob storage. ``LocalObjectStore`` (disk) is the
  default; ``S3ObjectStore`` (boto3, optional) targets MinIO/S3.

Factories select the backend from env and degrade gracefully when the optional
dependency or a configured service is unavailable (same pattern as the
retrieval adapters):

- ``OPENMATRIX_METADATA_ENGINE``: "sqlite" (default) | "postgres"
- ``OPENMATRIX_PG_DSN``: postgres://user:pass@host/db for the PG engine
- ``OPENMATRIX_OBJECT_STORE``: "local" (default) | "s3"
- ``OPENMATRIX_S3_*``: endpoint/region/bucket/credentials for the S3 engine
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ObjectStore(Protocol):
    """Blob storage for document content."""

    def put(self, key: str, data: bytes) -> None:
        ...

    def get(self, key: str) -> bytes | None:
        ...

    def delete(self, key: str) -> bool:
        ...


@runtime_checkable
class MetadataStore(Protocol):
    """Project/document/version CRUD (P4 document library metadata)."""

    async def create_project(self, project_id: str, name: str, description: str | None = None) -> dict:
        ...

    async def get_project(self, project_id: str) -> dict | None:
        ...

    async def list_projects(self) -> list[dict]:
        ...

    async def delete_project(self, project_id: str) -> bool:
        ...

    async def add_document(self, doc_id: str, project_id: str, filename: str, content: str, page_count: int = 0, provenance: dict[str, str] | None = None) -> dict:
        ...

    async def get_document(self, doc_id: str) -> dict | None:
        ...

    async def list_documents(self, project_id: str | None = None) -> list[dict]:
        ...

    async def delete_document(self, doc_id: str) -> bool:
        ...

    async def get_document_content(self, doc_id: str) -> str | None:
        ...

    async def update_document_status(self, doc_id: str, status: str) -> bool:
        ...

    async def update_document(self, doc_id: str, filename: str, content: str, page_count: int = 0) -> dict:
        ...

    async def list_versions(self, doc_id: str) -> list[dict]:
        ...

    async def restore_version(self, doc_id: str, version: int) -> dict | None:
        ...


# ---------------------------------------------------------------------------
# Object stores
# ---------------------------------------------------------------------------


class LocalObjectStore:
    """Disk-backed object store under a root directory."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        # confine keys to the root; reject traversal (sibling-prefix safe)
        p = (self._root / key).resolve()
        if not p.is_relative_to(self._root.resolve()):
            raise ValueError(f"invalid object key {key!r}")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        if not p.is_file():
            return None
        return p.read_bytes()

    def delete(self, key: str) -> bool:
        p = self._path(key)
        if not p.is_file():
            return False
        p.unlink()
        return True


class S3ObjectStore:
    """MinIO/S3-backed object store (boto3, optional dependency).

    Configuration via env: ``OPENMATRIX_S3_ENDPOINT``, ``OPENMATRIX_S3_REGION``,
    ``OPENMATRIX_S3_BUCKET``, ``OPENMATRIX_S3_ACCESS_KEY``,
    ``OPENMATRIX_S3_SECRET_KEY``.
    """

    def __init__(self, bucket: str | None = None) -> None:
        import boto3  # optional; ImportError surfaces at construction

        self._bucket = bucket or os.environ.get("OPENMATRIX_S3_BUCKET")
        if not self._bucket:
            raise ValueError("S3 bucket not configured (OPENMATRIX_S3_BUCKET)")
        self._client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("OPENMATRIX_S3_ENDPOINT"),
            region_name=os.environ.get("OPENMATRIX_S3_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("OPENMATRIX_S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("OPENMATRIX_S3_SECRET_KEY"),
        )

    def health_check(self) -> bool:
        """Confirm the bucket is reachable (head_bucket)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_bucket(self) -> bool:
        """Create the configured bucket if it does not exist (idempotent).

        Distinguishes a missing bucket (creates it) from authorization/network
        errors (returns False). A concurrent creator winning the race is treated
        as success, as are already-exists errors from the create call.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
            if code and code not in ("404", "NoSuchBucket", "NoSuchKey", "NotFound"):
                return False  # auth / network / other error: not a missing bucket
            # Missing bucket - fall through to create. A missing Error code
            # (e.g. some providers raise without a response body) is treated
            # as possibly-missing, so the create attempt decides.
        try:
            kwargs = {"Bucket": self._bucket}
            region = os.environ.get("OPENMATRIX_S3_REGION", "us-east-1")
            if region and region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            self._client.create_bucket(**kwargs)
            return True
        except Exception as exc:  # noqa: BLE001
            # AlreadyOwnedByYou / BucketAlreadyExists: another creator won the race.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists", "AlreadyOwnedByYou"):
                return self.health_check()
            return False

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes | None:
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=key)
            return obj["Body"].read()
        except Exception:  # noqa: BLE001 - missing object / network
            return None

    def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Metadata store: PostgreSQL (optional)
# ---------------------------------------------------------------------------

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_path TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploaded',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents (project_id);
CREATE TABLE IF NOT EXISTS document_versions (
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    filename TEXT,
    content_path TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (doc_id, version)
);
"""



class _ReentrantAsyncLock:
    """asyncio lock that the owning task may re-acquire (nesting-safe)."""

    def __init__(self) -> None:
        import asyncio

        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self):
        import asyncio

        current = asyncio.current_task()
        if self._owner is current:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = current
        self._depth = 1
        return self

    async def __aexit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
        return False


class PostgresMetadataStore:
    """PostgreSQL metadata store (psycopg async, optional).

    Content blobs are managed via the configured ObjectStore (LocalObjectStore
    by default or S3ObjectStore).
    Every mutating/read operation runs inside an explicit transaction scope so
    a SQL error never leaves the singleton connection in
    ``InFailedSqlTransaction``; versioned updates snapshot previous versions
    and roll back object writes on failure.
    """

    def __init__(self, dsn: str | None = None, root: Path | str | None = None,
                 object_store: "ObjectStore" | None = None) -> None:
        import psycopg  # optional; ImportError surfaces at construction

        self._dsn = dsn or os.environ.get("OPENMATRIX_PG_DSN")
        if not self._dsn:
            raise ValueError("PG DSN not configured (OPENMATRIX_PG_DSN)")
        self._root = Path(root) if root else Path(os.environ.get("OPENMATRIX_LIBRARY_ROOT", ".openmatrix/library"))
        self._conn = None
        self._lock: Any = None
        self._objects = object_store if object_store is not None else get_object_store()

    @staticmethod
    def _doc_key(project_id: str, doc_id: str, version: "int | None" = None, unique: str | None = None) -> str:
        """Object key for a document blob. `unique` makes the key writer-unique
        (cross-process publish safety); `version` addresses a snapshot."""
        if version is not None:
            uid = unique or uuid.uuid4().hex[:12]
            return f"{project_id}/{doc_id}.{uid}.v{version}.txt"
        if unique:
            return f"{project_id}/{doc_id}.{unique}.txt"
        return f"{project_id}/{doc_id}.txt"

    def _get_lock(self):
        import asyncio

        if self._lock is None:
            self._lock = _ReentrantAsyncLock()
        return self._lock

    @staticmethod
    def _serialized(func):
        """Serialize the wrapped async method under the (reentrant) store lock."""
        import functools

        @functools.wraps(func)
        async def wrapper(self, *a, **kw):
            async with self._get_lock():
                return await func(self, *a, **kw)
        return wrapper

    async def health_check(self) -> bool:
        """Connect + run a trivial query; never retains a connection.

        Uses its own short-lived connection so no loop-bound handle leaks
        into the store (safe to call from a temporary loop).
        """
        try:
            import psycopg

            conn = await psycopg.AsyncConnection.connect(self._dsn)
            try:
                async with conn.transaction():
                    await conn.execute("SELECT 1")
            finally:
                await conn.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _ensure_conn(self):
        if self._conn is None:
            import psycopg

            schema_conn = await psycopg.AsyncConnection.connect(self._dsn)
            try:
                async with schema_conn.transaction():
                    # Schema DDL runs on a short-lived connection before the
                    # business connection is exposed. This prevents a fresh
                    # writer from holding row locks while another fresh writer
                    # waits for an AccessExclusiveLock during lazy migration.
                    await schema_conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('openmatrix.metadata.schema.v1'))"
                    )
                    await schema_conn.execute(_PG_SCHEMA)
                    await schema_conn.execute(
                        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS provenance_json TEXT NOT NULL DEFAULT '{}'"
                    )
            finally:
                await schema_conn.close()

            self._conn = await psycopg.AsyncConnection.connect(self._dsn)
        return self._conn

    @_serialized
    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _obj_put(self, key: str, data: bytes) -> None:
        import asyncio

        await asyncio.to_thread(self._objects.put, key, data)

    async def _obj_get(self, key: str) -> bytes | None:
        import asyncio

        return await asyncio.to_thread(self._objects.get, key)

    async def _obj_delete(self, key: str) -> bool:
        import asyncio

        try:
            return await asyncio.to_thread(self._objects.delete, key)
        except Exception:  # noqa: BLE001 - object cleanup is best-effort
            return False

    # projects
    @_serialized
    async def create_project(self, project_id: str, name: str, description: str | None = None) -> dict:
        import time

        conn = await self._ensure_conn()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO projects (id, name, description, created_at) VALUES (%s, %s, %s, %s)",
                (project_id, name.strip(), description, now),
            )
        return {"id": project_id, "name": name.strip(), "description": description, "created_at": now}

    @_serialized
    async def get_project(self, project_id: str) -> dict | None:
        conn = await self._ensure_conn()
        async with conn.transaction():
            cur = await conn.execute("SELECT id, name, description, created_at FROM projects WHERE id = %s", (project_id,))
            row = await cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "description": row[2], "created_at": row[3]}

    @_serialized
    async def list_projects(self) -> list[dict]:
        conn = await self._ensure_conn()
        async with conn.transaction():
            cur = await conn.execute("SELECT id, name, description, created_at FROM projects ORDER BY created_at")
            rows = await cur.fetchall()
        return [{"id": r[0], "name": r[1], "description": r[2], "created_at": r[3]} for r in rows]

    async def delete_project(self, project_id: str) -> bool:
        async with self._get_lock():
            conn = await self._ensure_conn()
            async with conn.transaction():
                cur = await conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
                if await cur.fetchone() is None:
                    return False
                cur = await conn.execute(
                    "SELECT content_path FROM documents WHERE project_id = %s", (project_id,))
                keys = [r[0] for r in await cur.fetchall()]
                cur = await conn.execute("SELECT id FROM documents WHERE project_id = %s", (project_id,))
                doc_ids = [r[0] for r in await cur.fetchall()]
                for doc_id in doc_ids:
                    cur = await conn.execute(
                        "SELECT content_path FROM document_versions WHERE doc_id = %s", (doc_id,))
                    keys.extend(r[0] for r in await cur.fetchall())
                    await conn.execute("DELETE FROM document_versions WHERE doc_id = %s", (doc_id,))
                await conn.execute("DELETE FROM documents WHERE project_id = %s", (project_id,))
                await conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        for k in keys:
            await self._obj_delete(k)
        return True

    # documents
    async def add_document(self, doc_id: str, project_id: str, filename: str, content: str, page_count: int = 0, provenance: dict[str, str] | None = None) -> dict:
        """Add a document (blob via the configured ObjectStore).

        The blob is stored under a writer-unique key BEFORE the FK-enforced
        row commits, so a committed row never references missing content and
        a losing racer never touches the winner's blob. On row-commit failure
        only this writer's key is deleted.
        """
        import json
        import time

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        unique = uuid.uuid4().hex[:12]
        key = self._doc_key(project_id, doc_id, unique=unique)

        async with self._get_lock():
            conn = await self._ensure_conn()
            # validate inside a transaction (FK enforced at insert)
            async with conn.transaction():
                cur = await conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
                if await cur.fetchone() is None:
                    raise ValueError(f"project not found: {project_id}")
                cur = await conn.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
                if await cur.fetchone() is not None:
                    raise ValueError(f"document already exists: {doc_id}")

        # stage blob (writer-unique key) outside the lock before committing any row
        staged = False
        try:
            await self._obj_put(key, content.encode("utf-8"))
            staged = True
            async with self._get_lock():
                conn = await self._ensure_conn()
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO documents (id, project_id, filename, content_path, page_count, status, provenance_json, created_at)"
                        " VALUES (%s, %s, %s, %s, %s, 'uploaded', %s, %s)",
                        (doc_id, project_id, filename, key, page_count, json.dumps(provenance or {}, sort_keys=True), now),
                    )
        except Exception:
            if staged:
                await self._obj_delete(key)
            raise

        return {"id": doc_id, "project_id": project_id, "filename": filename,
                 "content_path": key, "page_count": page_count,
                 "status": "uploaded", "provenance": provenance or {}, "created_at": now}

    @_serialized
    async def get_document(self, doc_id: str) -> dict | None:
        conn = await self._ensure_conn()
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at FROM documents WHERE id = %s",
                (doc_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        import json
        return {"id": row[0], "project_id": row[1], "filename": row[2], "content_path": row[3],
                "page_count": row[4], "status": row[5], "provenance": json.loads(row[6] or "{}"), "created_at": row[7]}

    @_serialized
    async def list_documents(self, project_id: str | None = None) -> list[dict]:
        conn = await self._ensure_conn()
        async with conn.transaction():
            if project_id:
                cur = await conn.execute(
                    "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at"
                    " FROM documents WHERE project_id = %s ORDER BY created_at", (project_id,))
            else:
                cur = await conn.execute(
                    "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at"
                    " FROM documents ORDER BY created_at")
            rows = await cur.fetchall()
        import json
        return [{"id": r[0], "project_id": r[1], "filename": r[2], "content_path": r[3],
                  "page_count": r[4], "status": r[5], "provenance": json.loads(r[6] or "{}"), "created_at": r[7]} for r in rows]

    async def delete_document(self, doc_id: str) -> bool:
        async with self._get_lock():
            conn = await self._ensure_conn()
            async with conn.transaction():
                cur = await conn.execute("SELECT content_path FROM documents WHERE id = %s", (doc_id,))
                doc_row = await cur.fetchone()
                if doc_row is None:
                    return False
                existing_key = doc_row[0]
                cur = await conn.execute(
                    "SELECT content_path FROM document_versions WHERE doc_id = %s", (doc_id,))
                version_keys = [r[0] for r in await cur.fetchall()]
                await conn.execute("DELETE FROM document_versions WHERE doc_id = %s", (doc_id,))
                await conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        for k in [existing_key] + version_keys:
            await self._obj_delete(k)
        return True

    async def get_document_content(self, doc_id: str) -> str | None:
        existing = await self.get_document(doc_id)
        if existing is None:
            return None
        data = await self._obj_get(existing["content_path"])
        if data is None:
            return None
        return data.decode("utf-8")

    @_serialized
    async def update_document_status(self, doc_id: str, status: str) -> bool:
        conn = await self._ensure_conn()
        async with conn.transaction():
            cur = await conn.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
            if await cur.fetchone() is None:
                return False
            await conn.execute("UPDATE documents SET status = %s WHERE id = %s", (status, doc_id))
        return True

    async def update_document(self, doc_id: str, filename: str, content: str, page_count: int = 0) -> dict:
        """Versioned overwrite via the ObjectStore.

        New content goes to a writer-unique key; the old content is snapshotted
        to a version key. On DB failure the newly written keys are deleted and
        the row keeps pointing at its previous key.
        """
        import time

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        async with self._get_lock():
            conn = await self._ensure_conn()
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at"
                    " FROM documents WHERE id = %s FOR UPDATE",
                    (doc_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ValueError(f"document not found: {doc_id}")
                existing = {
                    "id": row[0], "project_id": row[1], "filename": row[2],
                    "content_path": row[3], "page_count": row[4], "status": row[5], "provenance_json": row[6], "created_at": row[7],
                }
                cur = await conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM document_versions WHERE doc_id = %s", (doc_id,))
                v_row = await cur.fetchone()
                next_version = (v_row[0] if v_row else 0) + 1

        snap_key = self._doc_key(existing["project_id"], doc_id, version=next_version,
                                 unique=uuid.uuid4().hex[:12])
        new_key = self._doc_key(existing["project_id"], doc_id, unique=uuid.uuid4().hex[:12])

        written = []
        try:
            old = await self._obj_get(existing["content_path"])
            if old is None:
                old = b""
            await self._obj_put(snap_key, old)
            written.append(snap_key)
            await self._obj_put(new_key, content.encode("utf-8"))
            written.append(new_key)

            async with self._get_lock():
                conn = await self._ensure_conn()
                async with conn.transaction():
                    # Lock the document row before reading/allocating the next
                    # version so concurrent update_document calls are serialized
                    # (the version query itself cannot use FOR UPDATE on an
                    # aggregate; the row lock provides the ordering).
                    cur = await conn.execute(
                        "SELECT id FROM documents WHERE id = %s FOR UPDATE", (doc_id,))
                    if await cur.fetchone() is None:
                        raise ValueError(f"document not found: {doc_id}")
                    cur = await conn.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM document_versions WHERE doc_id = %s",
                        (doc_id,),
                    )
                    v_row = await cur.fetchone()
                    final_version = (v_row[0] if v_row else 0) + 1
                    final_snap_key = snap_key if final_version == next_version else self._doc_key(
                        existing["project_id"], doc_id, version=final_version,
                        unique=uuid.uuid4().hex[:12]
                    )
                    if final_snap_key != snap_key:
                        await self._obj_put(final_snap_key, old)
                        written.append(final_snap_key)
                        # The originally-staged snap_key was written for the stale
                        # next_version and is now orphaned (final_snap_key wins).
                        # Delete it so no S3/MinIO object leaks on every contended
                        # update; the exception-path cleanup stays for the rest.
                        await self._obj_delete(snap_key)
                        written.remove(snap_key)

                    await conn.execute(
                        "INSERT INTO document_versions (doc_id, version, filename, content_path, page_count, created_at)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (doc_id, final_version, existing["filename"], final_snap_key, existing["page_count"], now),
                    )
                    await conn.execute(
                        "UPDATE documents SET filename = %s, content_path = %s, page_count = %s, status = 'uploaded'"
                        " WHERE id = %s",
                        (filename, new_key, page_count, doc_id),
                    )
                    next_version = final_version
        except Exception:
            for k in written:
                await self._obj_delete(k)
            raise

        import json
        return {"id": doc_id, "project_id": existing["project_id"], "filename": filename,
                 "content_path": new_key, "page_count": page_count,
                 "status": "uploaded", "provenance": json.loads(existing["provenance_json"] or "{}"),
                 "created_at": existing["created_at"], "version": next_version}

    @_serialized
    async def list_versions(self, doc_id: str) -> list[dict]:
        conn = await self._ensure_conn()
        async with conn.transaction():
            cur = await conn.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
            if await cur.fetchone() is None:
                raise ValueError(f"document not found: {doc_id}")
            cur = await conn.execute(
                "SELECT version, filename, content_path, page_count, created_at FROM document_versions"
                " WHERE doc_id = %s ORDER BY version", (doc_id,))
            rows = await cur.fetchall()
        return [{"version": r[0], "filename": r[1], "content_path": r[2], "page_count": r[3], "created_at": r[4]}
                for r in rows]

    async def restore_version(self, doc_id: str, version: int) -> dict | None:
        async with self._get_lock():
            conn = await self._ensure_conn()
            async with conn.transaction():
                cur = await conn.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
                if await cur.fetchone() is None:
                    raise ValueError(f"document not found: {doc_id}")
                cur = await conn.execute(
                    "SELECT filename, content_path, page_count FROM document_versions WHERE doc_id = %s AND version = %s",
                    (doc_id, version),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                filename, snap_key, page_count = row[0], row[1], row[2]
        data = await self._obj_get(snap_key)
        content = (data or b"").decode("utf-8")
        return await self.update_document(doc_id, filename, content, page_count=page_count)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _fail_closed() -> bool:
    """Fail closed (raise) when an explicitly configured production backend is
    unavailable instead of silently falling back to SQLite/local disk.

    Enabled by default; set OPENMATRIX_STORAGE_FAIL_CLOSED=0 to keep the
    graceful-fallback behaviour for existing deployments.
    """
    return os.environ.get("OPENMATRIX_STORAGE_FAIL_CLOSED", "1").strip().lower() not in ("0", "false", "no")


def get_object_store() -> ObjectStore:
    """Return the configured ObjectStore.

    ``OPENMATRIX_OBJECT_STORE=s3`` selects the S3/MinIO backend. When the
    configured backend is unreachable the factory fails closed (raises) by
    default so a production deployment never silently runs on local disk;
    set ``OPENMATRIX_STORAGE_FAIL_CLOSED=0`` to restore graceful fallback.
    """
    engine = os.environ.get("OPENMATRIX_OBJECT_STORE", "local").strip().lower()
    if engine == "s3":
        try:
            candidate = S3ObjectStore()
            # ensure the bucket exists before use so a fresh MinIO/S3
            # deployment does not silently fall back to local disk.
            if candidate.ensure_bucket() and candidate.health_check():
                return candidate
            raise RuntimeError("S3 bucket unreachable (ensure_bucket/health_check failed)")
        except Exception as exc:  # noqa: BLE001 - unreachable / misconfigured
            if _fail_closed():
                raise RuntimeError(
                    f"OPENMATRIX_OBJECT_STORE=s3 selected but S3/MinIO is unavailable: {exc}"
                ) from exc
        from library import library_root

        return LocalObjectStore(library_root(None))
    from library import library_root

    return LocalObjectStore(library_root(None))


def _pg_reachable(dsn: str, timeout_seconds: float = 5.0) -> bool:
    """Probe PostgreSQL reachability from a dedicated thread+loop.

    Runs regardless of (and without blocking) the caller's event loop, so the
    factory works both at import time and inside async contexts.
    """
    import threading

    result: dict = {"ok": False}

    def _probe() -> None:
        import asyncio

        async def _run() -> bool:
            import psycopg

            conn = await psycopg.AsyncConnection.connect(dsn, connect_timeout=timeout_seconds)
            try:
                async with conn.transaction():
                    await conn.execute("SELECT 1")
                return True
            finally:
                await conn.close()

        try:
            result["ok"] = asyncio.run(_run())
        except Exception:  # noqa: BLE001 - unreachable/misconfigured
            result["ok"] = False

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds + 2.0)
    return result["ok"]


def get_metadata_store() -> MetadataStore:
    """Return the configured MetadataStore.

    ``OPENMATRIX_METADATA_ENGINE=postgres`` selects PostgreSQL. When the
    configured backend is unreachable the factory fails closed (raises) by
    default so a production deployment never silently runs on SQLite;
    set ``OPENMATRIX_STORAGE_FAIL_CLOSED=0`` to restore graceful fallback.
    """
    engine = os.environ.get("OPENMATRIX_METADATA_ENGINE", "sqlite").strip().lower()
    if engine == "postgres":
        try:
            candidate = PostgresMetadataStore()
            if _pg_reachable(candidate._dsn):  # noqa: SLF001
                return candidate
            raise RuntimeError("PostgreSQL unreachable")
        except Exception as exc:  # noqa: BLE001 - unreachable / misconfigured
            if _fail_closed():
                raise RuntimeError(
                    f"OPENMATRIX_METADATA_ENGINE=postgres selected but PostgreSQL is unavailable: {exc}"
                ) from exc
        from library import ProjectLibraryStore, library_root

        return ProjectLibraryStore(library_root(None))
    from library import ProjectLibraryStore, library_root

    return ProjectLibraryStore(library_root(None))


def effective_storage_engines() -> dict:
    """Report the configured storage engines (metadata/object) as selected by env.

    This reflects configuration intent, not runtime reachability; health
    endpoints can combine it with per-backend probes.
    """
    return {
        "metadata": os.environ.get("OPENMATRIX_METADATA_ENGINE", "sqlite").strip().lower(),
        "object": os.environ.get("OPENMATRIX_OBJECT_STORE", "local").strip().lower(),
    }
