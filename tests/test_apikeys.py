"""Tests for H4 apikeys.py (ApiKeyStore)."""

import asyncio
import tempfile
import unittest
from pathlib import Path

import apikeys
from apikeys import ApiKeyStore, hash_key, master_key_configured, require_master_key


def run_coro(coro):
    return asyncio.run(coro)


class ApiKeyStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ApiKeyStore(db_path=Path(self._tmp.name))

    def tearDown(self):
        run_coro(self.store.close())
        self._tmp.cleanup()

    def test_create_key_returns_secret_once(self):
        rec = run_coro(self.store.create_key("dev"))
        self.assertTrue(rec["secret"].startswith("om_"))
        self.assertEqual(rec["prefix"], rec["secret"][:8])
        # secret not stored plaintext
        self.assertNotIn(rec["secret"], str(run_coro(self.store.list_keys())))

    def test_list_keys_metadata_only(self):
        run_coro(self.store.create_key("dev"))
        run_coro(self.store.create_key("prod"))
        keys = run_coro(self.store.list_keys())
        self.assertEqual(2, len(keys))
        self.assertNotIn("secret", keys[0])
        self.assertIn("prefix", keys[0])

    def test_verify_valid_key(self):
        rec = run_coro(self.store.create_key("dev"))
        verified = run_coro(self.store.verify_key(rec["secret"]))
        self.assertEqual(rec["id"], verified["id"])

    def test_verify_wrong_key(self):
        rec = run_coro(self.store.create_key("dev"))
        self.assertIsNone(run_coro(self.store.verify_key("om_wrong")))

    def test_revoke_key(self):
        rec = run_coro(self.store.create_key("dev"))
        self.assertTrue(run_coro(self.store.revoke_key(rec["id"])))
        # double revoke returns False
        self.assertFalse(run_coro(self.store.revoke_key(rec["id"])))
        self.assertIsNone(run_coro(self.store.verify_key(rec["secret"])))
        keys = run_coro(self.store.list_keys())
        self.assertEqual(1, keys[0]["revoked"])

    def test_invalid_name_rejected(self):
        with self.assertRaises(ValueError):
            run_coro(self.store.create_key("bad name with !!! too long value here"))
        with self.assertRaises(ValueError):
            run_coro(self.store.create_key(""))


class MasterKeyTests(unittest.TestCase):
    def test_configured_flag(self):
        self.assertIsInstance(master_key_configured(), bool)

    def test_require_master_key_constant_time(self):
        import os

        os.environ["OPENMATRIX_MASTER_KEY"] = "correct horse battery staple"
        try:
            self.assertTrue(require_master_key("correct horse battery staple"))
            self.assertFalse(require_master_key("wrong"))
            self.assertFalse(require_master_key(None))
            self.assertFalse(require_master_key(""))
        finally:
            del os.environ["OPENMATRIX_MASTER_KEY"]

    def test_require_master_key_unset(self):
        import os

        os.environ.pop("OPENMATRIX_MASTER_KEY", None)
        self.assertFalse(require_master_key("anything"))

    def test_hash_key_deterministic(self):
        self.assertEqual(hash_key("abc"), hash_key("abc"))
        self.assertNotEqual(hash_key("abc"), hash_key("abd"))


class ApiKeyRbacTests(unittest.TestCase):
    """P7-1: role support in ApiKeyStore."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ApiKeyStore(db_path=Path(self._tmp.name))

    def tearDown(self):
        run_coro(self.store.close())
        self._tmp.cleanup()

    def test_create_key_with_role(self):
        rec = run_coro(self.store.create_key("adm", role="admin"))
        self.assertEqual("admin", rec["role"])
        rec2 = run_coro(self.store.create_key("ro", role="readonly"))
        self.assertEqual("readonly", rec2["role"])

    def test_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            run_coro(self.store.create_key("bad", role="superuser"))

    def test_verify_key_returns_role(self):
        rec = run_coro(self.store.create_key("member1", role="member"))
        verified = run_coro(self.store.verify_key(rec["secret"]))
        self.assertEqual("member", verified["role"])
        self.assertIn("role", verified)

    def test_default_role_is_member(self):
        rec = run_coro(self.store.create_key("plain"))
        self.assertEqual("member", rec["role"])

    def test_list_keys_include_role(self):
        run_coro(self.store.create_key("x", role="readonly"))
        keys = run_coro(self.store.list_keys())
        self.assertEqual("readonly", keys[0]["role"])


if __name__ == "__main__":
    unittest.main()
