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


async def process_message(chat_id: str, text: str, event_id: str, phone: str = None):
    """Process an inbound message: classify, call LLM, reply."""
    from app.models.user import ProjectEnum, OnboardingState

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

        # --- PROJECT ROUTING ---
        # If user exists but belongs to another project, ignore
        if user and user.project != ProjectEnum.HERCULES:
            return

        # If no user exists, check if this is a Hercules entry message
        if not user:
            text_lower = text.lower().strip()
            is_hercules_entry = any(p in text_lower for p in [
                "whats hercules", "what's hercules", "what is hercules"
            ])
            if not is_hercules_entry:
                # Not a Hercules message, ignore (likely for another project)
                return

            # Create new pre-onboarding user tagged as Hercules
            user = User(
                linq_chat_id=chat_id,
                phone=phone or chat_id,  # fallback to chat_id if phone not available
                name="Unbekannt",
                password_hash="pending",
                project=ProjectEnum.HERCULES,
                onboarding_state=OnboardingState.CHAT_NAME,
                onboarding_complete=False,
                is_active=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            # Send Hercules intro message
            welcome = (
                "Hey! Ich bin Hercules — dein persönlicher Trainer für deine Fitness-Reise 💪 "
                "Wie darf ich dich nennen?"
            )
            await linq.send_message(chat_id, welcome)
            await add_message(user.id, "assistant", welcome, db)
            return

        # --- ONBOARDING STATE MACHINE ---
        # Handle users still in the iMessage onboarding funnel
        if user.onboarding_state in (
            OnboardingState.CHAT_NAME,
            OnboardingState.CHAT_GOAL,
            OnboardingState.CHAT_PITCH,
            OnboardingState.FORM,
        ):
            await _handle_onboarding(user, chat_id, text, db)
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


async def _handle_onboarding(user: User, chat_id: str, text: str, db) -> None:
    """Handle the iMessage onboarding state machine."""
    from app.models.user import OnboardingState

    state = user.onboarding_state

    if state == OnboardingState.CHAT_NAME:
        # Save name from user's reply
        user.name = text.strip().split()[0].capitalize()  # Use first word as name
        user.onboarding_state = OnboardingState.CHAT_GOAL
        await db.commit()

        reply = f"Freut mich, dich kennenzulernen, {user.name}! 🙌 Was möchtest du erreichen?"
        await linq.send_message(chat_id, reply)
        await add_message(user.id, "assistant", reply, db)

    elif state == OnboardingState.CHAT_GOAL:
        # Save goal from user's reply
        user.goal = text.strip()
        user.onboarding_state = OnboardingState.CHAT_PITCH
        await db.commit()

        pitch = (
            f"Ich verstehe — {user.goal.lower()} ist ein starkes Ziel 🔥\n\n"
            "Hercules ist dein persönlicher KI-Coach, der dich jeden Tag per iMessage begleitet. "
            "Kein App-Download, kein Gym-Abo nötig — nur du, dein Handy und ein Plan, "
            "der wirklich zu dir passt.\n\n"
            "Bist du ready, dein Leben zu verändern? 💪"
        )
        await linq.send_message(chat_id, pitch)
        await add_message(user.id, "assistant", pitch, db)

    elif state == OnboardingState.CHAT_PITCH:
        text_lower = text.lower().strip()
        is_yes = any(w in text_lower for w in ["ja", "yes", "jo", "klar", "ready", "let's go", "lets go", "yep", "yup", "auf jeden", "natürlich"])
        is_no = any(w in text_lower for w in ["nein", "no", "nö", "nicht", "nope"])

        if is_yes:
            user.onboarding_state = OnboardingState.FORM
            await db.commit()

            # TODO: replace with real token-based URL once web form is ready
            form_url = "https://hercules.chat/start"
            reply = (
                f"Let's go, {user.name}! 🚀\n\n"
                f"Füll hier kurz dein Profil aus — dauert nur 2 Minuten:\n{form_url}\n\n"
                "Danach geht's sofort los 🔥"
            )
            await linq.send_message(chat_id, reply)
            await add_message(user.id, "assistant", reply, db)

        elif is_no:
            reply = (
                "Kein Problem! Falls du doch noch Fragen hast oder bereit bist — "
                "ich bin jederzeit hier. Schreib mir einfach nochmal 💬"
            )
            await linq.send_message(chat_id, reply)
            await add_message(user.id, "assistant", reply, db)

        else:
            # Unclear answer, re-ask
            reply = "Kurze Ja/Nein Frage 😄 — Bist du ready, loszulegen?"
            await linq.send_message(chat_id, reply)
            await add_message(user.id, "assistant", reply, db)

    elif state == OnboardingState.FORM:
        # User texted again while form is pending
        reply = (
            f"Hey {user.name}! Du hast noch das Formular offen 👆 "
            "Füll es kurz aus und dann legen wir direkt los! 🚀"
        )
        await linq.send_message(chat_id, reply)
        await add_message(user.id, "assistant", reply, db)


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
