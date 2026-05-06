import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('http://localhost:8501', wait_until='networkidle')
        await page.wait_for_timeout(25000)
        
        # Get all text content
        texts = await page.evaluate('() => document.body.innerText')
        with open('debug_texts.txt', 'w', encoding='utf-8') as f:
            f.write(texts)
            
        # Get specifically error alerts in Streamlit
        errors = await page.query_selector_all('[data-testid="stException"]')
        if errors:
            with open('debug_errors.txt', 'w', encoding='utf-8') as f:
                for err in errors:
                    text = await err.inner_text()
                    f.write(text + '\n')
        await browser.close()

asyncio.run(main())