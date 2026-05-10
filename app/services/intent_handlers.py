"""
Intent handlers — each handler runs for one detected intent and may return
a context string that gets injected into the system prompt before the final
GPT reply is generated.
"""
import asyncio
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services import linq
from app.services.memory import add_message
from app.services.progress import parse_and_store_progress
from app.services.training_plan import generate_plan, chunk_plan_text
from app.redis import redis_pool


# When user says they feel bad/tired and want today lighter — handle as today view + coach tip, NOT a plan modification
_TODAY_ONLY_PATTERNS = (
    "heute", "today", "this session", "diese einheit", "jetzt", "right now",
    "for today", "für heute", "today only", "nur heute",
)

# Day name overrides — if user specifies a day explicitly
_DAY_NAMES = {
    "monday": "Monday", "montag": "Monday",
    "tuesday": "Tuesday", "dienstag": "Tuesday",
    "wednesday": "Wednesday", "mittwoch": "Wednesday",
    "thursday": "Thursday", "donnerstag": "Thursday",
    "friday": "Friday", "freitag": "Friday",
    "saturday": "Saturday", "samstag": "Saturday",
    "sunday": "Sunday", "sonntag": "Sunday",
}


def _extract_day_from_text(text: str):
    """Return explicit day name mentioned in text, or None."""
    t = text.lower()
    for key, val in _DAY_NAMES.items():
        if key in t:
            return val
    return None


# Modification verbs/phrases — permanent plan changes (only when no today-only qualifier)
_MODIFICATION_KEYWORDS = (
    # direct action words
    "swap", "replace", "change", "remove", "add", "drop", "skip",
    "make it", "instead of", "more", "less", "fewer", "shorter", "longer",
    "harder", "easier", "tweak", "adjust", "modify", "update",
    # complaint / absence phrasing — user notices something missing
    "don't see", "dont see", "not in my plan", "missing", "no running",
    "no cardio", "not there", "still not", "i want", "include",
    "why is there no", "where is", "can you add", "can you include",
    "put in", "add in", "need more", "want more", "want less",
    # german
    "tausch", "änder", "ersetze", "füge", "hinzufüg", "entfern",
    "ich sehe kein", "fehlt", "nicht im plan", "ich will", "mehr", "weniger",
)

# When ANY of these appear the user explicitly wants the whole weekly plan.
_FULL_PLAN_KEYWORDS = (
    "full plan", "whole plan", "entire plan", "all days", "weekly plan",
    "whole week", "all workouts", "show me the plan", "send the plan",
    "send me the plan", "complete plan", "full week", "den ganzen plan",
    "gesamten plan", "ganzen plan", "alle tage", "ganze woche",
)



# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------

async def handle_progress_log(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Parse and store workout / progress log. Updates fitness profile + stores vector memory."""
    try:
        from app.services.fitness_profile import (
            update_profile_from_workout,
        )
        from app.services.memory_search import store_memory
        from datetime import date

        entries = await parse_and_store_progress(user.id, text, db)
        if not entries:
            return None

        for e in entries:
            # Rule-based profile update (zero tokens)
            try:
                await update_profile_from_workout(
                    user.id, db,
                    label=e.label,
                    value=e.value,
                    unit=e.unit or "",
                    category=e.category or "exercise",
                )
            except Exception as ex:
                print(f"[handle_progress_log profile ERROR] {ex}")

            # Vector memory (semantic recall later)
            try:
                detail = f"{e.label} {e.value}{e.unit or ''}"
                if e.sets and e.reps:
                    detail += f" ({e.sets}x{e.reps})"
                detail += f" on {date.today().isoformat()}"
                await store_memory(user.id, detail, "workout", db)
            except Exception as ex:
                print(f"[handle_progress_log memory ERROR] {ex}")

        summary = ", ".join(
            f"{e.label} {e.value}{e.unit or ''}" for e in entries[:3]
        )
        return f"User just logged: {summary}. Acknowledge specifically and give brief feedback."
    except Exception as ex:
        print(f"[handle_progress_log ERROR] {ex}")
        return None


def _plan_url(user) -> str:
    """Generate a signed plan URL for the user."""
    from app.services.token import create_plan_token
    from app.config import settings
    token = create_plan_token(user.phone)
    base = settings.PUBLIC_BASE_URL.rstrip('/')
    return f"{base}/plan?token={token}"


async def handle_modify_plan(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """User wants to permanently change their plan — regenerate with their request, return context."""
    try:
        from app.services.training_plan import generate_plan
        await generate_plan(user, db, user_request=text)
        url = _plan_url(user)
        return (
            f"PLAN UPDATED: You just regenerated the user's training plan based on their request: '{text}'. "
            f"Plan URL: {url}. "
            "In your reply: confirm in 1-2 short sentences what you changed (reference their specific request), "
            "then include the plan URL so they can view it. "
            "Do NOT say you can't modify plans through text — you already did it."
        )
    except Exception as ex:
        print(f"[handle_modify_plan ERROR] {ex}")
        return "Plan update failed. Tell the user briefly and ask them to try again."


async def handle_view_plan(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """User wants to see their existing plan — return the URL as context."""
    try:
        from app.services.training_plan import _get_active_plan, generate_plan
        active_plan = await _get_active_plan(user, db)
        if active_plan is None:
            # No plan yet — generate one
            await generate_plan(user, db)
            url = _plan_url(user)
            return (
                f"NEW PLAN CREATED: You just built a training plan for the user. "
                f"Plan URL: {url}. "
                "Tell them their plan is ready and include the URL."
            )
        url = _plan_url(user)
        return (
            f"PLAN URL: {url}. "
            "Include this URL in your reply so the user can view their training plan. "
            "Answer any specific question they asked about the plan first, then offer the link."
        )
    except Exception as ex:
        print(f"[handle_view_plan ERROR] {ex}")
        return "Failed to retrieve training plan. Apologize briefly."


async def handle_new_plan(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """User wants a brand new plan built from scratch."""
    try:
        from app.services.training_plan import generate_plan
        await generate_plan(user, db, user_request=text)
        url = _plan_url(user)
        return (
            f"NEW PLAN CREATED from scratch based on the user's request. "
            f"Plan URL: {url}. "
            "Tell them their new plan is ready and include the URL."
        )
    except Exception as ex:
        print(f"[handle_new_plan ERROR] {ex}")
        return "Plan creation failed. Apologize briefly and ask them to try again."


async def handle_plan_request(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Legacy fallback — routes to modify or view based on keyword detection."""
    text_lower = (text or "").lower()
    is_today_only = any(p in text_lower for p in _TODAY_ONLY_PATTERNS)
    is_modification = (
        any(kw in text_lower for kw in _MODIFICATION_KEYWORDS)
        and not is_today_only
    )
    if is_modification:
        return await handle_modify_plan(user, text, db)
    return await handle_view_plan(user, text, db)


async def handle_whoop_data(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Fetch live WHOOP recovery/sleep data, refresh cache, return as context."""
    if not user.whoop_access_token:
        return "User does not have WHOOP connected. Let them know they can connect it by typing 'connect WHOOP'."
    try:
        from app.api.whoop import _ensure_fresh_token
        from app.services import whoop as whoop_svc

        access_token = await _ensure_fresh_token(user, db)
        if not access_token:
            return "WHOOP token could not be refreshed. Tell the user their WHOOP connection needs to be re-authorized."

        lines = []

        # Fetch TODAY's recovery (cycle-based; falls back to latest SCORED)
        try:
            rec = await whoop_svc.get_today_recovery(access_token)
            if rec is None:
                rec = await whoop_svc.get_latest_recovery(access_token)
            if rec:
                score_data = rec.get("score") or {}
                rs = score_data.get("recovery_score")
                hrv = score_data.get("hrv_rmssd_milli")
                rhr = score_data.get("resting_heart_rate")
                if rs is not None:
                    user.last_recovery_score = int(rs)
                    emoji = "🟢" if rs >= 67 else ("🟡" if rs >= 34 else "🔴")
                    lines.append(f"Recovery: {emoji} {int(rs)}%")
                if hrv is not None:
                    user.last_hrv = float(hrv)
                    lines.append(f"HRV: {float(hrv):.0f}ms")
                if rhr is not None:
                    lines.append(f"Resting HR: {int(rhr)}bpm")
        except Exception as ex:
            print(f"[handle_whoop_data recovery ERROR] {ex}")

        # Fetch latest sleep
        try:
            from app.services.whoop import get_latest_sleep
            sleep = await get_latest_sleep(access_token)
            if sleep:
                score_data = sleep.get("score") or {}
                perf = score_data.get("sleep_performance_percentage")
                total_ms = score_data.get("total_in_bed_time_milli", 0) or 0
                hours = (total_ms // 1000) // 3600
                mins = ((total_ms // 1000) % 3600) // 60
                if perf is not None:
                    user.last_sleep_performance = int(perf)
                    lines.append(f"Sleep performance: {int(perf)}% ({hours}h{mins}m)")
        except Exception as ex:
            print(f"[handle_whoop_data sleep ERROR] {ex}")

        await db.commit()

        if lines:
            data_str = ", ".join(lines)
            return (
                f"LIVE WHOOP DATA just fetched: {data_str}. "
                "Use this to give the user a personalised coaching insight about their readiness/recovery. "
                "Be specific with the numbers."
            )
        return "WHOOP connected but no recent data available yet — tell the user to sync their WHOOP device."
    except Exception as ex:
        print(f"[handle_whoop_data ERROR] {ex}")
        return None


async def handle_streak_check(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Look up user's recent activity streak and return as context."""
    try:
        from sqlalchemy import select, func
        from app.models.progress_entry import ProgressEntry
        from datetime import date, timedelta

        today = date.today()
        # Count distinct days with entries in the last 30 days
        result = await db.execute(
            select(func.count(func.distinct(func.date(ProgressEntry.recorded_at))))
            .where(
                ProgressEntry.user_id == user.id,
                ProgressEntry.recorded_at >= today - timedelta(days=30),
            )
        )
        active_days = result.scalar() or 0

        # Simple consecutive-days streak
        streak = 0
        check = today
        for _ in range(30):
            key = f"streak:{user.id}:{check.isoformat()}"
            exists = await redis_pool.get(key)
            if exists:
                streak += 1
                check -= timedelta(days=1)
            else:
                # Also check the DB for that day
                r = await db.execute(
                    select(ProgressEntry)
                    .where(
                        ProgressEntry.user_id == user.id,
                        func.date(ProgressEntry.recorded_at) == check,
                    )
                    .limit(1)
                )
                if r.scalar_one_or_none():
                    streak += 1
                    check -= timedelta(days=1)
                else:
                    break

        return (
            f"User's current training streak: {streak} consecutive day(s). "
            f"Active training days in the last 30 days: {active_days}. "
            "Celebrate their consistency or gently encourage them if streak is 0."
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLER_MAP = {
    "PROGRESS_LOG": handle_progress_log,
    "MODIFY_PLAN": handle_modify_plan,
    "VIEW_PLAN": handle_view_plan,
    "NEW_PLAN": handle_new_plan,
    "PLAN_REQUEST": handle_plan_request,  # legacy fallback
    "STREAK_CHECK": handle_streak_check,
    "WHOOP_DATA": handle_whoop_data,
}


async def run_handlers(
    intents: list[str], user: User, text: str, db: AsyncSession
) -> str:
    """
    Run all matched handlers in parallel and return a combined context string
    to inject into the system prompt. The LLM always gets to respond — handlers
    only perform actions and return context, never bypass the LLM.
    """
    tasks = [
        _HANDLER_MAP[intent](user, text, db)
        for intent in intents
        if intent in _HANDLER_MAP
    ]
    if not tasks:
        return ""

    results = await asyncio.gather(*tasks, return_exceptions=True)
    lines = [
        r for r in results
        if isinstance(r, str) and r and r != "__SENT__"
    ]
    return "\n".join(lines)
