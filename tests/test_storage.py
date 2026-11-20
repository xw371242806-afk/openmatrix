"""Tests for P4.1 storage abstraction (ObjectStore + MetadataStore factories)."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import storage
from storage import (
    LocalObjectStore,
    PostgresMetadataStore,
    S3ObjectStore,
    get_metadata_store,
    get_object_store,
)


class LocalObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LocalObjectStore(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_put_get_delete(self):
        self.store.put("a/b.txt", b"hello")
        self.assertEqual(b"hello", self.store.get("a/b.txt"))
        self.assertTrue(self.store.delete("a/b.txt"))
        self.assertIsNone(self.store.get("a/b.txt"))
        self.assertFalse(self.store.delete("a/b.txt"))

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.store.put("../evil.txt", b"x")

    def test_sibling_prefix_rejected(self):
        # root=/tmp/x ; ../x-escape/file must NOT pass the prefix check
        root = Path(self._tmp.name)
        sibling = Path(self._tmp.name).parent / (root.name + "-escape")
        sibling.mkdir(exist_ok=True)
        try:
            with self.assertRaises(ValueError):
                self.store.put("../" + root.name + "-escape/evil.txt", b"x")
        finally:
            import shutil

            shutil.rmtree(sibling, ignore_errors=True)


class S3ObjectStoreTests(unittest.TestCase):
    def test_requires_bucket(self):
        os.environ.pop("OPENMATRIX_S3_BUCKET", None)
        with mock.patch.dict("sys.modules", {"boto3": mock.MagicMock()}):
            with self.assertRaises(ValueError):
                S3ObjectStore(bucket=None)

    def test_put_get_delete_delegates(self):
        os.environ["OPENMATRIX_S3_BUCKET"] = "test-bucket"
        fake_client = mock.MagicMock()
        fake_client.get_object.return_value = {"Body": mock.MagicMock()}
        fake_client.get_object.return_value["Body"].read.return_value = b"data"
        fake_boto3 = mock.MagicMock()
        fake_boto3.client.return_value = fake_client
        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
            store = S3ObjectStore(bucket="test-bucket")
            store.put("k", b"data")
            fake_client.put_object.assert_called_once()
            self.assertEqual(b"data", store.get("k"))
            self.assertTrue(store.delete("k"))

    def test_ensure_bucket_creates_when_missing(self):
        os.environ["OPENMATRIX_S3_BUCKET"] = "test-bucket"
        fake_client = mock.MagicMock()
        # head_bucket raises (missing bucket); create_bucket succeeds
        fake_client.head_bucket.side_effect = [Exception("NoSuchBucket")]
        fake_boto3 = mock.MagicMock()
        fake_boto3.client.return_value = fake_client
        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
            store = S3ObjectStore(bucket="test-bucket")
            self.assertTrue(store.ensure_bucket())
            fake_client.create_bucket.assert_called_once_with(Bucket="test-bucket")

    def test_ensure_bucket_idempotent_when_present(self):
        os.environ["OPENMATRIX_S3_BUCKET"] = "test-bucket"
        fake_client = mock.MagicMock()
        fake_client.head_bucket.return_value = {}  # bucket exists
        fake_boto3 = mock.MagicMock()
        fake_boto3.client.return_value = fake_client
        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
            store = S3ObjectStore(bucket="test-bucket")
            self.assertTrue(store.ensure_bucket())
            fake_client.create_bucket.assert_not_called()

    def test_ensure_bucket_returns_false_on_create_error(self):
        os.environ["OPENMATRIX_S3_BUCKET"] = "test-bucket"
        fake_client = mock.MagicMock()
        fake_client.head_bucket.side_effect = [Exception("NoSuchBucket")]
        fake_client.create_bucket.side_effect = [Exception("boom")]
        fake_boto3 = mock.MagicMock()
        fake_boto3.client.return_value = fake_client
        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
            store = S3ObjectStore(bucket="test-bucket")
            self.assertFalse(store.ensure_bucket())


class ObjectStoreFactoryTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "engine": os.environ.get("OPENMATRIX_OBJECT_STORE"),
            "bucket": os.environ.get("OPENMATRIX_S3_BUCKET"),
            "fail_closed": os.environ.get("OPENMATRIX_STORAGE_FAIL_CLOSED"),
        }

    def tearDown(self):
        mapping = {
            "engine": "OPENMATRIX_OBJECT_STORE",
            "bucket": "OPENMATRIX_S3_BUCKET",
            "fail_closed": "OPENMATRIX_STORAGE_FAIL_CLOSED",
        }
        for k, v in self._orig.items():
            key = mapping[k]
            if v is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = v

    def test_default_local(self):
        os.environ.pop("OPENMATRIX_OBJECT_STORE", None)
        store = get_object_store()
        self.assertIsInstance(store, LocalObjectStore)

    def test_s3_unavailable_graceful_fallback(self):
        os.environ["OPENMATRIX_OBJECT_STORE"] = "s3"
        os.environ["OPENMATRIX_S3_BUCKET"] = "b"
        os.environ["OPENMATRIX_STORAGE_FAIL_CLOSED"] = "0"
        # boto3 missing -> fall back to local when fail-closed disabled
        store = get_object_store()
        self.assertIsInstance(store, LocalObjectStore)

    def test_s3_unavailable_fails_closed_by_default(self):
        os.environ["OPENMATRIX_OBJECT_STORE"] = "s3"
        os.environ["OPENMATRIX_S3_BUCKET"] = "b"
        os.environ.pop("OPENMATRIX_STORAGE_FAIL_CLOSED", None)
        # boto3 missing + fail-closed default -> raises instead of silent fallback
        with self.assertRaises(RuntimeError):
            get_object_store()


class MetadataStoreFactoryTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "metadata": os.environ.get("OPENMATRIX_METADATA_ENGINE"),
            "fail_closed": os.environ.get("OPENMATRIX_STORAGE_FAIL_CLOSED"),
        }

    def tearDown(self):
        for k, v in self._orig.items():
            key = "OPENMATRIX_METADATA_ENGINE" if k == "metadata" else "OPENMATRIX_STORAGE_FAIL_CLOSED"
            if v is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = v

    def test_default_sqlite(self):
        os.environ.pop("OPENMATRIX_METADATA_ENGINE", None)
        store = get_metadata_store()
        from library import ProjectLibraryStore

        self.assertIsInstance(store, ProjectLibraryStore)

    def test_postgres_unavailable_graceful_fallback(self):
        os.environ["OPENMATRIX_METADATA_ENGINE"] = "postgres"
        os.environ.pop("OPENMATRIX_PG_DSN", None)
        os.environ["OPENMATRIX_STORAGE_FAIL_CLOSED"] = "0"
        store = get_metadata_store()
        from library import ProjectLibraryStore

        self.assertIsInstance(store, ProjectLibraryStore)

    def test_postgres_unavailable_fails_closed_by_default(self):
        os.environ["OPENMATRIX_METADATA_ENGINE"] = "postgres"
        os.environ.pop("OPENMATRIX_PG_DSN", None)
        os.environ.pop("OPENMATRIX_STORAGE_FAIL_CLOSED", None)
        with self.assertRaises(RuntimeError):
            get_metadata_store()


class StorageEnginesTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "metadata": os.environ.get("OPENMATRIX_METADATA_ENGINE"),
            "object": os.environ.get("OPENMATRIX_OBJECT_STORE"),
        }

    def tearDown(self):
        for k, v in self._orig.items():
            key = "OPENMATRIX_METADATA_ENGINE" if k == "metadata" else "OPENMATRIX_OBJECT_STORE"
            if v is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = v

    def test_defaults_sqlite_local(self):
        os.environ.pop("OPENMATRIX_METADATA_ENGINE", None)
        os.environ.pop("OPENMATRIX_OBJECT_STORE", None)
        self.assertEqual({"metadata": "sqlite", "object": "local"}, storage.effective_storage_engines())

    def test_reports_configured_production_engines(self):
        os.environ["OPENMATRIX_METADATA_ENGINE"] = "postgres"
        os.environ["OPENMATRIX_OBJECT_STORE"] = "s3"
        self.assertEqual({"metadata": "postgres", "object": "s3"}, storage.effective_storage_engines())

    def test_normalizes_case_and_whitespace(self):
        os.environ["OPENMATRIX_METADATA_ENGINE"] = " Postgres "
        os.environ["OPENMATRIX_OBJECT_STORE"] = "S3"
        self.assertEqual({"metadata": "postgres", "object": "s3"}, storage.effective_storage_engines())


class FakeAsyncCursor:
    """In-memory SQL double for the psycopg cursor (metadata contract tests)."""

    def __init__(self, db, query, params=None):
        self.db = db
        self.query = query
        self.params = params or ()
        self._result = self._execute()
        self._idx = 0

    def _execute(self):
        q = self.query.strip()
        if q.startswith("SELECT 1"):
            return [(1,)]
        if "pg_advisory_xact_lock" in q:
            return []
        if "SELECT id, name, description, created_at FROM projects WHERE id = %s" in q:
            p = self.db["projects"].get(self.params[0])
            return [(p["id"], p["name"], p["description"], p["created_at"])] if p else []
        if "SELECT id FROM projects WHERE id = %s" in q:
            return [(self.params[0],)] if self.params[0] in self.db["projects"] else []
        if "SELECT id, name, description, created_at FROM projects ORDER BY created_at" in q:
            return [(p["id"], p["name"], p["description"], p["created_at"]) for p in self.db["projects"].values()]
        if "INSERT INTO projects" in q:
            self.db["projects"][self.params[0]] = {"id": self.params[0], "name": self.params[1], "description": self.params[2], "created_at": self.params[3]}
            return []
        if "SELECT id FROM documents WHERE project_id = %s" in q:
            return [(d["id"],) for d in self.db["documents"].values() if d["project_id"] == self.params[0]]
        if "DELETE FROM projects WHERE id = %s" in q:
            self.db["projects"].pop(self.params[0], None)
            return []
        if "DELETE FROM documents WHERE project_id = %s" in q:
            for k in [k for k, v in self.db["documents"].items() if v["project_id"] == self.params[0]]:
                self.db["documents"].pop(k, None)
            return []
        if "SELECT id FROM documents WHERE id = %s" in q:
            return [(self.params[0],)] if self.params[0] in self.db["documents"] else []
        if "SELECT content_path FROM documents WHERE id = %s" in q:
            d = self.db["documents"].get(self.params[0])
            return [(d["content_path"],)] if d else []
        if "SELECT content_path FROM documents WHERE project_id = %s" in q:
            return [(d["content_path"],) for d in self.db["documents"].values() if d["project_id"] == self.params[0]]
        if "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at FROM documents WHERE id = %s" in q:
            d = self.db["documents"].get(self.params[0])
            return [(d["id"], d["project_id"], d["filename"], d["content_path"], d["page_count"], d["status"], d.get("provenance_json", "{}"), d["created_at"])] if d else []
        if "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at FROM documents WHERE project_id = %s" in q:
            return [(d["id"], d["project_id"], d["filename"], d["content_path"], d["page_count"], d["status"], d.get("provenance_json", "{}"), d["created_at"])
                    for d in self.db["documents"].values() if d["project_id"] == self.params[0]]
        if "SELECT id, project_id, filename, content_path, page_count, status, provenance_json, created_at FROM documents ORDER BY created_at" in q:
            return [(d["id"], d["project_id"], d["filename"], d["content_path"], d["page_count"], d["status"], d.get("provenance_json", "{}"), d["created_at"])
                    for d in self.db["documents"].values()]
        if "INSERT INTO documents" in q:
            self.db["documents"][self.params[0]] = {"id": self.params[0], "project_id": self.params[1], "filename": self.params[2], "content_path": self.params[3], "page_count": self.params[4], "status": "uploaded", "provenance_json": self.params[5], "created_at": self.params[6]}
            return []
        if "DELETE FROM documents WHERE id = %s" in q:
            self.db["documents"].pop(self.params[0], None)
            return []
        if "UPDATE documents SET status = %s WHERE id = %s" in q:
            if self.params[1] in self.db["documents"]:
                self.db["documents"][self.params[1]]["status"] = self.params[0]
            return []
        if "UPDATE documents SET filename = %s, content_path = %s, page_count = %s" in q:
            d = self.db["documents"].get(self.params[3])
            if d:
                d["filename"], d["content_path"], d["page_count"] = self.params[0], self.params[1], self.params[2]
            return []
        if "SELECT COALESCE(MAX(version), 0) FROM document_versions WHERE doc_id = %s" in q:
            vers = [v["version"] for v in self.db["versions"].get(self.params[0], [])]
            return [(max(vers) if vers else 0,)]
        if "INSERT INTO document_versions" in q:
            self.db["versions"].setdefault(self.params[0], []).append({"doc_id": self.params[0], "version": self.params[1], "filename": self.params[2], "content_path": self.params[3], "page_count": self.params[4], "created_at": self.params[5]})
            return []
        if "SELECT filename, content_path, page_count FROM document_versions WHERE doc_id = %s" in q:
            for v in self.db["versions"].get(self.params[0], []):
                if v["version"] == self.params[1]:
                    return [(v["filename"], v["content_path"], v["page_count"])]
            return []
        if "SELECT version, filename, content_path, page_count, created_at FROM document_versions WHERE doc_id = %s" in q:
            if "AND version = %s" in q:
                for v in self.db["versions"].get(self.params[0], []):
                    if v["version"] == self.params[1]:
                        return [(v["filename"], v["content_path"], v["page_count"])]
                return []
            return [(v["version"], v["filename"], v["content_path"], v["page_count"], v["created_at"]) for v in self.db["versions"].get(self.params[0], [])]
        if "DELETE FROM document_versions WHERE doc_id = %s" in q:
            self.db["versions"].pop(self.params[0], None)
            return []
        if "SELECT content_path FROM document_versions WHERE doc_id = %s" in q:
            return [(v["content_path"],) for v in self.db["versions"].get(self.params[0], [])]
        return []

    async def fetchone(self):
        if self._idx < len(self._result):
            row = self._result[self._idx]
            self._idx += 1
            return row
        return None

    async def fetchall(self):
        res = self._result[self._idx:]
        self._idx = len(self._result)
        return res


class FakeLockRegistry:
    """Per-row asyncio.Lock registry shared across fake connections so that
    SELECT ... FOR UPDATE on the *same row* genuinely blocks a second
    connection's transaction until the first commits (simulating the real
    PostgreSQL row lock)."""

    def __init__(self):
        self._locks = {}

    def get(self, key):
        lock = self._locks.get(key)
        if lock is None:
            import asyncio

            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


def _make_fake_db():
    """Fresh in-memory DB (projects/documents/versions) + a shared row-lock registry."""
    return {"projects": {}, "documents": {}, "versions": {}, "_locks": FakeLockRegistry()}


class FakeAsyncTx:
    def __init__(self, conn):
        self.conn = conn
        self._held = []

    async def __aenter__(self):
        self.conn._tx = self
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for lock in self._held:
            lock.release()
        self.conn._tx = None
        return False


class FakeAsyncConnection:
    def __init__(self, db):
        self.db = db
        self.closed = False
        self._tx = None

    def transaction(self):
        return FakeAsyncTx(self)

    async def execute(self, query, params=None):
        # Simulate SELECT ... FOR UPDATE: block until no other transaction
        # holds the row lock, then hold it for the duration of this tx.
        if "FOR UPDATE" in query and "FROM documents WHERE id =" in query:
            pk = params[0]
            if self._tx is not None:
                lock = self.db["_locks"].get(("documents", pk))
                await lock.acquire()
                self._tx._held.append(lock)
        return FakeAsyncCursor(self.db, query, params)

    async def close(self):
        self.closed = True


class RecordingObjectStore:
    """In-memory ObjectStore double that records every put/delete so tests can
    assert no orphaned snapshot object leaks on contended updates."""

    def __init__(self):
        self.keys = {}  # key -> present in store
        self.operations = []  # ordered (op, key) log

    def put(self, key, data):
        self.keys[key] = True
        self.operations.append(('put', key))

    def get(self, key):
        return b'x' if self.keys.get(key) else None

    def delete(self, key):
        self.operations.append(('del', key))
        self.keys.pop(key, None)
        return True


class PostgresMetadataStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = _make_fake_db()
        self.psycopg_mock = mock.MagicMock()
        self.psycopg_mock.AsyncConnection.connect = mock.AsyncMock(
            side_effect=lambda *_a, **_kw: FakeAsyncConnection(self.db)
        )
        self.patcher = mock.patch.dict("sys.modules", {"psycopg": self.psycopg_mock})
        self.patcher.start()
        self.store = PostgresMetadataStore(dsn="postgresql://test:test@localhost/test", root=self.root)

    async def asyncTearDown(self):
        await self.store.close()
        self.patcher.stop()
        self._tmp.cleanup()

    async def test_health_check(self):
        self.assertTrue(await self.store.health_check())

    async def test_schema_connection_closes_before_business_connection_is_exposed(self):
        schema_conn = FakeAsyncConnection(self.db)
        business_conn = FakeAsyncConnection(self.db)
        connect = mock.AsyncMock(side_effect=[schema_conn, business_conn])
        self.psycopg_mock.AsyncConnection.connect = connect
        store = PostgresMetadataStore(
            dsn="postgresql://test:test@localhost/test", root=self.root
        )
        try:
            result = await store._ensure_conn()  # noqa: SLF001
            self.assertEqual(2, connect.await_count)
            self.assertTrue(schema_conn.closed)
            self.assertIs(business_conn, result)
            self.assertFalse(business_conn.closed)
        finally:
            await store.close()

    async def test_concurrent_stores_initialize_schema_before_business_connections(self):
        connections = []

        async def connect(*_a, **_kw):
            conn = FakeAsyncConnection(self.db)
            connections.append(conn)
            return conn

        self.psycopg_mock.AsyncConnection.connect = mock.AsyncMock(side_effect=connect)
        stores = [
            PostgresMetadataStore(
                dsn="postgresql://test:test@localhost/test", root=self.root
            )
            for _ in range(3)
        ]
        try:
            business = await asyncio.gather(*(store._ensure_conn() for store in stores))  # noqa: SLF001
            self.assertEqual(6, len(connections))
            self.assertEqual(3, sum(conn.closed for conn in connections))
            self.assertEqual(set(business), {conn for conn in connections if not conn.closed})
            self.assertTrue(all(not conn.closed for conn in business))
        finally:
            for store in stores:
                await store.close()

    async def test_schema_failure_closes_connection_without_opening_business_connection(self):
        class FailingSchemaConnection(FakeAsyncConnection):
            async def execute(self, query, params=None):
                if "ALTER TABLE" in query:
                    raise RuntimeError("schema failed")
                return await super().execute(query, params)

        schema_conn = FailingSchemaConnection(self.db)
        connect = mock.AsyncMock(return_value=schema_conn)
        self.psycopg_mock.AsyncConnection.connect = connect
        store = PostgresMetadataStore(
            dsn="postgresql://test:test@localhost/test", root=self.root
        )

        with self.assertRaisesRegex(RuntimeError, "schema failed"):
            await store._ensure_conn()  # noqa: SLF001

        self.assertEqual(1, connect.await_count)
        self.assertTrue(schema_conn.closed)
        self.assertIsNone(store._conn)  # noqa: SLF001

    async def test_document_provenance_round_trips(self):
        await self.store.create_project("pev", "Evidence project")
        provenance = {
            "source_kind": "browser_current_page",
            "source_url": "https://example.com/article",
            "captured_at": "2026-08-28T10:00:00+00:00",
            "content_sha256": "abc",
        }
        created = await self.store.add_document(
            "evidence1", "pev", "article.txt", "captured text", provenance=provenance
        )
        self.assertEqual(provenance, created["provenance"])
        self.assertEqual(provenance, (await self.store.get_document("evidence1"))["provenance"])
        self.assertEqual(provenance, (await self.store.list_documents("pev"))[0]["provenance"])

    async def test_crud_and_version_lifecycle(self):
        p = await self.store.create_project("p1", "Project 1", "desc")
        self.assertEqual(p["id"], "p1")
        fetched = await self.store.get_project("p1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["name"], "Project 1")
        self.assertEqual(1, len(await self.store.list_projects()))

        doc = await self.store.add_document("d1", "p1", "file.txt", "initial content", page_count=1)
        self.assertEqual("d1", doc["id"])
        self.assertEqual("initial content", await self.store.get_document_content("d1"))

        self.assertTrue(await self.store.update_document_status("d1", "indexed"))
        self.assertEqual("indexed", (await self.store.get_document("d1"))["status"])

        v2 = await self.store.update_document("d1", "file_v2.txt", "v2 content", page_count=2)
        self.assertEqual(1, v2["version"])
        self.assertEqual("v2 content", await self.store.get_document_content("d1"))
        versions = await self.store.list_versions("d1")
        self.assertEqual(1, len(versions))
        self.assertEqual(1, versions[0]["version"])

        restored = await self.store.restore_version("d1", 1)
        self.assertIsNotNone(restored)
        self.assertEqual("initial content", await self.store.get_document_content("d1"))

        self.assertTrue(await self.store.delete_document("d1"))
        self.assertIsNone(await self.store.get_document("d1"))

    async def test_delete_project_removes_objects(self):
        await self.store.create_project("p_race", "Race Project")
        doc = await self.store.add_document("d_race", "p_race", "a.txt", "content")
        key = doc["content_path"]
        # blob exists under the object store
        self.assertIsNotNone(self.store._objects.get(key))
        self.assertTrue(await self.store.delete_project("p_race"))
        # both metadata and blob are gone
        self.assertIsNone(await self.store.get_document("d_race"))
        self.assertIsNone(self.store._objects.get(key))

    async def test_concurrent_updates_serialized(self):
        """Concurrent updates from SEPARATE connections (multiple store
        instances) must serialize version allocation via the SELECT ... FOR
        UPDATE row lock - a per-store in-process lock cannot (each store has its
        own connection/lock, mirroring cross-process writers)."""
        await self.store.create_project("p_conc", "Conc Project")
        await self.store.add_document("d_conc", "p_conc", "doc.txt", "v0")

        # One PostgresMetadataStore per writer -> separate connections, so the
        # only thing serializing version allocation is the DB row lock, not a
        # shared in-process _get_lock().
        stores = [
            PostgresMetadataStore(dsn="postgresql://test:test@localhost/test", root=self.root)
            for _ in range(3)
        ]
        for s in stores:
            s._conn = FakeAsyncConnection(self.db)  # noqa: SLF001 - share the in-memory DB

        async def do_update(store, i):
            return await store.update_document("d_conc", f"doc{i}.txt", f"content_{i}")

        try:
            results = await asyncio.gather(*(do_update(s, i) for i, s in enumerate(stores, 1)))
            self.assertEqual(3, len(results))
            # Each writer returned its own distinct, monotonic version.
            self.assertEqual([1, 2, 3], sorted(r['version'] for r in results))
            versions = await self.store.list_versions("d_conc")
            self.assertEqual(3, len(versions))
            version_nums = [v['version'] for v in versions]
            self.assertEqual([1, 2, 3], sorted(version_nums))
        finally:
            for s in stores:
                await s.close()

    async def test_contended_update_deletes_stale_snapshot(self):
        """A contended update where the loser re-allocates a larger final_version
        must delete its originally-staged (now stale) snapshot key so no orphaned
        S3/MinIO object leaks; only DB-referenced snapshots survive."""
        objects = RecordingObjectStore()
        storeA = PostgresMetadataStore(dsn="postgresql://test:test@localhost/test", root=self.root, object_store=objects)
        storeB = PostgresMetadataStore(dsn="postgresql://test:test@localhost/test", root=self.root, object_store=objects)
        storeA._conn = FakeAsyncConnection(self.db)  # noqa: SLF001
        storeB._conn = FakeAsyncConnection(self.db)  # noqa: SLF001
        try:
            await storeA.create_project("p_orph", "Orph")
            await storeA.add_document("d_orph", "p_orph", "a.txt", "v0")
            await storeA.update_document("d_orph", "a.txt", "v1")  # committed version 1

            staged = asyncio.Event()
            go = asyncio.Event()
            orig_put = storeA._obj_put  # noqa: SLF001

            async def gated_put(key, data):
                await orig_put(key, data)
                if ".v2." in key:  # pause right after the loser stages its snapshot
                    staged.set()
                    await go.wait()

            with mock.patch.object(storeA, "_obj_put", side_effect=gated_put):
                a_task = asyncio.create_task(storeA.update_document("d_orph", "a.txt", "A"))
                await staged.wait()
                # A competing writer commits version 2 while A is paused, so A's
                # staged version-2 snapshot becomes stale.
                await storeB.update_document("d_orph", "b.txt", "B")
                go.set()
                await a_task

            # Both writers committed distinct, monotonic versions.
            versions = await storeA.list_versions("d_orph")
            self.assertEqual([1, 2, 3], sorted(v['version'] for v in versions))
            # Every snapshot the DB references must exist, and no stale/orphaned
            # snapshot key may remain.
            db_snap_keys = {v['content_path'] for v in versions}
            store_snap_keys = {k for k in objects.keys if ".v" in k}
            self.assertEqual(db_snap_keys, store_snap_keys)
        finally:
            await storeA.close()
            await storeB.close()

    async def test_concurrent_reads_mixed_with_writes(self):
        """get_document is serialized too - reads never interleave with writer transactions."""
        await self.store.create_project("p_rw", "RW")
        await self.store.add_document("d_rw", "p_rw", "a.txt", "initial")

        async def reader():
            for _ in range(10):
                await self.store.get_document("d_rw")

        async def writer():
            for i in range(5):
                await self.store.update_document("d_rw", "a.txt", f"c{i}")

        await asyncio.gather(reader(), writer())
        # final content is one of the writer's versions - consistent
        content = await self.store.get_document_content("d_rw")
        self.assertIn(content, {f"c{i}" for i in range(5)})

    async def test_cross_process_duplicate_publish_does_not_clobber(self):
        """Two stores racing on the same doc_id: loser never deletes winner's blob."""
        store2 = PostgresMetadataStore(dsn="postgresql://test:test@localhost/test", root=self.root)
        store2._conn = FakeAsyncConnection(self.db)  # noqa: SLF001 - share the in-memory DB

        try:
            await self.store.create_project("p_dup", "Dup")
            # pre-publish the winner's blob at the final path (simulating the
            # racing winner between validation and INSERT)
            winner_path = self.root / "p_dup" / "d_dup.txt"
            winner_path.parent.mkdir(parents=True, exist_ok=True)
            winner_path.write_text("WINNER", encoding="utf-8")

            # loser validates OK (no row yet), then exclusive publish must fail
            with mock.patch.object(
                self.store, "get_document", new_callable=mock.AsyncMock, return_value=None
            ):
                # force INSERT failure path: duplicate row in db
                self.db["documents"]["d_dup"] = {
                    "id": "d_dup", "project_id": "p_dup", "filename": "x",
                    "content_path": str(winner_path), "page_count": 0,
                    "status": "uploaded", "created_at": "t",
                }
                with self.assertRaises(Exception):
                    await self.store.add_document("d_dup", "p_dup", "a.txt", "LOSER")
            # winner's blob untouched
            self.assertEqual("WINNER", winner_path.read_text(encoding="utf-8"))
        finally:
            await store2.close()

    async def test_delete_recreate_race_preserves_reincarnated_version_content(self):
        """Snapshot keys are incarnation-unique: post-commit cleanup of a deleted
        project never deletes a recreated project's freshly committed blobs."""
        await self.store.create_project("p_rec", "Rec")
        await self.store.add_document("d_rec", "p_rec", "a.txt", "v1")
        await self.store.update_document("d_rec", "a.txt", "v2")  # version-1 snapshot

        gate = asyncio.Event()
        entered = asyncio.Event()
        original_delete = self.store._obj_delete  # noqa: SLF001

        async def gated_delete(key):
            entered.set()
            await gate.wait()
            await original_delete(key)

        # pause blob cleanup right after delete_project commits its DB rows
        with mock.patch.object(self.store, "_obj_delete", side_effect=gated_delete):
            del_task = asyncio.create_task(self.store.delete_project("p_rec"))
            await entered.wait()
            # reincarnate the same project/doc/version while cleanup is paused
            await self.store.create_project("p_rec", "Rec")
            await self.store.add_document("d_rec", "p_rec", "a.txt", "NEW")
            await self.store.update_document("d_rec", "a.txt", "NEW2")
            gate.set()
            await del_task

        # reincarnated document + version-1 snapshot must be intact
        self.assertEqual("NEW2", await self.store.get_document_content("d_rec"))
        versions = await self.store.list_versions("d_rec")
        self.assertEqual(1, len(versions))
        await self.store.restore_version("d_rec", 1)
        self.assertEqual("NEW", await self.store.get_document_content("d_rec"))

    def test_pg_unreachable_falls_back_thread_probe(self):
        """Configured but unreachable PG service falls back to SQLite (thread probe)."""
        import storage as storage_mod

        with mock.patch.dict(
            os.environ,
            {
                "OPENMATRIX_METADATA_ENGINE": "postgres",
                "OPENMATRIX_PG_DSN": "postgresql://u:p@127.0.0.1:1/none",
                "OPENMATRIX_STORAGE_FAIL_CLOSED": "0",
            },
        ):
            # connect raises -> probe thread marks unreachable -> SQLite fallback
            with mock.patch("psycopg.AsyncConnection.connect",
                            side_effect=OSError("connection refused")):
                store = storage_mod.get_metadata_store()
            self.assertEqual("ProjectLibraryStore", store.__class__.__name__)


class FactoryFallbackTests(unittest.TestCase):
    def test_object_store_fallback(self):
        with mock.patch.dict(
            os.environ,
            {
                "OPENMATRIX_OBJECT_STORE": "s3",
                "OPENMATRIX_S3_BUCKET": "b",
                "OPENMATRIX_STORAGE_FAIL_CLOSED": "0",
            },
        ):
            with mock.patch("storage.S3ObjectStore.health_check", return_value=False):
                store = get_object_store()
                self.assertIsInstance(store, LocalObjectStore)

    def test_metadata_store_fallback(self):
        with mock.patch.dict(
            os.environ,
            {"OPENMATRIX_METADATA_ENGINE": "postgres", "OPENMATRIX_STORAGE_FAIL_CLOSED": "0"},
        ):
            os.environ.pop("OPENMATRIX_PG_DSN", None)
            store = get_metadata_store()
            self.assertEqual(store.__class__.__name__, "ProjectLibraryStore")


class PostgresS3RoutingTests(unittest.IsolatedAsyncioTestCase):
    """Library CRUD routes blobs through the injected ObjectStore (S3 double)."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_fake_db()
        self.psycopg_mock = mock.MagicMock()
        self.psycopg_mock.AsyncConnection.connect = mock.AsyncMock(
            side_effect=lambda *_a, **_kw: FakeAsyncConnection(self.db)
        )
        self.patcher = mock.patch.dict("sys.modules", {"psycopg": self.psycopg_mock})
        self.patcher.start()

        # S3 double: in-memory bucket + recorded call order
        self.bucket = {}
        self.calls = []
        self.client = mock.MagicMock()
        self.client.put_object.side_effect = lambda Bucket, Key, Body: (
            self.calls.append(("put", Key)), self.bucket.__setitem__(Key, Body)
        )
        self.client.get_object.side_effect = lambda Bucket, Key: (
            self.calls.append(("get", Key)),
            {"Body": mock.MagicMock(read=lambda: self.bucket.get(Key, b""))},
        )[1]
        self.client.delete_object.side_effect = lambda Bucket, Key: (
            self.calls.append(("del", Key)), self.bucket.pop(Key, None)
        )[0]
        fake_boto3 = mock.MagicMock()
        fake_boto3.client.return_value = self.client
        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
            self.objects = S3ObjectStore("test-bucket")

        self.store = PostgresMetadataStore(
            dsn="postgresql://test:test@localhost/test",
            root=Path(self._tmp.name),
            object_store=self.objects,
        )

    async def asyncTearDown(self):
        await self.store.close()
        self.patcher.stop()
        self._tmp.cleanup()

    async def test_s3_routed_crud_versions_restore_delete(self):
        await self.store.create_project("s3p", "S3 Project")
        # PG+S3 touches no local disk: creating a project leaves the root empty
        self.assertEqual([], [p for p in Path(self._tmp.name).rglob("*")])
        doc = await self.store.add_document("s3d", "s3p", "a.txt", "v1")
        key = doc["content_path"]
        self.assertTrue(key.endswith(".txt"))
        self.assertEqual(b"v1", self.bucket[key])
        self.assertEqual("v1", await self.store.get_document_content("s3d"))

        await self.store.update_document("s3d", "a.txt", "v2")
        self.assertEqual("v2", await self.store.get_document_content("s3d"))

        await self.store.restore_version("s3d", 1)
        self.assertEqual("v1", await self.store.get_document_content("s3d"))

        await self.store.delete_document("s3d")
        self.assertIsNone(await self.store.get_document("s3d"))

        # S3 object operations were exercised for add/get/update/restore/delete
        self.assertIn("put", [c[0] for c in self.calls])
        self.assertIn("get", [c[0] for c in self.calls])
        self.assertIn("del", [c[0] for c in self.calls])


def run_coro_async(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
