import asyncio
import random
import threading
import posthog


def _posthog_capture(event: str, distinct_id: str, properties: dict = None):
    """Thread-safe posthog capture — won't conflict with arq's running event loop."""
    try:
        threading.Thread(
            target=posthog.capture,
            kwargs={"distinct_id": distinct_id, "event": event, "properties": properties or {}},
            daemon=True,
        ).start()
    except Exception:
        pass

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


async def process_message(chat_id: str, text: str, event_id: str, phone: str = None, image_url: str = None):
    """Process an inbound message: classify, call LLM, reply."""
    try:
        await _process_message_inner(chat_id, text, event_id, phone, image_url=image_url)
    except Exception as e:
        import traceback
        print(f"[process_message ERROR] chat_id={chat_id} text={text!r}: {e}")
        traceback.print_exc()


async def _process_message_inner(chat_id: str, text: str, event_id: str, phone: str = None, image_url: str = None):
    from app.models.user import ProjectEnum, OnboardingState

    # Dedup
    dedup_key = f"event:{event_id}"
    if event_id:
        already = await redis_pool.get(dedup_key)
        if already:
            return
        await redis_pool.set(dedup_key, "1", ex=86400)

    # --- Rate limiting (per user/phone, not per chat_id so new users are also covered) ---
    _rl_id = phone or chat_id
    _hour_key = f"rl:hour:{_rl_id}:{__import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H')}"
    _day_key  = f"rl:day:{_rl_id}:{__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d')}"

    _hour_count = int(await redis_pool.get(_hour_key) or 0)
    _day_count  = int(await redis_pool.get(_day_key)  or 0)

    if _hour_count >= 30:
        print(f"[rate_limit] hourly cap hit for {_rl_id} ({_hour_count} msgs this hour)")
        return
    if _day_count >= 100:
        print(f"[rate_limit] daily cap hit for {_rl_id} ({_day_count} msgs today)")
        return

    await redis_pool.incr(_hour_key)
    await redis_pool.expire(_hour_key, 3600)
    await redis_pool.incr(_day_key)
    await redis_pool.expire(_day_key, 86400)

    async with async_session() as db:
        # Find user by chat_id
        result = await db.execute(
            select(User).where(User.linq_chat_id == chat_id)
        )
        user = result.scalar_one_or_none()

        # --- NEW KANO USER — any message starts the new onboarding ---
        if not user:
            _lang = "de" if (phone or "").startswith("+49") else "en"
            user = User(
                linq_chat_id=chat_id,
                phone=phone or chat_id,
                name="Unbekannt",
                password_hash="pending",
                project=ProjectEnum.KANO,
                onboarding_state=OnboardingState.INFORM,
                onboarding_complete=False,
                is_active=True,
                language=_lang,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            from app.services.onboarding_chat import _send_inform_intro
            from app.config import settings as _cfg
            await db.refresh(user)  # ensure language is loaded
            if _cfg.LINQ_PHONE_NUMBER:
                try:
                    name_parts = _cfg.LINQ_CONTACT_NAME.split(" ", 1)
                    await linq.setup_contact_card(
                        _cfg.LINQ_PHONE_NUMBER,
                        name_parts[0],
                        name_parts[1] if len(name_parts) > 1 else "",
                        _cfg.LINQ_CONTACT_AVATAR_URL,
                    )
                except Exception:
                    pass
            await _send_inform_intro(chat_id, user.id, db, user=user)
            await linq.share_contact_card(chat_id)
            print(f"[message_worker] new user created, intro sent chat_id={chat_id}")
            _posthog_capture("user_created", distinct_id=str(user.id), properties={"channel": "imessage"})
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
            OnboardingState.AWAITING_SUBSCRIPTION,
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
        _posthog_capture("message_received", distinct_id=str(user.id), properties={"message_length": len(text)})

        # Mark as read
        await linq.mark_as_read(chat_id)

        # Start typing + keep refreshing every 4s while we wait for the LLM
        # (Linq auto-clears the indicator after ~5s if not refreshed)
        _typing_active = True

        async def _keep_typing():
            while _typing_active:
                try:
                    await linq.start_typing(chat_id)
                except Exception:
                    pass
                await asyncio.sleep(4)

        # Fire immediately so the indicator shows before the first await below
        await linq.start_typing(chat_id)
        _typing_task = asyncio.create_task(_keep_typing())

        async def _stop_typing():
            nonlocal _typing_active
            _typing_active = False
            _typing_task.cancel()
            await linq.stop_typing(chat_id)

        # Load recent conversation for context-aware intent classification
        recent_conv = await get_conversation(user.id, db)
        # Pass last 4 messages (2 turns) as context so classifier understands follow-ups
        context_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in recent_conv[-4:]
        ] if recent_conv else None

        # Classify intents with conversational context
        # If an image is attached, prepend a hint so the LLM classifier knows
        classify_text = text
        if image_url:
            classify_text = "[USER SENT AN IMAGE ATTACHMENT] " + (text or "")
        intents = await classify_intents(classify_text, context_messages=context_messages)
        if image_url:
            print(f"[message_worker] image detected, intents={intents} url={image_url[:60]}", flush=True)

        # Run all matched handlers — they perform actions and return context for the LLM
        # The LLM ALWAYS responds; handlers never bypass it
        handler_context = await run_handlers(intents, user, text, db, image_url=image_url)

        # Handlers that send replies themselves — skip LLM
        if "CALENDAR_LINK" in intents or "VIEW_PLAN" in intents:
            await _stop_typing()
            return

        # FOOD_LOG handler sends the analysis result itself when an image was
        # processed successfully — skip the LLM so stale conversation history
        # can't override the calorie estimate. When image_url is present and
        # FOOD_LOG ran, the handler has already replied.
        if "FOOD_LOG" in intents and image_url:
            await _stop_typing()
            return

        # Load persona
        result = await db.execute(
            select(CoachPersona).where(CoachPersona.id == user.persona_id)
        )
        persona = result.scalar_one_or_none()
        if not persona:
            await _stop_typing()
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
            if not reply:
                print(f"[message_worker] call_llm returned empty for chat_id={chat_id}", flush=True)
                reply = "something went wrong on my end — try again 💪"
        except Exception as e:
            import traceback
            print(f"[message_worker] call_llm FAILED chat_id={chat_id} text={text!r}: {e}", flush=True)
            traceback.print_exc()
            reply = "something went wrong on my end — try again 💪"

        # If handler context has a plan URL that will be sent standalone below,
        # strip it from the LLM reply to avoid sending the URL twice
        import re as _re
        _plan_url_pattern = r'https?://\S+/plan\?token=\S+'
        if handler_context and _re.search(_plan_url_pattern, handler_context):
            reply = _re.sub(_plan_url_pattern, '', reply).strip()
            # Clean up any orphaned punctuation left after URL removal (e.g. "here: .")
            reply = _re.sub(r':\s*\.', '.', reply)
            reply = _re.sub(r'\s{2,}', ' ', reply)

        # Split into chunks and send with delays (double-texting)
        chunks = _split_response(reply)
        for i, chunk in enumerate(chunks):
            if i == 0:
                # First chunk — stop the background keep-typing loop and send
                await _stop_typing()
            else:
                # Between chunks — brief pause then show typing again
                await asyncio.sleep(random.uniform(0.5, 3.0))
                await linq.start_typing(chat_id)
                await asyncio.sleep(random.uniform(0.5, 2.0))
                await linq.stop_typing(chat_id)

            # Send with confetti for PRs
            if "PROGRESS_LOG" in intents and any(w in reply.lower() for w in ["pr", "record", "bestleistung", "persönlich"]):
                await linq.send_message_with_effect(chat_id, chunk, "screen", "confetti")
            else:
                await linq.send_message(chat_id, chunk)

            await add_message(user.id, "assistant", chunk, db)

        # If the handler context contains a Plan URL, send it as a standalone message
        # so iMessage renders the rich link preview
        if handler_context:
            import re as _re
            url_match = _re.search(r'https?://\S+/plan\?token=\S+', handler_context)
            if url_match:
                plan_url = url_match.group(0).rstrip('.')
                await asyncio.sleep(0.8)
                await linq.send_message(chat_id, plan_url)
                await add_message(user.id, "assistant", plan_url, db)


def _split_response(text: str) -> list[str]:
    """Split response into chunks for double-texting."""
    if not text:
        return []
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
