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
from app.redis import redis_pool





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
    """User wants to see their existing plan — send the URL directly."""
    try:
        from app.services.training_plan import _get_active_plan, generate_plan
        active_plan = await _get_active_plan(user, db)
        if active_plan is None:
            await generate_plan(user, db)
        url = _plan_url(user)
        lang = (user.language or "en").lower()
        if lang == "de":
            intro = "hier ist dein trainingsplan 💪"
        else:
            intro = "here's your training plan 💪"
        await linq.send_message(user.linq_chat_id, intro)
        await linq.send_message(user.linq_chat_id, url)
        await add_message(user.id, "assistant", f"{intro}\n{url}", db)
        return "__SENT__"
    except Exception as ex:
        print(f"[handle_view_plan ERROR] {ex}")
        import traceback; traceback.print_exc()
        return None


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


async def handle_connect_whoop(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """User wants to connect their WHOOP — generate a connect link and return as context."""
    from app.services.token import create_onboarding_token
    from app.config import settings
    token = create_onboarding_token(user.phone)
    url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/whoop/connect?token={token}"
    return (
        f"WHOOP CONNECT URL: {url}. "
        "Tell the user to tap the link to connect their WHOOP. Keep it to 1 sentence + the URL."
    )


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


async def handle_performance_data(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Fetch recent logged weights/reps from progress_entries and return as context."""
    try:
        from sqlalchemy import select
        from app.models.progress_entry import ProgressEntry

        result = await db.execute(
            select(ProgressEntry)
            .where(
                ProgressEntry.user_id == user.id,
                ProgressEntry.category == "exercise",
            )
            .order_by(ProgressEntry.recorded_at.desc())
            .limit(15)
        )
        entries = result.scalars().all()
        if not entries:
            return (
                "User has no workout history logged yet via chat. "
                "Let them know they can log workouts by texting Kano (e.g. 'did 5x5 bench at 80kg'). "
                "They can also log sets directly on their plan page."
            )

        lines = []
        for e in entries:
            parts = [f"{e.label}: {e.value}{e.unit or ''}"]
            if e.sets and e.reps:
                parts.append(f"({e.sets}x{e.reps})")
            date_str = e.recorded_at.strftime("%b %d") if e.recorded_at else ""
            if date_str:
                parts.append(f"on {date_str}")
            lines.append(" ".join(parts))

        history = "\n".join(f"  - {l}" for l in lines)
        return (
            f"USER'S RECENT WORKOUT HISTORY (last {len(lines)} logged entries):\n{history}\n"
            "Use this data to answer their question about previous lifts or progress. "
            "Be specific with the numbers."
        )
    except Exception as ex:
        print(f"[handle_performance_data ERROR] {ex}")
        return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def handle_calendar_link(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Send the webcal:// subscription link directly and signal to skip LLM reply."""
    try:
        from app.config import settings
        from app.services.token import create_calendar_token

        base_url = settings.PUBLIC_BASE_URL.rstrip('/')
        host = base_url.removeprefix("https://").removeprefix("http://")
        token = create_calendar_token(user.phone)
        cal_url = f"webcal://{host}/calendar/{token}.ics"

        if user.language == "de":
            intro = "Hier ist dein Kalender-Link — einfach antippen und dein Trainingsplan landet direkt in deinem Kalender 📅"
        else:
            intro = "Here's your calendar link — tap it to add your training plan straight to your calendar 📅"

        await linq.send_message(user.linq_chat_id, intro)
        await linq.send_message(user.linq_chat_id, cal_url)
        await add_message(user.id, "assistant", f"{intro}\n{cal_url}", db)
        return "__SENT__"
    except Exception as ex:
        print(f"[handle_calendar_link ERROR] {ex}")
        return None

_HANDLER_MAP = {
    "PROGRESS_LOG": handle_progress_log,
    "MODIFY_PLAN": handle_modify_plan,
    "VIEW_PLAN": handle_view_plan,
    "NEW_PLAN": handle_new_plan,
    "STREAK_CHECK": handle_streak_check,
    "WHOOP_DATA": handle_whoop_data,
    "CONNECT_WHOOP": handle_connect_whoop,
    "PERFORMANCE_DATA": handle_performance_data,
    "CALENDAR_LINK": handle_calendar_link,
}


async def handle_food_log(
    user: User, text: str, db: AsyncSession, image_url: str | None = None
) -> Optional[str]:
    """Analyse a food photo with gpt-4o-mini vision and store the result."""
    if not image_url:
        return (
            "INSTRUCTION — OVERRIDE PREVIOUS CONVERSATION: You DO support calorie tracking. "
            "The user is asking if you can track their calories. Answer YES. "
            "Tell them to send you a photo of their meal and you will instantly analyse it and estimate the calories. "
            "Be warm and enthusiastic. 1-2 sentences max. Do NOT say no, do NOT refer them to another app."
        )
    try:
        from app.services.food_log import analyze_food_image
        from app.services.memory import add_message

        result = await analyze_food_image(
            image_url=image_url,
            user_id=user.id,
            db=db,
            caption=text or "",
            language=user.language or "en",
        )

        description = result.get("description", "")
        kcal = result.get("estimated_calories", 0)
        items = result.get("items", [])

        # If analysis failed (no calories estimated), apologise directly
        if not kcal:
            lang = (user.language or "en").lower()
            if lang == "de":
                fail = "ich konnte das essen auf dem foto nicht ganz erkennen — schick mir ein klareres bild oder beschreib was drauf ist 🙏"
            else:
                fail = "i couldn't quite make out the meal in that photo — send a clearer one or just tell me what's on the plate 🙏"
            await linq.send_message(user.linq_chat_id, fail)
            await add_message(user.id, "assistant", fail, db)
            return "__SENT__"

        # Build the reply directly and send it ourselves (bypass LLM so the
        # conversation history can't override the result with stale "send a photo" replies)
        lang = (user.language or "en").lower()
        if lang == "de":
            reply = f"das sieht aus wie {description.lower().rstrip('.')} — etwa {kcal} kcal 📸"
            if items:
                items_short = ", ".join(
                    f"{i['name']} ~{i['calories']}" for i in items[:4]
                )
                reply += f"\n({items_short} kcal)"
            reply += "\n\nim log gespeichert. mach so weiter 💪"
        else:
            reply = f"that looks like {description.lower().rstrip('.')} — about {kcal} kcal 📸"
            if items:
                items_short = ", ".join(
                    f"{i['name']} ~{i['calories']}" for i in items[:4]
                )
                reply += f"\n({items_short} kcal)"
            reply += "\n\nlogged. keep it going 💪"

        await linq.send_message(user.linq_chat_id, reply)
        await add_message(user.id, "assistant", reply, db)
        return "__SENT__"
    except Exception as ex:
        import traceback
        print(f"[handle_food_log ERROR] {ex}")
        traceback.print_exc()
        return None


async def run_handlers(
    intents: list[str],
    user: User,
    text: str,
    db: AsyncSession,
    image_url: str | None = None,
) -> str:
    """
    Run all matched handlers in parallel and return a combined context string
    to inject into the system prompt. The LLM always gets to respond — handlers
    only perform actions and return context, never bypass the LLM.
    """
    tasks = []
    for intent in intents:
        if intent == "FOOD_LOG":
            tasks.append(handle_food_log(user, text, db, image_url=image_url))
        elif intent in _HANDLER_MAP:
            tasks.append(_HANDLER_MAP[intent](user, text, db))

    if not tasks:
        return ""

    results = await asyncio.gather(*tasks, return_exceptions=True)
    lines = [
        r for r in results
        if isinstance(r, str) and r and r != "__SENT__"
    ]
    return "\n".join(lines)
