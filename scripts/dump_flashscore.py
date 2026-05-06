import asyncio
from playwright.async_api import async_playwright

async def get_html():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.flashscore.com/tennis/")
        await asyncio.sleep(4)
        
        container = await page.query_selector(".sportName.tennis")
        if container:
            html = await container.inner_html()
            with open("flashscore_dump.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("Dump saved.")
        else:
            print("Container not found.")
            
        await browser.close()

asyncio.run(get_html())