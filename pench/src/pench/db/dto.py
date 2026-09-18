from enum import Enum
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    PAYMENT_GATEWAY = "PAYMENT GATEWAY"
    WEBSITE = "WEBSITE"


class UpiRow(BaseModel):
    """Model for inserting UPI records."""

    upi: str
    payment_page_screenshot_url: str | None = None
    home_page_screenshot_url: str | None = None
    raw_source_url: str | None = None
    payment_page_url: str # ! Is same as raw source in case of gaming websites where they show QR directly in the game site.
    source_type: SourceType 
    payment_gateway: str | None = None

    class Config:
        use_enum_values = True
