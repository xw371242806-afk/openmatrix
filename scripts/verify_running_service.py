import argparse
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_api import FakeOpenAIHandler, make_pdf


def multipart_form_data(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----openmatrix-{uuid.uuid4().hex}"
    chunks = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    for name, (filename, content, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def post_analyze(service_url: str, fake_model_url: str, model_name: str, pdf: bytes) -> tuple[int, dict]:
    body, boundary = multipart_form_data(
        {
            "api_key": "test-key",
            "base_url": fake_model_url,
            "model_name": model_name,
            "questions": json.dumps(["What was total revenue?"]),
        },
        {"file": ("fixture.pdf", pdf, "application/pdf")},
    )
    request = Request(
        f"{service_url}/api/analyze",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def assert_running_service(service_url: str) -> None:
    try:
        with urlopen(f"{service_url}/static/index.html", timeout=10) as response:
            if response.status != 200:
                raise AssertionError(f"Expected UI status 200, got {response.status}")
    except URLError as err:
        raise AssertionError(f"OpenMatrix service is not reachable at {service_url}: {err}") from err


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the OpenMatrix running service end-to-end.")
    parser.add_argument("--service-url", default="http://127.0.0.1:8000", help="Base URL of a running OpenMatrix service")
    args = parser.parse_args(argv)
    service_url = args.service_url.rstrip("/")

    import os
    import tempfile

    # MATRIX_DB_PATH treats OPENMATRIX_RUN_DB as a *directory* and appends
    # openmatrix_runs.sqlite3. Give a clear diagnostic when the value is not a
    # writable directory so failures are not opaque.
    configured = os.environ.get("OPENMATRIX_RUN_DB", tempfile.gettempdir())
    db_dir = Path(configured)
    if not db_dir.is_dir():
        raise SystemExit(
            f"OPENMATRIX_RUN_DB must be an existing directory, got: {configured!r}. "
            "The service appends openmatrix_runs.sqlite3 inside it."
        )
    if not os.access(db_dir, os.W_OK):
        raise SystemExit(f"OPENMATRIX_RUN_DB directory is not writable: {db_dir}")

    assert_running_service(service_url)

    fake_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
    thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
    thread.start()
    fake_model_url = f"http://127.0.0.1:{fake_server.server_port}/v1"

    pdf = make_pdf("OpenMatrix Test Company 2025 Annual Report. Total revenue was 125 million dollars.")

    try:
        status, payload = post_analyze(service_url, fake_model_url, "exact-model", pdf)
        assert status == 200, payload
        assert payload["status"] == "success", payload
        row = payload["data"][0]
        assert row["answer"] == "125 million dollars.", payload
        assert row["quote"] == "Total revenue was 125 million dollars.", payload
        assert row["citation_status"] == "verified_exact", payload
        assert row["page"] == 1, payload
        run_id = payload["run_id"]
        assert run_id, payload

        # The run is persisted; the export chain must work against the real service.
        for export_path in (f"/api/export-csv/{run_id}", f"/api/export-xlsx/{run_id}", f"/api/matrix-runs/{run_id}"):
            try:
                with urlopen(f"{service_url}{export_path}", timeout=30) as response:
                    assert response.status == 200, export_path
            except HTTPError as err:
                raise AssertionError(f"export path {export_path} returned {err.code}") from err

        status, payload = post_analyze(service_url, fake_model_url, "hallucination-model", pdf)
        assert status == 200, payload
        assert payload["status"] == "needs_review", payload
        assert payload["data"][0]["citation_status"] == "not_found", payload

        status, payload = post_analyze(service_url, fake_model_url, "api-error-model", pdf)
        assert status == 500, payload
        assert "Invalid API key" in payload["error"], payload

        status, payload = post_analyze(service_url, fake_model_url, "exact-model", b"not a pdf")
        assert status == 400, payload
        assert "Invalid PDF" in payload["error"], payload

        print(json.dumps({"status": "passed", "service_url": service_url, "run_id": run_id}, indent=2))
        return 0
    finally:
        fake_server.shutdown()
        fake_server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
