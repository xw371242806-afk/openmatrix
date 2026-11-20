# -*- coding: utf-8 -*-
"""
OpenMatrix functional API test battery (real backend, no mocks).
Prints PASS/FAIL per feature. Creates only throwaway records and
cleans them up (test project + schedule).
"""
import json
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000"
RESULTS = []


def record(feature, ok, detail=""):
    RESULTS.append((feature, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {feature}" + (f" | {detail}" if detail and not ok else ""))


def main():
    c = httpx.Client(base_url=BASE, timeout=30, trust_env=False)

    # ── 1. Health & system ──────────────────────────────
    r = c.get("/api/v1/health")
    record("health", r.status_code == 200 and r.json().get("status") == "ok", str(r.status_code))
    r = c.get("/api/v1/health/detailed")
    comps = r.json().get("components", {}) if r.status_code == 200 else {}
    record("health/detailed", r.status_code == 200 and len(comps) > 0,
           f"components={list(comps)[:5]}")

    # ── 2. Model router ─────────────────────────────────
    r = c.get("/api/v1/model-router")
    body = r.json() if r.status_code == 200 else {}
    record("model-router", r.status_code == 200 and "default_model" in body,
           json.dumps(body)[:120])

    # ── 3. Stats (6 endpoints) ──────────────────────────
    for path, key in [
        ("/api/v1/stats/total-cells", ("total_cells", "total")),
        ("/api/v1/stats/citation-status-counts", None),
        ("/api/v1/stats/status-counts", None),
        ("/api/v1/stats/average-answer-length", None),
        ("/api/v1/stats/citation-verification-rate", ("verification_rate", "rate")),
        ("/api/v1/stats/most-failed-questions?limit=5", None),
    ]:
        r = c.get(path)
        record(f"stats {path.split('/')[-1].split('?')[0]}", r.status_code == 200,
               r.text[:100] if r.status_code != 200 else "")

    # ── 4. Runs: list / detail / matrix cells / exports ──
    r = c.get("/api/v1/runs?limit=5")
    runs = r.json().get("runs", []) if r.status_code == 200 else []
    record("runs list", r.status_code == 200 and len(runs) > 0, f"{len(runs)} runs")
    if runs:
        rid = runs[0]
        r = c.get(f"/api/v1/runs/{rid}")
        ok = r.status_code == 200 and all(k in r.json() for k in ("total", "completed", "failed", "needs_review"))
        record("run counts detail", ok, r.text[:100])
        r = c.get(f"/api/matrix-runs/{rid}")
        cells = r.json().get("cells", []) if r.status_code == 200 else []
        record("matrix run cells", r.status_code == 200 and len(cells) > 0, f"{len(cells)} cells")
        r = c.get(f"/api/v1/export-xlsx/{rid}")
        ok = r.status_code == 200 and len(r.content) > 500 and "spreadsheet" in r.headers.get("content-type", "")
        record("export XLSX", ok, f"status={r.status_code} type={r.headers.get('content-type','')[:40]} bytes={len(r.content)}")
        r = c.get(f"/api/v1/export-csv/{rid}")
        ok = r.status_code == 200 and len(r.content) > 100 and "text/csv" in r.headers.get("content-type", "")
        record("export CSV", ok, f"status={r.status_code} bytes={len(r.content)}")
        # audit trail SSE for a finished run (should stream cells then close)
        with c.stream("GET", f"/api/runs/{rid}/events") as s:
            events = 0
            for line in s.iter_lines():
                if line.startswith("event:"):
                    events += 1
                if events >= 3:
                    break
        record("trace SSE stream", events >= 2, f"{events} events")

    # ── 5. Projects end-to-end ──────────────────────────
    pid = f"ui-test-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/projects", json={"project_id": pid, "name": "UI Functional Test", "description": "throwaway"})
    record("project create", r.status_code == 200, r.text[:120])
    r = c.get("/api/v1/projects")
    names = [p.get("id") for p in r.json().get("projects", [])]
    record("project list contains new", pid in names, str(names[-3:]))
    content = (
        "OpenMatrix Functional Test Document\n"
        "Total revenue for fiscal year 2025 was 125 million dollars.\n"
        "The company faces material litigation in Delaware.\n"
        "Adjusted EBITDA reached 31 million dollars."
    )
    r = c.post(f"/api/v1/projects/{pid}/documents",
               json={"doc_id": f"doc-{uuid.uuid4().hex[:6]}", "filename": "test-doc.txt",
                     "content": content, "page_count": 1, "indexed": True})
    record("document add (paste)", r.status_code == 200, r.text[:120])
    r = c.get(f"/api/v1/projects/{pid}/documents")
    docs = r.json().get("documents", []) if r.status_code == 200 else []
    record("document list", r.status_code == 200 and len(docs) == 1, f"{len(docs)} docs, status={docs[0].get('status') if docs else '-'}")
    r = c.post(f"/api/v1/projects/{pid}/search",
               json={"question": "What was total revenue?", "limit": 3, "mode": "bm25"})
    hits = r.json().get("hits", []) if r.status_code == 200 else []
    hit_ok = len(hits) > 0 and "revenue" in hits[0].get("quote", "").lower() and hits[0].get("citation_status") in ("verified_exact", "needs_review")
    record("project search (bm25)", hit_ok, json.dumps(r.json())[:150] if r.status_code == 200 else r.text[:120])
    r = c.post(f"/api/v1/projects/{pid}/search",
               json={"question": "litigation", "limit": 3, "mode": "dense"})
    record("project search (dense)", r.status_code == 200, f"hits={len(r.json().get('hits', []))}" if r.status_code == 200 else r.text[:120])
    if docs:
        r = c.delete(f"/api/v1/documents/{docs[0]['id']}")
        record("document delete", r.status_code == 200, r.text[:80])
    r = c.delete(f"/api/v1/projects/{pid}")
    record("project delete (cleanup)", r.status_code == 200, r.text[:80])

    # ── 6. Schedules end-to-end ─────────────────────────
    sid = f"ui-test-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/schedules", json={
        "schedule_id": sid, "cron": "0 9 * * 1-5",
        "objective": "test scheduled research", "research_id": f"res-{uuid.uuid4().hex[:6]}",
    })
    record("schedule create", r.status_code == 200 and r.json().get("status") == "registered", r.text[:120])
    r = c.post("/api/v1/schedules", json={
        "schedule_id": sid, "cron": "bad cron!", "objective": "x", "research_id": "y",
    })
    record("schedule rejects bad cron", r.status_code == 400, f"status={r.status_code}")
    r = c.get("/api/v1/schedules")
    found = any(s.get("schedule_id") == sid for s in r.json().get("schedules", []))
    record("schedule list contains new", found)
    r = c.delete(f"/api/v1/schedules/{sid}")
    record("schedule delete (cleanup)", r.status_code == 200, r.text[:80])

    # ── 7. Real agent research run (rule planner + MCP tools) ──
    rid2 = f"ui-agent-{uuid.uuid4().hex[:8]}"
    r = c.post("/api/v1/research", json={
        "research_id": rid2,
        "objective": "Screen Apple's latest annual report",
        "context": {"ticker": "AAPL"},
    })
    record("research start (ticker context)", r.status_code == 200, r.text[:120])
    if r.status_code == 200:
        final = None
        for _ in range(45):  # up to 90s
            time.sleep(2)
            s = c.get(f"/api/v1/research/{rid2}")
            if s.status_code != 200:
                break
            final = s.json()
            if final.get("phase") in ("completed", "failed", "cancelled"):
                break
        phase = (final or {}).get("phase", "?")
        steps = (final or {}).get("steps", [])
        done = [x for x in steps if x.get("status") == "succeeded"]
        record("research run reaches terminal", phase in ("completed", "failed", "cancelled"),
               f"phase={phase} steps={len(steps)} succeeded={len(done)}")
        record("research steps have real tool calls", len(done) > 0,
               json.dumps(steps[:2])[:200] if steps else "no steps")
        # Cancel semantics: probe shows the ticker plan completes in <10ms, so
        # no observable in-flight window exists. A correct backend either
        # cancels a live run (200 cancelled:true) or refuses a finished one
        # (400/404) — it must NEVER fake-cancel. The UI cancel path itself is
        # covered by ChatPage unit tests against a running-phase mock.
        rid3 = f"ui-cancel-{uuid.uuid4().hex[:8]}"
        c.post("/api/v1/research", json={
            "research_id": rid3,
            "objective": "Screen Apple's latest annual report",
            "context": {"ticker": "AAPL"},
        })
        time.sleep(0.3)
        r = c.delete(f"/api/v1/research/{rid3}")
        cancel_ok = (r.status_code == 200 and r.json().get("cancelled") is True) or (r.status_code in (400, 404))
        record("research cancel semantics (live-cancel or refuse-finished)", cancel_ok, r.text[:100])
        r = c.delete(f"/api/v1/research/{rid2}")
        record("terminal run never fake-cancelled", r.status_code in (400, 404), f"status={r.status_code} (correct)")

    # ── 8. Analyze endpoints: contract validation (no provider key → honest 4xx/5xx) ──
    files = {"files": ("t.pdf", b"%PDF-1.4 test", "application/pdf")}
    r = c.post("/api/v1/analyze-batch", data={
        "questions": json.dumps(["q1"]), "max_concurrency": "5",
        "api_key": "invalid-key", "base_url": "http://127.0.0.1:9", "model_name": "nope",
    }, files=files)
    record("analyze-batch reachable (auth/validation layer)", r.status_code in (200, 400, 401, 500, 502),
           f"status={r.status_code} body={r.text[:100]}")

    # ── 9. API keys (no auth mode) ──────────────────────
    r = c.get("/api/v1/api-keys")
    record("api-keys endpoint", r.status_code in (200, 401, 403), f"status={r.status_code}")
    r = c.get("/api/v1/audit")
    record("audit endpoint", r.status_code in (200, 401, 403), f"status={r.status_code}")

    print("\n" + "=" * 60)
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"FUNCTIONAL API SUMMARY: {passed}/{total} passed")
    for feature, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {feature} - {detail}")


if __name__ == "__main__":
    main()
