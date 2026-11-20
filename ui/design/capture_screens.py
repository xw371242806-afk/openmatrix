# -*- coding: utf-8 -*-
"""
OpenMatrix UI redesign screenshot capture (Phase 6 verification aid).
Captures real rendered pages from the running backend at BASE.
Outputs PNGs to ui/design/screenshots/. Not part of the app build.
"""
import asyncio
import os
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000/ui/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")

ROUTES = [
    ("overview", "overview"),
    ("projects", "projects"),
    ("analysis", "analysis"),
    ("results", "results"),
    ("agent", "agent"),
    ("trace", "trace"),
    ("quality", "quality"),
    ("evidence", "evidence"),
    ("settings", "settings"),
]


async def capture(browser, viewport, suffix, mobile=False):
    page = await browser.new_page(viewport=viewport)
    for route, name in ROUTES:
        await page.goto(f"{BASE}#/{route}")
        await page.wait_for_timeout(2000)
        await page.screenshot(path=os.path.join(OUT, f"{suffix}_{name}.png"), full_page=True)
        print(f"captured {suffix}_{name}.png")
    if mobile:
        # mobile nav drawer open state
        await page.goto(f"{BASE}#/overview")
        await page.wait_for_timeout(1500)
        btn = page.locator(".topbar-menu-btn")
        if await btn.count() > 0:
            await btn.click()
            await page.wait_for_timeout(800)
            await page.screenshot(path=os.path.join(OUT, f"{suffix}_nav_drawer.png"))
            print("captured mobile_nav_drawer.png")
    await page.close()


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        await capture(browser, {"width": 1440, "height": 900}, "desktop_1440")
        await capture(browser, {"width": 1024, "height": 768}, "tablet_1024")
        await capture(browser, {"width": 390, "height": 844}, "mobile_390", mobile=True)
        await browser.close()


asyncio.run(main())
