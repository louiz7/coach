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

    async with async_session() as db:
        # Find user by chat_id
        result = await db.execute(
            select(User).where(User.linq_chat_id == chat_id)
        )
        user = result.scalar_one_or_none()

        # --- NEW KANO USER — any message starts the new onboarding ---
        if not user:
            user = User(
                linq_chat_id=chat_id,
                phone=phone or chat_id,
                name="Unbekannt",
                password_hash="pending",
                project=ProjectEnum.KANO,
                onboarding_state=OnboardingState.INFORM,
                onboarding_complete=False,
                is_active=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            from app.services.onboarding_chat import _send_inform_intro
            await _send_inform_intro(chat_id, user.id, db)
            await linq.share_contact_card(chat_id)
            print(f"[message_worker] new user created, intro sent chat_id={chat_id}")
            return

        # --- ONBOARDING STATE MACHINE ---
        _onboarding_states = {
            # New conversational flow
            OnboardingState.INFORM,
            OnboardingState.CAPTURE_GOAL,
            OnboardingState.STATUS_QUO,
            OnboardingState.CONSTRAINTS,
            OnboardingState.WHOOP_OR_BASICS,
            OnboardingState.PLAN_REVIEW,
            OnboardingState.CHALLENGE,
            # Legacy — routed to handler for graceful fast-forward to new flow
            OnboardingState.BETA_GATE,
            OnboardingState.CHAT_NAME,
            OnboardingState.CHAT_GOAL,
            OnboardingState.CHAT_SPORTS_FOCUS,
            OnboardingState.CHAT_STATUS,
            OnboardingState.CHAT_CHALLENGE,
            OnboardingState.CHAT_STYLE,
            OnboardingState.CHAT_INTENSITY,
            OnboardingState.CHAT_WHOOP_PROMPT,
            OnboardingState.AWAITING_PLAN_CONFIRM,
            OnboardingState.SPORTS_FOCUS_BACKFILL,
            OnboardingState.CHAT_PITCH,
            OnboardingState.FORM,
            OnboardingState.CHAT_BODY_METRICS,
            OnboardingState.CHAT_INJURIES,
            OnboardingState.CHAT_CURRENT_SCHEDULE,
            OnboardingState.CHAT_EQUIPMENT,
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
                f"{settings.PUBLIC_BASE_URL.rstrip('/')}/whoop/connect?token={token}"
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
            payment_link = settings.STRIPE_PAYMENT_LINK or f"{settings.PUBLIC_BASE_URL.rstrip('/')}/subscribe"
            paywall_msg = (
                f"you completed your week with me — that's real 💪\n\n"
                f"if you want to keep this going, here's how:\n{payment_link}"
            )
            await linq.send_message(chat_id, paywall_msg)
            return

        # Save inbound message
        await add_message(user.id, "user", text, db)

        # Mark as read
        await linq.mark_as_read(chat_id)

        # Start typing
        await linq.start_typing(chat_id)

        # Load recent conversation for context-aware intent classification
        recent_conv = await get_conversation(user.id, db)
        # Pass last 4 messages (2 turns) as context so classifier understands follow-ups
        context_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in recent_conv[-4:]
        ] if recent_conv else None

        # Classify intents with conversational context
        intents = await classify_intents(text, context_messages=context_messages)

        # Run all matched handlers — they perform actions and return context for the LLM
        # The LLM ALWAYS responds; handlers never bypass it
        handler_context = await run_handlers(intents, user, text, db)

        # Live-fetch WHOOP data when user asks about their metrics
        if "WHOOP_DATA" in intents and user.whoop_access_token:
            try:
                from app.services import whoop as whoop_svc
                recovery = await whoop_svc.get_today_recovery(user.whoop_access_token)
                strain = await whoop_svc.get_today_strain(user.whoop_access_token)

                whoop_lines = []
                if recovery:
                    score = recovery.get("score", {})
                    rs = score.get("recovery_score")
                    hrv = score.get("hrv_rmssd_milli")
                    sleep = score.get("sleep_performance_percentage")
                    rhr = score.get("resting_heart_rate")
                    if rs is not None:
                        user.last_recovery_score = int(rs)
                        emoji = "🟢" if rs >= 67 else ("🟡" if rs >= 34 else "🔴")
                        whoop_lines.append(f"Recovery: {emoji} {int(rs)}%")
                    if hrv is not None:
                        user.last_hrv = float(hrv)
                        whoop_lines.append(f"HRV: {float(hrv):.0f}ms")
                    if sleep is not None:
                        user.last_sleep_performance = int(sleep)
                        whoop_lines.append(f"Sleep performance: {int(sleep)}%")
                    if rhr is not None:
                        whoop_lines.append(f"Resting HR: {int(rhr)}bpm")
                if strain is not None:
                    whoop_lines.append(f"Today's strain: {strain}/21")

                if whoop_lines:
                    extra = "LIVE WHOOP DATA (just fetched — use these exact numbers):\n"
                    extra += "\n".join(whoop_lines) + "\n"
                    handler_context = (handler_context or "") + extra

                await db.commit()
                print(f"[message_worker] live WHOOP fetch: recovery={user.last_recovery_score} hrv={user.last_hrv} sleep={user.last_sleep_performance} strain={strain}")
            except Exception as e:
                print(f"[message_worker] WHOOP live fetch failed (non-fatal): {e}")

        # Load persona
        result = await db.execute(
            select(CoachPersona).where(CoachPersona.id == user.persona_id)
        )
        persona = result.scalar_one_or_none()
        if not persona:
            return

        # Build context
        system_prompt = await build_system_prompt(
            user, persona, db, user_message=text, intents=intents
        )
        if handler_context:
            system_prompt += f"\nCONTEXT:\n{handler_context}\n"

        conversation = recent_conv  # already loaded above

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
    """Split response into chunks for double-texting.

    If the model used the `[MSG]` separator (per the persona prompt's
    "output format" instruction), honor that split directly. Otherwise
    fall back to a sentence-boundary heuristic.
    """
    if "[MSG]" in text:
        parts = [p.strip() for p in text.split("[MSG]")]
        return [p for p in parts if p]

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
