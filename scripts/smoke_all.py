#!/usr/bin/env python
"""One-step P0-B readiness smoke: rerun all verified evidence in order.

Aggregates the existing verification gates into a single reproducible command:

    1. unit tests      python -m unittest discover -v
    2. retrieval eval  python main.py --eval-retrieval
    3. frontend static python -m unittest tests.test_frontend_static
    4. service E2E     start server -> scripts/verify_running_service.py
    5. browser smoke   node scripts/browser-smoke.cjs (only when node is found)

Exits 0 only when every stage passes. Each stage's stdout/stderr and exit code
are surfaced so reviewers can reproduce evidence from one invocation.

Usage:
    python scripts/smoke_all.py [--skip-browser]
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(cmd, stage, env=None) -> bool:
    print(f"\n=== {stage} ===")
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, cwd=ROOT, env=merged)
    ok = result.returncode == 0
    print(f"--- {stage}: {'PASS' if ok else 'FAIL (exit %d)' % result.returncode} ---")
    return ok


def wait_for_service(url, timeout_ms=30000) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def service_e2e(openmatrix_run_db: str) -> bool:
    print("\n=== running-service E2E ===")
    # Allocate a free port for the test server to avoid colliding with stale instances.
    import socket
    sock = socket.socket()
    sock.bind(("", 0))
    free_port = sock.getsockname()[1]
    sock.close()
    env = {**os.environ, "OPENMATRIX_RUN_DB": openmatrix_run_db, "OPENMATRIX_PORT": str(free_port)}
    server = subprocess.Popen(
        [PYTHON, "main.py"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_service(f"http://127.0.0.1:{free_port}/static/index.html"):
            print("--- running-service E2E: FAIL (server did not become ready) ---")
            return False
        result = subprocess.run(
            [PYTHON, "scripts/verify_running_service.py", "--service-url", f"http://127.0.0.1:{free_port}"],
            cwd=ROOT,
            env=env,
        )
        ok = result.returncode == 0
        print(f"--- running-service E2E: {'PASS' if ok else 'FAIL (exit %d)' % result.returncode} ---")
        return ok
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def browser_smoke():
    """Run the headless-browser smoke; returns True/False/None (None = skipped)."""
    if os.environ.get("OPENMATRIX_SKIP_BROWSER", ""):
        print("\n=== browser smoke: SKIPPED (OPENMATRIX_SKIP_BROWSER set) ===")
        return None
    if subprocess.run(["node", "--version"], capture_output=True).returncode != 0:
        print("\n=== browser smoke: FAIL (node not found) ===")
        return False
    # playwright-core is an optional dev dependency; without it the browser
    # smoke cannot run and is reported as failure (unless explicitly skipped),
    # ensuring readiness reflects missing browser tooling.
    probe = subprocess.run(
        ["node", "-e", "require('playwright-core'); process.exit(0)"],
        capture_output=True,
    )
    if probe.returncode != 0:
        print("\n=== browser smoke: FAIL (playwright-core not installed) ===")
        return False
    result = subprocess.run(["node", "scripts/browser-smoke.cjs"], cwd=ROOT)
    ok = result.returncode == 0
    print(f"--- browser smoke: {'PASS' if ok else 'FAIL (exit %d)' % result.returncode} ---")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-browser", action="store_true", help="skip the optional headless-browser smoke")
    args = parser.parse_args()

    if args.skip_browser:
        os.environ["OPENMATRIX_SKIP_BROWSER"] = "1"

    run_db = os.environ.get("OPENMATRIX_RUN_DB", "") or os.path.join(tempfile.gettempdir(), "openmatrix_smoke_all_runs")
    Path(run_db).mkdir(parents=True, exist_ok=True)

    stages = [
        ("unit tests", lambda: run([PYTHON, "-m", "unittest", "discover", "-v"], "unit tests")),
        ("retrieval eval", lambda: run([PYTHON, "main.py", "--eval-retrieval"], "retrieval eval")),
        ("frontend static", lambda: run([PYTHON, "-m", "unittest", "tests.test_frontend_static"], "frontend static")),
        ("service E2E", lambda: service_e2e(run_db)),
        ("browser smoke", browser_smoke),
    ]

    results = {}
    for name, stage_fn in stages:
        results[name] = stage_fn()

    print("\n=== P0-B readiness smoke summary ===")
    all_ok = True
    for name, ok in results.items():
        if ok is None:
            print(f"SKIP  {name}")
        else:
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
            all_ok = all_ok and ok

    print(f"\nOVERALL: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
