# -*- coding: utf-8 -*-
"""Capture the guided analysis workspace (empty + filled + confirm modal)."""
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

        await page.goto(f"{BASE}#/analysis")
        await page.wait_for_timeout(2000)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_analysis.png"))
        print("analysis empty captured")

        # Fill everything via the file input directly (jsdom-free, real browser)
        files_input = page.locator("input[type='file']").first
        await files_input.set_input_files([
            {"name": "sample-report.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 guided"}
        ])
        await page.fill("textarea[aria-label='分析问题']", "What was revenue?\nWhat are the risk factors?")
        await page.fill("input[aria-label='模型提供商 API 密钥']", "demo-key")
        await page.fill("input[aria-label='模型服务 Base URL']", "http://127.0.0.1:3000/v1")
        await page.fill("input[aria-label='模型名称']", "demo-model")
        await page.wait_for_timeout(400)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_analysis_ready.png"))
        print("analysis ready captured")

        await page.click("button:has-text('确认并启动')")
        await page.wait_for_timeout(600)
        await page.screenshot(path=os.path.join(OUT, "desktop_1440_analysis_confirm.png"))
        print("confirm modal captured")

        await browser.close()


asyncio.run(main())
