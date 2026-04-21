from pydantic import BaseModel
from typing import Optional, List, Any


class WebhookHandleData(BaseModel):
    handle: Optional[str] = None
    is_me: Optional[bool] = None


class WebhookMessagePart(BaseModel):
    type: str
    value: Optional[str] = None


class WebhookMessageData(BaseModel):
    id: Optional[str] = None
    chat_id: Optional[str] = None
    service: Optional[str] = None
    from_handle: Optional[WebhookHandleData] = None
    parts: Optional[List[WebhookMessagePart]] = None
    is_from_me: Optional[bool] = None
    created_at: Optional[str] = None


class LinqWebhookPayload(BaseModel):
    id: Optional[str] = None
    event: Optional[str] = None
    data: Optional[WebhookMessageData] = None

    class Config:
        extra = "allow"  # future-proofing
