import asyncio
from playwright.async_api import async_playwright
from datetime import datetime, timedelta

async def get_results(target_date):
    url = f"https://www.tennisexplorer.com/results/?type=all&year={target_date.year}&month={target_date.strftime('%m')}&day={target_date.strftime('%d')}"
    print(url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, timeout=60000)
        
        rows = await page.query_selector_all("table.result tbody tr")
        for i, row in enumerate(rows[:6]):
            cl = await row.get_attribute("class") or ""
            html = await row.inner_html()
            print(f"Row {i} class='{cl}':\n{html}\n---\n")
            
        await browser.close()

asyncio.run(get_results(datetime.now() - timedelta(days=1)))
