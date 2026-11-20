# -*- coding: utf-8 -*-
import asyncio
from playwright.async_api import async_playwright

UI = "http://127.0.0.1:8000/ui/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(UI + "#/projects")
        await page.wait_for_timeout(1500)
        btn = page.locator("button", has_text="新建项目").first
        await btn.click()
        await page.wait_for_timeout(1200)
        print("drawers_open:", await page.locator(".ant-drawer-open").count())
        print("drawers_total:", await page.locator(".ant-drawer").count())
        btns = page.locator(".ant-drawer button")
        print("drawer buttons:", await btns.count())
        texts = []
        for i in range(await btns.count()):
            texts.append((await btns.nth(i).inner_text()).strip())
        print("texts:", texts)
        await page.screenshot(path="ui_design_debug_drawer.png")
        await browser.close()


asyncio.run(main())
