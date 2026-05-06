import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://www.tennisexplorer.com/matches/', timeout=60000)
        
        rows = await page.query_selector_all('table.result tbody tr')
        for row in rows[:20]:
            cl = await row.get_attribute('class') or ''
            if 'bott' in cl:
                text = await row.inner_text()
                # Find course cells
                course_cells = await row.query_selector_all('td.course, td.coursew')
                texts = [await c.inner_text() for c in course_cells]
                
                print(f"Match: {text.split()[1]}")
                print(f"Odds extracted: {texts}")
                print("---")
        
        await browser.close()

asyncio.run(main())
