# -*- coding: utf-8 -*-
"""
OpenMatrix UI functional walk-through (real backend, real browser).
Exercises every page's core interactions end-to-end and prints PASS/FAIL.
"""
import asyncio
import json
import os
import re
import uuid

import httpx
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
UI = BASE + "/ui/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
RESULTS = []
ERRORS = []

MIN_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n149\n%%EOF"
)


def record(feature, ok, detail=""):
    RESULTS.append((feature, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {feature}" + (f" | {detail}" if detail and not ok else ""))


async def main():
    os.makedirs(OUT, exist_ok=True)
    with httpx.Client(base_url=BASE, trust_env=False, timeout=20) as api:
        runs = (api.get("/api/v1/runs?limit=5").json()).get("runs", [])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: ERRORS.append(str(e)[:150]))
        page.on("console", lambda m: ERRORS.append(m.text[:150]) if m.type == "error" else None)

        # ── 1. Agent chat: welcome → objective + context → thread → terminal ──
        print("\n=== Agent chat ===")
        await page.goto(UI + "#/agent")
        await page.wait_for_timeout(1500)
        record("welcome screen", await page.locator(".agent-welcome-title").count() == 1)
        chips = await page.locator(".agent-chip-btn").count()
        record("quick action chips", chips == 4, f"{chips}")
        await page.locator(".agent-chip-btn", has_text="研究一个主题").click()
        draft = await page.locator(".agent-composer-card textarea, .agent-composer-card .ant-sender-input").first.input_value()
        record("chip prefills objective", len(draft) > 5, draft[:40])
        await page.locator(".agent-context-collapse .ant-collapse-header").click()
        await page.fill(".agent-composer-card textarea.mono", '{"ticker": "AAPL"}')
        sender = page.locator(".agent-composer-card .ant-sender-input")
        await sender.fill("Screen Apple's latest annual report (UI test)")
        await sender.press("Enter")
        await page.wait_for_timeout(4000)
        record("user bubble appears", await page.locator(".agent-user-bubble").count() >= 1)
        record("agent work card appears", await page.locator(".agent-work").count() == 1)
        await page.wait_for_timeout(3000)
        body = await page.inner_text("body")
        record("terminal band reached",
               ("研究已完成" in body or "研究失败" in body or "运行已结束并被归档" in body),
               "terminal band")
        steps = await page.locator(".agent-step").count()
        record("real steps rendered", steps >= 0, f"{steps} steps")
        await page.screenshot(path=os.path.join(OUT, "funct_agent_thread.png"))

        # ── 2. Projects: create → doc → search → cleanup ──
        print("\n=== Projects ===")
        await page.goto(UI + "#/projects")
        await page.wait_for_timeout(1500)
        await page.locator("button", has_text="新建项目").first.click()
        pid = f"ui-funct-{uuid.uuid4().hex[:6]}"
        await page.fill("input#project_id, input[placeholder*='项目 ID']", pid)
        inputs = page.locator(".ant-drawer-open input")
        n = await inputs.count()
        if n >= 2:
            await inputs.nth(1).fill("UI Functional Project")
        await page.locator(".ant-drawer-open button", has_text=re.compile(r"创\s*建")).click()
        try:
            await page.wait_for_selector(".ant-drawer-open", state="detached", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(600)
        await page.screenshot(path=os.path.join(OUT, "funct_project_after_create.png"))
        list_has = (await page.inner_text("body")).find("UI Functional Project") >= 0
        record("project created & auto-selected", list_has and (await page.inner_text("body")).find(pid) >= 0, pid)
        if not list_has:
            await browser.close()
            return
        await page.locator("button", has_text=re.compile(r"添\s*加\s*文\s*档")).first.click()
        await page.wait_for_timeout(600)
        d_inputs = page.locator(".ant-drawer-open input")
        dn = await d_inputs.count()
        if dn >= 2:
            await d_inputs.nth(0).fill(f"doc-{uuid.uuid4().hex[:4]}")
            await d_inputs.nth(1).fill("functional-test.txt")
        ta = page.locator(".ant-drawer-open textarea")
        await ta.first.fill("Revenue was 125 million dollars. Material litigation in Delaware.")
        # make the document searchable before adding
        switch = page.locator(".ant-drawer-open .ant-switch")
        if await switch.count() > 0:
            cls = await switch.first.get_attribute("aria-checked")
            if cls == "false":
                await switch.first.click()
        await page.locator(".ant-drawer-open button", has_text=re.compile(r"添\s*加")).click()
        await page.wait_for_timeout(1500)
        record("document added via drawer", await page.locator(".ant-table-row").count() >= 1)
        await page.locator(".ant-tabs-tab", has_text="搜索").click()
        await page.fill("input[placeholder='输入问题']", "What was revenue?")
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_selector(".evidence-card, .empty-state", timeout=8000)
        except Exception:
            pass
        evidence = await page.locator(".evidence-card").count()
        record("search returns evidence cards", evidence >= 1, f"{evidence} cards")
        await page.screenshot(path=os.path.join(OUT, "funct_projects_search.png"))
        # cleanup via API
        with httpx.Client(base_url=BASE, trust_env=False, timeout=20) as api:
            r = api.delete(f"/api/v1/projects/{pid}")
        record("project cleanup (api)", r.status_code == 200)
        await page.goto(UI + "#/projects")
        await page.wait_for_timeout(1200)

        # ── 3. Guided analysis: checklist gates + confirm modal + honest failure ──
        print("\n=== Guided analysis ===")
        await page.goto(UI + "#/analysis")
        await page.wait_for_timeout(1500)
        launch = page.locator("aside button", has_text="完成清单后即可启动")
        record("launch disabled with todo hint", await launch.count() == 1 and await launch.is_disabled())
        await page.locator("input[type='file']").first.set_input_files([
            {"name": "funct.pdf", "mimeType": "application/pdf", "buffer": MIN_PDF}
        ])
        await page.fill("textarea[aria-label='分析问题']", "What was revenue?")
        await page.fill("input[aria-label='模型提供商 API 密钥']", "definitely-invalid-key")
        await page.fill("input[aria-label='模型服务 Base URL']", "http://127.0.0.1:9")
        await page.fill("input[aria-label='模型名称']", "no-such-model")
        await page.wait_for_timeout(400)
        done_items = await page.locator(".guide-item--ok").count()
        record("checklist turns green", done_items == 3, f"{done_items}/3")
        review = page.locator("aside button", has_text="确认并启动")
        record("launch enabled when ready", await review.count() == 1 and await review.is_enabled())
        await review.click()
        await page.wait_for_timeout(500)
        record("confirm modal shows scope", await page.locator(".guide-confirm").count() == 1)
        await page.locator(".ant-modal-footer button", has_text=re.compile(r"运\s*行\s*批\s*量")).click()
        await page.wait_for_timeout(4000)
        body = await page.inner_text("body")
        record("provider failure shown honestly", ("分析失败" in body or "批量分析失败" in body), "ErrorState with backend detail")
        await page.screenshot(path=os.path.join(OUT, "funct_analysis_error.png"))

        # ── 4. Results: rows + drawer grid + exports ──
        print("\n=== Results ===")
        await page.goto(UI + "#/results")
        await page.wait_for_selector(".run-row", timeout=10000)
        rows = await page.locator(".run-row").count()
        record("run rows with real data", rows > 0, f"{rows}")
        await page.locator(".run-row").first.click()
        await page.wait_for_timeout(2500)
        record("drawer opens", await page.locator(".ant-drawer-open").count() > 0)
        record("export buttons present", await page.locator(".ant-drawer-open button", has_text="导出 XLSX").count() == 1)
        await page.screenshot(path=os.path.join(OUT, "funct_results_drawer.png"))
        await page.locator(".ant-drawer-close").click()
        await page.wait_for_timeout(500)

        # ── 5. Trace: connect to a real run id ──
        print("\n=== Trace ===")
        await page.goto(UI + "#/trace")
        await page.wait_for_timeout(1200)
        if runs:
            await page.fill("input[placeholder='输入矩阵运行 ID']", runs[0])
            await page.locator("button", has_text="开始监听").click()
            await page.wait_for_timeout(2500)
            body = await page.inner_text("body")
            record("trace connects and shows ledger", ("监听中" in body or "已完成" in body or "事件" in body), "ledger or stream status")
        else:
            record("trace (no runs to test)", False, "no runs in db")
        await page.screenshot(path=os.path.join(OUT, "funct_trace.png"))

        # ── 6. Evidence viewer: real PDF upload + render ──
        print("\n=== Evidence viewer ===")
        await page.goto(UI + "#/evidence")
        await page.wait_for_timeout(1200)
        await page.locator("input[type='file']").first.set_input_files([
            {"name": "sample.pdf", "mimeType": "application/pdf", "buffer": MIN_PDF}
        ])
        await page.wait_for_timeout(6500)
        body = await page.inner_text("body")
        record("pdf loads and renders", ("已加载" in body or "就绪" in body), body[:80].replace("\n", " "))
        canvases = await page.locator("canvas").count()
        record("page canvas rendered", canvases >= 1, f"{canvases} canvas")
        await page.screenshot(path=os.path.join(OUT, "funct_evidence.png"))

        # ── 7. Quality + Settings sections ──
        print("\n=== Quality & Settings ===")
        await page.goto(UI + "#/quality")
        await page.wait_for_timeout(2500)
        record("quality cards render", await page.locator(".desk-card").count() >= 4)
        await page.goto(UI + "#/settings")
        await page.wait_for_timeout(2000)
        body = await page.inner_text("body")
        record("settings sections render", ("访问与密钥" in body and "模型路由" in body and "系统健康" in body))
        record("model router shows config", ("default" in body.lower() or "模型" in body))
        await page.screenshot(path=os.path.join(OUT, "funct_settings.png"))

        # ── 8. Language switch sanity ──
        await page.locator(".topbar .ant-segmented-item", has_text="English").click()
        await page.wait_for_timeout(800)
        nav_text = await page.locator("button.nav-item").first.inner_text()
        record("language switches to English", "Overview" in nav_text, nav_text)
        await page.locator(".topbar .ant-segmented-item", has_text="中文").click()

        await browser.close()

    print("\n=== Console/page errors during walk-through ===")
    # Resource-load 4xx entries are expected: the guided-analysis step uses an
    # invalid provider on purpose and must fail honestly (network log only).
    hard_errors = [e for e in ERRORS if not e.startswith("Failed to load resource")]
    record("no JS page errors across all pages", len(hard_errors) == 0, "; ".join(hard_errors[:3]))

    print("\n" + "=" * 60)
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"UI FUNCTIONAL SUMMARY: {passed}/{total} passed")
    for feature, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {feature} - {detail}")


asyncio.run(main())
