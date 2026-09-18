import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from functools import partial

logger = logging.getLogger(__name__)


def _get_webhook_url() -> str | None:
    """Get Slack webhook URL from environment."""
    return os.getenv("SLACK_WEBHOOK_URL")


def _send_slack_message_sync(payload: dict) -> bool:
    """
    Send a message to Slack (blocking).
    Returns True on success, False on failure.
    """
    webhook_url = _get_webhook_url()

    if not webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not set, skipping notification")
        return False

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 200

    except urllib.error.URLError as e:
        logger.error(f"Failed to send Slack notification: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending Slack notification: {e}")
        return False


async def _send_slack_message(payload: dict) -> bool:
    """Async wrapper for sending Slack messages."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_send_slack_message_sync, payload))


async def notify_failure(
    scraper_name: str,
    error: str | Exception,
    url: str | None = None,
    proxy_provider: str | None = None,
) -> bool:
    """
    Send a failure notification to Slack.

    Args:
        scraper_name: Name of the scraper that failed
        error: Error message or exception
        url: URL being scraped (optional)
        proxy_provider: Proxy provider being used (optional)

    Returns:
        True if notification was sent successfully
    """
    error_msg = str(error)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🚨 Scraper Failed",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Scraper:*\n`{scraper_name}`"},
                {"type": "mrkdwn", "text": "*Status:*\n❌ Failed"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Error:*\n```{error_msg[:500]}```",
            },
        },
    ]

    if url:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"🔗 URL: {url}"}],
            }
        )
    
    if proxy_provider:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"🔒 Proxy: {proxy_provider}"}],
            }
        )

    payload = {
        "text": f"🚨 Scraper `{scraper_name}` failed: {error_msg[:100]}",
        "blocks": blocks,
    }

    success = await _send_slack_message(payload)

    if success:
        logger.info(f"[{scraper_name}] Slack notification sent")
    else:
        logger.warning(f"[{scraper_name}] Failed to send Slack notification")

    return success
