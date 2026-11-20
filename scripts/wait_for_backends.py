#!/usr/bin/env python3
"""Bounded startup readiness probe for the configured production backends.

Used as a preflight step in the production compose overlay so the app only
starts after PostgreSQL and/or MinIO/S3 are actually ready. When a backend is
explicitly configured (via OPENMATRIX_METADATA_ENGINE/OPENMATRIX_OBJECT_STORE)
this waits up to --timeout seconds; an unreachable backend then exits non-zero,
which (combined with the storage factory's fail-closed default) makes the
production path deterministic instead of silently running on SQLite/local disk.

Exit codes:
  0 - ready (or no production backend configured)
  1 - a configured backend did not become ready in time
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _wait(probe, label: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if probe():
                print(f"readiness: {label} ready")
                return
        except Exception as exc:  # noqa: BLE001 - probe may raise while booting
            last = exc
            print(f"readiness: {label} probe raised: {exc!r}", file=sys.stderr)
        time.sleep(2)
    raise SystemExit(f"readiness: {label} not ready within {timeout:.0f}s (last error: {last!r})")


def _pg_probe(dsn: str) -> bool:
    """Reachability probe with error surfaced for diagnosis."""
    import asyncio
    import psycopg

    async def _run() -> bool:
        conn = await psycopg.AsyncConnection.connect(dsn, connect_timeout=10.0)
        try:
            async with conn.transaction():
                await conn.execute("SELECT 1")
            return True
        finally:
            await conn.close()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"readiness: postgres connect failed: {exc!r}", file=sys.stderr)
        return False


def _s3_probe(candidate) -> bool:
    """MinIO/S3 readiness probe with error surfaced for diagnosis."""
    try:
        return bool(candidate.ensure_bucket() and candidate.health_check())
    except Exception as exc:  # noqa: BLE001
        print(f"readiness: s3 probe raised: {exc!r}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-backend wait budget in seconds")
    args = parser.parse_args()

    import storage

    engine = os.environ.get("OPENMATRIX_METADATA_ENGINE", "sqlite").strip().lower()
    if engine == "postgres":
        dsn = os.environ.get("OPENMATRIX_PG_DSN")
        if not dsn:
            raise SystemExit("readiness: OPENMATRIX_METADATA_ENGINE=postgres but OPENMATRIX_PG_DSN is not set")
        _wait(lambda: _pg_probe(dsn), "postgres", args.timeout)

    ostore = os.environ.get("OPENMATRIX_OBJECT_STORE", "local").strip().lower()
    if ostore == "s3":
        bucket = os.environ.get("OPENMATRIX_S3_BUCKET")
        if not bucket:
            raise SystemExit("readiness: OPENMATRIX_OBJECT_STORE=s3 but OPENMATRIX_S3_BUCKET is not set")
        candidate = storage.S3ObjectStore(bucket=bucket)
        _wait(lambda: _s3_probe(candidate), "s3", args.timeout)

    print("readiness: all configured backends ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())