import httpx
from typing import Optional
from app.config import settings

BASE = settings.LINQ_BASE_URL
HEADERS = {
    "Authorization": f"Bearer {settings.LINQ_API_TOKEN}",
    "Content-Type": "application/json",
}


async def _client():
    return httpx.AsyncClient(timeout=15, headers=HEADERS)


async def list_phone_numbers() -> list[dict]:
    async with await _client() as c:
        r = await c.get(f"{BASE}/phone_numbers")
        r.raise_for_status()
        return r.json().get("phone_numbers", [])


async def create_chat(from_number: str, to_number: str, text: str) -> dict:
    """Create a new chat and send the initial message. Returns the chat object."""
    async with await _client() as c:
        r = await c.post(f"{BASE}/chats", json={
            "from": from_number,
            "to": [to_number],
            "message": {
                "parts": [{"type": "text", "value": text}]
            }
        })
        r.raise_for_status()
        return r.json().get("chat", {})


async def send_message(chat_id: str, text: str, idempotency_key: Optional[str] = None) -> dict:
    payload = {
        "message": {
            "parts": [{"type": "text", "value": text}]
        }
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    async with await _client() as c:
        r = await c.post(f"{BASE}/chats/{chat_id}/messages", json=payload)
        r.raise_for_status()
        return r.json()


async def send_message_with_effect(chat_id: str, text: str, effect_type: str, effect_name: str) -> dict:
    async with await _client() as c:
        r = await c.post(f"{BASE}/chats/{chat_id}/messages", json={
            "message": {
                "parts": [{"type": "text", "value": text}],
                "effect": {"type": effect_type, "name": effect_name}
            }
        })
        r.raise_for_status()
        return r.json()


async def start_typing(chat_id: str):
    async with await _client() as c:
        try:
            await c.post(f"{BASE}/chats/{chat_id}/typing")
        except Exception:
            pass  # best-effort


async def stop_typing(chat_id: str):
    async with await _client() as c:
        try:
            await c.delete(f"{BASE}/chats/{chat_id}/typing")
        except Exception:
            pass


async def mark_as_read(chat_id: str):
    async with await _client() as c:
        try:
            await c.post(f"{BASE}/chats/{chat_id}/read")
        except Exception:
            pass


async def setup_contact_card(phone_number: str, first_name: str, last_name: str = "", image_url: str = "") -> dict:
    payload = {"phone_number": phone_number, "first_name": first_name}
    if last_name:
        payload["last_name"] = last_name
    if image_url:
        payload["image_url"] = image_url
    async with await _client() as c:
        r = await c.post(f"{BASE}/contact_card", json=payload)
        r.raise_for_status()
        return r.json()


async def share_contact_card(chat_id: str):
    async with await _client() as c:
        try:
            await c.post(f"{BASE}/chats/{chat_id}/share_contact_card")
        except Exception:
            pass


async def send_voice_memo(chat_id: str, voice_memo_url: str) -> dict:
    async with await _client() as c:
        r = await c.post(f"{BASE}/chats/{chat_id}/voicememo", json={
            "voice_memo_url": voice_memo_url
        })
        r.raise_for_status()
        return r.json()


async def create_webhook_subscription(target_url: str, events: list[str]) -> dict:
    async with await _client() as c:
        r = await c.post(f"{BASE}/webhook-subscriptions", json={
            "target_url": target_url,
            "subscribed_events": events
        })
        r.raise_for_status()
        return r.json()
