"""Tests for B2 audit store + B1/B3 REST endpoints (FastAPI TestClient)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit
from audit import AuditLogStore


def run_coro(coro):
    import asyncio

    return asyncio.run(coro)


class AuditLogStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AuditLogStore(db_path=Path(self._tmp.name))

    def tearDown(self):
        run_coro(self.store.close())
        self._tmp.cleanup()

    def test_append_and_recent(self):
        run_coro(self.store.append("project_create", actor="user", detail="p1"))
        run_coro(self.store.append("api_key_create", actor="master", detail="k1"))
        entries = run_coro(self.store.recent(limit=10))
        self.assertEqual(2, len(entries))
        self.assertEqual("api_key_create", entries[0]["event"])  # newest first
        self.assertEqual("user", entries[1]["actor"])

    def test_recent_limit(self):
        for i in range(5):
            run_coro(self.store.append(f"event_{i}"))
        entries = run_coro(self.store.recent(limit=2))
        self.assertEqual(2, len(entries))
        self.assertEqual("event_4", entries[0]["event"])


class AuditMcpTests(unittest.TestCase):
    def setUp(self):
        import apikeys

        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENMATRIX_LIBRARY_ROOT"] = self._tmp.name
        os.environ["OPENMATRIX_MASTER_KEY"] = "test-master"
        audit._default_audit = None
        apikeys._default_store = None

    def tearDown(self):
        import apikeys

        import asyncio

        if audit._default_audit is not None:
            asyncio.run(audit._default_audit.close())
            audit._default_audit = None
        if apikeys._default_store is not None:
            asyncio.run(apikeys._default_store.close())
            apikeys._default_store = None
        os.environ.pop("OPENMATRIX_MASTER_KEY", None)
        os.environ.pop("OPENMATRIX_LIBRARY_ROOT", None)
        self._tmp.cleanup()

    def test_audit_list_mcp(self):
        from mcp_server import audit_list

        out = json.loads(audit_list(limit=5, master_key="test-master"))
        self.assertIn("audit", out)

    def test_audit_list_wrong_master(self):
        from mcp_server import audit_list

        self.assertTrue(audit_list(limit=5, master_key="wrong").startswith("Error:"))


class RestApiKeyTests(unittest.TestCase):
    """B1/B3 via FastAPI TestClient (no server needed)."""

    def setUp(self):
        import apikeys

        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENMATRIX_KEYS_DB"] = os.path.join(self._tmp.name, "api_keys.db")
        os.environ["OPENMATRIX_MASTER_KEY"] = "test-master"
        os.environ["OPENMATRIX_AUDIT_DB"] = os.path.join(self._tmp.name, "audit.db")
        apikeys._default_store = None
        audit._default_audit = None
        from fastapi.testclient import TestClient

        import main

        self.client = TestClient(main.app)

    def tearDown(self):
        import apikeys

        import asyncio

        if apikeys._default_store is not None:
            asyncio.run(apikeys._default_store.close())
            apikeys._default_store = None
        if audit._default_audit is not None:
            asyncio.run(audit._default_audit.close())
            audit._default_audit = None
        os.environ.pop("OPENMATRIX_KEYS_DB", None)
        os.environ.pop("OPENMATRIX_MASTER_KEY", None)
        os.environ.pop("OPENMATRIX_AUDIT_DB", None)
        os.environ.pop("OPENMATRIX_ENABLE_AUTH", None)
        self._tmp.cleanup()

    def test_create_list_revoke_via_master(self):
        headers = {"Authorization": "Bearer test-master"}
        r = self.client.post("/api/v1/api-keys", params={"name": "dev"}, headers=headers)
        self.assertEqual(200, r.status_code)
        secret = r.json()["secret"]
        self.assertTrue(secret.startswith("om_"))

        r2 = self.client.get("/api/v1/api-keys", headers=headers)
        self.assertEqual(200, r2.status_code)
        self.assertNotIn("secret", json.dumps(r2.json()))

        key_id = r.json()["id"]
        r3 = self.client.delete(f"/api/v1/api-keys/{key_id}", headers=headers)
        self.assertEqual(200, r3.status_code)
        self.assertTrue(r3.json()["revoked"])

    def test_master_key_required(self):
        r = self.client.get("/api/v1/api-keys")
        self.assertEqual(401, r.status_code)

    def test_bearer_auth_middleware(self):
        # create a key, enable auth, then call a protected endpoint
        import apikeys

        rec = run_coro(apikeys.default_key_store().create_key("svc"))
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "1"
        # without token -> 401
        r = self.client.get("/api/v1/runs")
        self.assertEqual(401, r.status_code)
        # with valid API key -> passes (route may 200 or 404, but not 401)
        r2 = self.client.get("/api/v1/runs", headers={"Authorization": f"Bearer {rec['secret']}"})
        self.assertNotEqual(401, r2.status_code)
        # with invalid key -> 401
        r3 = self.client.get("/api/v1/runs", headers={"Authorization": "Bearer bad-key"})
        self.assertEqual(401, r3.status_code)

    def test_matrix_run_results_require_auth_when_enabled(self):
        import apikeys

        rec = run_coro(apikeys.default_key_store().create_key("results-reader"))
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "1"

        with patch("main.load_matrix_run", return_value={"run_id": "run-1", "cells": []}):
            self.assertEqual(401, self.client.get("/api/matrix-runs/run-1").status_code)
            self.assertEqual(
                401,
                self.client.get(
                    "/api/matrix-runs/run-1",
                    headers={"Authorization": "Bearer bad-key"},
                ).status_code,
            )
            response = self.client.get(
                "/api/matrix-runs/run-1",
                headers={"Authorization": f"Bearer {rec['secret']}"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("run-1", response.json()["run_id"])

    def test_matrix_run_results_remain_available_in_dev_mode(self):
        os.environ.pop("OPENMATRIX_ENABLE_AUTH", None)

        with patch("main.load_matrix_run", return_value={"run_id": "run-dev", "cells": []}):
            response = self.client.get("/api/matrix-runs/run-dev")

        self.assertEqual(200, response.status_code)
        self.assertEqual("run-dev", response.json()["run_id"])

    def test_legacy_result_and_analysis_routes_require_auth(self):
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "1"
        routes = (
            ("GET", "/api/export-csv/run-1"),
            ("GET", "/api/export-xlsx/run-1"),
            ("GET", "/api/runs/run-1/events"),
            ("POST", "/api/analyze_chain"),
            ("POST", "/api/analyze_batch"),
            ("POST", "/api/analyze-batch"),
        )

        for method, path in routes:
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual(401, response.status_code)

    def test_audit_rest(self):
        headers = {"Authorization": "Bearer test-master"}
        self.client.post("/api/v1/api-keys", params={"name": "dev"}, headers=headers)
        r = self.client.get("/api/v1/audit", headers=headers)
        self.assertEqual(200, r.status_code)
        self.assertTrue(len(r.json()["audit"]) >= 1)
        self.assertEqual("api_key_create", r.json()["audit"][0]["event"])

    def test_rbac_admin_only_paths_require_admin_role(self):
        # P7-1: with auth enabled, member/readonly keys get 403 on admin paths;
        # admin key passes; master key passes.
        import apikeys

        rec_member = run_coro(apikeys.default_key_store().create_key("m1", role="member"))
        rec_admin = run_coro(apikeys.default_key_store().create_key("a1", role="admin"))
        rec_ro = run_coro(apikeys.default_key_store().create_key("r1", role="readonly"))
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "1"

        member_h = {"Authorization": f"Bearer {rec_member['secret']}"}
        ro_h = {"Authorization": f"Bearer {rec_ro['secret']}"}
        admin_h = {"Authorization": f"Bearer {rec_admin['secret']}"}
        master_h = {"Authorization": "Bearer test-master"}

        # admin-only path /api/v1/audit:
        # member/readonly keys blocked at RBAC layer (403); admin key passes
        # the RBAC layer but the audit endpoint still requires the master key
        # (401, existing B2 behavior); master key passes (200).
        self.assertEqual(403, self.client.get("/api/v1/audit", headers=member_h).status_code)
        self.assertEqual(403, self.client.get("/api/v1/audit", headers=ro_h).status_code)
        self.assertEqual(401, self.client.get("/api/v1/audit", headers=admin_h).status_code)
        self.assertEqual(200, self.client.get("/api/v1/audit", headers=master_h).status_code)
        # admin-only path /api/v1/api-keys: member blocked (403), admin passes RBAC
        # then hits the endpoint's master-key requirement (401)
        self.assertEqual(403, self.client.get("/api/v1/api-keys", headers=member_h).status_code)
        self.assertEqual(401, self.client.get("/api/v1/api-keys", headers=admin_h).status_code)

    def test_rbac_admin_role_key_can_manage_keys(self):
        # an admin-role key passes the RBAC middleware but the key-management
        # endpoints still require the master key (B1 behavior): not 403, but 401.
        import apikeys

        rec_admin = run_coro(apikeys.default_key_store().create_key("a2", role="admin"))
        os.environ["OPENMATRIX_ENABLE_AUTH"] = "1"
        headers = {"Authorization": f"Bearer {rec_admin['secret']}"}
        r = self.client.post("/api/v1/api-keys?name=child", headers=headers)
        self.assertEqual(401, r.status_code)


if __name__ == "__main__":
    unittest.main()
