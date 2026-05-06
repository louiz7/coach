"""In-chat onboarding state machine for the Hercules beta phase.

Flow:
    BETA_GATE → CHAT_NAME → CHAT_GOAL → CHAT_SPORTS_FOCUS
              → CHAT_STATUS → CHAT_CHALLENGE → CHAT_STYLE → CHAT_INTENSITY
              → CHAT_WHOOP_PROMPT → DONE

Also handles SPORTS_FOCUS_BACKFILL — a one-time prompt for already-onboarded
users (Elias, Louiz) to capture sports_focus before they continue chatting.
"""
import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import OnboardingState, User
from app.services import linq
from app.services.memory import add_message
from app.services.persona import assign_persona_from_style


# ─── Question definitions ────────────────────────────────────────────────────

# Each multiple-choice question maps a letter → (stored_value, label).
# A free-text fallback is always allowed and stored verbatim.
GOAL_OPTIONS = {
    "a": ("lose_weight",          "Lose weight"),
    "b": ("increase_muscle_mass", "Build muscle"),
    "c": ("health_longevity",     "Health & longevity"),
    "d": ("athlete",              "Perform like an athlete"),
}

STATUS_OPTIONS = {
    "a": ("none",        "Not training right now"),
    "b": ("1_2_week",    "1–2x per week"),
    "c": ("3_4_week",    "3–4x per week"),
    "d": ("5_plus_week", "5+ times per week"),
}

CHALLENGE_OPTIONS = {
    "a": ("motivation",  "Staying motivated"),
    "b": ("consistency", "Being consistent"),
    "c": ("no_time",     "Not enough time"),
    "d": ("alone",       "Doing it alone"),
}

STYLE_OPTIONS = {
    "a": ("high_energy",     "High energy & hype"),
    "b": ("calm",            "Calm & supportive"),
    "c": ("drill_sergeant",  "Drill sergeant — no excuses"),
    "d": ("humor",           "Funny & laid-back"),
}

INTENSITY_OPTIONS = {
    "a": ("easy",     "Easy — ease me in"),
    "b": ("moderate", "Moderate — push me a bit"),
    "c": ("hard",     "Hard — I want results"),
    "d": ("maximum",  "Maximum — no excuses"),
}


# Maps `status` string → integer training_frequency (mirrors web form mapping)
_STATUS_TO_FREQ: dict[str, int] = {
    "none":        0,
    "1_2_week":    1,
    "3_4_week":    3,
    "5_plus_week": 5,
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_letter(text: str) -> Optional[str]:
    """Extract a single A/B/C/D letter from short replies like 'A', 'a)', 'A.'.

    Returns lowercase letter, or None if not a clear single-letter answer.
    """
    t = text.strip().lower()
    # Strip common decoration: dots, parens, spaces
    t = re.sub(r"[\s\.\)\(\-]", "", t)
    if len(t) == 1 and t in {"a", "b", "c", "d"}:
        return t
    return None


def _format_options(options: dict) -> str:
    return "\n".join(f"{k.upper()}) {label}" for k, (_, label) in options.items())


async def _send(chat_id: str, user_id, text: str, db: AsyncSession) -> None:
    await linq.send_message(chat_id, text)
    await add_message(user_id, "assistant", text, db)


# ─── Outgoing prompts ────────────────────────────────────────────────────────

BETA_GATE_PROMPT_TEMPLATE = (
    "Hey! I'm Hercules — your personal AI fitness coach 💪\n\n"
    "We're in beta right now. Drop your beta access code to get in.\n\n"
    "Don't have one? Join the community to get access:\nhttps://chat.whatsapp.com/K7TxauaQUPw9301LKtyNH9"
)

WRONG_CODE_PROMPT = (
    "That code didn't work. No worries — grab one from our community here:\n"
    "{url}\n\n"
    "Then send it back to me 🙏"
)

NAME_PROMPT = (
    "You're in! 🎉\n\n"
    "What's your first name? (just your first name, nothing else)"
)

GOAL_PROMPT = (
    "Nice to meet you, {name}! 🙌\n\n"
    "What's your main fitness goal?\n\n"
    "{options}\n\n"
    "Reply with A, B, C, D — or describe your own goal."
)

SPORTS_FOCUS_PROMPT = (
    "Got it. Now tell me — which sports or activities do you want to improve in?\n\n"
    "List as many as you like (e.g. running, climbing, jiu-jitsu, lifting). "
    "I'll use this to tailor your training."
)

STATUS_PROMPT = (
    "How often are you training right now?\n\n"
    "{options}\n\n"
    "Reply with A, B, C, or D."
)

CHALLENGE_PROMPT = (
    "What's your biggest challenge?\n\n"
    "{options}\n\n"
    "Reply with A, B, C, or D."
)

STYLE_PROMPT = (
    "What kind of coaching style works best for you?\n\n"
    "{options}\n\n"
    "Reply with A, B, C, or D."
)

INTENSITY_PROMPT = (
    "How intense do you want me to be?\n\n"
    "{options}\n\n"
    "Reply with A, B, C, or D."
)

BODY_METRICS_PROMPT = (
    "Quick stats — what's your age, weight, and height?\n\n"
    "Just reply like: 27, 82 kg, 183 cm"
)

INJURIES_PROMPT = (
    "Any injuries or movements you need to avoid?\n\n"
    "Reply 'none' if you're all good."
)

CURRENT_SCHEDULE_PROMPT = (
    "Briefly describe your current training routine. "
    "What does a typical week look like?\n\n"
    "Keep it short — use the dictate function if it's easier 🎙️"
)

EQUIPMENT_OPTIONS = {
    "a": ("gym",     "Full gym"),
    "b": ("home",    "Home gym / dumbbells"),
    "c": ("both",    "Both"),
    "d": ("outdoor", "Outdoor / bodyweight only"),
}

EQUIPMENT_PROMPT = (
    "What equipment do you have access to?\n\n"
    "{options}\n\n"
    "Reply with A, B, C, or D."
)

WHOOP_PROMPT_TEMPLATE = (
    "You're set up, {name}! 🔥\n\n"
    "Optional: connect your WHOOP so I can adapt training to your recovery. "
    "Tap below — or reply 'skip' to do it later.\n"
    "{url}"
)

WELCOME_DONE = (
    "Welcome to the beta, {name}! 🎉\n\n"
    "I've got everything I need. From now on, just text me whenever — log workouts, "
    "ask for plans, vent about a tough session. I'm your coach 💪"
)

PLAN_OFFER_PROMPT = (
    "Want me to build your first training plan right now based on your answers?\n\n"
    "Reply YES to get it now — or NOT YET if you'd rather wait."
)

PLAN_BUILDING_ACK = "On it — building your plan now 💪 give me a few seconds…"

PLAN_AFTER_HINT = (
    "That's your starting plan. Tell me anytime if you want to swap, add, or "
    "remove anything — I'll update it for you."
)

PLAN_DECLINED_ACK = (
    "All good. Just text me whenever you're ready and I'll build one — or you "
    "can ask me anything in the meantime 💪"
)

PLAN_ERROR_ACK = (
    "Hmm, I couldn't build the plan just now. Text me 'build me a plan' in a bit "
    "and I'll try again."
)

SPORTS_FOCUS_BACKFILL_PROMPT = (
    "Quick one before we continue — I want to tailor your coaching better.\n\n"
    "Which sports or activities are you focused on improving in? "
    "List as many as you like (e.g. running, climbing, jiu-jitsu, lifting)."
)

REASK_LETTER = "Quick — just reply with A, B, C, or D 🙂\n\n{options}"


# ─── State handlers ──────────────────────────────────────────────────────────

async def handle(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    """Single entry point — dispatches based on user.onboarding_state."""
    state = user.onboarding_state

    # One-off: existing users completing the sports_focus backfill
    if state == OnboardingState.SPORTS_FOCUS_BACKFILL:
        await _handle_sports_focus_backfill(user, chat_id, text, db)
        return

    if state == OnboardingState.BETA_GATE:
        await _handle_beta_gate(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_NAME:
        await _handle_name(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_GOAL:
        await _handle_goal(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_SPORTS_FOCUS:
        await _handle_sports_focus(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_STATUS:
        await _handle_status(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_CHALLENGE:
        await _handle_challenge(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_STYLE:
        await _handle_style(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_INTENSITY:
        await _handle_intensity(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_BODY_METRICS:
        await _handle_body_metrics(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_INJURIES:
        await _handle_injuries(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_CURRENT_SCHEDULE:
        await _handle_current_schedule(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_EQUIPMENT:
        await _handle_equipment(user, chat_id, text, db)
        return

    if state == OnboardingState.CHAT_WHOOP_PROMPT:
        await _handle_whoop_prompt(user, chat_id, text, db)
        return

    if state == OnboardingState.AWAITING_PLAN_CONFIRM:
        await _handle_awaiting_plan_confirm(user, chat_id, text, db)
        return

    # Legacy CHAT_PITCH / FORM — should be migrated by SQL, but be defensive:
    # treat them like BETA_GATE so the user re-enters the new funnel.
    user.onboarding_state = OnboardingState.BETA_GATE
    await db.commit()
    await _send(chat_id, user.id, BETA_GATE_PROMPT_TEMPLATE, db)


# --- BETA_GATE ---------------------------------------------------------------

async def _handle_beta_gate(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    code = (text or "").strip().lower()
    expected = (settings.BETA_CODE or "").strip().lower()
    if code and expected and code == expected:
        user.beta_unlocked = True
        user.onboarding_state = OnboardingState.CHAT_NAME
        await db.commit()
        await _send(chat_id, user.id, NAME_PROMPT, db)
    else:
        msg = WRONG_CODE_PROMPT.format(url=settings.WHATSAPP_COMMUNITY_URL or "https://hercules.chat")
        await _send(chat_id, user.id, msg, db)


# --- CHAT_NAME ---------------------------------------------------------------

async def _handle_name(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    raw = (text or "").strip()
    if not raw:
        await _send(chat_id, user.id, NAME_PROMPT, db)
        return
    # Take first whitespace-separated token, capitalize
    first = raw.split()[0].strip()
    # Strip trailing punctuation
    first = re.sub(r"[^\w\-]+$", "", first)
    if not first:
        await _send(chat_id, user.id, NAME_PROMPT, db)
        return
    user.name = first.capitalize()
    user.onboarding_state = OnboardingState.CHAT_GOAL
    await db.commit()
    msg = GOAL_PROMPT.format(name=user.name, options=_format_options(GOAL_OPTIONS))
    await _send(chat_id, user.id, msg, db)


# --- CHAT_GOAL ---------------------------------------------------------------

async def _handle_goal(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    letter = _parse_letter(text)
    if letter and letter in GOAL_OPTIONS:
        user.goal = GOAL_OPTIONS[letter][0]
    else:
        # Free-text fallback — store verbatim (column is now TEXT)
        cleaned = (text or "").strip()
        if not cleaned:
            msg = REASK_LETTER.format(options=_format_options(GOAL_OPTIONS))
            await _send(chat_id, user.id, msg, db)
            return
        user.goal = cleaned[:2000]  # sanity cap
    user.onboarding_state = OnboardingState.CHAT_SPORTS_FOCUS
    await db.commit()
    await _send(chat_id, user.id, SPORTS_FOCUS_PROMPT, db)


# --- CHAT_SPORTS_FOCUS -------------------------------------------------------

async def _handle_sports_focus(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        await _send(chat_id, user.id, SPORTS_FOCUS_PROMPT, db)
        return
    user.sports_focus = cleaned[:2000]
    user.onboarding_state = OnboardingState.CHAT_STATUS
    await db.commit()
    msg = STATUS_PROMPT.format(options=_format_options(STATUS_OPTIONS))
    await _send(chat_id, user.id, msg, db)


# --- CHAT_STATUS -------------------------------------------------------------

async def _handle_status(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    letter = _parse_letter(text)
    if not letter or letter not in STATUS_OPTIONS:
        msg = REASK_LETTER.format(options=_format_options(STATUS_OPTIONS))
        await _send(chat_id, user.id, msg, db)
        return
    value = STATUS_OPTIONS[letter][0]
    user.training_frequency = _STATUS_TO_FREQ.get(value, 0)
    user.onboarding_state = OnboardingState.CHAT_CHALLENGE
    await db.commit()
    msg = CHALLENGE_PROMPT.format(options=_format_options(CHALLENGE_OPTIONS))
    await _send(chat_id, user.id, msg, db)


# --- CHAT_CHALLENGE ----------------------------------------------------------

async def _handle_challenge(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    letter = _parse_letter(text)
    if not letter or letter not in CHALLENGE_OPTIONS:
        msg = REASK_LETTER.format(options=_format_options(CHALLENGE_OPTIONS))
        await _send(chat_id, user.id, msg, db)
        return
    user.challenge = CHALLENGE_OPTIONS[letter][0]
    user.onboarding_state = OnboardingState.CHAT_STYLE
    await db.commit()
    msg = STYLE_PROMPT.format(options=_format_options(STYLE_OPTIONS))
    await _send(chat_id, user.id, msg, db)


# --- CHAT_STYLE --------------------------------------------------------------

async def _handle_style(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    letter = _parse_letter(text)
    if not letter or letter not in STYLE_OPTIONS:
        msg = REASK_LETTER.format(options=_format_options(STYLE_OPTIONS))
        await _send(chat_id, user.id, msg, db)
        return
    user.coach_style = STYLE_OPTIONS[letter][0]
    user.onboarding_state = OnboardingState.CHAT_INTENSITY
    await db.commit()
    msg = INTENSITY_PROMPT.format(options=_format_options(INTENSITY_OPTIONS))
    await _send(chat_id, user.id, msg, db)


# --- CHAT_INTENSITY ----------------------------------------------------------

async def _handle_intensity(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    letter = _parse_letter(text)
    if not letter or letter not in INTENSITY_OPTIONS:
        msg = REASK_LETTER.format(options=_format_options(INTENSITY_OPTIONS))
        await _send(chat_id, user.id, msg, db)
        return
    user.coach_intensity = INTENSITY_OPTIONS[letter][0]

    # Resolve persona NOW based on style/intensity
    await assign_persona_from_style(user, db, user.coach_style, user.coach_intensity)

    user.onboarding_state = OnboardingState.CHAT_BODY_METRICS
    await db.commit()
    await _send(chat_id, user.id, BODY_METRICS_PROMPT, db)


# --- CHAT_BODY_METRICS -------------------------------------------------------

_METRICS_RE = re.compile(
    r"(\d{1,3})[,\s]+"
    r"(\d{2,3}(?:\.\d)?)[\s]*(?:kg|lbs?|pounds?|kilos?)?[,\s]+"
    r"(\d{2,3}(?:\.\d)?)[\s]*(?:cm|m|ft|feet|inches?)?",
    re.IGNORECASE,
)


async def _handle_body_metrics(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    m = _METRICS_RE.search(text or "")
    if m:
        try:
            user.age = int(m.group(1))
            user.weight_kg = float(m.group(2))
            user.height_cm = float(m.group(3))
        except (ValueError, AttributeError):
            pass  # store whatever parsed, skip the rest
    # Always advance — don't block users who can't format precisely
    user.onboarding_state = OnboardingState.CHAT_INJURIES
    await db.commit()
    await _send(chat_id, user.id, INJURIES_PROMPT, db)


# --- CHAT_INJURIES -----------------------------------------------------------

async def _handle_injuries(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    raw = (text or "").strip()
    if raw.lower() in {"none", "no", "nope", "nothing", "none!", "all good", "nein"}:
        user.injuries = None
    else:
        user.injuries = raw[:2000]
    user.onboarding_state = OnboardingState.CHAT_CURRENT_SCHEDULE
    await db.commit()
    await _send(chat_id, user.id, CURRENT_SCHEDULE_PROMPT, db)


# --- CHAT_CURRENT_SCHEDULE ---------------------------------------------------

async def _handle_current_schedule(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    raw = (text or "").strip()
    user.current_schedule_notes = raw[:3000] if raw else None
    user.onboarding_state = OnboardingState.CHAT_EQUIPMENT
    await db.commit()
    msg = EQUIPMENT_PROMPT.format(options=_format_options(EQUIPMENT_OPTIONS))
    await _send(chat_id, user.id, msg, db)


# --- CHAT_EQUIPMENT ----------------------------------------------------------

async def _handle_equipment(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    letter = _parse_letter(text)
    if not letter or letter not in EQUIPMENT_OPTIONS:
        msg = REASK_LETTER.format(options=_format_options(EQUIPMENT_OPTIONS))
        await _send(chat_id, user.id, msg, db)
        return
    user.equipment_access = EQUIPMENT_OPTIONS[letter][0]
    user.onboarding_state = OnboardingState.CHAT_WHOOP_PROMPT
    await db.commit()
    # Build WHOOP connect link
    from app.services.token import create_onboarding_token
    token = create_onboarding_token(user.phone)
    base_url = settings.ALLOWED_ORIGINS.split(",")[0].strip()
    whoop_url = f"{base_url}/whoop/connect?token={token}"
    msg = WHOOP_PROMPT_TEMPLATE.format(name=user.name, url=whoop_url)
    await _send(chat_id, user.id, msg, db)


# --- CHAT_WHOOP_PROMPT -------------------------------------------------------

_SKIP_WORDS = {"skip", "later", "no", "nein", "pass", "nah", "skip it", "not now", "spaeter", "später"}


async def _handle_whoop_prompt(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    t = (text or "").strip().lower()
    is_skip = any(t == w or t.startswith(w + " ") or t.startswith(w + "!") for w in _SKIP_WORDS)

    # Move user out of the WHOOP step. The WHOOP callback (if they tap the link)
    # will set whoop_access_token in the background regardless.
    user.onboarding_state = OnboardingState.AWAITING_PLAN_CONFIRM
    user.onboarding_complete = True
    await db.commit()

    # Share contact card now that onboarding is complete — ensures Hercules
    # appears as a named contact in iMessage for all users
    try:
        from app.services import linq
        await linq.share_contact_card(chat_id)
        print(f"[onboarding] contact card shared on completion for user={user.id}")
    except Exception as _cc_err:
        print(f"[onboarding] contact card share failed (non-fatal): {_cc_err}")

    welcome = WELCOME_DONE.format(name=user.name)
    await _send(chat_id, user.id, welcome, db)
    await _send(chat_id, user.id, PLAN_OFFER_PROMPT, db)

    _ = is_skip  # noqa: F841 (kept for future analytics)


# --- AWAITING_PLAN_CONFIRM ---------------------------------------------------

_YES_WORDS = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "k", "do it",
    "let's go", "lets go", "go", "build it", "yes please", "absolutely",
    "ja", "klar",
}
_NO_WORDS = {
    "no", "n", "nope", "not yet", "later", "skip", "pass", "nah", "not now",
    "nein", "spaeter", "später",
}


def _matches_words(text: str, words: set[str]) -> bool:
    t = (text or "").strip().lower().rstrip("!.?")
    if t in words:
        return True
    return any(t.startswith(w + " ") for w in words)


async def _handle_awaiting_plan_confirm(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    # Only decline if they clearly say no. Any other reply (yes, nice, sure,
    # "create my plan", "okay", silence, emoji, etc.) → build the plan.
    if _matches_words(text, _NO_WORDS):
        user.onboarding_state = OnboardingState.DONE
        await db.commit()
        await _send(chat_id, user.id, PLAN_DECLINED_ACK, db)
        return

    # Everything else → build the plan
    await _send(chat_id, user.id, PLAN_BUILDING_ACK, db)
    try:
        from app.services.training_plan import generate_plan
        from app.services.token import create_plan_token
        from app.config import settings
        plan = await generate_plan(user, db)
        token = create_plan_token(user.phone)
        base = settings.ALLOWED_ORIGINS.split(",")[0].strip()
        url = f"{base}/plan?token={token}"
        await _send(chat_id, user.id, f"Your plan is ready 💪\n{url}", db)
    except Exception as ex:
        print(f"[awaiting_plan_confirm generate ERROR] {ex}")
        await _send(chat_id, user.id, PLAN_ERROR_ACK, db)
    user.onboarding_state = OnboardingState.DONE
    await db.commit()


# --- SPORTS_FOCUS_BACKFILL (one-shot for existing users) ---------------------

async def _handle_sports_focus_backfill(user: User, chat_id: str, text: str, db: AsyncSession) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        await _send(chat_id, user.id, SPORTS_FOCUS_BACKFILL_PROMPT, db)
        return
    user.sports_focus = cleaned[:2000]
    # Move existing onboarded users into the plan-offer flow as well
    user.onboarding_state = OnboardingState.AWAITING_PLAN_CONFIRM
    await db.commit()
    ack = "Got it — thanks! 💪"
    await _send(chat_id, user.id, ack, db)
    await _send(chat_id, user.id, PLAN_OFFER_PROMPT, db)
