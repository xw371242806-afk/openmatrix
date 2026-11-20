"""Tests for P4-2 / H2 MCP tools (project_* / document_* / project_search)."""

import json
import os
import tempfile
import unittest

import library
import mcp_server
from mcp_server import (
    document_add,
    document_delete,
    document_list,
    project_create,
    project_delete,
    project_list,
    project_search,
)


def setUpModule():
    global _tmp
    if os.environ.get("OPENMATRIX_LIBRARY_ROOT") is None:
        _tmp = tempfile.TemporaryDirectory()
        os.environ["OPENMATRIX_LIBRARY_ROOT"] = _tmp.name
    # fresh store per run
    library._default_store = None


def tearDownModule():
    import asyncio

    if library._default_store is not None:
        asyncio.run(library._default_store.close())
        library._default_store = None
    if os.environ.get("OPENMATRIX_LIBRARY_ROOT") and "_tmp" in globals():
        _tmp.cleanup()


class ProjectMcpToolsTests(unittest.TestCase):
    def test_project_create_list(self):
        out = project_create("Acme", "quarterly reports")
        proj = json.loads(out)
        self.assertTrue(proj["id"].startswith("p-"))
        self.assertEqual("Acme", proj["name"])

        listed = json.loads(project_list())
        self.assertTrue(any(p["id"] == proj["id"] for p in listed["projects"]))

    def test_project_create_invalid_name(self):
        self.assertTrue(project_create("", "x").startswith("Error:"))
        self.assertTrue(project_create(123, "x").startswith("Error:"))

    def test_document_add_list(self):
        proj = json.loads(project_create("Docs", None))
        out = document_add(proj["id"], "a.txt", "Revenue was 125 million dollars.")
        doc = json.loads(out)
        self.assertEqual("a.txt", doc["filename"])
        self.assertEqual("indexed", doc["status"])

        listed = json.loads(document_list(proj["id"]))
        self.assertEqual(1, len(listed["documents"]))
        self.assertEqual(doc["id"], listed["documents"][0]["id"])

    def test_document_add_missing_project(self):
        self.assertTrue(document_add("nope", "a.txt", "x").startswith("Error:"))

    def test_document_delete(self):
        proj = json.loads(project_create("Docs2", None))
        doc = json.loads(document_add(proj["id"], "a.txt", "hello"))
        out = json.loads(document_delete(doc["id"]))
        self.assertTrue(out["removed"])

    def test_project_search_finds_evidence(self):
        proj = json.loads(project_create("Search", None))
        document_add(proj["id"], "r.txt", "Total revenue was 125 million dollars in 2025.")
        document_add(proj["id"], "d.txt", "Debt stood at 40 million dollars.")
        out = json.loads(project_search(proj["id"], "how much revenue", limit=2))
        hits = out["hits"]
        self.assertTrue(len(hits) >= 1)
        self.assertIn(hits[0]["citation_status"], ("verified_exact", "needs_review"))

    def test_project_search_excludes_unindexed(self):
        proj = json.loads(project_create("NoIdx", None))
        document_add(proj["id"], "a.txt", "Revenue was 125 million dollars.", index=False)
        out = json.loads(project_search(proj["id"], "revenue", limit=2))
        self.assertEqual([], out["hits"])

    def test_project_search_later_created_document_strongest_match_limit_one(self):
        proj = json.loads(project_create("LaterDocMatch", None))
        doc1 = json.loads(document_add(proj["id"], "first.txt", "Weather in Seattle is rainy throughout November."))
        doc2 = json.loads(
            document_add(
                proj["id"],
                "second.txt",
                "Quantum computing breakthrough achieves fault tolerant superconducting qubits.",
            )
        )
        out = json.loads(project_search(proj["id"], "quantum computing fault tolerant superconducting qubits", limit=1))
        hits = out["hits"]
        self.assertEqual(1, len(hits))
        self.assertEqual(doc2["id"], hits[0]["document_id"])
        self.assertEqual("second.txt", hits[0]["filename"])
        self.assertIsNone(hits[0]["page"])
        self.assertEqual(1,hits[0]["section"])

    def test_project_search_global_relevance_ordering_across_multiple_docs(self):
        proj = json.loads(project_create("GlobalRanking", None))
        doc1 = json.loads(
            document_add(
                proj["id"],
                "doc1.txt",
                "Partial match mentions quantum algorithms briefly.",
            )
        )
        doc2 = json.loads(
            document_add(
                proj["id"],
                "doc2.txt",
                "Exact target: quantum computing fault tolerant superconducting qubits architectures.",
            )
        )
        doc3 = json.loads(
            document_add(
                proj["id"],
                "doc3.txt",
                "Irrelevant agricultural soil rotation techniques and farming practices.",
            )
        )
        out = json.loads(
            project_search(proj["id"], "quantum computing fault tolerant superconducting qubits", limit=2)
        )
        hits = out["hits"]
        self.assertEqual(2, len(hits))
        # doc3 is irrelevant and must not rank above quantum-relevant docs
        self.assertNotEqual(doc3["id"], hits[0]["document_id"])
        self.assertNotEqual(doc3["id"], hits[1]["document_id"])
        relevant_ids = {doc1["id"], doc2["id"]}
        self.assertTrue(all(h["document_id"] in relevant_ids for h in hits))

    def test_project_search_preserves_provenance(self):
        proj = json.loads(project_create("Prov", None))
        document_add(proj["id"], "r.txt", "Revenue was 125 million dollars in 2025.")
        document_add(proj["id"], "capex.txt", "Capex reached 9 million dollars.")
        out = json.loads(project_search(proj["id"], "how much revenue", limit=3))
        hits = out["hits"]
        self.assertTrue(len(hits) >= 1)
        hit = next(h for h in hits if "revenue" in h["quote"].lower())
        self.assertEqual("r.txt", hit["filename"])
        self.assertIsNone(hit["page"])
        self.assertEqual(1,hit["section"])
        self.assertIsInstance(hit["document_id"], str)
        # Unpaged text is verified against its source section.
        if hit["citation_status"] == "verified_exact":
            self.assertIn(hit["quote"].strip(), "Revenue was 125 million dollars in 2025.")

    def test_project_search_missing_project(self):
        self.assertTrue(project_search("nope", "q").startswith("Error:"))

    def test_project_delete_cascades(self):
        proj = json.loads(project_create("Del", None))
        document_add(proj["id"], "a.txt", "x")
        out = json.loads(project_delete(proj["id"]))
        self.assertTrue(out["removed"])
        listed = json.loads(document_list(proj["id"]))
        self.assertEqual(0, len(listed["documents"]))

    def test_document_versions_flat_shape(self):
        from mcp_server import document_update, document_versions

        proj = json.loads(project_create("Vers", None))
        doc = json.loads(document_add(proj["id"], "a.txt", "v1 content", page_count=3))
        document_update(doc["id"], "a.txt", "v2 content", page_count=5)
        versions = json.loads(document_versions(doc["id"]))
        # contract: top-level flat list, no content_path
        self.assertIsInstance(versions, list)
        self.assertEqual(1, len(versions))
        self.assertEqual({"version", "filename", "created_at"}, set(versions[0].keys()))
        self.assertNotIn("content_path", json.dumps(versions))

    def test_document_restore_preserves_page_count(self):
        from mcp_server import document_restore, document_update, document_versions

        proj = json.loads(project_create("Rest", None))
        doc = json.loads(document_add(proj["id"], "a.txt", "v1 content", page_count=3))
        document_update(doc["id"], "a.txt", "v2 content", page_count=5)
        restored = json.loads(document_restore(doc["id"], 1))
        self.assertEqual(3, restored["page_count"])  # snapshot page_count restored

    def test_document_versions_missing_doc_is_error(self):
        from mcp_server import document_versions

        self.assertTrue(document_versions("nope").startswith("Error:"))


if __name__ == "__main__":
    unittest.main()
