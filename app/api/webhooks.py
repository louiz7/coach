from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from app.config import settings
from app.utils.webhook_verify import verify_linq_signature
import sys

router = APIRouter(tags=["webhooks"])


def _log(msg):
    print(f"[WEBHOOK] {msg}", flush=True, file=sys.stderr)


@router.post("/api/v1/webhooks/linq")
async def linq_webhook(request: Request, bg: BackgroundTasks):
    # Verify signature
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    signature = request.headers.get("X-Webhook-Signature", "")
    body = await request.body()

    if not verify_linq_signature(timestamp, body, signature, settings.LINQ_WEBHOOK_SECRET):
        _log("Invalid signature, rejecting")
        raise HTTPException(401, "Invalid signature")

    data = await request.json()
    event = request.headers.get("X-Webhook-Event", "")
    _log(f"event={event} data_keys={list(data.keys())}")

    data = await request.json()
    # Linq sends event_type in the body (not X-Webhook-Event header)
    event = data.get("event_type") or request.headers.get("X-Webhook-Event", "")
    _log(f"event={event} data_keys={list(data.keys())}")

    if event != "message.received":
        _log(f"Ignoring non-message event: {event}")
        return {"ok": True}

    # Extract message data (Linq v3 payload format)
    msg_data = data.get("data", {})
    chat = msg_data.get("chat", {})
    chat_id = chat.get("id") or msg_data.get("chat_id")

    sender = msg_data.get("sender_handle") or msg_data.get("from_handle", {})
    phone = sender.get("handle") or sender.get("value")
    is_from_me = sender.get("is_me", False) or msg_data.get("is_from_me", False)

    # Ignore our own messages
    if is_from_me:
        _log("Ignoring own message")
        return {"ok": True}

    # Extract text and image from parts
    # Linq v3 part types: "text" (plain text) and "media" (attachments incl. images)
    parts = msg_data.get("parts", [])
    text = ""
    image_url: str | None = None
    for part in parts:
        if part.get("type") == "text":
            text += part.get("value", "")
        elif part.get("type") == "media":
            mime = part.get("mime_type", "")
            if mime.startswith("image/") and not image_url:
                image_url = part.get("url")

    if not chat_id:
        _log(f"Missing chat_id")
        return {"ok": True}

    # Require at least text OR an image — ignore empty messages
    if not text and not image_url:
        _log(f"No text or image, skipping: chat_id={chat_id!r}")
        return {"ok": True}

    event_id = data.get("event_id") or data.get("id", "")
    _log(f"Processing: chat_id={chat_id} phone={phone} text={text!r} image={bool(image_url)}")

    # Process async, pass phone for new user creation
    bg.add_task(_process_inbound, chat_id, text, event_id, phone, image_url)
    return {"ok": True}


async def _process_inbound(
    chat_id: str,
    text: str,
    event_id: str,
    phone: str = None,
    image_url: str = None,
):
    """Background: route to message worker."""
    from app.workers.message_worker import process_message
    await process_message(chat_id, text, event_id, phone, image_url=image_url)
