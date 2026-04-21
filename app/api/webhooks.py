from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from app.config import settings
from app.utils.webhook_verify import verify_linq_signature

router = APIRouter(tags=["webhooks"])


@router.post("/api/v1/webhooks/linq")
async def linq_webhook(request: Request, bg: BackgroundTasks):
    # Verify signature
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    signature = request.headers.get("X-Webhook-Signature", "")
    body = await request.body()

    if not verify_linq_signature(timestamp, body, signature, settings.LINQ_WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid signature")

    data = await request.json()
    event = request.headers.get("X-Webhook-Event", "")

    if event != "message.received":
        return {"ok": True}

    # Extract message data
    msg_data = data.get("data", {})
    chat_id = msg_data.get("chat_id")
    from_handle = msg_data.get("from_handle", {})
    is_from_me = msg_data.get("is_from_me", False)

    # Ignore our own messages
    if is_from_me:
        return {"ok": True}

    # Extract text from parts
    parts = msg_data.get("parts", [])
    text = ""
    for part in parts:
        if part.get("type") == "text":
            text += part.get("value", "")

    if not text or not chat_id:
        return {"ok": True}

    event_id = data.get("id", "")

    # Process async
    bg.add_task(_process_inbound, chat_id, text, event_id)
    return {"ok": True}


async def _process_inbound(chat_id: str, text: str, event_id: str):
    """Background: route to message worker."""
    from app.workers.message_worker import process_message
    await process_message(chat_id, text, event_id)
