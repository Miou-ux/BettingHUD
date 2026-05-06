import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        c = await b.new_context(user_agent="Mozilla/5.0")
        page = await c.new_page()
        
        await page.goto("https://www.tennisexplorer.com/matches/")
        await page.wait_for_timeout(2000)
        
        links = await page.query_selector_all("td.t-name a, td a")
        for l in links[:100]:
            href = await l.get_attribute("href")
            text = await l.inner_text()
            if "/player/" in href:
                print(f"Joueur: {text} -> {href}")
            
        await b.close()

if __name__ == "__main__":
    asyncio.run(test())
