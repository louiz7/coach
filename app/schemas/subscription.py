from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class SubscriptionStatus(BaseModel):
    status: str
    current_period_end: Optional[datetime] = None


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalResponse(BaseModel):
    portal_url: str
