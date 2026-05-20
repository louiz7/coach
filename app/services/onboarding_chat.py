"""In-chat onboarding state machine for Kano.

New conversational flow — 7 steps, free text, LLM extraction, no letter menus:

    INFORM → CAPTURE_GOAL → STATUS_QUO → CONSTRAINTS
           → WHOOP_OR_BASICS → PLAN_REVIEW → CHALLENGE → DONE

Legacy states (BETA_GATE, CHAT_NAME, CHAT_GOAL, …) are automatically
fast-forwarded to INFORM so users who started the old flow get a clean restart.
"""

import asyncio
import json
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import OnboardingState, User
from app.services import analytics, linq
from app.services.memory import add_message
from app.services.persona import assign_persona_from_style


# ─── Legacy state set (all routed → INFORM restart) ──────────────────────────

_LEGACY_STATES = {
    OnboardingState.BETA_GATE,
    OnboardingState.CHAT_NAME,
    OnboardingState.CHAT_GOAL,
    OnboardingState.CHAT_SPORTS_FOCUS,
    OnboardingState.CHAT_STATUS,
    OnboardingState.CHAT_CHALLENGE,
    OnboardingState.CHAT_STYLE,
    OnboardingState.CHAT_INTENSITY,
    OnboardingState.CHAT_BODY_METRICS,
    OnboardingState.CHAT_INJURIES,
    OnboardingState.CHAT_CURRENT_SCHEDULE,
    OnboardingState.CHAT_EQUIPMENT,
    OnboardingState.CHAT_WHOOP_PROMPT,
    OnboardingState.AWAITING_PLAN_CONFIRM,
    OnboardingState.CHAT_PITCH,
    OnboardingState.FORM,
}


# ─── Bilingual strings ───────────────────────────────────────────────────────

_STRINGS: dict[str, dict] = {
    "en": {
        "inform_intro": [
            "I'm your AI personal trainer, right here in iMessage",
            "before we get into it: what's your name?",
        ],
        "inform_name_question": "what's your name?",
        "inform_just_name": "just your first name is fine 🙂",
        "inform_greeting": [
            "nice to meet you {name}!",
            "I can build your workout plan, check in daily to keep you on track, hook up your wearables for data-driven coaching, and track your nutrition — just send me a photo of your meal and I'll estimate the calories 📸",
            "so you actually hit your goals",
            "Speaking of goals, what are your fitness goals? running a 5k? building muscle? losing weight? throw everything at me",
        ],
        "goal_sidebar_q": "what are your fitness goals? build muscle? lose weight? run a 5k? throw everything at me 🎯",
        "goal_reask": "could be anything — build muscle, run a 5k, lose weight, get stronger. what do you want to work towards? 🎯",
        "status_prompt": [
            "got it. how does your current training look like?",
            "how many days a week, what do you do and when",
            "give me the honest version",
        ],
        "status_sidebar_q": "how does your current training look? how many days a week, what do you do — give me the honest version",
        "status_reask": "just a quick picture of your week like 'I train 3x, mostly lifting, sometimes I run'",
        "constraints_prompt": [
            "what should I keep in mind when coaching you and creating your plan?",
            "injuries, busy weeks/days, exercises you hate, multiple disciplines (e.g. running + gym) – anything goes",
        ],
        "constraints_sidebar_q": "what should I keep in mind when coaching you? injuries, busy days, exercises you hate, anything goes",
        "whoop_prompt": [
            "got it. one more thing before I build your plan",
            "you can connect your WHOOP so I can coach you based on your actual recovery, sleep and strain data 📊\n{whoop_url}",
            "no WHOOP? just reply with your age, weight and gender",
        ],
        "basics_sidebar_q": "just send your age, weight and gender — like: 24, 78 kg, male",
        "basics_reask": "just send your age, weight and gender — like: 24, 78 kg, male",
        "plan_ack": "ty, got your data",
        "plan_ack_whoop": "ty, got your WHOOP data",
        "plan_building_tease": "building your plan now… gimme a sec",
        "plan_pitch": [
            "i have everything i need to build your plan 💪",
            "first 7 days are on me — free trial, no charge upfront",
            "start here and i'll send your plan straight here 👇\n{payment_link}",
        ],
        "sub_reminder": [
            "your plan is ready — unlock it with your free trial 🙌",
            "{paywall_url}",
        ],
        "plan_building": "building your plan now… 💪",
        "plan_review_prompt": "want to change anything or does this work for you?",
        "plan_regen_ack": "on it — rebuilding your plan now ⚙️",
        "plan_regen_done": "updated 💪",
        "plan_regen_fail": "hmm, had trouble rebuilding — you can text me changes anytime and I'll fix it 🔄",
        "plan_fail": "had a small hiccup building your plan — just text me 'build my plan' and i'll get it sorted 🔄",
        "done": [
            "perfect, let's get to work 🫡 text me anytime",
            "you can also add your workouts to Apple Calendar 📅",
        ],
        "sports_backfill_q": "which sports or activities are you focused on improving? (e.g. running, climbing, lifting)",
        "sports_backfill_done": "got it 💪 just text me whenever you need anything.",
        "sidebar_lang_instruction": "Answer in English.",
    },
    "de": {
        "inform_intro": [
            "Ich bin dein KI-Personal Trainer – direkt in iMessage",
            "bevor wir loslegen: wie heißt du?",
        ],
        "inform_name_question": "wie heißt du?",
        "inform_just_name": "einfach dein Vorname reicht 🙂",
        "inform_greeting": [
            "nice to meet you {name}!",
            "ich kann dir deinen Trainingsplan erstellen, täglich einchecken, deine Wearables verbinden und deine Ernährung tracken — schick mir einfach ein Foto deiner Mahlzeit und ich schätze die Kalorien 📸",
            "damit du deine Ziele wirklich erreichst",
            "apropos Ziele — was sind deine Fitnessziele? einen 5k laufen? Muskeln aufbauen? Gewicht verlieren? alles raus damit",
        ],
        "goal_sidebar_q": "was sind deine Fitnessziele? Muskeln aufbauen? abnehmen? 5k laufen? alles raus damit 🎯",
        "goal_reask": "kann alles sein — Muskeln aufbauen, 5k laufen, abnehmen, stärker werden. was willst du erreichen? 🎯",
        "status_prompt": [
            "alles klar. wie sieht dein aktuelles Training aus?",
            "wie viele Tage pro Woche, was machst du und wann",
            "die ehrliche Version bitte",
        ],
        "status_sidebar_q": "wie sieht dein aktuelles Training aus? wie viele Tage pro Woche, was machst du — die ehrliche Version",
        "status_reask": "kurz reicht — z.B. 'ich trainiere 3x, meistens Kraftsport, manchmal laufen'",
        "constraints_prompt": [
            "was soll ich beim Coaching und bei deinem Plan beachten?",
            "Verletzungen, stressige Wochen/Tage, Übungen die du hasst, mehrere Sportarten (z.B. Laufen + Gym) – alles erlaubt",
        ],
        "constraints_sidebar_q": "was soll ich beim Coaching beachten? Verletzungen, stressige Tage, Übungen die du hasst, alles erlaubt",
        "whoop_prompt": [
            "alles klar. noch eine Sache bevor ich deinen Plan erstelle",
            "du kannst deinen WHOOP verbinden, damit ich dich basierend auf deinen echten Recovery-, Schlaf- und Belastungsdaten coache 📊\n{whoop_url}",
            "kein WHOOP? schick mir einfach dein Alter, Gewicht und Geschlecht",
        ],
        "basics_sidebar_q": "schick mir Alter, Gewicht und Geschlecht — z.B.: 24, 78 kg, männlich",
        "basics_reask": "schick mir Alter, Gewicht und Geschlecht — z.B.: 24, 78 kg, männlich",
        "plan_ack": "danke, hab deine Daten",
        "plan_ack_whoop": "danke, hab deine WHOOP-Daten",
        "plan_building_tease": "ich erstelle deinen Plan… einen Moment 💪",
        "plan_pitch": [
            "ich hab alles was ich brauche um deinen Plan zu erstellen 💪",
            "die ersten 7 Tage sind kostenlos — keine Kosten im Voraus",
            "starte hier und ich schick dir deinen Plan direkt hierher 👇\n{payment_link}",
        ],
        "sub_reminder": [
            "dein Plan ist fertig — schalte ihn mit deiner kostenlosen Testphase frei 🙌",
            "{paywall_url}",
        ],
        "plan_building": "ich erstelle deinen Plan… 💪",
        "plan_review_prompt": "möchtest du etwas ändern oder passt das so für dich?",
        "plan_regen_ack": "alles klar — ich erstelle deinen Plan neu ⚙️",
        "plan_regen_done": "fertig 💪",
        "plan_regen_fail": "hmm, hatte Probleme beim Neuerstellen — schreib mir einfach Änderungen und ich fixe es 🔄",
        "plan_fail": "kleiner Fehler beim Erstellen — schreib mir einfach 'bau meinen Plan' und ich kümmer mich drum 🔄",
        "done": [
            "perfekt, lass uns loslegen 🫡 schreib mir jederzeit",
            "du kannst dein Training auch zu Apple Kalender hinzufügen 📅",
        ],
        "sports_backfill_q": "auf welche Sportarten oder Aktivitäten möchtest du dich konzentrieren? (z.B. Laufen, Klettern, Kraftsport)",
        "sports_backfill_done": "alles klar 💪 schreib mir jederzeit.",
        "sidebar_lang_instruction": "Antworte auf Deutsch.",
    },
}


def _t(user, key: str):
    """Return the string(s) for the given key in the user's language."""
    lang = (getattr(user, "language", None) or "en")
    bucket = _STRINGS.get(lang, _STRINGS["en"])
    return bucket.get(key, _STRINGS["en"].get(key, ""))


# ─── LLM extraction helper ────────────────────────────────────────────────────

_EXTRACT_PROMPTS: dict[str, str] = {
    "name": (
        'Extract the person\'s first name from this message: "{text}"\n'
        'Return JSON: {{"name": "FirstName or null", "valid": true or false}}\n'
        "valid=false if no real name is present (e.g. it's a question, random words, or a greeting with no name)."
    ),
    "goal": (
        'Extract fitness goals from this message (may be in any language): "{text}"\n'
        'Return JSON: {{"goal": "concise goal description in English", "sports_focus": "sports/activities mentioned or null", "valid": true or false}}\n'
        "Be generous: valid=true for anything expressing a fitness desire, sport, or body goal. valid=false ONLY for complete gibberish with zero fitness relevance."
    ),
    "status": (
        'Extract training info from this message (may be in any language): "{text}"\n'
        'Return JSON: {{"training_frequency": <int days per week, 0 if none>, "schedule_summary": "brief description in English", "valid": true or false}}\n'
        "Be generous: valid=true for anything describing training, sport, frequency, activity level, or even 'I don't train'. valid=false ONLY for complete gibberish with zero fitness relevance."
    ),
    "constraints": (
        'Extract training constraints from this message (may be in any language): "{text}"\n'
        'Return JSON: {{"injuries": "description or null", "equipment": "gym/home/both/outdoor or null", "notes": "other constraints", "valid": true}}\n'
        "Always valid=true. injuries=null if none mentioned."
    ),
    "basics": (
        'Extract body metrics from this message (may be in any language) — fields can appear in ANY order and with ANY units.\n'
        'Message: "{text}"\n'
        'Return JSON: {{"age": <int or null>, "weight_kg": <float or null>, "gender": "male/female/other or null", "height_cm": <float or null>, "valid": true or false}}\n'
        "Rules:\n"
        "- Convert lbs → kg (1 lb = 0.453592 kg)\n"
        "- Convert feet/inches → cm (1 ft = 30.48 cm, 1 in = 2.54 cm)\n"
        "- Height in cm stays as-is; height like '193cm' → 193.0\n"
        "- Gender: male/man/m/männlich → 'male'; female/woman/f/weiblich → 'female'; anything else → 'other'\n"
        "- valid=false ONLY if none of age/weight/gender could be extracted at all."
    ),
    "constraints_intent": (
        'Does this message (may be in any language) indicate the person has NO constraints, injuries, or special requirements? Message: "{text}"\n'
        'Return JSON: {{"no_constraints": true or false}}\n'
        "no_constraints=true if they said something like none, nothing, all good, nope, no injuries, etc. false if they mentioned anything specific."
    ),
    "challenge": (
        'The user was asked if they want to do a 7-day fitness challenge. Did they say yes or no? Message (may be in any language): "{text}"\n'
        'Return JSON: {{"accepted": true or false}}\n'
        "accepted=true for yes/sure/let's go/in/absolutely/etc. false for no/nope/pass/later/not now/etc."
    ),
}


async def _llm_extract(step: str, text: str) -> dict | None:
    """Use gpt-4o-mini to extract structured data from a user message.

    Returns None on API failure; returns dict with a 'valid' key otherwise.
    """
    from openai import AsyncOpenAI

    prompt = _EXTRACT_PROMPTS[step].format(text=text)
    client = AsyncOpenAI(api_key=settings.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
    try:
        resp = await client.chat.completions.create(
            model="deepseek/deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[onboarding _llm_extract] step={step} failed: {e}")
        return None


# ─── Sidebar question detector ──────────────────────────────────────────────────

_SIDEBAR_PROMPT = """
You are Kano, an AI personal fitness coach onboarding a new user via iMessage.
{asked_context}
The user said: "{user_message}"

Decide: is their message a separate question or comment about how Kano works / fitness in general,
or is it an attempt to answer the pending question (if any)?

Return JSON:
{{
  "is_sidebar": true or false,
  "answer": "if is_sidebar=true: your short direct answer (1-3 sentences max, casual iMessage tone). Do NOT end with a question — we will re-ask the pending question separately. null otherwise."
}}

is_sidebar=true examples: questions about privacy/data storage, how Kano works, pricing, what WHOOP is, whether it's free, greetings with no name, random off-topic comments, asking what iMessage bot this is.
is_sidebar=false examples: actually answering the pending question — e.g. giving their name, stating their fitness goal, describing their training, listing constraints, giving age/weight/gender. When in doubt, treat as an answer (is_sidebar=false).

Facts about Kano you MUST use if pricing or cost comes up:
- 7-day free trial, no charge upfront
- After the trial: €3.49 per week
- Cancel anytime
- Works entirely via iMessage, no app to download

What Kano can do: build personalised workout plans, daily check-ins via iMessage, connect to WHOOP for data-driven coaching, track nutrition via food photos.
{lang_instruction}
"""


async def _sidebar_check(
    user: User,
    chat_id: str,
    text: str,
    pending_question: str,
    db: AsyncSession,
) -> bool:
    """Check if the user's message is a sidebar question rather than an onboarding answer.

    If it is, answer it briefly and re-ask the pending question.
    Returns True if it was a sidebar (caller should return early),
    False if the message is an actual answer (proceed with extraction).
    """
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
    try:
        asked_context = (
            f'You just asked the user: "{pending_question}"'
            if pending_question
            else "This is the user's very first message — you haven't asked them anything yet."
        )
        # Escape the user message so embedded quotes/newlines don't break the JSON the LLM returns
        safe_text = (text or "").replace("\\", "\\\\").replace('"', "'")
        prompt = _SIDEBAR_PROMPT.format(
            asked_context=asked_context,
            user_message=safe_text,
            lang_instruction=_t(user, "sidebar_lang_instruction"),
        )
        resp = await client.chat.completions.create(
            model="deepseek/deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
        try:
            data = json.loads(raw)
        except Exception:
            # Fallback: try to extract fields with regex if JSON is malformed
            import re as _re
            is_sidebar = bool(_re.search(r'"is_sidebar"\s*:\s*true', raw, _re.I))
            answer_match = _re.search(r'"answer"\s*:\s*"(.*?)"(?:\s*[,}])', raw, _re.S)
            data = {
                "is_sidebar": is_sidebar,
                "answer": answer_match.group(1).replace('\\n', '\n') if answer_match else None,
            }
        if data.get("is_sidebar") and data.get("answer"):
            await _send(chat_id, user.id, data["answer"], db)
            await asyncio.sleep(0.8)
            await _send(chat_id, user.id, pending_question, db)
            return True
    except Exception as e:
        print(f"[onboarding _sidebar_check] failed: {e}")
    return False


# ─── Reask counter (Redis-backed, per user per state) ─────────────────────────

async def _reask_count(user_id, state: str) -> int:
    from app.redis import redis_pool
    val = await redis_pool.get(f"onboarding:reask:{user_id}:{state}")
    return int(val) if val else 0


async def _inc_reask(user_id, state: str) -> None:
    from app.redis import redis_pool
    key = f"onboarding:reask:{user_id}:{state}"
    await redis_pool.incr(key)
    await redis_pool.expire(key, 86400)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _send(chat_id: str, user_id, text: str, db: AsyncSession) -> None:
    await linq.send_message(chat_id, text)
    await add_message(user_id, "assistant", text, db)


async def _send_multi(
    chat_id: str,
    user_id,
    messages: list[str],
    db: AsyncSession,
    delay: float = 1.2,
) -> None:
    """Send multiple messages with short typing pauses between them."""
    for i, msg in enumerate(messages):
        if i > 0:
            await asyncio.sleep(delay)
        await _send(chat_id, user_id, msg, db)


# ─── Entry point ──────────────────────────────────────────────────────────────

async def handle(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    state = user.onboarding_state

    # Legacy users → fresh restart
    if state in _LEGACY_STATES:
        await _restart_inform(user, chat_id, db)
        return

    if state == OnboardingState.SPORTS_FOCUS_BACKFILL:
        await _handle_sports_focus_backfill(user, chat_id, text, db)
        return

    dispatch = {
        OnboardingState.INFORM:                  _handle_inform,
        OnboardingState.CAPTURE_GOAL:            _handle_capture_goal,
        OnboardingState.STATUS_QUO:              _handle_status_quo,
        OnboardingState.CONSTRAINTS:             _handle_constraints,
        OnboardingState.WHOOP_OR_BASICS:         _handle_whoop_or_basics,
        OnboardingState.PLAN_REVIEW:             _handle_plan_review,
        OnboardingState.CHALLENGE:               _handle_challenge,
        OnboardingState.AWAITING_SUBSCRIPTION:   _handle_awaiting_subscription,
    }
    handler = dispatch.get(state)
    if handler:
        await handler(user, chat_id, text, db)
    else:
        # Unknown state — restart
        await _restart_inform(user, chat_id, db)


# ─── Initial intro (called by message_worker for brand-new users) ─────────────

async def _send_inform_intro(chat_id: str, user_id, db: AsyncSession, user=None) -> None:
    """Send the Kano intro and ask for the user's name."""
    # user may be None for brand-new users; fall back to English
    class _FallbackUser:
        language = "en"
    _u = user or _FallbackUser()
    await _send_multi(chat_id, user_id, _t(_u, "inform_intro"), db, delay=1.0)


# ─── State handlers ───────────────────────────────────────────────────────────

async def _restart_inform(user: User, chat_id: str, db: AsyncSession) -> None:
    """Move a legacy-state user to the new INFORM flow."""
    user.onboarding_state = OnboardingState.INFORM
    user.name = "Unbekannt"
    await db.commit()
    await _send_inform_intro(chat_id, user.id, db, user=user)


async def _handle_inform(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Capture the user's first name, then pitch and ask for their goal.

    If the message doesn't look like a name (e.g. "whats kano?", "can you help
    me lose weight?", "hi"), we:
      1. Check if it's a sidebar question/comment via _sidebar_check.
         If yes → answer it naturally + re-ask for name. Done.
      2. Otherwise (e.g. plain "hi", one word, no name extractable):
         - If intro not yet shown (name=unbekannt) → send intro (which asks name).
         - If intro already shown → politely re-ask for just the name.
    """
    raw = (text or "").strip()
    name_already_set = (user.name or "").lower() not in {"", "unbekannt"}

    if not raw:
        if not name_already_set:
            await _send_inform_intro(chat_id, user.id, db, user=user)
        else:
            await _send(chat_id, user.id, _t(user, "inform_name_question"), db)
        return

    data = await _llm_extract("name", raw)
    extracted = (data or {}).get("name") if (data or {}).get("valid") else None

    if not extracted:
        # Try to handle it as a sidebar question/comment first.
        # _sidebar_check will answer naturally and re-ask the pending question.
        # After the sidebar answer we always want to get the name, so if the
        # intro hasn't been sent yet we prepend it before the name question.
        if not name_already_set:
            # Build a combined pending question: intro lines + name ask
            intro_lines = _t(user, "inform_intro")
            # Use the last intro line (the name question) as the pending question;
            # _sidebar_check will re-ask only that line. We send the rest of the
            # intro ourselves before the sidebar answer if not already shown.
            name_question = _t(user, "inform_name_question")
            handled = await _sidebar_check(user, chat_id, text, name_question, db)
            if handled:
                return
            # Not a sidebar — just plain "hi" or something with no name.
            # Send the full intro (it already ends with the name question).
            await _send_inform_intro(chat_id, user.id, db, user=user)
        else:
            # Intro already shown — check if it's a sidebar question.
            handled = await _sidebar_check(
                user, chat_id, text, _t(user, "inform_name_question"), db
            )
            if handled:
                return
            await _send(chat_id, user.id, _t(user, "inform_just_name"), db)
        return

    user.name = extracted.strip().capitalize()
    user.onboarding_state = OnboardingState.CAPTURE_GOAL
    await db.commit()

    analytics.capture("onboarding_step_completed", user, properties={"step": "inform"})

    msgs = [m.format(name=user.name) if "{name}" in m else m for m in _t(user, "inform_greeting")]
    await _send_multi(chat_id, user.id, msgs, db)


async def _handle_capture_goal(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Extract fitness goal and sports focus from free text."""
    if await _sidebar_check(
        user, chat_id, text,
        _t(user, "goal_sidebar_q"),
        db,
    ):
        return

    data = await _llm_extract("goal", text or "")

    if not data or not data.get("valid"):
        reasks = await _reask_count(user.id, OnboardingState.CAPTURE_GOAL)
        if reasks >= 1:
            user.goal = (text or "").strip()[:2000] or "general fitness"
        else:
            await _inc_reask(user.id, OnboardingState.CAPTURE_GOAL)
            await _send(chat_id, user.id, _t(user, "goal_reask"), db)
            return
    else:
        user.goal = (data.get("goal") or (text or "").strip())[:2000]
        if data.get("sports_focus"):
            user.sports_focus = data["sports_focus"][:2000]

    user.onboarding_state = OnboardingState.STATUS_QUO
    await db.commit()

    reasks = await _reask_count(user.id, OnboardingState.CAPTURE_GOAL)
    analytics.capture(
        "onboarding_step_completed",
        user,
        properties={
            "step": "capture_goal",
            "reasks": reasks,
            "has_sports_focus": bool(user.sports_focus),
        },
    )

    await _send_multi(chat_id, user.id, _t(user, "status_prompt"), db)


async def _handle_status_quo(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Extract current training frequency and schedule from free text."""
    if await _sidebar_check(
        user, chat_id, text,
        _t(user, "status_sidebar_q"),
        db,
    ):
        return

    data = await _llm_extract("status", text or "")

    if not data or not data.get("valid"):
        reasks = await _reask_count(user.id, OnboardingState.STATUS_QUO)
        if reasks >= 1:
            user.training_frequency = 0
            user.current_schedule_notes = (text or "").strip()[:3000]
        else:
            await _inc_reask(user.id, OnboardingState.STATUS_QUO)
            await _send(chat_id, user.id, _t(user, "status_reask"), db)
            return
    else:
        user.training_frequency = int(data.get("training_frequency") or 0)
        user.current_schedule_notes = (
            data.get("schedule_summary") or (text or "").strip()
        )[:3000]

    user.onboarding_state = OnboardingState.CONSTRAINTS
    await db.commit()

    reasks = await _reask_count(user.id, OnboardingState.STATUS_QUO)
    analytics.capture(
        "onboarding_step_completed",
        user,
        properties={
            "step": "status_quo",
            "reasks": reasks,
            "training_frequency": user.training_frequency,
        },
    )

    await _send_multi(chat_id, user.id, _t(user, "constraints_prompt"), db)


async def _handle_constraints(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Extract injuries, equipment and other constraints from free text."""
    if await _sidebar_check(
        user, chat_id, text,
        _t(user, "constraints_sidebar_q"),
        db,
    ):
        return

    raw = (text or "").strip()

    # Use LLM to detect "no constraints" intent — handles any language/phrasing
    intent = await _llm_extract("constraints_intent", raw)
    no_constraints = (intent or {}).get("no_constraints", False)

    if not no_constraints and raw:
        data = await _llm_extract("constraints", raw)
        if data:
            if data.get("injuries"):
                user.injuries = data["injuries"][:2000]
            if data.get("equipment"):
                user.equipment_access = data["equipment"]
            extra_notes = data.get("notes", "")
            if extra_notes:
                existing = user.current_schedule_notes or ""
                combined = f"{existing} | {extra_notes}" if existing else extra_notes
                user.current_schedule_notes = combined[:3000]
        else:
            # LLM failed — store raw text as injury notes
            user.injuries = raw[:2000]

    user.onboarding_state = OnboardingState.WHOOP_OR_BASICS
    await db.commit()

    analytics.capture(
        "onboarding_step_completed",
        user,
        properties={
            "step": "constraints",
            "has_injuries": bool(user.injuries),
            "has_equipment_constraints": bool(user.equipment_access),
        },
    )

    # Build WHOOP link
    from app.services.token import create_onboarding_token
    token = create_onboarding_token(user.phone)
    base_url = settings.PUBLIC_BASE_URL.rstrip('/')
    whoop_url = f"{base_url}/whoop/connect?token={token}"

    whoop_msgs = [m.format(whoop_url=whoop_url) if "{whoop_url}" in m else m for m in _t(user, "whoop_prompt")]
    await _send_multi(chat_id, user.id, whoop_msgs, db)


async def _handle_whoop_or_basics(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Parse manual age / weight / gender basics, then build the plan."""
    if await _sidebar_check(
        user, chat_id, text,
        _t(user, "basics_sidebar_q"),
        db,
    ):
        return

    data = await _llm_extract("basics", text or "")

    if not data or not data.get("valid"):
        reasks = await _reask_count(user.id, OnboardingState.WHOOP_OR_BASICS)
        if reasks >= 1:
            pass
        else:
            await _inc_reask(user.id, OnboardingState.WHOOP_OR_BASICS)
            await _send(chat_id, user.id, _t(user, "basics_reask"), db)
            return
    else:
        if data.get("age") is not None:
            user.age = int(data["age"])
        if data.get("weight_kg") is not None:
            user.weight_kg = float(data["weight_kg"])
        if data.get("gender") is not None:
            user.gender = data["gender"]
        if data.get("height_cm") is not None:
            user.height_cm = float(data["height_cm"])

    await _build_plan_and_advance(user, chat_id, db)


async def _build_plan_and_advance(user: User, chat_id: str, db: AsyncSession, whoop_connected: bool = False) -> None:
    """Build the plan immediately, then send the paywall link.

    Plan GENERATION is unconditional — we want every user who finishes
    onboarding to get their plan written into the DB right away. Plan ACCESS
    (the /plan page) is what we gate behind the Stripe paywall.

    Why: previously we deferred generation until the Stripe webhook fired.
    That introduced a fragile chain (race conditions, webhook ordering,
    Redis locks) and meant if anything went wrong the user got a "had a
    small hiccup" message instead of their plan. Doing it here removes all
    of that — by the time the user makes it through the embedded Stripe
    flow on /unlock, the plan is already sitting on /plan waiting for them.
    """
    from app.services.token import create_onboarding_token
    from app.services.training_plan import generate_plan

    await assign_persona_from_style(user, db)

    user.onboarding_state = OnboardingState.AWAITING_SUBSCRIPTION
    await db.commit()

    analytics.capture(
        "onboarding_step_completed",
        user,
        properties={
            "step": "whoop_or_basics",
            "whoop_connected": whoop_connected,
            "has_age": user.age is not None,
            "has_weight": user.weight_kg is not None,
            "has_gender": user.gender is not None,
        },
    )

    ack = _t(user, "plan_ack_whoop") if whoop_connected else _t(user, "plan_ack")
    tease = _t(user, "plan_building_tease")

    await _send(chat_id, user.id, ack, db)
    await asyncio.sleep(1.2)
    await _send(chat_id, user.id, tease, db)

    # Actually build the plan now. The typing indicator keeps the UI alive
    # while the LLM runs. If generation fails we still send the paywall
    # link — the user can text us back and we'll retry, but >95% of the
    # time this will just work.
    await linq.start_typing(chat_id)
    try:
        try:
            await generate_plan(user, db)
        except Exception as ex:
            import traceback
            print(f"[_build_plan_and_advance] plan gen failed for user {user.id}: {ex}", flush=True)
            traceback.print_exc()
    finally:
        await linq.stop_typing(chat_id)

    token = create_onboarding_token(user.phone)
    base_url = settings.PUBLIC_BASE_URL.rstrip('/')
    paywall_url = f"{base_url}/unlock?token={token}"
    await _send(chat_id, user.id, paywall_url, db)

async def _handle_plan_review(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Accept plan feedback.

    If the user asks for a change → actually regenerate the plan, send the
    fresh link, then continue to the challenge pitch.
    Any other reply (ok / looks good / yes / 🔥 etc.) → go straight to pitch.
    """
    from openai import AsyncOpenAI
    from app.config import settings as cfg

    # Use LLM to classify — keyword lists miss natural phrasing like
    # "yeah I'd rather have 5 days" or "actually can you make it harder"
    try:
        client = AsyncOpenAI(api_key=cfg.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
        clf = await client.chat.completions.create(
            model="deepseek/deepseek-v4-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "The user just reviewed their training plan. "
                        "Reply with exactly one word: CHANGE if they want any modification "
                        "(different days, exercises, frequency, intensity, style, etc.), "
                        "or OK if they are happy with it / confirming it's good."
                    ),
                },
                {"role": "user", "content": text or ""},
            ],
            max_tokens=5,
            temperature=0,
        )
        verdict = clf.choices[0].message.content.strip().upper()
        wants_change = verdict == "CHANGE"
    except Exception:
        # Fallback to keyword check if LLM fails
        _MODIFY_HINTS = [
            "change", "swap", "add", "remove", "replace", "different",
            "more", "less", "instead", "andern", "ändern", "tauschen",
            "hinzufügen", "anders", "ohne", "not enough", "zu wenig",
            "zu viel", "too much", "too little", "don't want", "don't like",
            "hate", "prefer", "instead of", "rather", "would like",
        ]
        t = (text or "").lower()
        wants_change = any(h in t for h in _MODIFY_HINTS)

    if wants_change:
        await _send(chat_id, user.id, _t(user, "plan_regen_ack"), db)
        try:
            from app.services.training_plan import generate_plan
            from app.services.token import create_plan_token
            await generate_plan(user, db, user_request=text)
            analytics.capture("plan_regenerated", user, properties={"trigger": "user_request"})
            token = create_plan_token(user.phone)
            base_url = settings.PUBLIC_BASE_URL.rstrip('/')
            plan_url = f"{base_url}/plan?token={token}"
            await _send_multi(chat_id, user.id, [_t(user, "plan_regen_done"), plan_url], db)
        except Exception as ex:
            print(f"[_handle_plan_review] regen error: {ex}")
            await _send(chat_id, user.id, _t(user, "plan_regen_fail"), db)
        # Stay in PLAN_REVIEW so user can confirm the updated plan
        return

    # User is happy — move to DONE (already subscribed at this point)
    user.onboarding_state = OnboardingState.DONE
    await db.commit()
    analytics.capture("onboarding_step_completed", user, properties={"step": "plan_review"})
    analytics.capture("onboarding_completed", user)
    from app.services.token import create_calendar_token
    base_url = settings.PUBLIC_BASE_URL.rstrip('/')
    # Strip scheme and build webcal:// directly — iOS opens Calendar immediately
    # without a browser hop, no SSL warnings, no redirect
    host = base_url.removeprefix("https://").removeprefix("http://")
    token = create_calendar_token(user.phone)
    cal_url = f"webcal://{host}/calendar/{token}.ics"
    await _send_multi(chat_id, user.id, _t(user, "done") + [cal_url], db)


async def _challenge_pitch(user: User, chat_id: str, db: AsyncSession) -> None:
    """Legacy: send free trial pitch. Now only called if needed as fallback."""
    payment_link = settings.STRIPE_PAYMENT_LINK
    await _send_multi(chat_id, user.id, [
        "first 7 days are on me — free trial, no charge",
        f"start here 👇\n{payment_link}",
    ], db)


async def _handle_awaiting_subscription(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """User is in the subscription gate. Check if they've subscribed.

    - If subscribed → generate plan, send it, advance to DONE.
    - If not → remind them about the free trial.
    """
    from app.services.billing import check_subscription
    from app.services.token import create_onboarding_token

    has_sub = await check_subscription(user.id, db)
    if not has_sub:
        token = create_onboarding_token(user.phone)
        base_url = settings.PUBLIC_BASE_URL.rstrip('/')
        paywall_url = f"{base_url}/unlock?token={token}"
        reminder_msgs = [m.format(paywall_url=paywall_url) if "{paywall_url}" in m else m for m in _t(user, "sub_reminder")]
        await _send_multi(chat_id, user.id, reminder_msgs, db)
        return

    # Subscribed — generate the plan now
    await _deliver_plan_after_subscription(user, chat_id, db)


async def _generate_plan_post_subscription(user: User, db: AsyncSession) -> bool:
    """Generate the plan and advance state.

    Used by the web paywall flow — the user is watching the /processing
    spinner and will be redirected to /plan once this succeeds. We also
    send a short iMessage so they know they can text back for changes.

    Idempotent: guarded by a Redis lock so the same user can't trigger
    parallel plan generation from competing Stripe webhooks (e.g. a
    checkout.session.completed + customer.subscription.updated race).

    Returns True on success, False on failure.
    """
    from app.services.training_plan import generate_plan
    from app.models.training_plan import TrainingPlan
    from app.redis import redis_pool
    from sqlalchemy import select as _select

    lock_key = f"plan_gen_lock:{user.id}"
    # SET NX EX — only the first caller wins; others bail out fast
    acquired = await redis_pool.set(lock_key, "1", nx=True, ex=180)
    if not acquired:
        print(f"[_generate_plan_post_subscription] lock held for user {user.id}, skipping")
        return False

    try:
        # New flow: the plan is already generated during onboarding, before
        # the user even sees the paywall. So 99% of the time this just
        # needs to advance state + send the iMessage nudge. We only call
        # generate_plan as a defensive fallback for users whose plan
        # somehow didn't get written (e.g. mid-deploy, LLM outage).
        existing = await db.execute(
            _select(TrainingPlan)
            .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
            .limit(1)
        )
        has_plan = existing.scalar_one_or_none() is not None
        if not has_plan:
            print(f"[_generate_plan_post_subscription] no plan found for user {user.id}, generating now", flush=True)
            await generate_plan(user, db)
        else:
            print(f"[_generate_plan_post_subscription] plan already exists for user {user.id}, advancing state only", flush=True)

        user.onboarding_state = OnboardingState.PLAN_REVIEW
        user.onboarding_complete = True
        await db.commit()

        # Nudge via iMessage so the user knows feedback is welcome.
        # No plan link here — they're already on /plan in the browser.
        if user.linq_chat_id:
            try:
                await _send(user.linq_chat_id, user.id, "your plan is ready 💪", db)
                await asyncio.sleep(0.8)
                await _send(
                    user.linq_chat_id,
                    user.id,
                    _t(user, "plan_review_prompt"),
                    db,
                )
            except Exception as ex:
                print(f"[_generate_plan_post_subscription] follow-up iMessage failed: {ex}")
        return True
    except Exception as ex:
        print(f"[onboarding _generate_plan_post_subscription] ERROR: {ex}")
        user.onboarding_state = OnboardingState.DONE
        user.onboarding_complete = True
        await db.commit()
        if user.linq_chat_id:
            try:
                await _send(user.linq_chat_id, user.id, _t(user, "plan_fail"), db)
            except Exception:
                pass
        return False


async def _deliver_plan_after_subscription(user: User, chat_id: str, db: AsyncSession) -> None:
    """Generate the plan and SMS the link. Used when the user is not on the
    web paywall flow (e.g. they texted back after the awaiting-sub reminder)."""
    from app.services.token import create_plan_token

    await _send(chat_id, user.id, _t(user, "plan_building"), db)
    ok = await _generate_plan_post_subscription(user, db)
    if ok:
        token = create_plan_token(user.phone)
        base_url = settings.PUBLIC_BASE_URL.rstrip('/')
        plan_url = f"{base_url}/plan?token={token}"
        await _send_multi(chat_id, user.id, [
            plan_url,
            _t(user, "plan_review_prompt"),
        ], db)
    else:
        await _send(chat_id, user.id, _t(user, "plan_fail"), db)


async def _handle_challenge(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Legacy handler — onboarding already complete at pitch. Hand off to coach."""
    pass


async def _handle_sports_focus_backfill(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        await _send(chat_id, user.id, _t(user, "sports_backfill_q"), db)
        return
    user.sports_focus = cleaned[:2000]
    user.onboarding_state = OnboardingState.DONE
    user.onboarding_complete = True
    await db.commit()
    await _send(chat_id, user.id, _t(user, "sports_backfill_done"), db)
