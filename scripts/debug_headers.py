import asyncio
from playwright.async_api import async_playwright

async def debug_headers():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.flashscore.com/tennis/")
        await asyncio.sleep(2)
        
        headers = await page.query_selector_all(".event__title")
        print(f"Found {len(headers)} titles")
        for h in headers[:5]:
            print(await h.inner_text())
            
        elements = await page.query_selector_all(".event__title, .event__match")
        print(f"Found {len(elements)} total elements")
        for e in elements[:10]:
            cl = await e.get_attribute("class")
            text = await e.inner_text()
            print(f"[{cl}] {text[:50].replace(chr(10), ' ')}")
            
        await browser.close()

asyncio.run(debug_headers())