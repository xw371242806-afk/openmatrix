"""Tests for H4 MCP tools (apikey_create/list/revoke/verify)."""

import json
import os
import tempfile
import unittest

import apikeys
from mcp_server import (
    apikey_create,
    apikey_list,
    apikey_revoke,
    apikey_verify,
)

MASTER = "test-master-key-abc"


def run(coro):
    import asyncio

    return asyncio.run(coro)


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["OPENMATRIX_KEYS_DB"] = os.path.join(_tmp.name, "api_keys.db")
    os.environ["OPENMATRIX_MASTER_KEY"] = MASTER
    apikeys._default_store = None


def tearDownModule():
    import asyncio

    if apikeys._default_store is not None:
        asyncio.run(apikeys._default_store.close())
        apikeys._default_store = None
    os.environ.pop("OPENMATRIX_MASTER_KEY", None)
    os.environ.pop("OPENMATRIX_KEYS_DB", None)
    _tmp.cleanup()


class ApiKeyMcpToolsTests(unittest.TestCase):
    def test_create_returns_secret_once(self):
        out = json.loads(run(apikey_create("dev", MASTER)))
        self.assertTrue(out["secret"].startswith("om_"))
        self.assertEqual(out["prefix"], out["secret"][:8])

    def test_create_rejects_wrong_master(self):
        self.assertTrue(run(apikey_create("dev", "wrong")).startswith("Error:"))

    def test_create_rejects_missing_master(self):
        self.assertTrue(run(apikey_create("dev", None)).startswith("Error:"))

    def test_list_no_secrets(self):
        run(apikey_create("dev", MASTER))
        data = json.loads(run(apikey_list(MASTER)))
        self.assertTrue(len(data["api_keys"]) >= 1)
        self.assertNotIn("secret", json.dumps(data))

    def test_revoke(self):
        rec = json.loads(run(apikey_create("dev", MASTER)))
        out = json.loads(run(apikey_revoke(rec["id"], MASTER)))
        self.assertTrue(out["revoked"])
        self.assertTrue(run(apikey_verify(rec["secret"])).startswith("Error:"))

    def test_verify_valid(self):
        rec = json.loads(run(apikey_create("dev", MASTER)))
        out = json.loads(run(apikey_verify(rec["secret"])))
        self.assertEqual(rec["id"], out["id"])

    def test_verify_invalid(self):
        self.assertTrue(run(apikey_verify("om_not_a_real_key")).startswith("Error:"))

    def test_concurrent_create_list_verify_first_use(self):
        """Concurrent first-use initialization must not create duplicate connections."""
        import asyncio

        apikeys._default_store = None  # force re-init

        async def exercise():
            coros = [
                apikey_create("c1", MASTER),
                apikey_create("c2", MASTER),
                apikey_list(MASTER),
            ]
            results = await asyncio.gather(*coros)
            return results

        results = asyncio.run(exercise())
        self.assertEqual(3, len(results))
        self.assertTrue(all(not r.startswith("Error:") for r in results))


if __name__ == "__main__":
    unittest.main()
