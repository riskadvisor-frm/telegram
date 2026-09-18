import os
import logging

from dotenv import load_dotenv
from supabase import AsyncClient, acreate_client

from pench.db.dto import UpiRow

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    """Get or create Supabase client (singleton)."""
    global _client
    if _client is None:
        _client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


async def save_upi(row: UpiRow) -> dict:
    """Save UPI record to Supabase."""
    client = await get_client()
    res = await client.table("upis").insert(row.model_dump()).execute()
    logger.info(f"Saved UPI: {row.upi}")
    return res.data[0] if res.data else {}

