import asyncio
import base64
import logging
import os
import random
import re
import string
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import cv2
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
import requests

from pench.config import get_proxy
from pench.db import UpiRow, SourceType, save_upi
from pench.services import GCPBucketService, UPIExtractionService, notify_failure, add_payment_page_overlay, add_home_page_overlay, ARTIFACTS_DIR
from pench.scrapers.slider_captcha_solver import PuzzleSolver
import ipaddress

load_dotenv()

logger = logging.getLogger(__name__)

# Analytics, tracking, and ads domains to block (saves significant bandwidth)
BLOCKED_DOMAINS = [
    # Google Analytics & Tag Manager
    "googletagmanager.com",
    "google-analytics.com",
    "googleadservices.com",
    # Other Analytics
    "cloudflareinsights.com",
    "clarity.ms",
    "hotjar.com",
    "newrelic.com",
    "nr-data.net",
    "sentry.io",
    "smartico.ai",
    "covery.ai",
    "visualwebsiteoptimizer.com",
    "getinappstory.com",
    # Ads
    "doubleclick.net",
    "exoclick.com",
    "adtng.com",
    "realsrv.com",
    "magsrv.com",
    "pemsrv.com",
    "orbsrv.com",
    "tsyndicate.com",
    "psbcktrk.com",
    "rtmark.net",
    "adjs.media",

    # Image hosts (huge bandwidth)
    "i.postimg.cc",
    "i.ibb.co",
    "img.icons8.com",
    "ik.imagekit.io",
    "ossimg",
    "img1.",
    "data:image/png;base64,",
    "upload.4rabet6.com",
    "upload.4rabet-original.com",
    ".gif",
    ".bmp",
    ".tiff",
    ".ico",
    ".webp",
    ".svg",
    ".pdf",

]


def generate_credentials(
    min_length: int = 10,
    include_letter: bool = True,
    include_number: bool = True,
    include_special_char: bool = False,
) -> dict:
    """
    Generate realistic credentials for account registration (Phone, Email, Password).
    Suitable for Indian platforms (Mobile starts with 6-9).

    Args:
        min_length: Minimum length of the password (default: 10).
        include_letter: Whether to include letters in the password (default: True).
        include_number: Whether to include numbers in the password (default: True).
        include_special_char: Whether to include special characters in the password (default: False).

    Returns:
        dict: A dictionary containing 'mobile', 'email', and 'password'.
    """
    logger.info("Generating new credentials...")

    # Generate Indian mobile number: starts with 6-9, 10 digits total
    first_digit = random.choice(["6", "7", "8", "9"])
    rest_digits = "".join(random.choices(string.digits, k=9))
    mobile = f"{first_digit}{rest_digits}"

    # Generate password based on configuration
    pool = ""
    required_chars = []

    if include_letter:
        pool += string.ascii_letters
        required_chars.append(random.choice(string.ascii_letters))
    if include_number:
        pool += string.digits
        required_chars.append(random.choice(string.digits))
    if include_special_char:
        special_chars = "!@#$?/&%*"
        pool += special_chars
        required_chars.append(random.choice(special_chars))

    # Fallback if no options selected (default to letters)
    if not pool:
        pool = string.ascii_letters
        required_chars.append(random.choice(string.ascii_letters))

    # Determine target length (random between min and min+4)
    target_length = random.randint(min_length, min_length + 4)
    remaining_length = max(0, target_length - len(required_chars))

    # Fill remaining characters and shuffle
    password_chars = required_chars + random.choices(pool, k=remaining_length)
    random.shuffle(password_chars)
    password = "".join(password_chars)

    # Generate email
    domains = ["gmail.com", "outlook.com", "yahoo.com"]
    email_user = "".join(random.choices(string.ascii_lowercase, k=6)) + str(
        random.randint(100, 999)
    )
    email = f"{email_user}@{random.choice(domains)}"

    credentials = {"mobile": mobile, "email": email, "password": password}

    logger.info(
        f"Generated credentials - Mobile: {mobile}, Email: {email}, Password: {password}"
    )

    return credentials


class BaseScraper(ABC):
    """Base class for site scrapers. Subclass and implement `scrape()`."""

    # Override these in subclass
    name: str = "base"
    url: str = ""
    use_proxy: bool = False  # Set True if site needs residential proxy
    scrape_timeout: int = 180  # Global timeout in seconds (default: 3 minutes)

    def __init__(self, headless: bool = False):
        """
        Args:
            headless: Run browser without UI
        """
        self.headless = headless
        self.extraction_service = UPIExtractionService()
        self.bucket_service = GCPBucketService()
        self.home_screenshot_path = None

    @abstractmethod
    async def scrape(self, page: Page, context: BrowserContext) -> Page:
        """
        Site-specific scraping logic.

        Navigate through registration, recharge flow, etc.
        Return the page where the UPI/QR is displayed.
        """
        pass

    async def extract_and_save(self, page: Page, home_page_screenshot_url: str) -> str | None:
        """Extract UPI from page, upload screenshot, save to DB."""
        timestamp = int(time.time())
        screenshot_path = str(ARTIFACTS_DIR / f"{self.name}_{timestamp}.png")

        try:
            # Take screenshot
            await page.screenshot(path=screenshot_path, full_page=True, timeout=10000)
            logger.info(f"Screenshot saved: {screenshot_path}")

            # Add timestamp overlay to payment page screenshot
            add_payment_page_overlay(screenshot_path)

            # Try HTML extraction first
            html_content = await page.content()
            upi = self.extraction_service.extract_upi_from_html(html_content)

            # Fallback to iframe extraction
            if not upi:
                logger.info("No UPI in HTML, trying iframe extraction...")
                for frame in page.frames:
                    try:
                        frame_url = frame.url
                        if frame_url:
                            upi = await self.extraction_service.extract_upi_from_iframe(
                                frame
                            )
                            if upi:
                                break
                    except Exception:
                        continue

            # Fallback to QR extraction
            if not upi:
                logger.info("No UPI in HTML or iframes, trying QR extraction...")
                upi = self.extraction_service.extract_upi_from_qr(screenshot_path)

            if not upi:
                logger.warning("No UPI found on page")
                return None

            logger.info(f"Extracted UPI: {upi}")

            # Upload screenshot
            blob_name = f"{uuid.uuid4()}"
            payment_page_screenshot_url = self.bucket_service.upload_screenshot(
                screenshot_path, blob_name
            )

            # Save to DB
            row = UpiRow(
                upi=upi,
                payment_page_screenshot_url=payment_page_screenshot_url,
                home_page_screenshot_url=home_page_screenshot_url,
                raw_source_url=self.url,
                payment_page_url=page.url,
                source_type=SourceType.WEBSITE,
            )
            await save_upi(row)

            return upi

        finally:
            # Cleanup local screenshot
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)

    async def _get_proxy_ip(self, page: Page) -> str | None:
        """Get the actual public IP visible through the proxy."""
        try:
            response = await page.request.get(
                "https://api64.ipify.org?format=text",
                timeout=2000
            )

            if not response.ok:
                logger.debug(f"[{self.name}] IP check failed with status {response.status}")
                return None

            ip_content = (await response.text()).strip()

            try:
                ip = ipaddress.ip_address(ip_content)  # supports IPv4 and IPv6
                logger.debug(f"[{self.name}] Exit IP detected: {ip} (IPv{ip.version})")
                return str(ip)
            except ValueError:
                logger.debug(f"[{self.name}] Invalid IP response: {ip_content!r}")
                return None

        except Exception as e:
            logger.debug(f"[{self.name}] Failed to get proxy IP: {e}")
            return None
            
    async def _setup_resource_blocking(self, context: BrowserContext) -> None:
        """Block analytics, tracking, and ads to reduce proxy bandwidth."""
        blocked_counts: dict[str, int] = {}

        async def handle_route(route):
            url = route.request.url.lower()

            # Check if URL contains any blocked domain
            for domain in BLOCKED_DOMAINS:
                if domain in url:
                    blocked_counts[domain] = blocked_counts.get(domain, 0) + 1
                    # Truncate URL for cleaner logs
                    display_url = url[:80] + "..." if len(url) > 80 else url
                    logger.info(f"[{self.name}] 🚫 Blocked ({domain}): {display_url}")
                    await route.abort()
                    return

            # Use fallback() instead of continue() to allow other route handlers
            # (e.g., did parameter interception in site_generator.py) to run
            await route.fallback()

        await context.route("**/*", handle_route)
        logger.info(f"[{self.name}] 🛡️ Resource blocking enabled (analytics/ads)")

    async def run_once(self, browser: Browser) -> str | None:
        """Run a single scraping cycle using the provided browser instance."""

        proxy_provider = None  # Track proxy provider for error reporting
        context: BrowserContext | None = None
        page: Page | None = None

        # Use shared browser with new context and optional proxy
        proxy = get_proxy() if self.use_proxy else None

        if self.use_proxy and proxy:
            proxy_provider = proxy.get("provider", "Unknown")
            logger.info(
                f"[{self.name}] 🔒 Proxy enabled: {proxy['server']} (user: {proxy['username']}, provider: {proxy_provider})"
            )
        elif self.use_proxy:
            logger.warning(
                f"[{self.name}] ⚠️ use_proxy=True but PROXY_HOST not set - running without proxy!"
            )

        try:
            context = await browser.new_context(
                proxy=proxy,
                viewport={"width": 430, "height": 932},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                locale="en-US",
                timezone_id="Asia/Kolkata",
            )

            # Add comprehensive stealth scripts to hide automation detection
            await context.add_init_script("""
                // navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // Chrome runtime
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };

                // Permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );

                // Plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                Object.defineProperty(navigator, 'mimeTypes', {
                    get: () => [1, 2, 3, 4]
                });

                // Languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });

                // Platform (mobile)
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'iPhone'
                });

                // Vendor
                Object.defineProperty(navigator, 'vendor', {
                    get: () => 'Apple Computer, Inc.'
                });

                // User agent override (client hints)
                Object.defineProperty(navigator, 'userAgentData', {
                    get: () => undefined
                });

                // Battery API
                if (!('getBattery' in navigator)) {
                    navigator.getBattery = () => Promise.resolve({
                        charging: true,
                        chargingTime: 0,
                        dischargingTime: Infinity,
                        level: 1.0,
                        addEventListener: () => {},
                        removeEventListener: () => {},
                        dispatchEvent: () => true
                    });
                }

                // Connection API
                Object.defineProperty(navigator, 'connection', {
                    get: () => ({
                        effectiveType: '4g',
                        rtt: 50,
                        downlink: 10,
                        saveData: false
                    })
                });

                // Hardware concurrency (mobile typical)
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 6
                });

                // Device memory (mobile typical)
                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 4
                });

                // Max touch points (mobile)
                Object.defineProperty(navigator, 'maxTouchPoints', {
                    get: () => 5
                });

                // WebGL vendor override
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Apple Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Apple GPU';
                    }
                    return getParameter.apply(this, [parameter]);
                };

                // Override toString to hide proxy
                const originalToString = Function.prototype.toString;
                Function.prototype.toString = function() {
                    if (this === navigator.permissions.query) {
                        return 'function query() { [native code] }';
                    }
                    if (this === WebGLRenderingContext.prototype.getParameter) {
                        return 'function getParameter() { [native code] }';
                    }
                    return originalToString.apply(this, arguments);
                };

                // Canvas fingerprinting noise
                const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                CanvasRenderingContext2D.prototype.getImageData = function() {
                    const imageData = originalGetImageData.apply(this, arguments);
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        imageData.data[i] += Math.floor(Math.random() * 3) - 1;
                    }
                    return imageData;
                };

                // Notification permission
                Object.defineProperty(Notification, 'permission', {
                    get: () => 'default'
                });

                // Remove automation indicators
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

                // OuterDimensions (make it look like mobile)
                Object.defineProperty(window, 'outerWidth', {
                    get: () => 430
                });
                Object.defineProperty(window, 'outerHeight', {
                    get: () => 932
                });
            """)

            # Block analytics/ads to save bandwidth (only when using proxy)
            if self.use_proxy:
                await self._setup_resource_blocking(context)

            page = await context.new_page()

            mode = "Local+Proxy" if proxy else "Local"
            logger.info(f"[{self.name}] Starting scrape ({mode}) of {self.url}")

            async with asyncio.timeout(self.scrape_timeout):
                # Log proxy IP if proxy is being used
                if proxy:
                    logger.info(f"[{self.name}] 🌐 Fetching proxy IP address...")
                    proxy_ip = await self._get_proxy_ip(page)
                    if proxy_ip:
                        logger.info(f"[{self.name}] 📍 Proxy IP: {proxy_ip}")
                    else:
                        logger.warning(f"[{self.name}] ⚠️ Could not retrieve proxy IP")

                # Run site-specific scraping
                target_page = await self.scrape(page, context)

                # Wait for page to settle
                await target_page.wait_for_timeout(3000)

                # Process home page screenshot if captured
                home_page_screenshot_url = None
                if self.home_screenshot_path and os.path.exists(self.home_screenshot_path):
                    try:
                        logger.info(f"[{self.name}] Processing home page screenshot")
                        # Add overlay with URL + timestamp
                        add_home_page_overlay(self.home_screenshot_path, self.url)

                        # Upload to GCP
                        home_blob_name = f"{uuid.uuid4()}"
                        home_page_screenshot_url = self.bucket_service.upload_screenshot(
                            self.home_screenshot_path, home_blob_name
                        )
                        logger.info(f"[{self.name}] Home page screenshot uploaded")
                    except Exception as e:
                        logger.error(f"[{self.name}] Failed to process home screenshot: {e}")
                    finally:
                        # Cleanup local file
                        if os.path.exists(self.home_screenshot_path):
                            os.remove(self.home_screenshot_path)
                        self.home_screenshot_path = None

                # Extract and save
                upi = await self.extract_and_save(target_page, home_page_screenshot_url)

                if upi:
                    logger.info(f"[{self.name}] SUCCESS: {upi}")
                    # Send Slack alert for successful UPI extraction
                    await notify_failure(
                        scraper_name=self.name,
                        error=f"✅ SUCCESS - UPI extracted: {upi}",
                        url=self.url,
                        proxy_provider=proxy_provider,
                    )
                else:
                    logger.warning(f"[{self.name}] No UPI extracted")
                    # Send Slack alert if no UPI found
                    await notify_failure(
                        scraper_name=self.name,
                        error="No UPI extracted",
                        url=self.url,
                        proxy_provider=proxy_provider,
                    )

                return upi

        except TimeoutError:
            logger.error(
                f"[{self.name}] ⏰ TIMEOUT: Scrape exceeded {self.scrape_timeout}s limit"
            )
            await notify_failure(
                scraper_name=self.name,
                error=TimeoutError(f"Scrape exceeded {self.scrape_timeout}s timeout"),
                url=self.url,
                proxy_provider=proxy_provider,
            )
            raise

        except Exception as e:
            logger.error(f"[{self.name}] Error: {e}")
            # Save error screenshot
            if page:
                error_path = str(
                    ARTIFACTS_DIR / f"{self.name}_error_{int(time.time())}.png"
                )
                await page.screenshot(path=error_path, timeout=10000)

            # Send Slack alert
            await notify_failure(
                scraper_name=self.name,
                error=e,
                url=self.url,
                proxy_provider=proxy_provider,
            )
            raise

        finally:
            if context:
                try:
                    await context.close()
                except Exception as close_error:
                    logger.warning(f"[{self.name}] Failed to close context cleanly: {close_error}")

    async def solve_slider_captcha(
        self,
        page: Page,
        background_selector: str = ".captcha_background",
        slider_selector: str = ".captcha_slider",
        handler_selector: str = ".captcha_handler",
        timeout: int = 10000,
    ) -> bool:
        """
        Solve a slider captcha using computer vision.

        This method:
        1. Waits for captcha elements to appear
        2. Extracts background and slider images from base64 src
        3. Uses PuzzleSolver to calculate the drag offset
        4. Performs human-like drag interaction

        Args:
            page: The Playwright page object
            background_selector: CSS selector for the background image
            slider_selector: CSS selector for the slider/puzzle piece image
            handler_selector: CSS selector for the draggable handler
            timeout: Max time to wait for captcha elements (ms)

        Returns:
            bool: True if captcha was solved successfully, False otherwise
        """
        logger.info(f"[{self.name}] 🧩 Attempting to solve slider captcha...")

        timestamp = int(time.time())
        slider_path = str(ARTIFACTS_DIR / f"captcha_slider_{timestamp}.png")
        bg_path = str(ARTIFACTS_DIR / f"captcha_bg_{timestamp}.png")

        try:
            # Wait for captcha elements to appear
            logger.info(f"[{self.name}] Waiting for captcha elements...")
            await page.wait_for_selector(background_selector, timeout=timeout)
            await page.wait_for_selector(slider_selector, timeout=timeout)
            await page.wait_for_selector(handler_selector, timeout=timeout)

            # Get background image
            bg_element = page.locator(background_selector).first
            bg_src = await bg_element.get_attribute("src")
            if not bg_src:
                logger.error(f"[{self.name}] ❌ Background image src not found")
                return False

            # Get slider image
            slider_element = page.locator(slider_selector).first
            slider_src = await slider_element.get_attribute("src")
            if not slider_src:
                logger.error(f"[{self.name}] ❌ Slider image src not found")
                return False

            # Decode base64 images and save to files
            bg_bytes = self._decode_base64_image(bg_src)
            slider_bytes = self._decode_base64_image(slider_src)

            if not bg_bytes or not slider_bytes:
                logger.error(f"[{self.name}] ❌ Failed to decode captcha images")
                return False

            with open(bg_path, "wb") as f:
                f.write(bg_bytes)
            with open(slider_path, "wb") as f:
                f.write(slider_bytes)

            logger.info(f"[{self.name}] 📸 Saved captcha images to artifacts")

            # Calculate offset using PuzzleSolver
            solver = PuzzleSolver(slider_path, bg_path)
            offset = solver.get_position()
            logger.info(f"[{self.name}] 🔢 Calculated raw offset: {offset}px")

            # Get bounding boxes to calculate scale
            bg_box = await bg_element.bounding_box()
            if not bg_box:
                logger.error(f"[{self.name}] ❌ Could not get background bounding box")
                return False

            # Read image to get natural dimensions
            bg_img = cv2.imread(bg_path)
            if bg_img is None:
                logger.error(f"[{self.name}] ❌ Could not read background image")
                return False

            natural_width = bg_img.shape[1]
            scale = bg_box["width"] / natural_width
            drag_distance = offset * scale

            logger.info(
                f"[{self.name}] 📏 Scale: {scale:.4f}, Drag distance: {drag_distance:.2f}px"
            )

            # Get handler position
            handler = page.locator(handler_selector).first
            handler_box = await handler.bounding_box()
            if not handler_box:
                logger.error(f"[{self.name}] ❌ Could not get handler bounding box")
                return False

            start_x = handler_box["x"] + handler_box["width"] / 2
            start_y = handler_box["y"] + handler_box["height"] / 2

            # Perform human-like drag
            logger.info(
                f"[{self.name}] 🖱️ Starting drag from ({start_x:.1f}, {start_y:.1f})"
            )
            await self._human_like_drag(page, start_x, start_y, drag_distance)

            # Wait for verification
            await page.wait_for_timeout(1500)

            logger.info(f"[{self.name}] ✅ Slider captcha drag completed")
            return True

        except Exception as e:
            logger.error(f"[{self.name}] ❌ Failed to solve slider captcha: {e}")
            return False

        finally:
            # Cleanup temp files
            for path in [slider_path, bg_path]:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass

    def _decode_base64_image(self, src: str) -> bytes | None:
        """
        Decode a base64 image from a data URL or raw base64 string.

        Args:
            src: The image src (data:image/png;base64,... or raw base64)

        Returns:
            bytes: The decoded image bytes, or None if decoding failed
        """
        try:
            if src.startswith("data:"):
                # Extract base64 part from data URL
                base64_data = src.split(",", 1)[1]
            else:
                base64_data = src

            return base64.b64decode(base64_data)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to decode base64 image: {e}")
            return None

    async def get_captcha_code(self, page: Page) -> str | None:
        """
        Extract captcha code from a page screenshot using Azure OpenAI vision.

        Takes a screenshot of the page and uses GPT-4o to extract the captcha text.

        Args:
            page: The Playwright page object

        Returns:
            str: The extracted captcha code, or None if extraction failed
        """
        logger.info(f"[{self.name}] 🔍 Extracting captcha code using Azure OpenAI...")

        timestamp = int(time.time())
        screenshot_path = str(
            ARTIFACTS_DIR / f"captcha_ocr_{self.name}_{timestamp}.png"
        )

        try:
            # Take screenshot
            await page.screenshot(path=screenshot_path, timeout=10000)
            logger.info(f"[{self.name}] 📸 Captcha screenshot saved: {screenshot_path}")

            # Read and encode image to base64
            with open(screenshot_path, "rb") as f:
                image_bytes = f.read()
            b64_image = base64.b64encode(image_bytes).decode("utf-8")

            # Initialize Azure OpenAI client
            client = AsyncAzureOpenAI(
                api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION"),
                azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
            )

            # Call Azure OpenAI with vision
            response = await client.chat.completions.create(
                model=os.environ.get("AZURE_OPENAI_DEPLOYMENT"),
                messages=[
                    {
                        "role": "system",
                        "content": "You are a captcha extraction assistant. Your only job is to look at images and extract the captcha text/code shown. Respond with ONLY the captcha characters, nothing else. No explanations, no quotes, just the exact captcha text.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "What is the captcha code shown in this image? Reply with only the captcha characters.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64_image}",
                                },
                            },
                        ],
                    },
                ],
            )

            # Extract the captcha code from response
            captcha_code = response.choices[0].message.content
            if captcha_code:
                captcha_code = captcha_code.strip()
                logger.info(f"[{self.name}] ✅ Extracted captcha code: {captcha_code}")
                return captcha_code
            else:
                logger.warning(f"[{self.name}] ⚠️ No captcha code in response")
                return None

        except Exception as e:
            logger.error(f"[{self.name}] ❌ Failed to extract captcha code: {e}")
            return None

        finally:
            # Cleanup screenshot
            if os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except Exception:
                    pass

    async def _human_like_drag(
        self,
        page: Page,
        start_x: float,
        start_y: float,
        distance: float,
        steps: int = 30,
    ) -> None:
        """
        Perform a human-like drag operation using JavaScript events.

        This simulates realistic mouse/touch movement with:
        - Pointer, touch, and mouse events for compatibility
        - Eased movement curve (not linear)
        - Small random Y jitter
        - Variable timing between steps

        Args:
            page: The Playwright page object
            start_x: Starting X coordinate
            start_y: Starting Y coordinate
            distance: Distance to drag horizontally
            steps: Number of intermediate steps (more = smoother but slower)
        """
        await page.evaluate(
            """
            async (params) => {
                const { startX, startY, distance, steps } = params;
                
                // Find element at start point
                let element = document.elementFromPoint(startX, startY);
                if (!element) {
                    throw new Error(`No element found at ${startX}, ${startY}`);
                }
                
                // Walk up to find the actual handler if we hit a child
                const handler = element.closest('.captcha_handler, .slider-handle, [role="slider"]');
                if (handler) element = handler;
                
                const endX = startX + distance;
                
                // Helper to fire pointer events
                const firePointer = (type, x, y) => {
                    element.dispatchEvent(new PointerEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        pointerId: 1,
                        pointerType: 'mouse',
                        isPrimary: true,
                        clientX: x,
                        clientY: y,
                        screenX: x,
                        screenY: y,
                        buttons: type === 'pointerup' ? 0 : 1,
                        pressure: type === 'pointerup' ? 0 : 0.5
                    }));
                };
                
                // Helper to fire mouse events
                const fireMouse = (type, x, y, buttons) => {
                    element.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        buttons: buttons,
                        clientX: x,
                        clientY: y,
                        screenX: x,
                        screenY: y
                    }));
                };
                
                // Helper to fire touch events
                const fireTouch = (type, x, y) => {
                    try {
                        const touch = new Touch({
                            identifier: 1,
                            target: element,
                            clientX: x,
                            clientY: y,
                            screenX: x,
                            screenY: y,
                            pageX: x + window.scrollX,
                            pageY: y + window.scrollY,
                            radiusX: 2.5,
                            radiusY: 2.5,
                            rotationAngle: 0,
                            force: 0.5,
                        });
                        element.dispatchEvent(new TouchEvent(type, {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            touches: type === 'touchend' ? [] : [touch],
                            targetTouches: type === 'touchend' ? [] : [touch],
                            changedTouches: [touch],
                        }));
                    } catch (e) {
                        // Touch may not be supported
                    }
                };
                
                // === MOUSE DOWN ===
                firePointer('pointerdown', startX, startY);
                fireTouch('touchstart', startX, startY);
                fireMouse('mousedown', startX, startY, 1);
                
                await new Promise(r => setTimeout(r, 100 + Math.random() * 100));
                
                // === MOVE with easing ===
                for (let i = 1; i <= steps; i++) {
                    const ratio = i / steps;
                    // Ease-out cubic for natural deceleration
                    const ease = 1 - Math.pow(1 - ratio, 3);
                    const currX = startX + distance * ease;
                    // Small random jitter in Y direction
                    const currY = startY + (Math.random() * 4 - 2);
                    
                    firePointer('pointermove', currX, currY);
                    fireTouch('touchmove', currX, currY);
                    fireMouse('mousemove', currX, currY, 1);
                    
                    await new Promise(r => setTimeout(r, 8 + Math.random() * 12));
                }
                
                // === MOUSE UP ===
                firePointer('pointerup', endX, startY);
                fireTouch('touchend', endX, startY);
                fireMouse('mouseup', endX, startY, 0);
            }
            """,
            {
                "startX": start_x,
                "startY": start_y,
                "distance": distance,
                "steps": steps,
            },
        )

    async def run_loop(self, cycles: int = 0, delay: tuple[float, float] = (1.0, 3.0)):
        """
        Run multiple scraping cycles.

        Args:
            cycles: Number of cycles (0 = infinite)
            delay: (min, max) seconds between cycles
        """
        async with async_playwright() as playwright:
            # Launch a single browser for all cycles with stealth args
            browser = await playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            logger.info(f"[{self.name}] Browser launched (headless={self.headless})")

            try:
                count = 0
                while cycles == 0 or count < cycles:
                    count += 1
                    logger.info(f"[{self.name}] Cycle {count}")

                    try:
                        await self.run_once(browser)
                    except Exception as e:
                        logger.error(f"[{self.name}] Cycle failed: {e}")

                    jitter = random.uniform(*delay)
                    logger.info(f"[{self.name}] Sleeping {jitter:.1f}s...")
                    await asyncio.sleep(jitter)
            finally:
                await browser.close()
                logger.info(f"[{self.name}] Browser closed")
