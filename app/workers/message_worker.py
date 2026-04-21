import asyncio
import random
from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.models.coach_persona import CoachPersona
from app.services import linq
from app.services.coach import build_system_prompt, call_llm, classify_intent
from app.services.memory import get_conversation, add_message
from app.services.progress import parse_and_store_progress
from app.services.training_plan import generate_plan
from app.services.billing import check_subscription
from app.redis import redis_pool


async def process_message(chat_id: str, text: str, event_id: str):
    """Process an inbound message: classify, call LLM, reply."""

    # Dedup
    dedup_key = f"event:{event_id}"
    if event_id:
        already = await redis_pool.get(dedup_key)
        if already:
            return
        await redis_pool.set(dedup_key, "1", ex=86400)

    async with async_session() as db:
        # Find user by chat_id
        result = await db.execute(
            select(User).where(User.linq_chat_id == chat_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return

        # Check subscription
        has_sub = await check_subscription(user.id, db)
        if not has_sub:
            paywall_msg = "Hey! Um weiter zu chatten, brauchst du ein Abo. Check die App fuer mehr Infos 🙏"
            await linq.send_message(chat_id, paywall_msg)
            return

        # Save inbound message
        await add_message(user.id, "user", text, db)

        # Mark as read
        await linq.mark_as_read(chat_id)

        # Start typing
        await linq.start_typing(chat_id)

        # Classify intent
        try:
            intent = await classify_intent(text)
        except Exception:
            intent = "GENERAL"

        # Handle progress logging
        if intent == "PROGRESS_LOG":
            try:
                await parse_and_store_progress(user.id, text, db)
            except Exception:
                pass

        # Handle plan requests
        modification_context = None
        if intent == "PLAN_REQUEST":
            try:
                plan = await generate_plan(user, db, modification=text)
                modification_context = f"New plan generated. Summary: {plan.raw_text[:300]}"
            except Exception:
                modification_context = "Failed to generate plan, apologize and try again."

        # Load persona
        result = await db.execute(
            select(CoachPersona).where(CoachPersona.id == user.persona_id)
        )
        persona = result.scalar_one_or_none()
        if not persona:
            return

        # Build context
        system_prompt = await build_system_prompt(user, persona, db)
        if modification_context:
            system_prompt += f"\nCONTEXT: {modification_context}\n"

        conversation = await get_conversation(user.id, db)

        # Call LLM
        try:
            reply = await call_llm(system_prompt, conversation)
        except Exception:
            reply = "Sorry, da ist was schiefgelaufen. Versuch's nochmal! 💪"

        # Split into chunks and send with delays (double-texting)
        chunks = _split_response(reply)
        for i, chunk in enumerate(chunks):
            if i > 0:
                delay = random.uniform(0.5, 3.0)
                await asyncio.sleep(delay)
                await linq.start_typing(chat_id)
                await asyncio.sleep(random.uniform(0.5, 2.0))

            # Send with confetti for PRs
            if intent == "PROGRESS_LOG" and any(w in reply.lower() for w in ["pr", "record", "bestleistung", "persönlich"]):
                await linq.send_message_with_effect(chat_id, chunk, "screen", "confetti")
            else:
                await linq.send_message(chat_id, chunk)

            await add_message(user.id, "assistant", chunk, db)


def _split_response(text: str) -> list[str]:
    """Split response into chunks at sentence boundaries for double-texting."""
    if len(text) < 80:
        return [text]

    # Split on sentence endings
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)

    if len(sentences) <= 1:
        return [text]

    # Group into chunks of 1-2 sentences
    chunks = []
    current = ""
    for s in sentences:
        if current and len(current) + len(s) > 120:
            chunks.append(current.strip())
            current = s
        else:
            current = (current + " " + s).strip() if current else s

    if current:
        chunks.append(current.strip())

    return chunks if chunks else [text]
