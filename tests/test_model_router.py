import unittest
from unittest import mock

from model_router import (
    TASK_BATCH,
    TASK_CHAIN,
    TASK_FAST,
    ModelRouter,
)


class ModelRouterTests(unittest.TestCase):
    def test_disabled_router_always_returns_default(self):
        router = ModelRouter("gpt-test", enabled=False)
        with mock.patch.dict("os.environ", {f"OPENMATRIX_MODEL_{TASK_FAST.upper()}": "fast-model"}):
            self.assertEqual(router.resolve(TASK_FAST), "gpt-test")
            self.assertEqual(router.resolve(TASK_CHAIN), "gpt-test")
            self.assertEqual(router.resolve(TASK_BATCH), "gpt-test")

    def test_enabled_router_returns_env_override_per_task(self):
        router = ModelRouter("gpt-test", enabled=True)
        env = {
            "OPENMATRIX_MODEL_FAST": "flash-1",
            "OPENMATRIX_MODEL_CHAIN": "reasoner-1",
        }
        with mock.patch.dict("os.environ", env):
            self.assertEqual(router.resolve(TASK_FAST), "flash-1")
            self.assertEqual(router.resolve(TASK_CHAIN), "reasoner-1")
            # batch not configured -> default
            self.assertEqual(router.resolve(TASK_BATCH), "gpt-test")

    def test_blank_override_falls_back_to_default(self):
        router = ModelRouter("gpt-test", enabled=True)
        with mock.patch.dict("os.environ", {"OPENMATRIX_MODEL_FAST": "   "}):
            self.assertEqual(router.resolve(TASK_FAST), "gpt-test")

    def test_unknown_task_returns_default_even_when_enabled(self):
        router = ModelRouter("gpt-test", enabled=True)
        self.assertEqual(router.resolve("unknown"), "gpt-test")

    def test_empty_default_model_rejected(self):
        with self.assertRaises(ValueError):
            ModelRouter("")
        with self.assertRaises(ValueError):
            ModelRouter("   ")

    def test_describe_reflects_config(self):
        router = ModelRouter("gpt-test", enabled=True)
        with mock.patch.dict("os.environ", {"OPENMATRIX_MODEL_FAST": "flash-1"}):
            info = router.describe()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["default_model"], "gpt-test")
        self.assertEqual(info["tasks"]["fast"]["model"], "flash-1")
        self.assertEqual(info["tasks"]["fast"]["env"], "OPENMATRIX_MODEL_FAST")


if __name__ == "__main__":
    unittest.main()
