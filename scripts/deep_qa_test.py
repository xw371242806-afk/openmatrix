# -*- coding: utf-8 -*-
"""
OpenMatrix Deep QA Suite - applying fugazi QA skills methodology:
  1. api-testing skill       -> API contract tests (response shape, status codes)
  2. a11y-playwright skill   -> axe-core WCAG scan + keyboard navigation
  3. playwright-e2e skill    -> responsive viewport + locator strategy audit
  4. regression skill        -> flaky detection (repeat runs)
"""
import asyncio
import json
import httpx
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
RESULTS = []

def record(cat, check, passed, detail=""):
    RESULTS.append((cat, check, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {check}" + (f" | {detail}" if detail and not passed else ""))

# ---------------- Phase A: API contract tests ----------------
def phase_a():
    print("\n=== PHASE A: API Contract Testing (api-testing skill) ===")
    with httpx.Client(trust_env=False, timeout=60) as c:
        # A1: health response shape contract
        r = c.get(f"{BASE}/api/v1/health")
        body = r.json()
        record("api", "health has status+service keys", "status" in body and "service" in body)
        # A2: 404 for unknown run (consistency contract)
        r = c.get(f"{BASE}/api/v1/runs/nonexistent-id-xyz")
        record("api", "unknown run -> 404", r.status_code == 404, f"got {r.status_code}")
        # A3: strict validation -> 400 not 500
        r = c.post(f"{BASE}/api/v1/schedules", json={"cron": "", "objective": "", "research_id": ""})
        record("api", "invalid schedule -> 400 (not 500)", r.status_code == 400, f"got {r.status_code}")
        r = c.post(f"{BASE}/api/v1/projects", json={})
        record("api", "empty project body -> 400", r.status_code == 400, f"got {r.status_code}")
        # A4: stats endpoints all share 200 + json object shape
        ok = True
        for ep in ["total-cells","status-counts","citation-status-counts","average-answer-length","most-failed-questions","citation-verification-rate"]:
            rr = c.get(f"{BASE}/api/v1/stats/{ep}")
            if rr.status_code != 200 or not isinstance(rr.json(), dict):
                ok = False; break
        record("api", "6 stats endpoints uniform 200+json-object", ok)
        # A5: idempotency - GET runs twice returns same count (read-only stability)
        a = len(c.get(f"{BASE}/api/v1/runs").json().get("runs", []))
        b = len(c.get(f"{BASE}/api/v1/runs").json().get("runs", []))
        record("api", "GET /runs idempotent within window", a == b, f"{a} vs {b}")
        # A6: security - no auth header on admin endpoint behavior documented
        r = c.get(f"{BASE}/api/v1/api-keys")
        record("api", "api-keys without master key -> 401/403", r.status_code in (401, 403), f"got {r.status_code}")

# ---------------- Phase B+C: browser-based (a11y + responsive + flaky) ----------------
async def phases_bc():
    print("\n=== PHASE B: Accessibility (a11y-playwright skill, axe-core) ===")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(f"{BASE}/ui/", wait_until="networkidle")

        # inject axe-core
        await page.add_script_tag(url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js")
        try:
            axe = await page.evaluate("""async () => {
                const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}});
                return {violations: r.violations.map(v => ({
                    id: v.id,
                    impact: v.impact,
                    nodes: v.nodes.length,
                    help: v.help,
                    targets: v.nodes.slice(0, 12).map(n => n.target.join(' ')),
                }))};
            }""")
            viols = axe["violations"]
            critical = [v for v in viols if v["impact"] in ("critical", "serious")]
            print(f"  axe-core: {len(viols)} violation types, {len(critical)} serious/critical")
            for v in viols[:6]:
                print(f"    [{v['impact']}] {v['id']}: {v['help']} ({v['nodes']} nodes)")
                for target in v.get("targets", []):
                    print(f"      {target}")
            record("a11y", "no critical WCAG violations", len(critical) == 0,
                   "; ".join(v["id"] for v in critical[:4]))
        except Exception as e:
            record("a11y", "axe-core scan executed", False, f"cdn blocked? {str(e)[:80]}")

        print("\n=== PHASE B2: Keyboard navigation ===")
        # Tab into nav buttons - keyboard operability
        await page.keyboard.press("Tab")
        focused = await page.evaluate("document.activeElement.className + '|' + document.activeElement.tagName")
        record("a11y-keyboard", "Tab reaches interactive element", "button" in focused.lower() or "nav" in focused.lower(), focused[:60])

        print("\n=== PHASE C: Responsive viewports (playwright-e2e skill) ===")
        for name, vw, vh in [("mobile-375", 375, 812), ("tablet-768", 768, 1024), ("desktop-1440", 1440, 900)]:
            await page.set_viewport_size({"width": vw, "height": vh})
            await page.wait_for_timeout(600)
            overflow = await page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
            rail_visible = await page.locator("aside.rail").is_visible()
            record("responsive", f"{name}: no horizontal scroll", not overflow, f"scrollWidth>clientWidth")
            if name == "desktop-1440":
                record("responsive", f"{name}: navigation rail visible", rail_visible)
            else:
                record("responsive", f"{name}: navigation rail collapsed", not rail_visible)

        print("\n=== PHASE D: Flaky detection (regression skill - repeat critical path 3x) ===")
        flaky = []
        for i in range(3):
            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.goto(f"{BASE}/ui/#/results", wait_until="networkidle")
            await page.wait_for_timeout(500)
            rows = await page.locator(".run-row").count()
            await page.goto(f"{BASE}/ui/#/overview", wait_until="networkidle")
            flaky.append(rows)
        stable = len(set(flaky)) == 1
        record("flaky", f"runs card count stable across 3 visits", stable, f"samples={flaky}")

        await browser.close()

async def main():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, phase_a)
    await phases_bc()

    print("\n" + "=" * 60)
    total = len(RESULTS); passed = sum(1 for r in RESULTS if r[2])
    failed = [r for r in RESULTS if not r[2]]
    print(f"DEEP QA SUMMARY: {passed}/{total} passed")
    for cat, chk, _, det in failed:
        print(f"  FAIL [{cat}] {chk} - {det}")
    print("VERDICT:", "ALL GREEN" if not failed else f"{len(failed)} FAILURES")

if __name__ == "__main__":
    asyncio.run(main())
