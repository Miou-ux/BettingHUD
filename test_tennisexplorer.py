import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://www.tennisexplorer.com/matches/', timeout=60000)
        rows = await page.query_selector_all('table.result tbody tr')
        for i, row in enumerate(rows[:5]):
            cl = await row.get_attribute('class') or ''
            if 'bott' in cl:
                cells = await row.query_selector_all('td')
                print(f"Row {i} cells:")
                for c in cells[-3:]:
                    print(await c.inner_text())
                print("---")
        await browser.close()

asyncio.run(main())
