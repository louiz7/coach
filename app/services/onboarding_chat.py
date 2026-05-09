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
from app.services import linq
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


# ─── LLM extraction helper ────────────────────────────────────────────────────

_EXTRACT_PROMPTS: dict[str, str] = {
    "name": (
        'Extract the person\'s first name from this message: "{text}"\n'
        'Return JSON: {{"name": "FirstName or null", "valid": true or false}}\n'
        "valid=false if no real name is present (e.g. it's a question, random words, or a greeting with no name)."
    ),
    "goal": (
        'Extract fitness goals from this message: "{text}"\n'
        'Return JSON: {{"goal": "concise goal description", "sports_focus": "sports/activities mentioned or null", "valid": true or false}}\n'
        "valid=false only if zero fitness intent was expressed."
    ),
    "status": (
        'Extract training info from: "{text}"\n'
        'Return JSON: {{"training_frequency": <int days per week, 0 if none>, "schedule_summary": "brief description", "valid": true or false}}\n'
        "valid=false only if completely off-topic (e.g. random noise)."
    ),
    "constraints": (
        'Extract training constraints from: "{text}"\n'
        'Return JSON: {{"injuries": "description or null", "equipment": "gym/home/both/outdoor or null", "notes": "other constraints", "valid": true}}\n'
        "Always valid=true. injuries=null if none mentioned."
    ),
    "basics": (
        'Extract body basics from: "{text}"\n'
        'Return JSON: {{"age": <int or null>, "weight_kg": <float or null>, "gender": "male/female/other or null", "valid": true or false}}\n'
        "valid=false only if none of age/weight/gender could be extracted."
    ),
    "constraints_intent": (
        'Does this message indicate the person has NO constraints, injuries, or special requirements? Message: "{text}"\n'
        'Return JSON: {{"no_constraints": true or false}}\n'
        "no_constraints=true if they said something like none, nothing, all good, nope, no injuries, etc. false if they mentioned anything specific."
    ),
    "challenge": (
        'The user was asked if they want to do a 7-day fitness challenge. Did they say yes or no? Message: "{text}"\n'
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
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
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
You just asked the user: "{pending_question}"
They replied: "{user_message}"

Decide: is their reply a separate question or comment about how Kano works,
or is it an attempt to answer your question?

Return JSON:
{{
  "is_sidebar": true or false,
  "answer": "if is_sidebar=true: your short direct answer to their question (1-3 sentences max, casual iMessage tone). null otherwise."
}}

is_sidebar=true examples: questions about privacy/data storage, how the app works, pricing, what WHOOP is, etc.
is_sidebar=false examples: actually answering the question you asked, even briefly.
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
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        prompt = _SIDEBAR_PROMPT.format(
            pending_question=pending_question,
            user_message=text or "",
        )
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
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
        OnboardingState.INFORM:          _handle_inform,
        OnboardingState.CAPTURE_GOAL:    _handle_capture_goal,
        OnboardingState.STATUS_QUO:      _handle_status_quo,
        OnboardingState.CONSTRAINTS:     _handle_constraints,
        OnboardingState.WHOOP_OR_BASICS: _handle_whoop_or_basics,
        OnboardingState.PLAN_REVIEW:     _handle_plan_review,
        OnboardingState.CHALLENGE:       _handle_challenge,
    }
    handler = dispatch.get(state)
    if handler:
        await handler(user, chat_id, text, db)
    else:
        # Unknown state — restart
        await _restart_inform(user, chat_id, db)


# ─── Initial intro (called by message_worker for brand-new users) ─────────────

async def _send_inform_intro(chat_id: str, user_id, db: AsyncSession) -> None:
    """Send the Kano intro and ask for the user's name."""
    await _send_multi(chat_id, user_id, [
        "I'm your AI personal trainer, right here in iMessage",
        "before we get into it: what's your name?",
    ], db, delay=1.0)


# ─── State handlers ───────────────────────────────────────────────────────────

async def _restart_inform(user: User, chat_id: str, db: AsyncSession) -> None:
    """Move a legacy-state user to the new INFORM flow."""
    user.onboarding_state = OnboardingState.INFORM
    user.name = "Unbekannt"
    await db.commit()
    await _send_inform_intro(chat_id, user.id, db)


async def _handle_inform(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Capture the user's first name, then pitch and ask for their goal.

    If the message doesn't contain a name (e.g. "whats kano?", "hi", etc.)
    and the user hasn't shared their name yet, send the full intro instead of
    a cold reask. The intro itself asks for the name.
    """
    raw = (text or "").strip()
    name_already_set = (user.name or "").lower() not in {"", "unbekannt"}

    if not raw:
        if not name_already_set:
            await _send_inform_intro(chat_id, user.id, db)
        else:
            await _send(chat_id, user.id, "what's your name?", db)
        return

    data = await _llm_extract("name", raw)
    extracted = (data or {}).get("name") if (data or {}).get("valid") else None

    if not extracted:
        # No name found — if user has never identified themselves, this is
        # effectively a first-touch message ("whats kano?", "hi", etc.).
        # Send the intro so they get a proper greeting.
        if not name_already_set:
            await _send_inform_intro(chat_id, user.id, db)
        else:
            await _send(chat_id, user.id, "just your first name is fine 🙂", db)
        return

    user.name = extracted.strip().capitalize()
    user.onboarding_state = OnboardingState.CAPTURE_GOAL
    await db.commit()

    await _send_multi(chat_id, user.id, [
        f"nice to meet you {user.name}!",
        "I can build your workout plan, check in daily to keep you on track, and hook up your wearables for data-driven coaching.",
        "so you actually hit your goals",
        "Speaking of goals, what are your fitness goals? running a 5k? building muscle? losing weight? throw everything at me",
    ], db)


async def _handle_capture_goal(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Extract fitness goal and sports focus from free text."""
    if await _sidebar_check(
        user, chat_id, text,
        "what are your fitness goals? build muscle? lose weight? run a 5k? throw everything at me 🎯",
        db,
    ):
        return

    data = await _llm_extract("goal", text or "")

    if not data or not data.get("valid"):
        reasks = await _reask_count(user.id, OnboardingState.CAPTURE_GOAL)
        if reasks >= 1:
            # Accept whatever was said and move on
            user.goal = (text or "").strip()[:2000] or "general fitness"
        else:
            await _inc_reask(user.id, OnboardingState.CAPTURE_GOAL)
            await _send(
                chat_id, user.id,
                "could be anything — build muscle, run a 5k, lose weight, get stronger. what do you want to work towards? 🎯",
                db,
            )
            return
    else:
        user.goal = (data.get("goal") or (text or "").strip())[:2000]
        if data.get("sports_focus"):
            user.sports_focus = data["sports_focus"][:2000]

    user.onboarding_state = OnboardingState.STATUS_QUO
    await db.commit()

    await _send_multi(chat_id, user.id, [
        "got it. how does your current training look like?",
        "how many days a week, what do you do and when",
        "give me the honest version"
    ], db)


async def _handle_status_quo(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Extract current training frequency and schedule from free text."""
    if await _sidebar_check(
        user, chat_id, text,
        "how does your current training look? how many days a week, what do you do — give me the honest version",
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
            await _send(
                chat_id, user.id,
                "just a quick picture of your week like 'I train 3x, mostly lifting, sometimes I run'",
                db,
            )
            return
    else:
        user.training_frequency = int(data.get("training_frequency") or 0)
        user.current_schedule_notes = (
            data.get("schedule_summary") or (text or "").strip()
        )[:3000]

    user.onboarding_state = OnboardingState.CONSTRAINTS
    await db.commit()

    await _send_multi(chat_id, user.id, [
        "what should I keep in mind when coaching you and creating your plan?",
        "injuries, busy weeks/days, exercises you hate, multiple disciplines (e.g. running + gym) – anything goes",
    ], db)


async def _handle_constraints(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Extract injuries, equipment and other constraints from free text."""
    if await _sidebar_check(
        user, chat_id, text,
        "what should I keep in mind when coaching you? injuries, busy days, exercises you hate, anything goes",
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

    # Build WHOOP link
    from app.services.token import create_onboarding_token
    token = create_onboarding_token(user.phone)
    base_url = settings.ALLOWED_ORIGINS.split(",")[0].strip()
    whoop_url = f"{base_url}/whoop/connect?token={token}"

    await _send_multi(chat_id, user.id, [
        "got it. last thing before I build your plan",
        f"if you connect me with your WHOOP you can skip most of this + get data-driven coaching (more wearables coming soon)\n{whoop_url}",
        "no WHOOP? just reply with your age, weight and gender",
    ], db)


async def _handle_whoop_or_basics(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Parse manual age / weight / gender basics, then build the plan."""
    if await _sidebar_check(
        user, chat_id, text,
        "just send your age, weight and gender — like: 24, 78 kg, male",
        db,
    ):
        return

    data = await _llm_extract("basics", text or "")

    if not data or not data.get("valid"):
        reasks = await _reask_count(user.id, OnboardingState.WHOOP_OR_BASICS)
        if reasks >= 1:
            # Accept partial / empty and move on
            pass
        else:
            await _inc_reask(user.id, OnboardingState.WHOOP_OR_BASICS)
            await _send(
                chat_id, user.id,
                "just send your age, weight and gender — like: 24, 78 kg, male",
                db,
            )
            return
    else:
        if data.get("age") is not None:
            user.age = int(data["age"])
        if data.get("weight_kg") is not None:
            user.weight_kg = float(data["weight_kg"])
        if data.get("gender") is not None:
            user.gender = data["gender"]

    await _build_plan_and_advance(user, chat_id, db)


async def _build_plan_and_advance(user: User, chat_id: str, db: AsyncSession) -> None:
    """Build the training plan, send the link, and move to PLAN_REVIEW.

    Called from both the manual-basics handler and the WHOOP OAuth callback.
    """
    from app.services.token import create_plan_token

    # Assign persona if not yet set
    await assign_persona_from_style(user, db)

    await _send_multi(chat_id, user.id, [
        "ty, got your WHOOP data",
        "building your plan now…",
    ], db)

    try:
        from app.services.training_plan import generate_plan
        await generate_plan(user, db)
        token = create_plan_token(user.phone)
        base_url = settings.ALLOWED_ORIGINS.split(",")[0].strip()
        plan_url = f"{base_url}/plan?token={token}"

        user.onboarding_state = OnboardingState.PLAN_REVIEW
        await db.commit()

        await _send_multi(chat_id, user.id, [
            f"your plan is ready 💪\n{plan_url}",
            "want to change anything or does this work for you?",
        ], db)

    except Exception as ex:
        print(f"[onboarding _build_plan_and_advance] plan gen ERROR: {ex}")
        user.onboarding_state = OnboardingState.PLAN_REVIEW
        await db.commit()
        await _send(
            chat_id, user.id,
            "I had trouble building your plan right now — text me 'build me a plan' in a moment and I'll get it done",
            db,
        )
        await _challenge_pitch(user, chat_id, db)
        return

    # NOTE: contact card was already shared on the very first inbound message
    # in message_worker.py (so the user could save us before answering the
    # name prompt). No need to share again here.


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
        client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
        clf = await client.chat.completions.create(
            model="gpt-4o-mini",
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
        await _send(chat_id, user.id, "on it — rebuilding your plan now ⚙️", db)
        try:
            from app.services.training_plan import generate_plan
            from app.services.token import create_plan_token
            await generate_plan(user, db, user_request=text)
            token = create_plan_token(user.phone)
            base_url = settings.ALLOWED_ORIGINS.split(",")[0].strip()
            plan_url = f"{base_url}/plan?token={token}"
            await _send(chat_id, user.id, f"updated 💪\n{plan_url}", db)
        except Exception as ex:
            print(f"[_handle_plan_review] regen error: {ex}")
            await _send(
                chat_id, user.id,
                "hmm, had trouble rebuilding — you can text me changes anytime and I'll fix it 🔄",
                db,
            )

    await _challenge_pitch(user, chat_id, db)


async def _challenge_pitch(user: User, chat_id: str, db: AsyncSession) -> None:
    """Send the 7-day challenge pitch and advance state to CHALLENGE."""
    user.onboarding_state = OnboardingState.CHALLENGE
    await db.commit()
    await _send_multi(chat_id, user.id, [
        "perfect, your plan is locked in 🫡",
        "oone more thing... and this is important",
        "I want you to do a 7-day challenge with me. complete it and you earn your spot as a Kano member. think of it as proving to yourself you actually want this",
        "you in?",
    ], db)


async def _handle_challenge(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Handle the 7-day challenge response and complete onboarding."""
    result = await _llm_extract("challenge", text or "")
    # Default to accepted=True if LLM fails — better to onboard than to reject
    accepted = (result or {}).get("accepted", True)

    user.onboarding_state = OnboardingState.DONE
    user.onboarding_complete = True
    await db.commit()

    if not accepted:
        await _send(
            chat_id, user.id,
            "no worries — still here whenever you're ready 💪",
            db,
        )
    else:
        await _send_multi(chat_id, user.id, [
            "let's get it. our first check-in is tomorrow morning",
            "I'll message you then. don't ghost me",
        ], db)


async def _handle_sports_focus_backfill(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        await _send(
            chat_id, user.id,
            "which sports or activities are you focused on improving? (e.g. running, climbing, lifting)",
            db,
        )
        return
    user.sports_focus = cleaned[:2000]
    user.onboarding_state = OnboardingState.DONE
    user.onboarding_complete = True
    await db.commit()
    await _send(chat_id, user.id, "got it 💪 just text me whenever you need anything.", db)
