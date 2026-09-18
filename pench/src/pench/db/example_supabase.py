import asyncio
import os

from dotenv import load_dotenv
from supabase import AsyncClient, acreate_client

from pench.db.dto import UpiRow

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


async def main():
    supabase: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

    # test insert using UpiRow model
    row = UpiRow(
        upi="test@ybl",
        payment_page_screenshot_url="https://example.com/screenshot.png",
        raw_source_url="https://tirangalotto.net/#/register?invitationCode=5563412461883",
        payment_page_url="https://tirangalotto.net/#/register?invitationCode=5563412461883",
    )

    res = await supabase.table("upis").insert(row.model_dump()).execute()
    
    print(res.data)


if __name__ == "__main__":
    asyncio.run(main())
