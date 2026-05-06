import asyncio
from playwright.async_api import async_playwright
from datetime import datetime, timedelta

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        target_date = datetime.now() + timedelta(days=1)
        url = f"https://www.tennisexplorer.com/matches/?type=all&year={target_date.year}&month={target_date.strftime('%m')}&day={target_date.strftime('%d')}"
        print(f"Going to: {url}")
        await page.goto(url)
        
        rows = await page.query_selector_all("table.result tbody tr")
        print(f"Trouvé {len(rows)} lignes")
        
        count = 0
        for r in rows:
            class_name = await r.get_attribute("class") or ""
            if "head" in class_name or "one" in class_name or "two" in class_name:
                print(f"Row {count}: {await r.inner_text()}")
                count += 1
            if count > 5:
                break
            
        await browser.close()

asyncio.run(test())