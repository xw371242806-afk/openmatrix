import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_smoke.py"


class FixtureHandler(BaseHTTPRequestHandler):
    marker_received = False
    auth_received = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "")
        content = body.get("messages", [{}])
        user_text = next((m.get("content", "") for m in content if m.get("role") == "user"), "")

        self.__class__.auth_received = self.headers.get("Authorization")
        self.__class__.marker_received = "OPENMATRIX_SMOKE_OK" in user_text.upper()
        # A twisted provider may echo back an unrelated token; model name drives it.
        marker_echo = "UNRELATED_TOKEN" if model == "twisted-model" else "OPENMATRIX_SMOKE_OK"
        payload = json.dumps(
            {
                "id": "chatcmpl-smoke",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": marker_echo}}],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class ModelSmokeScriptTests(unittest.TestCase):
    def run_script(self, env: dict) -> subprocess.CompletedProcess:
        base_env = {k: v for k, v in os.environ.items() if "OPENMATRIX_MODEL_" not in k}
        base_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env=base_env,
            timeout=60,
        )

    def test_missing_env_exits_2(self):
        result = self.run_script({})
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required env", result.stdout)

    def test_suspicious_model_name_exits_2(self):
        result = self.run_script(
            {
                "OPENMATRIX_MODEL_URL": "http://127.0.0.1:9999/v1",
                "OPENMATRIX_MODEL_NAME": "model name with spaces; rm -rf",
                "OPENMATRIX_MODEL_KEY": "sk-test",
            }
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unexpected characters", result.stdout)

    def test_never_prints_the_key(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {
                "OPENMATRIX_MODEL_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "OPENMATRIX_MODEL_NAME": "fixture-model",
                "OPENMATRIX_MODEL_KEY": "sk-super-secret-token-xyz",
            }
            result = self.run_script(env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("sk-super-secret-token-xyz", result.stdout)
            self.assertNotIn("sk-super-secret-token-xyz", result.stderr)
            expected_auth = "Bearer " + "sk-super-secret-token-xyz"
            self.assertEqual(FixtureHandler.auth_received, expected_auth)
            self.assertTrue(FixtureHandler.marker_received)
            self.assertIn("PASS", result.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_non_marker_response_exits_3(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {
                "OPENMATRIX_MODEL_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "OPENMATRIX_MODEL_NAME": "twisted-model",
                "OPENMATRIX_MODEL_KEY": "sk-test",
            }
            result = self.run_script(env)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("no fixture marker", result.stdout)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()