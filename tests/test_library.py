"""Tests for P4 library.py (ProjectLibraryStore)."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from library import ProjectLibraryStore, library_db_path, library_root


def run_coro(coro):
    return asyncio.run(coro)


class ProjectLibraryStoreTests(unittest.TestCase):
    def test_existing_sqlite_database_migrates_provenance_column(self):
        import sqlite3

        db_path = self._base / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, created_at TEXT NOT NULL);
            CREATE TABLE documents (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL, content_path TEXT NOT NULL, page_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'uploaded', created_at TEXT NOT NULL);
            CREATE TABLE document_versions (doc_id TEXT NOT NULL, version INTEGER NOT NULL, filename TEXT, content_path TEXT NOT NULL, page_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, PRIMARY KEY (doc_id, version));
        """)
        conn.execute("INSERT INTO projects VALUES ('legacy', 'Legacy', NULL, '2026-08-28T00:00:00')")
        conn.commit()
        conn.close()
        store = ProjectLibraryStore(db_path=db_path)
        try:
            run_coro(store.add_document("d1", "legacy", "a.txt", "content"))
            self.assertEqual({}, run_coro(store.get_document("d1"))["provenance"])
        finally:
            run_coro(store.close())

    def test_document_provenance_persists(self):
        run_coro(self.store.create_project("p1", "Project"))
        provenance = {
            "source_kind": "browser_current_page",
            "source_url": "https://example.com/article",
            "captured_at": "2026-08-28T10:00:00+00:00",
            "content_sha256": "abc",
        }
        doc = run_coro(
            self.store.add_document("d1", "p1", "article.txt", "content", provenance=provenance)
        )
        self.assertEqual(provenance, doc["provenance"])
        self.assertEqual(provenance, run_coro(self.store.get_document("d1"))["provenance"])
        self.assertEqual(provenance, run_coro(self.store.list_documents("p1"))[0]["provenance"])
        updated = run_coro(self.store.update_document("d1", "article.txt", "new content"))
        self.assertEqual(provenance, updated["provenance"])
        self.assertEqual(provenance, run_coro(self.store.get_document("d1"))["provenance"])
    """Project/document CRUD on an isolated temp store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self.store = ProjectLibraryStore(db_path=self._base)

    def tearDown(self):
        run_coro(self.store.close())
        self._tmp.cleanup()

    def test_library_db_path_resolution(self):
        self.assertTrue(str(library_db_path(self._base)).endswith("library.db"))
        self.assertEqual(self._base, library_root(self._base))

    def test_create_and_get_project(self):
        run_coro(self.store.create_project("p1", "Acme", "quarterly"))
        proj = run_coro(self.store.get_project("p1"))
        self.assertEqual("Acme", proj["name"])
        self.assertEqual("quarterly", proj["description"])
        self.assertIsNotNone(proj["created_at"])

    def test_create_duplicate_project_rejected(self):
        run_coro(self.store.create_project("p1", "Acme"))
        with self.assertRaises(ValueError):
            run_coro(self.store.create_project("p1", "Other"))

    def test_invalid_project_id_rejected(self):
        with self.assertRaises(ValueError):
            run_coro(self.store.create_project("bad id!@#", "x"))

    def test_list_projects(self):
        run_coro(self.store.create_project("p1", "One"))
        run_coro(self.store.create_project("p2", "Two"))
        projs = run_coro(self.store.list_projects())
        self.assertEqual(["p1", "p2"], [p["id"] for p in projs])

    def test_add_get_document_with_disk_content(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "hello world", page_count=2))
        doc = run_coro(self.store.get_document("d1"))
        self.assertEqual("p1", doc["project_id"])
        self.assertEqual("hello world", run_coro(self.store.get_document_content("d1")))
        # content persisted on disk
        self.assertTrue(Path(doc["content_path"]).is_file())

    def test_add_document_missing_project_rejected(self):
        with self.assertRaises(ValueError):
            run_coro(self.store.add_document("d1", "nope", "a.txt", "x"))

    def test_list_documents_by_project(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.create_project("p2", "Beta"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "x"))
        run_coro(self.store.add_document("d2", "p2", "b.txt", "y"))
        self.assertEqual(["d1"], [d["id"] for d in run_coro(self.store.list_documents("p1"))])
        self.assertEqual(2, len(run_coro(self.store.list_documents())))

    def test_delete_document(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "hello"))
        self.assertTrue(run_coro(self.store.delete_document("d1")))
        self.assertIsNone(run_coro(self.store.get_document("d1")))
        self.assertFalse(run_coro(self.store.delete_document("d1")))

    def test_delete_project_cascades(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "hello"))
        self.assertTrue(run_coro(self.store.delete_project("p1")))
        self.assertIsNone(run_coro(self.store.get_project("p1")))
        self.assertIsNone(run_coro(self.store.get_document("d1")))

    def test_update_document_status(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "x"))
        self.assertTrue(run_coro(self.store.update_document_status("d1", "indexed")))
        self.assertEqual("indexed", run_coro(self.store.get_document("d1"))["status"])


class SplitPagesTests(unittest.TestCase):
    """Deterministic page splitting for project_search provenance."""

    def test_form_feed_preserves_physical_page_numbers(self):
        from mcp_server import _split_pages
        pages = _split_pages("Page one content.\fPage two content.\fPage three.")
        self.assertEqual([1, 2, 3], [item["page"] for item in pages])
        self.assertEqual("Page three.", pages[2]["text"])

    def test_plain_text_has_sections_without_fake_page_numbers(self):
        from mcp_server import _split_pages
        pages = _split_pages("Page one content.\n\nPage two content.\n\nPage three.")
        self.assertEqual([None, None, None], [item["page"] for item in pages])
        self.assertEqual([1, 2, 3], [item["section"] for item in pages])
        self.assertIn("Page one", pages[0]["text"])

    def test_long_section_hard_split(self):
        from mcp_server import _split_pages

        long_text = "word " * 3000  # ~15000 chars
        pages = _split_pages(long_text, max_chars=2000)
        self.assertGreaterEqual(len(pages), 7)
        self.assertTrue(all(len(p['text']) <= 2000 for p in pages))

    def test_empty_and_whitespace(self):
        from mcp_server import _split_pages

        self.assertEqual([], _split_pages(""))
        self.assertEqual([], _split_pages("   \n\n  "))


class DocumentVersionTests(unittest.TestCase):
    """P4.4 versioned document updates."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ProjectLibraryStore(db_path=Path(self._tmp.name))

    def tearDown(self):
        run_coro(self.store.close())
        self._tmp.cleanup()

    def test_update_snapshots_and_restore(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "version one content", page_count=1))
        # versioned update
        doc = run_coro(self.store.update_document("d1", "a.txt", "version two content", page_count=1))
        self.assertEqual(1, doc["version"])
        self.assertEqual("version two content", run_coro(self.store.get_document_content("d1")))
        # second update -> v2
        doc2 = run_coro(self.store.update_document("d1", "a.txt", "version three content"))
        self.assertEqual(2, doc2["version"])

        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(2, len(versions))
        self.assertEqual(1, versions[0]["version"])
        self.assertEqual(2, versions[1]["version"])
        # History rows point to dedicated version-stamped files
        self.assertTrue(versions[0]["content_path"].endswith("d1.v1.txt"))
        self.assertTrue(versions[1]["content_path"].endswith("d1.v2.txt"))
        self.assertEqual("version one content", Path(versions[0]["content_path"]).read_text(encoding="utf-8"))
        self.assertEqual("version two content", Path(versions[1]["content_path"]).read_text(encoding="utf-8"))

        # restore v1 -> creates new snapshot v3, content back to first snapshot
        restored = run_coro(self.store.restore_version("d1", 1))
        self.assertIsNotNone(restored)
        self.assertEqual(3, restored["version"])
        self.assertEqual("version one content", run_coro(self.store.get_document_content("d1")))

        # Existing snapshots remain intact and unmodified
        versions_after = run_coro(self.store.list_versions("d1"))
        self.assertEqual(3, len(versions_after))
        self.assertEqual("version one content", Path(versions_after[0]["content_path"]).read_text(encoding="utf-8"))
        self.assertEqual("version two content", Path(versions_after[1]["content_path"]).read_text(encoding="utf-8"))
        self.assertEqual("version three content", Path(versions_after[2]["content_path"]).read_text(encoding="utf-8"))

    def test_snapshots_isolated_from_path_reuse(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v1 original"))
        run_coro(self.store.update_document("d1", "a.txt", "v2 updated"))

        v1 = run_coro(self.store.list_versions("d1"))[0]
        # Overwrite the primary path directly
        proj_dir = self.store._root / "p1"
        (proj_dir / "d1.txt").write_text("clobbered base path", encoding="utf-8")

        # Snapshot file remains unchanged
        self.assertEqual("v1 original", Path(v1["content_path"]).read_text(encoding="utf-8"))

    def test_concurrent_updates_preserve_all_snapshots(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))

        async def _concurrent():
            tasks = [
                self.store.update_document("d1", "a.txt", f"update-{i}")
                for i in range(1, 11)
            ]
            return await asyncio.gather(*tasks)

        results = run_coro(_concurrent())
        self.assertEqual(10, len(results))
        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(10, len(versions))
        ver_nums = [v["version"] for v in versions]
        self.assertEqual(list(range(1, 11)), ver_nums)
        for v in versions:
            path = Path(v["content_path"])
            self.assertTrue(path.exists())
            self.assertTrue(path.name.startswith("d1.v"))

    def test_concurrent_updates_across_event_loops_same_store(self):
        """Same store instance, concurrent updates from separate event loops (MCP path)."""
        import threading

        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))
        errors = []
        results = []

        def worker(label):
            try:
                doc = asyncio.run(self.store.update_document("d1", "a.txt", f"content-{label}"))
                results.append(doc["version"])
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual([], errors)
        self.assertEqual(5, len(results))
        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(5, len(versions))
        ver_nums = [v["version"] for v in versions]
        self.assertEqual(list(range(1, 6)), ver_nums)

    def test_delete_clears_version_history(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v1 content"))
        run_coro(self.store.update_document("d1", "a.txt", "v2 content"))
        self.assertTrue(run_coro(self.store.delete_document("d1")))
        # versions of a deleted document now raise (document not found)
        with self.assertRaises(ValueError):
            run_coro(self.store.list_versions("d1"))
        # recreate -> clean history (no stale restore possible)
        run_coro(self.store.add_document("d1", "p1", "a.txt", "fresh"))
        self.assertEqual([], run_coro(self.store.list_versions("d1")))
        self.assertIsNone(run_coro(self.store.restore_version("d1", 1)))

    def test_delete_project_clears_version_rows(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v1"))
        run_coro(self.store.update_document("d1", "a.txt", "v2"))
        self.assertTrue(run_coro(self.store.delete_project("p1")))
        # document gone; versions now raise
        self.assertIsNone(run_coro(self.store.get_document("d1")))
        with self.assertRaises(ValueError):
            run_coro(self.store.list_versions("d1"))

    def test_restore_missing_version_none(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "x"))
        self.assertIsNone(run_coro(self.store.restore_version("d1", 99)))

    def test_concurrent_updates_separate_store_instances(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))

        store2 = ProjectLibraryStore(db_path=Path(self._tmp.name))

        async def _run_concurrent():
            t1 = self.store.update_document("d1", "a.txt", "content from store 1")
            t2 = store2.update_document("d1", "a.txt", "content from store 2")
            return await asyncio.gather(t1, t2)

        try:
            results = run_coro(_run_concurrent())
            self.assertEqual(2, len(results))
            versions = run_coro(self.store.list_versions("d1"))
            self.assertEqual(2, len(versions))
            self.assertEqual([1, 2], [v["version"] for v in versions])

            # Verify the complete content chain across sequential snapshots
            v1_content = Path(versions[0]["content_path"]).read_text(encoding="utf-8")
            v2_content = Path(versions[1]["content_path"]).read_text(encoding="utf-8")
            final_content = run_coro(self.store.get_document_content("d1"))

            self.assertEqual("v0 initial", v1_content)
            self.assertEqual({"content from store 1", "content from store 2"}, {v2_content, final_content})
            self.assertNotEqual(v2_content, final_content)
        finally:
            run_coro(store2.close())

    def test_replace_failure_rolls_back_cleanly_without_orphaned_snapshots(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))

        from unittest.mock import patch

        with patch.object(Path, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                run_coro(self.store.update_document("d1", "a.txt", "failed update"))

        self.assertEqual("v0 initial", run_coro(self.store.get_document_content("d1")))
        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(0, len(versions))
        snap_v1 = self.store._root / "p1" / "d1.v1.txt"
        self.assertFalse(snap_v1.exists())

    def test_commit_failure_after_replace_restores_primary_and_no_missing_snapshots(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))

        conn = run_coro(self.store._ensure_open())
        orig_commit = conn.commit

        async def _failing_commit():
            raise RuntimeError("simulated commit failure")

        conn.commit = _failing_commit
        try:
            with self.assertRaises(RuntimeError):
                run_coro(self.store.update_document("d1", "a.txt", "failed update"))
        finally:
            conn.commit = orig_commit

        self.assertEqual("v0 initial", run_coro(self.store.get_document_content("d1")))
        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(0, len(versions))
        for v in versions:
            self.assertTrue(Path(v["content_path"]).exists())

    def test_snapshot_collision_raises_without_clobbering(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))

        snap_path = self.store._root / "p1" / "d1.v1.txt"
        snap_path.write_text("existing pre-created snapshot", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            run_coro(self.store.update_document("d1", "a.txt", "v1 update attempt"))

        self.assertEqual("existing pre-created snapshot", snap_path.read_text(encoding="utf-8"))
        self.assertEqual("v0 initial", run_coro(self.store.get_document_content("d1")))
        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(0, len(versions))

    def test_injected_failure_rolls_back_cleanly(self):
        run_coro(self.store.create_project("p1", "Acme"))
        run_coro(self.store.add_document("d1", "p1", "a.txt", "v0 initial"))

        conn = run_coro(self.store._ensure_open())
        orig_execute = conn.execute

        async def _failing_execute(sql, *args, **kwargs):
            if "UPDATE documents SET" in str(sql):
                raise RuntimeError("simulated SQL failure")
            return await orig_execute(sql, *args, **kwargs)

        conn.execute = _failing_execute
        try:
            with self.assertRaises(RuntimeError):
                run_coro(self.store.update_document("d1", "a.txt", "v1 failed update"))
        finally:
            conn.execute = orig_execute

        self.assertEqual("v0 initial", run_coro(self.store.get_document_content("d1")))
        versions = run_coro(self.store.list_versions("d1"))
        self.assertEqual(0, len(versions))
        snap_v1 = self.store._root / "p1" / "d1.v1.txt"
        self.assertFalse(snap_v1.exists())

        doc = run_coro(self.store.update_document("d1", "a.txt", "v1 successful"))
        self.assertEqual(1, doc["version"])

    def test_update_missing_document_raises(self):
        with self.assertRaises(ValueError):
            run_coro(self.store.update_document("nope", "a.txt", "x"))


if __name__ == "__main__":
    unittest.main()
