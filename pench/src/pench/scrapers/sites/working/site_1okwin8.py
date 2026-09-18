import logging
import re
import random
import os

from playwright.async_api import BrowserContext, Page

from pench.scrapers.sites.base import BaseScraper, generate_credentials
from pench.services.screenshot_overlay import take_screenshot

logger = logging.getLogger(__name__)


class OneOkwin8Scraper(BaseScraper):
    name = "1okwin8"
    url = "https://1okwin8.com/#/register?invitationCode=524747006284"
    recharge_url = "https://1okwin8.com/#/wallet/Recharge"

    # Browser config (per-site)
    use_browserbase = False
    use_proxy = True

    async def scrape(self, page: Page, context: BrowserContext) -> Page:
        creds = generate_credentials()
        phone_number = creds["mobile"]
        password = creds["password"]

        # --- Navigation ---
        logger.info(f"[{self.name}] Navigating to {self.url}")
        try:
            await page.goto(self.url, timeout=50000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
        except Exception:
            await page.close()
            return page

        # Capture home page screenshot
        self.home_screenshot_path = await take_screenshot(page, self.name)

        # --- Registration ---
        logger.info(f"[{self.name}] Filling registration form with {phone_number}")

        # Phone
        try:
            await page.get_by_role("textbox", name="Please enter the phone number").fill(
                phone_number,
                timeout=10000
            )
        except Exception:
            await page.close()
            return page

        # Password
        await page.get_by_role("textbox", name="Set password").fill(password)
        await page.get_by_role("textbox", name="Confirm password").fill(password)

        # Privacy Policy Checkbox
        try:
            privacy_checkbox = page.get_by_role(
                "checkbox", name=re.compile(r"I have read and agree")
            )
            if not await privacy_checkbox.is_checked():
                logger.info(f"[{self.name}] Checking privacy policy checkbox...")
                await privacy_checkbox.click(force=True)
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Submit Registration
        logger.info(f"[{self.name}] Clicking Register button...")
        try:
            await page.get_by_role("button", name="Register").click()
        except:
            await page.locator("button:has-text('Register')").click()

        logger.info(f"[{self.name}] Registration submitted")

        # Solve slider captcha
        # captcha_solved = await self.solve_slider_captcha(page)
        # if not captcha_solved:
        #     logger.warning(f"[{self.name}] Slider captcha may not have been solved")
        await page.wait_for_timeout(4000)

        if 'register' in page.url:
            await page.close()
            return page

        # DO NOT touch the same page again
        try:
            await page.close()
        except:
            pass

        # open a clean page
        page = await context.new_page()

        # --- Recharge ---
        logger.info(f"[{self.name}] Going to recharge...")
        try:
            await page.goto(self.recharge_url, wait_until='domcontentloaded', timeout=50000)
            await page.wait_for_timeout(3000)
            # await page.reload()
            # await page.wait_for_timeout(3000)
        except Exception:
            await page.close()
            return page
        
        if 'recharge' not in page.url.lower():
            logger.warning(f"[{self.name}] Failed to reach recharge page")
            await page.close()
            return page
        
        await page.wait_for_timeout(2000)


        try:
            receive_btn = page.locator("div.dialog-btn:has-text('Receive')")
            if await receive_btn.is_visible(timeout=5000):
                await receive_btn.click()
        except Exception:
            pass

        # --- Payment Method Selection ---
        logger.info(f"[{self.name}] Selecting Payment Method...")

        try:
            item = page.locator(
                ".Recharge__container-tabcard__items:visible",
                has_text="Innate"
            )

            await item.first.wait_for(state="visible", timeout=10000)
            await item.first.click()
            await page.wait_for_timeout(1000)

        except Exception as e:
            logger.warning(f"[{self.name}] Failed to select Payment method: {e}")
            await page.close()
            return page

        await page.wait_for_timeout(1000)

        # --- Quick Info Selection Loop ---
        # Get excluded keywords from environment variable (INNATE_EXCLUDED)
        excluded_str = os.getenv("INNATE_EXCLUDED", "")
        excluded_keywords = [k.strip() for k in excluded_str.split(",") if k.strip()]
        if excluded_keywords:
            logger.info(f"[{self.name}] Excluding payment gateways: {', '.join(excluded_keywords)}")
        max_retries = 3

        for attempt in range(max_retries):
            logger.info(f"[{self.name}] Attempt {attempt + 1}/{max_retries}: Selecting Quick Info item...")

            try:
                items = page.locator(
                    ".rechargeTypes_list .Recharge__content-quickInfo__item:visible"
                )

                count = await items.count()
                matching_indices = []

                for i in range(count):
                    item = items.nth(i)
                    text = (await item.text_content() or "").strip()

                    # Exclude items that contain any excluded keyword (case-insensitive)
                    if not any(keyword.lower() in text.lower() for keyword in excluded_keywords):
                        matching_indices.append(i)

                logger.info(f"[{self.name}] Found {len(matching_indices)} valid Quick Info items (total: {count})")
                if not matching_indices and count > 0:
                     matching_indices = list(range(count))

                if not matching_indices and count > 0:
                    matching_indices = list(range(count))

                if not matching_indices:
                    raise Exception("No valid Quick Info items found")

                chosen_index = random.choice(matching_indices)
                chosen_item = items.nth(chosen_index)

                logger.info(
                    f"[{self.name}] Randomly selected Quick Info item: "
                    f"{(await chosen_item.text_content()).strip()}"
                )

                await chosen_item.click()
                await page.wait_for_timeout(1000)

            except Exception as e:
                logger.warning(f"[{self.name}] Failed to select random Quick Info item: {e}")
                if attempt < max_retries - 1:
                    continue
                else:
                    break

            # --- Amount Selection ---
            logger.info(f"[{self.name}] Selecting Amount...")
            try:
                amount_items = page.locator(".Recharge__content-paymoney__money-list .amount")
                count = await amount_items.count()
                if count > 0:
                    await amount_items.nth(2).click()
                else:
                     await page.locator("div").filter(has_text=re.compile(r"^₹ 500$")).first.click()
                
            except Exception:
                pass

                        # --- Submit Deposit ---
            logger.info(f"[{self.name}] Submitting Deposit Request...")

            submit_btn = page.locator(".Recharge__container-rechageBtn")
            current_url = page.url

            # Handle three scenarios: new tab, same tab navigation, or nothing
            try:
                # Try to detect new tab opening
                async with context.expect_page(timeout=8000) as new_page_info:
                    await submit_btn.click()

                # Scenario 1: New tab opened
                target_page = await new_page_info.value
                await target_page.wait_for_load_state("networkidle", timeout=10000)
                await target_page.wait_for_timeout(5000)
                logger.info(f"[{self.name}] New tab opened for payment.")
                try:
                    await target_page.locator("text=Direct Transfer").click(timeout=1000)
                except Exception:
                    pass

                return target_page

            except Exception:
                # No new tab opened, check other scenarios
                await page.wait_for_timeout(3000)

                if page.url != current_url:
                    # Scenario 2: Same tab navigated to new page
                    logger.info(f"[{self.name}] Same tab navigated to payment page.")
                    await page.wait_for_timeout(2000)
                    try:
                        await page.locator("text=Direct Transfer").click(timeout=1000)
                    except Exception:
                        pass
                    return page
                else:
                    # Scenario 3: Nothing happened
                    logger.warning(f"[{self.name}] No navigation detected on attempt {attempt + 1}/{max_retries}.")

                    if attempt < max_retries - 1:
                        # Retry with a different quick info item
                        logger.info(f"[{self.name}] Retrying with different quick info item...")
                        continue
                    else:
                        # Last attempt failed, return current page
                        logger.warning(f"[{self.name}] Max retries reached. Continuing on same page.")
                        await page.wait_for_timeout(3000)
                        return page

        # Fallback (should not reach here)
        return page
