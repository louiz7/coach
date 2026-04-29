import asyncio
import random
from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.models.coach_persona import CoachPersona
from app.services import linq
from app.services.coach import build_system_prompt, call_llm
from app.services.intent_classifier import classify_intents
from app.services.intent_handlers import run_handlers
from app.services.memory import get_conversation, add_message
from app.services.billing import check_subscription
from app.redis import redis_pool


async def process_message(chat_id: str, text: str, event_id: str, phone: str = None):
    """Process an inbound message: classify, call LLM, reply."""
    try:
        await _process_message_inner(chat_id, text, event_id, phone)
    except Exception as e:
        import traceback
        print(f"[process_message ERROR] chat_id={chat_id} text={text!r}: {e}")
        traceback.print_exc()


async def _process_message_inner(chat_id: str, text: str, event_id: str, phone: str = None):
    from app.models.user import ProjectEnum, OnboardingState

    # Dedup
    dedup_key = f"event:{event_id}"
    if event_id:
        already = await redis_pool.get(dedup_key)
        if already:
            return
        await redis_pool.set(dedup_key, "1", ex=86400)

    # NEW: Check message content FIRST before database lookup
    text_lower = text.lower().strip()
    is_hercules_init = any(phrase in text_lower for phrase in [
        "whats hercules", "what's hercules", "what is hercules", "whatis hercules"
    ])

    async with async_session() as db:
        # Find user by chat_id
        result = await db.execute(
            select(User).where(User.linq_chat_id == chat_id)
        )
        user = result.scalar_one_or_none()

        # --- PROJECT ROUTING (message-content first) ---
        # If user exists but not Hercules, AND this is NOT a Hercules init message → skip
        if user and user.project != ProjectEnum.HERCULES:
            if not is_hercules_init:
                # Non-Hercules traffic, let other project handle it
                return
            # User exists in other project but typed "whats hercules?"
            # This is an edge case (user switching projects)
            # For now, we'll ignore this
            return

        # If no user exists but message doesn't contain Hercules keyword → skip
        if not user and not is_hercules_init:
            # Not a Hercules entry message, let other project handle it
            return

        # --- NEW HERCULES USER ---
        if not user:
            # Create new pre-onboarding user tagged as Hercules
            user = User(
                linq_chat_id=chat_id,
                phone=phone or chat_id,  # fallback to chat_id if phone not available
                name="Unbekannt",
                password_hash="pending",
                project=ProjectEnum.HERCULES,
                onboarding_state=OnboardingState.BETA_GATE,
                onboarding_complete=False,
                is_active=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            # Send BETA_GATE prompt with code gate + WhatsApp link
            from app.services import onboarding_chat
            from app.config import settings
            msg = onboarding_chat.BETA_GATE_PROMPT_TEMPLATE.format(url=settings.WHATSAPP_COMMUNITY_URL or "https://hercules.chat")
            await onboarding_chat._send(chat_id, user.id, msg, db)
            return

        # --- ONBOARDING STATE MACHINE ---
        # Handle users still in the iMessage onboarding funnel
        _onboarding_states = {
            OnboardingState.BETA_GATE,
            OnboardingState.CHAT_NAME,
            OnboardingState.CHAT_GOAL,
            OnboardingState.CHAT_SPORTS_FOCUS,
            OnboardingState.CHAT_STATUS,
            OnboardingState.CHAT_CHALLENGE,
            OnboardingState.CHAT_STYLE,
            OnboardingState.CHAT_INTENSITY,
            OnboardingState.CHAT_WHOOP_PROMPT,
            OnboardingState.SPORTS_FOCUS_BACKFILL,
            # Legacy states — onboarding_chat.handle() routes them back to BETA_GATE
            OnboardingState.CHAT_PITCH,
            OnboardingState.FORM,
        }
        if user.onboarding_state in _onboarding_states:
            from app.services import onboarding_chat
            await onboarding_chat.handle(user, chat_id, text, db)
            return

        # WHOOP connect keyword trigger
        _whoop_keywords = ["connect whoop", "link whoop", "whoop connect", "connect my whoop", "add whoop"]
        if any(kw in text_lower for kw in _whoop_keywords):
            from app.services.token import create_onboarding_token
            token = create_onboarding_token(user.phone)
            whoop_msg = (
                f"Tap the link below to connect your WHOOP 🟢\n"
                f"https://hercules.chat/whoop/connect?token={token}"
            )
            await linq.send_message(chat_id, whoop_msg)
            return

        # Check subscription (bypassed during beta phase)
        from app.config import settings
        if settings.BETA_MODE:
            has_sub = True
        else:
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

        # Classify intents (multi-intent)
        intents = await classify_intents(text)

        # Run all matched handlers in parallel, collect context
        handler_context = await run_handlers(intents, user, text, db)

        # Load persona
        result = await db.execute(
            select(CoachPersona).where(CoachPersona.id == user.persona_id)
        )
        persona = result.scalar_one_or_none()
        if not persona:
            return

        # Build context
        system_prompt = await build_system_prompt(user, persona, db, user_message=text)
        if handler_context:
            system_prompt += f"\nCONTEXT:\n{handler_context}\n"

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
            if "PROGRESS_LOG" in intents and any(w in reply.lower() for w in ["pr", "record", "bestleistung", "persönlich"]):
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
