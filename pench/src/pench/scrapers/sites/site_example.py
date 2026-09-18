"""
Template for new site scrapers.

To add a new site:
1. Copy this file: cp sites/site_aexample.py sites/site_yoursite.py
2. Update the class name, `name`, and `url`
3. Implement the `scrape()` method
4. Test: uv run main.py yoursite

Auto-registers - no need to edit main.py!
"""

import logging

from playwright.async_api import BrowserContext, Page

from sites.base import BaseScraper, generate_credentials

logger = logging.getLogger(__name__)


class TemplateScraper(BaseScraper):
    # CHANGE THESE
    name = "template"  # Used in CLI: uv run main.py template
    url = "https://example.com/register?ref=xxx"

    # Browser config (per-site)
    use_browserbase = False  # True = use cloud browser (Browserbase)
    use_proxy = False  # True = route through proxy (only with Browserbase)

    async def scrape(self, page: Page, context: BrowserContext) -> Page:
        """
        Site-specific scraping logic.

        Your job: Navigate from registration to the payment page.
        Return the page where the UPI ID or QR code is visible.

        The base class handles:
        - Browser setup
        - Screenshot capture
        - UPI extraction (from HTML + QR)
        - Upload to GCP
        - Save to Supabase
        """
        creds = generate_credentials()
        # creds["mobile"]   - Indian phone (6-9 prefix)
        # creds["email"]    - Random email
        # creds["password"] - Random password

        # Step 1: Navigate to registration page
        logger.info(f"[{self.name}] Navigating...")
        await page.goto(self.url, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Step 2: Fill registration form
        # Use browser devtools to find selectors
        # Common patterns:
        #   - page.get_by_role("textbox", name="Phone")
        #   - page.locator("input[placeholder='Phone']")
        #   - page.locator(".phone-input input")
        logger.info(f"[{self.name}] Filling form...")
        # await page.locator("input[name='phone']").fill(creds["mobile"])
        # await page.locator("input[name='password']").fill(creds["password"])
        # await page.locator("button[type='submit']").click()

        # Step 3: Handle popups (common on these sites)
        # try:
        #     await page.locator(".popup-close").click(timeout=3000)
        # except:
        #     pass

        # Step 4: Navigate to Recharge/Deposit
        logger.info(f"[{self.name}] Going to recharge...")
        # await page.locator("text=Recharge").click()
        # await page.wait_for_timeout(2000)

        # Step 5: Select amount
        logger.info(f"[{self.name}] Selecting amount...")
        # await page.locator(".amount-option").first.click()

        # Step 6: Select payment method (often random to get different UPIs)
        logger.info(f"[{self.name}] Selecting payment...")
        # payment_options = page.locator(".payment-method")
        # count = await payment_options.count()
        # if count > 0:
        #     await payment_options.nth(random.randint(0, count - 1)).click()

        # Step 7: Submit and get to payment page
        logger.info(f"[{self.name}] Submitting...")
        # Sometimes opens new tab:
        # try:
        #     async with context.expect_page(timeout=10000) as new_page_info:
        #         await page.locator("button.submit").click()
        #     target_page = await new_page_info.value
        #     await target_page.wait_for_load_state("networkidle")
        #     return target_page
        # except:
        #     await page.wait_for_timeout(3000)
        #     return page

        return page


# For testing directly: uv run sites/site_aexample.py
if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    scraper = TemplateScraper(headless=False)
    asyncio.run(scraper.run_loop(cycles=1))
