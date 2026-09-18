import asyncio
import csv
import json
import logging
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityUrl, MessageEntityTextUrl

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
logging.getLogger("telethon").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")
SLACK_WEBHOOK = os.getenv("SLACK_TELEGRAM_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL")

CHANNELS_FILE = Path(__file__).parent / "channels.txt"
DATA_DIR = Path(__file__).parent / "data"
SESSION_FILE = DATA_DIR / "telegram_session"
EXTRACTED_URLS_CSV = DATA_DIR / "telegram_sites.csv"
CSV_HEADERS = ["url", "message_link", "timestamp"]

BLOCKED_DOMAINS = [
    't.me', 'telegram.org', 'telegram.me',
    'youtube.com', 'youtu.be', 'yt.be',
    'facebook.com', 'fb.com', 'fb.me',
    'instagram.com', 'instagr.am',
    'whatsapp.com', 'wa.me',
    'twitter.com', 'x.com',
    'tiktok.com',
    'linkedin.com',
    'snapchat.com',
    'reddit.com',
    'pinterest.com',
    'discord.gg', 'discord.com'
]

seen_domains = set()
new_urls = set()
messages_processed = 0


def extract_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split('/')[0]
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain.lower()
    except Exception:
        return None


def load_channels():
    channel_ids = set()
    channel_usernames = set()

    with open(CHANNELS_FILE, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.lstrip('-').isdigit():
                channel_ids.add(int(line))
            else:
                channel_usernames.add(line.lstrip('@').lower())

    return channel_ids, channel_usernames


async def send_slack_batch():
    global messages_processed
    while True:
        await asyncio.sleep(60 * 60)

        msg_count = messages_processed
        unique_count = len(new_urls)

        if not new_urls:
            payload = {"text": f"📊 Stats: {msg_count} messages, 0 new sites"}
        else:
            payload = {
                "text": f"Found {unique_count} new site(s) from {msg_count} messages",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"📊 {msg_count} messages processed | {unique_count} unique URLs"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join([f"• {url}" for url in sorted(new_urls)])}},
                ]
            }

        if SLACK_WEBHOOK:
            try:
                req = urllib.request.Request(SLACK_WEBHOOK, json.dumps(payload).encode(), {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
                logger.info(f"Slack sent: {unique_count} URLs from {msg_count} messages")
            except Exception as e:
                logger.error(f"Slack error: {e}")

        new_urls.clear()
        messages_processed = 0


async def listen():
    if not API_ID or not API_HASH:
        logger.error("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
        return

    # Load channels from file
    if not CHANNELS_FILE.exists():
        logger.error(f"Channels file not found: {CHANNELS_FILE}")
        return

    channel_ids, channel_usernames = load_channels()
    logger.info(f"Loaded {len(channel_ids) + len(channel_usernames)} channels from {CHANNELS_FILE.name}")

    # Initialize CSV
    if not EXTRACTED_URLS_CSV.exists():
        EXTRACTED_URLS_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(EXTRACTED_URLS_CSV, 'w', newline='', encoding='utf-8') as f:
            csv.DictWriter(f, CSV_HEADERS).writeheader()
    else:
        try:
            with open(EXTRACTED_URLS_CSV, 'r', newline='', encoding='utf-8') as f:
                seen_domains.update(
                    domain for row in csv.DictReader(f)
                    if (domain := extract_domain(row['url']))
                )
            logger.info(f"Loaded {len(seen_domains)} unique domains from CSV")
        except Exception as e:
            logger.error(f"Error loading existing domains: {e}")

    client = TelegramClient(str(SESSION_FILE), int(API_ID), API_HASH, system_version="4.16.30-vxCUSTOM")
    await client.start(phone=PHONE)

    @client.on(events.NewMessage())
    async def handle(event):
        global messages_processed
        try:
            chat = event.chat
            if event.chat_id not in channel_ids:
                username = getattr(chat, 'username', None)
                if not username:
                    chat = await event.get_chat()
                    username = getattr(chat, 'username', None)
                if not username or username.lower() not in channel_usernames:
                    return

            if chat is None:
                chat = await event.get_chat()
            chat_name = getattr(chat, 'username', None) or getattr(chat, 'title', 'Unknown')
            logger.info(f"📨 New message from {chat_name}")                                                      
            
            urls = []

            # MessageEntityUrl: plain text URLs in message
            for entity, text in event.message.get_entities_text(MessageEntityUrl):
                urls.append(text)

            # MessageEntityTextUrl: hyperlinked text (URL is in entity.url)
            for entity, text in event.message.get_entities_text(MessageEntityTextUrl):
                urls.append(entity.url)

            urls = [url for url in urls if not any(domain in url.lower() for domain in BLOCKED_DOMAINS)]

            if not urls:
                return

            messages_processed += 1

            if chat.username:
                message_link = f"https://t.me/{chat.username}/{event.message.id}"
            else:
                # For channels without username, use channel ID format (remove -100 prefix)
                channel_id = str(chat.id).replace("-100", "")
                message_link = f"https://t.me/c/{channel_id}/{event.message.id}"

            for url in urls:
                domain = extract_domain(url)

                if domain and domain in seen_domains:
                    logger.info(f"⏭️  Duplicate domain: {domain}")
                    continue

                with open(EXTRACTED_URLS_CSV, 'a', newline='', encoding='utf-8') as f:
                    csv.DictWriter(f, CSV_HEADERS).writerow({
                        'url': url,
                        'message_link': message_link or '',
                        'timestamp': datetime.now().isoformat()
                    })

                if domain:
                    seen_domains.add(domain)
                new_urls.add(url)
                logger.info(f"✅ {url}")
        except Exception as e:
            logger.error(f"Error: {e}")

    logger.info(f"✅ Listening to {len(channel_ids) + len(channel_usernames)} channels | Slack every 1hr")
    asyncio.create_task(send_slack_batch())
    await client.run_until_disconnected()


def main():
    asyncio.run(listen())


if __name__ == "__main__":
    main()
