import logging
import random
import os
import re

from playwright.async_api import BrowserContext, Page

from pench.scrapers.sites.base import BaseScraper, generate_credentials
from pench.services.screenshot_overlay import take_screenshot

logger = logging.getLogger(__name__)


class Lottery1Scraper(BaseScraper):
    """
    Scraper for 1lottery2.com gambling site (czIndia.com platform).

    Flow:
    1. Register with phone + password on czIndia.com
    2. Navigate to Recharge page (closes page first for memory management)
    3. Select Innate gateway (uses INNATE_EXCLUDED env var for channel filtering)
    4. Select random non-excluded channel, amount, and submit
    5. Payment page shows UPI QR code

    Success rate: 67% (gateways have variable availability)
    """
    name = "1lottery2"
    url = "https://1lottery2.com/"
    recharge_url = "https://www.czIndia.com/#/wallet/Recharge"

    # Browser config
    use_browserbase = False
    use_proxy = True
    scrape_timeout = 180

    async def scrape(self, page: Page, context: BrowserContext) -> Page:
        creds = generate_credentials()
        phone_number = creds["mobile"]
        password = creds["password"]

        # --- Registration ---
        logger.info(f"[{self.name}] Navigating to {self.url}")
        try:
            await page.goto(self.url, timeout=50000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to load homepage: {e}")
            await page.close()
            return page

        # Capture home page screenshot
        self.home_screenshot_path = await take_screenshot(page, self.name)

        # Navigate directly to registration page (actual iframe target from homepage)
        logger.info(f"[{self.name}] Navigating to registration page")
        registration_url = "https://www.czIndia.com/#/register?invitationCode=8225819571635"
        try:
            await page.goto(registration_url, timeout=50000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to navigate to registration page: {e}")
            await page.close()
            return page

        # Fill registration form
        logger.info(f"[{self.name}] Filling registration form with {phone_number}")

        # Phone number
        try:
            await page.locator('input[name="userNumber"][placeholder="Please enter the phone number"]').fill(
                phone_number, timeout=10000
            )
        except Exception as e:
            logger.error(f"[{self.name}] Failed to fill phone number: {e}")
            await page.close()
            return page

        # Set password
        await page.locator('input[placeholder="Set password"]').fill(password)

        # Confirm password
        await page.locator('input[placeholder="Confirm password"]').fill(password)

        # Submit registration
        logger.info(f"[{self.name}] Clicking Register button")
        await page.locator('button:has-text("Register")').first.click()
        await page.wait_for_timeout(4000)

        # Check if still on register page (failed)
        if 'register' in page.url.lower():
            logger.warning(f"[{self.name}] Registration may have failed - still on register page")
            await page.close()
            return page

        logger.info(f"[{self.name}] Registration successful - Dashboard loaded")

        # Check for popups
        try:
            close_btn = page.locator('.van-popup__close-icon, .close-btn, button:has-text("Close"), button:has-text("×")')
            if await close_btn.is_visible(timeout=2000):
                await close_btn.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # --- Recharge Page ---
        # Close page and open new one to avoid memory issues (gambling site pattern)
        logger.info(f"[{self.name}] Closing page and opening fresh one")
        await page.close()
        page = await context.new_page()

        # Navigate to recharge page
        logger.info(f"[{self.name}] Navigating to recharge page: {self.recharge_url}")
        try:
            await page.goto(self.recharge_url, wait_until='domcontentloaded', timeout=50000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to reach recharge page: {e}")
            await page.close()
            return page

        if 'recharge' not in page.url.lower() and 'deposit' not in page.url.lower():
            logger.warning(f"[{self.name}] Failed to reach recharge page - current URL: {page.url}")
            await page.close()
            return page

        logger.info(f"[{self.name}] Recharge page loaded")

        await page.wait_for_timeout(2000)

        # Close any popups
        try:
            receive_btn = page.locator("div.dialog-btn:has-text('Receive'), button:has-text('Receive')")
            if await receive_btn.is_visible(timeout=3000):
                await receive_btn.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # --- Payment Gateway Selection ---
        logger.info(f"[{self.name}] Selecting payment gateway")

        # Check for Innate gateway type
        try:
            innate_gateway = page.locator(".Recharge__container-tabcard__items", has_text="Innate")
            await innate_gateway.first.wait_for(state="visible", timeout=10000)
            await innate_gateway.first.click()
            await page.wait_for_timeout(1000)
            logger.info(f"[{self.name}] Selected Innate gateway")
        except Exception as e:
            logger.warning(f"[{self.name}] Innate gateway not found, trying UPI-QR: {e}")
            try:
                upi_gateway = page.locator(".Recharge__container-tabcard__items", has_text="UPI-QR").first
                await upi_gateway.click()
                await page.wait_for_timeout(1000)
            except Exception:
                logger.warning(f"[{self.name}] No gateway found")

        logger.info(f"[{self.name}] Payment gateway selected")

        # --- Channel Selection & Deposit ---
        logger.info(f"[{self.name}] Selecting channel and amount")

        # Apply Innate exclude pattern
        excluded_str = os.getenv("INNATE_EXCLUDED", "")
        excluded_keywords = [k.strip() for k in excluded_str.split(",") if k.strip()]
        if excluded_keywords:
            logger.info(f"[{self.name}] Excluding channels: {', '.join(excluded_keywords)}")

        max_retries = 3
        for attempt in range(max_retries):
            logger.info(f"[{self.name}] Attempt {attempt + 1}/{max_retries}: Selecting channel")

            try:
                items = page.locator(".rechargeTypes_list .Recharge__content-quickInfo__item:visible")
                count = await items.count()
                matching_indices = []

                for i in range(count):
                    item = items.nth(i)
                    text = (await item.text_content() or "").strip()

                    # Exclude items that contain any excluded keyword (case-insensitive)
                    if not any(keyword.lower() in text.lower() for keyword in excluded_keywords):
                        matching_indices.append(i)

                if not matching_indices and count > 0:
                    matching_indices = list(range(count))

                if not matching_indices:
                    raise Exception("No valid channel items found")

                chosen_index = random.choice(matching_indices)
                chosen_item = items.nth(chosen_index)
                logger.info(f"[{self.name}] Randomly selected channel: {(await chosen_item.text_content()).strip()}")

                await chosen_item.click()
                await page.wait_for_timeout(1000)

            except Exception as e:
                logger.warning(f"[{self.name}] Failed to select channel: {e}")
                if attempt < max_retries - 1:
                    continue
                else:
                    break

            # Select amount
            logger.info(f"[{self.name}] Selecting amount")
            try:
                # Try to find amount buttons
                amount_items = page.locator(".Recharge__content-paymoney__money-list .amount, .Recharge__content-paymoney__item")
                count = await amount_items.count()
                if count > 0:
                    # Select 3rd option (index 2) or random if not enough
                    idx = min(2, count - 1)
                    await amount_items.nth(idx).click()
                    await page.wait_for_timeout(500)
                else:
                    # Try alternative selector
                    await page.locator("div").filter(has_text=re.compile(r"^₹ 500$")).first.click()
            except Exception:
                pass

            # Submit deposit
            logger.info(f"[{self.name}] Submitting deposit request")
            submit_btn = page.locator(".Recharge__container-rechageBtn, button:has-text('Deposit')")
            current_url = page.url

            # Handle new tab or same tab navigation
            try:
                async with context.expect_page(timeout=8000) as new_page_info:
                    await submit_btn.click()

                # New tab opened
                target_page = await new_page_info.value
                await target_page.wait_for_load_state("networkidle", timeout=10000)
                await target_page.wait_for_timeout(10000)  # Wait 10s for UPI/QR to render
                logger.info(f"[{self.name}] New tab opened for payment")

                # Try to click "Direct Transfer" if available
                try:
                    await target_page.locator("text=Direct Transfer").click(timeout=2000)
                    await target_page.wait_for_timeout(2000)
                except Exception:
                    pass

                logger.info(f"[{self.name}] Payment page loaded")
                return target_page

            except Exception:
                # No new tab, check same tab navigation
                await page.wait_for_timeout(3000)

                if page.url != current_url:
                    # Same tab navigated
                    logger.info(f"[{self.name}] Same tab navigated to payment page")
                    await page.wait_for_timeout(10000)  # Wait 10s for UPI/QR to render

                    try:
                        await page.locator("text=Direct Transfer").click(timeout=2000)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass

                    logger.info(f"[{self.name}] Payment page loaded")
                    return page
                else:
                    # Nothing happened, retry
                    logger.warning(f"[{self.name}] No navigation on attempt {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        logger.warning(f"[{self.name}] Max retries reached")
                        await page.wait_for_timeout(10000)  # Wait 10s anyway
                        return page

        # Fallback
        return page
