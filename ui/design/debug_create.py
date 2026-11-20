# -*- coding: utf-8 -*-
import asyncio
from playwright.async_api import async_playwright

UI = "http://127.0.0.1:8000/ui/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("request", lambda r: print("REQ", r.method, r.url) if "/api/" in r.url else None)
        page.on("response", lambda r: print("RES", r.status, r.url) if "/api/" in r.url else None)
        await page.goto(UI + "#/projects")
        await page.wait_for_timeout(1500)
        await page.locator("button", has_text="新建项目").first.click()
        await page.wait_for_timeout(800)
        await page.fill("#project_id", "ui-probe-01")
        await page.fill("#name", "Probe Project")
        await page.locator(".ant-drawer-open button", has_text="创 建").click()
        for i in range(6):
            await page.wait_for_timeout(700)
            print(f"t+{(i + 1) * 0.7:.1f}s drawer_open=", await page.locator(".ant-drawer-open").count(),
                  "selected_in_body=", (await page.inner_text("body")).find("ui-probe-01") >= 0)
        await page.screenshot(path="ui_design_debug_create.png")
        await browser.close()


asyncio.run(main())
