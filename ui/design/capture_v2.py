# -*- coding: utf-8 -*-
"""Capture v2 redesign highlights: agent welcome/chat, overview, analysis."""
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

        await page.goto(f"{BASE}#/agent")
        await page.wait_for_timeout(2200)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_agent.png"))
        print("agent welcome captured")

        # type an objective to show the chat thread (no submit — real run would fail planning)
        box = page.locator(".agent-composer-card textarea, .agent-composer-card .ant-sender-input")
        if await box.count() > 0:
            await box.first.fill("对比 Q1 与 Q2 的研发投入")
            await page.wait_for_timeout(300)
            await page.screenshot(path=os.path.join(OUT, "desktop_1440_agent_filled.png"))
            print("agent filled captured")

        await page.goto(f"{BASE}#/overview")
        await page.wait_for_timeout(2500)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_overview.png"), full_page=True)
        print("overview captured")

        await page.goto(f"{BASE}#/analysis")
        await page.wait_for_timeout(2200)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_analysis.png"))
        print("analysis captured")

        await page.goto(f"{BASE}#/projects")
        await page.wait_for_timeout(2200)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_projects.png"), full_page=True)
        print("projects captured")

        await browser.close()


asyncio.run(main())
