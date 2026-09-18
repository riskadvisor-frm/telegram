import logging
from datetime import datetime
import time
from pathlib import Path

import pytz
from PIL import Image, ImageDraw, ImageFont

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Artifacts directory for screenshots
ARTIFACTS_DIR = Path(__file__).parent.parent / "scrapers" / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


async def take_screenshot(page: Page, name: str) -> str | None:
    """Take a screenshot of the page."""
    try:
        timestamp = int(time.time())
        screenshot_path = str(ARTIFACTS_DIR / f"{name}_home_{timestamp}.png")
        await page.screenshot(path=screenshot_path, full_page=False, timeout=2000)
        logger.info(f"[{name}] Home page screenshot captured")
        return screenshot_path
    except Exception as e:
        logger.error(f"Failed to take screenshot: {e}")
        return None


def add_home_page_overlay(screenshot_path: str, url: str) -> None:
    """Add URL and IST timestamp overlay to home page screenshot at top-right."""
    try:
        ist = pytz.timezone('Asia/Kolkata')
        timestamp = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
        text = f"{url}\n{timestamp}"

        img = Image.open(screenshot_path).convert('RGB')
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("arial.ttf", int(img.height * 0.012))
        except:
            font = ImageFont.load_default(size=int(img.height * 0.012))

        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = img.width - text_width - 15
        y = 15

        draw.rectangle(
            [(x - 5, y - 5), (x + text_width + 5, y + text_height + 10)],
            fill=(255, 0, 0)
        )
        draw.text((x, y), text, font=font, fill=(255, 255, 255))

        img.save(screenshot_path)
        logger.info(f"Added home page overlay to {screenshot_path}")
    except Exception as e:
        logger.error(f"Failed to add home page overlay: {e}")


def add_payment_page_overlay(screenshot_path: str) -> None:
    """Add IST timestamp overlay to payment page screenshot at top-right."""
    try:
        ist = pytz.timezone('Asia/Kolkata')
        timestamp = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')

        img = Image.open(screenshot_path).convert('RGB')
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("arial.ttf", int(img.height * 0.02))
        except:
            font = ImageFont.load_default(size=int(img.height * 0.02))

        bbox = draw.textbbox((0, 0), timestamp, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = img.width - text_width - 15
        y = 15

        draw.rectangle(
            [(x - 5, y - 5), (x + text_width + 5, y + text_height + 25)],
            fill=(255, 0, 0)
        )
        draw.text((x, y), timestamp, font=font, fill=(255, 255, 255))

        img.save(screenshot_path)
        logger.info(f"Added payment page overlay to {screenshot_path}")
    except Exception as e:
        logger.error(f"Failed to add payment page overlay: {e}")
