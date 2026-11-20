#!/usr/bin/env python3
"""Verify the production PG+S3 storage backends against a live stack.

Requires reachable PostgreSQL + MinIO/S3 (e.g. from the CI prod-storage job or
`docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod up`).

Exercises:
  - S3ObjectStore: ensure_bucket (idempotent create) + put/get/delete round-trip
  - PostgresMetadataStore: project/document CRUD + versioned update + content
    round-trip through the configured ObjectStore
  - effective_storage_engines() reports postgres/s3 when configured

Exit code 0 on success; prints a short summary.
"""

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _s3_check() -> None:
    import time

    import storage

    bucket = os.environ.get("OPENMATRIX_S3_BUCKET")
    if not bucket:
        raise SystemExit("OPENMATRIX_S3_BUCKET is required")
    store = storage.S3ObjectStore(bucket=bucket)
    # MinIO may still be starting; poll briefly so CI services without a
    # container health-cmd still pass.
    last = None
    for _ in range(30):
        try:
            if store.ensure_bucket() and store.health_check():
                break
        except Exception as exc:  # noqa: BLE001 - unreachable while booting
            last = exc
        time.sleep(2)
    else:
        raise SystemExit(f"S3 unreachable after polling (last error: {last!r})")
    key = f"verify/{uuid.uuid4().hex}.txt"
    store.put(key, b"prod-storage-check")
    got = store.get(key)
    if got != b"prod-storage-check":
        raise SystemExit("S3 put/get round-trip mismatch")
    if not store.delete(key):
        raise SystemExit("S3 delete failed")
    print("S3ObjectStore: bucket ensured, put/get/delete round-trip OK")


async def _pg_check() -> None:
    import time

    import storage

    dsn = os.environ.get("OPENMATRIX_PG_DSN")
    if not dsn:
        raise SystemExit("OPENMATRIX_PG_DSN is required")
    store = storage.PostgresMetadataStore(dsn=dsn)
    try:
        last = None
        for _ in range(30):
            try:
                if await store.health_check():
                    break
            except Exception as exc:  # noqa: BLE001 - unreachable while booting
                last = exc
            await asyncio.sleep(2)
        else:
            raise SystemExit(f"PG unreachable after polling (last error: {last!r})")
        pid = f"v-{uuid.uuid4().hex[:8]}"
        did = f"d-{uuid.uuid4().hex[:8]}"
        await store.create_project(pid, "verify")
        try:
            await store.add_document(did, pid, "verify.txt", "content-one", page_count=1)
            doc = await store.get_document(did)
            if not doc or doc["filename"] != "verify.txt":
                raise SystemExit("PG get_document mismatch")
            content = await store.get_document_content(did)
            if content != "content-one":
                raise SystemExit("PG content round-trip mismatch")
            updated = await store.update_document(did, "verify.txt", "content-two", page_count=1)
            if await store.get_document_content(did) != "content-two":
                raise SystemExit("PG update round-trip mismatch")
            versions = await store.list_versions(did)
            if len(versions) < 1:
                raise SystemExit("PG version snapshot missing after update")
            # Concurrent updates from SEPARATE connections (one store instance
            # per writer) must serialize version allocation via the SELECT ...
            # FOR UPDATE row lock - a single store's in-process lock cannot
            # prove this because its coroutines serialize on that one lock.
            concurrent = 5
            writers = [storage.PostgresMetadataStore(dsn=dsn) for _ in range(concurrent)]
            try:
                await asyncio.gather(
                    *[
                        s.update_document(did, "verify.txt", f"content-conc-{i}", page_count=1)
                        for i, s in enumerate(writers)
                    ]
                )
            finally:
                for s in writers:
                    await s.close()
            versions = await store.list_versions(did)
            version_nums = sorted(v["version"] for v in versions)
            if len(versions) != 1 + concurrent or len(set(version_nums)) != len(version_nums):
                raise SystemExit(
                    f"PG concurrent updates not serialized: expected {1 + concurrent} distinct "
                    f"versions, got {len(versions)} ({version_nums})"
                )
        finally:
            await store.delete_document(did)
        # project delete cascades rows
        await store.delete_project(pid)
        if await store.get_project(pid) is not None:
            raise SystemExit("PG project delete did not remove row")
    finally:
        await store.close()
    print("PostgresMetadataStore: project/document CRUD + versioning OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", action="store_true", help="also assert effective_storage_engines")
    args = parser.parse_args()

    _s3_check()
    asyncio.run(_pg_check())

    if args.engines:
        import storage

        engines = storage.effective_storage_engines()
        if engines != {"metadata": "postgres", "object": "s3"}:
            raise SystemExit(f"unexpected effective engines: {engines}")
        print(f"effective_storage_engines: {engines}")

    print("prod-storage verify: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())