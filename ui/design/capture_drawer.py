# -*- coding: utf-8 -*-
"""Capture the Results drawer open state with real AG Grid cells."""
import asyncio
import os
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000/ui/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(f"{BASE}#/results")
        await page.wait_for_selector(".run-row", timeout=15000)
        # open a run that needs review if present, else the first row
        review_tag = page.locator(".run-row", has_text="需要复核")
        if await review_tag.count() > 0:
            await review_tag.first.click()
        else:
            await page.locator(".run-row").first.click()
        await page.wait_for_timeout(2500)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_results_drawer.png"), full_page=False)
        print("captured desktop_1440_results_drawer.png")
        await browser.close()


asyncio.run(main())
