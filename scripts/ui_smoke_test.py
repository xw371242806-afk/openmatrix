# -*- coding: utf-8 -*-
"""
OpenMatrix UI Smoke Test Suite (Playwright E2E)
Covers: all 10 pages render, i18n switch, core interactions, console errors.
Updated for the "Research Desk" redesign (feat/ui-research-desk).

Language prerequisites (C5):
- Chinese suites explicitly force zh via localStorage openmatrix.lang + reload.
- Also asserts English default (en-US locale, empty storage), manual switch,
  and language persistence across reload.
"""
import asyncio
import os
from pathlib import Path
import tempfile
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000/ui/"
RESULTS = []
REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_ERRORS = []


def artifact_dir(has_failures):
    configured = os.environ.get("UI_ARTIFACT_DIR")
    if configured:
        return Path(configured).expanduser()
    if has_failures:
        return REPO_ROOT / "artifacts" / "ui-smoke"
    return Path(tempfile.gettempdir()) / "openmatrix-ui-smoke"


def record(page_name, check, passed, detail=""):
    RESULTS.append({"page": page_name, "check": check, "pass": passed, "detail": detail})
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {check}" + (f" | {detail}" if detail and not passed else ""))


async def force_app_language(page, lang: str):
    """Force UI language the same way production persistence works."""
    assert lang in ("zh", "en")
    await page.evaluate(
        """(language) => {
            try { localStorage.setItem('openmatrix.lang', language); }
            catch (e) {}
        }""",
        lang,
    )
    await page.reload(wait_until="networkidle")
    await page.wait_for_timeout(400)


async def clear_app_language(page):
    await page.evaluate(
        """() => {
            try { localStorage.removeItem('openmatrix.lang'); }
            catch (e) {}
        }"""
    )


async def first_nav_text(page):
    return await page.locator("button.nav-item").first.inner_text()


async def test_page(page, key, label_zh, expect_text):
    """Generic page smoke: click nav, assert no crash + expected content."""
    errors_before = len(PAGE_ERRORS)
    await page.click(f"button.nav-item:has-text('{label_zh}')")
    await page.wait_for_timeout(2500)
    new_errors = PAGE_ERRORS[errors_before:]
    body = await page.inner_text("main.page-content")
    ok_no_crash = len(new_errors) == 0
    record(key, "no JS errors", ok_no_crash, "; ".join(new_errors[:2]))
    if expect_text:
        found = any(t in body for t in expect_text)
        record(key, f"content shows ({'|'.join(expect_text[:2])})", found)
    return body


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # en-US context matches CI runners; Chinese suites must force zh explicitly.
        context = await browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()
        page.on("pageerror", lambda e: PAGE_ERRORS.append(str(e)[:150]))
        page.on("console", lambda m: PAGE_ERRORS.append(m.text[:150]) if m.type == "error" else None)

        # ============ T0: English default + persistence prerequisites ============
        print("\n=== T0: Language Prerequisites (en default / zh force / persist) ===")
        await page.goto(BASE, wait_until="networkidle")
        await clear_app_language(page)
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(500)
        home_en = await first_nav_text(page)
        record("i18n-default", "English default nav (en-US, no saved lang)", "Overview" in home_en, home_en)

        # Manual switch via topbar Segmented control
        await page.locator(".topbar .ant-segmented-item:has-text('\u4e2d\u6587')").first.click()
        await page.wait_for_timeout(800)
        home_zh_manual = await first_nav_text(page)
        record("i18n-switch", "manual topbar switch to Chinese", "\u6982\u89c8\u9996\u9875" in home_zh_manual, home_zh_manual)
        saved_after_manual = await page.evaluate("() => localStorage.getItem('openmatrix.lang')")
        record("i18n-persist", "topbar switch writes openmatrix.lang=zh", saved_after_manual == "zh", str(saved_after_manual))

        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(500)
        home_zh_reload = await first_nav_text(page)
        record("i18n-persist", "Chinese survives reload", "\u6982\u89c8\u9996\u9875" in home_zh_reload, home_zh_reload)

        # Switch to English via Settings page control (also must persist)
        await page.click("button.nav-item:has-text('\u7cfb\u7edf\u8bbe\u7f6e')")
        await page.wait_for_timeout(800)
        settings_en = page.locator(".settings-language-row .ant-segmented-item:has-text('English')")
        if await settings_en.count() == 0:
            settings_en = page.locator("main.page-content .ant-segmented-item:has-text('English')")
        await settings_en.first.click()
        await page.wait_for_timeout(800)
        home_en_settings = await first_nav_text(page)
        record("i18n-switch", "Settings control switches to English", "Overview" in home_en_settings, home_en_settings)
        saved_en = await page.evaluate("() => localStorage.getItem('openmatrix.lang')")
        record("i18n-persist", "Settings switch writes openmatrix.lang=en", saved_en == "en", str(saved_en))
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(500)
        home_en_reload = await first_nav_text(page)
        record("i18n-persist", "English survives reload", "Overview" in home_en_reload, home_en_reload)

        # Force Chinese for the rest of the Chinese suite
        await force_app_language(page, "zh")
        home_zh_forced = await first_nav_text(page)
        record("i18n-force", "Chinese suite forces zh via localStorage", "\u6982\u89c8\u9996\u9875" in home_zh_forced, home_zh_forced)

        # ============ T1: App shell loads ============
        print("\n=== T1: App Shell & Navigation ===")
        brand = await page.locator(".brand-name").inner_text()
        record("shell", "brand visible", "OpenMatrix" in brand, brand)
        nav_count = await page.locator("button.nav-item").count()
        record("shell", "10 nav items", nav_count == 10, f"got {nav_count}")
        health = await page.locator(".health-badge-label").inner_text()
        record("shell", "backend live indicator", "live" in health or "\u8fd0\u884c\u4e2d" in health, health)
        language_switch = await page.locator(".topbar .ant-segmented").count()
        record("shell", "language switch in topbar", language_switch >= 1, f"{language_switch}")

        # ============ T2: Each of the 10 pages renders without crash ============
        pages = [
            ("\u6982\u89c8\u9996\u9875", ["\u5df2\u89e3\u6790\u5355\u5143\u683c", "\u9700\u8981\u5173\u6ce8", "Overview"]),
            ("\u5206\u6790\u7ed3\u679c", ["\u5206\u6790\u7ed3\u679c", "Analysis Results"]),
            ("\u5206\u6790\u5de5\u4f5c\u53f0", ["\u6267\u884c\u8303\u56f4", "Scope"]),
            ("\u9879\u76ee\u6587\u6863", ["\u9879\u76ee", "Projects"]),
            ("\u7814\u7a76\u4ee3\u7406", ["\u6211\u662f\u4f60\u7684\u5e38\u9a7b\u7814\u7a76\u5206\u6790\u5e08", "Your resident research analyst"]),
            ("\u5ba1\u8ba1\u8ffd\u8e2a", ["\u8be5\u6570\u636e\u6d41\u4ec5\u8986\u76d6\u77e9\u9635\u5355\u5143\u683c\u7684\u6267\u884c\u8fc7\u7a0b", "This stream covers matrix cell execution only"]),
            ("Matrix Deep Dive", ["High-Density Latent Swarm", "\u6df1\u5ea6\u6316\u6398\u610f\u56fe"]),
            ("\u8d28\u91cf\u5ba1\u8ba1", ["\u5355\u5143\u683c\u72b6\u6001", "\u5355\u5143\u683c\u603b\u6570", "Cell Status"]),
            ("\u8bc1\u636e\u9605\u89c8", ["PDF", "\u8bc1\u636e"]),
            ("\u7cfb\u7edf\u8bbe\u7f6e", ["\u4e3b\u5bc6\u94a5", "Master key"]),
        ]
        for label, expects in pages:
            print(f"\n--- Page: {label} ---")
            await test_page(page, label.split()[0], label, expects)

        print("\n=== T2b: Chinese Secondary-Page Translation Coverage ===")
        forbidden = [
            "Run batch analysis", "Run follow-up chain", "Batch document analysis",
            "New Project", "Add Document", "Delete project?", "Delete document?",
            "Cell Status", "Citation Status", "Most Failed Questions", "Total Cells",
            "Enter a matrix run id", "Watch", "Connect", "Refresh Audit",
            "PDF files", "Expert preset", "Provider API key", "Model name",
            "Chain steps", "No answer", "Enter question", "No hits yet",
            "Project ID", "Document ID", "Mark as indexed", "Upload PDF",
            "Open from URL", "Apply bbox JSON", "What would you like me to research?",
        ]
        for label in ["\u5206\u6790\u5de5\u4f5c\u53f0", "\u9879\u76ee\u6587\u6863", "\u5ba1\u8ba1\u8ffd\u8e2a", "Matrix Deep Dive", "\u7814\u7a76\u4ee3\u7406", "\u8d28\u91cf\u5ba1\u8ba1", "\u8bc1\u636e\u9605\u89c8", "\u7cfb\u7edf\u8bbe\u7f6e"]:
            await page.click(f"button.nav-item:has-text('{label}')")
            await page.wait_for_timeout(900)
            text = await page.inner_text("main.page-content")
            leaked = [phrase for phrase in forbidden if phrase in text]
            record("i18n-secondary", f"{label}: no untranslated UI labels", not leaked, str(leaked))

        print("\n=== T3: Language Switch ===")
        await page.click("button.nav-item:has-text('\u7cfb\u7edf\u8bbe\u7f6e')")
        await page.wait_for_timeout(1000)
        seg_en = page.locator(".topbar .ant-segmented-item:has-text('English')")
        if await seg_en.count() > 0:
            await seg_en.first.click()
            await page.wait_for_timeout(800)
            home_btn = await first_nav_text(page)
            record("i18n", "nav switches to English", "Overview" in home_btn, home_btn)
            await page.click("button.nav-item:has-text('Overview')")
            await page.wait_for_timeout(1200)
            body = await page.inner_text("main.page-content")
            record("i18n", "overview content English", "Needs attention" in body or "Overview" in body)
            await page.locator(".topbar .ant-segmented-item:has-text('\u4e2d\u6587')").first.click()
            await page.wait_for_timeout(800)
            home_btn2 = await first_nav_text(page)
            record("i18n", "switch back to Chinese", "\u6982\u89c8\u9996\u9875" in home_btn2, home_btn2)
            saved_zh = await page.evaluate("() => localStorage.getItem('openmatrix.lang')")
            record("i18n", "switch-back persists zh", saved_zh == "zh", str(saved_zh))
        else:
            record("i18n", "segmented control exists", False, "not found")

        await force_app_language(page, "zh")

        print("\n=== T4: Results Row Interaction ===")
        await page.click("button.nav-item:has-text('\u5206\u6790\u7ed3\u679c')")
        try:
            await page.wait_for_selector(".run-row, .empty-state, .error-state", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        rows = await page.locator(".run-row").count()
        empty = await page.locator(".empty-state").count()
        record(
            "results",
            "run list or empty state rendered",
            rows > 0 or empty > 0,
            f"{rows} rows, {empty} empty states",
        )
        if rows > 0:
            await page.locator(".run-row").first.click()
            await page.wait_for_timeout(2000)
            drawer_visible = await page.locator(".ant-drawer-open").count() > 0
            record("results", "drawer opens on row click", drawer_visible)
            grid_rows = await page.locator(".ag-row").count()
            record("results", "ag-grid rows present", grid_rows > 0, f"{grid_rows} rows")
            close = page.locator(".ant-drawer-close")
            if await close.count() > 0:
                await close.click()

        print("\n=== T5: Overview Work Digest ===")
        await page.click("button.nav-item:has-text('\u6982\u89c8\u9996\u9875')")
        await page.wait_for_timeout(1500)
        tiles = await page.locator(".stat-tile").count()
        record("overview", "stat tiles rendered", tiles >= 4, f"{tiles}")
        quick = await page.locator("main.page-content button").count()
        record("overview", "quick actions rendered", quick >= 4, f"{quick} buttons")

        print("\n" + "=" * 60)
        total = len(RESULTS)
        passed = sum(1 for r in RESULTS if r["pass"])
        failed = [r for r in RESULTS if not r["pass"]]
        print(f"SMOKE TEST SUMMARY: {passed}/{total} passed")
        for r in failed:
            print(f"  FAIL: [{r['page']}] {r['check']} - {r['detail']}")
        verdict = "ALL GREEN" if not failed else f"{len(failed)} FAILURES"
        print(f"VERDICT: {verdict}")

        screenshot_dir = artifact_dir(bool(failed))
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / "final_state.png"
        await page.screenshot(path=str(screenshot_path))
        print(f"screenshot saved: {screenshot_path}")

        await browser.close()
        if failed:
            raise SystemExit(1)


asyncio.run(main())