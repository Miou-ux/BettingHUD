import asyncio
from playwright.async_api import async_playwright

async def debug_flashscore():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating...")
        await page.goto("https://www.flashscore.com/tennis/")
        await asyncio.sleep(3)
        try:
            await page.click("button#onetrust-accept-btn-handler", timeout=3000)
            print("Cookies accepted")
        except:
            pass
            
        matches = await page.query_selector_all(".event__match")
        print(f"Found {len(matches)} matches")
        
        for m in matches[:5]:
            html = await m.inner_html()
            text = await m.inner_text()
            print("----- MATCH TEXT -----")
            print(text)
            print("----- MATCH HTML -----")
            print(html)
            
        await browser.close()

asyncio.run(debug_flashscore())