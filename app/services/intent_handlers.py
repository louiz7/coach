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


# Modification verbs/phrases — when the user has an active plan AND their
# message contains one of these, we treat the request as a modification.
_MODIFICATION_KEYWORDS = (
    "swap", "replace", "change", "remove", "add", "drop", "skip",
    "make it", "instead of", "more", "less", "fewer", "shorter", "longer",
    "harder", "easier", "tweak", "adjust", "modify", "update",
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


async def handle_plan_request(user: User, text: str, db: AsyncSession) -> Optional[str]:
    """Generate or modify the training plan AND send it to the user via iMessage.

    The full plan text is sent in chunks directly from this handler — the
    main LLM reply afterwards just acknowledges it (using the returned context).
    """
    try:
        plan = await generate_plan(user, db, user_request=text)

        # Send the full plan to the user as iMessage chunks (skip if no chat_id)
        if user.linq_chat_id and plan.raw_text:
            chunks = chunk_plan_text(plan.raw_text)
            import asyncio as _aio
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await _aio.sleep(0.6)
                await linq.send_message(user.linq_chat_id, chunk)
                await add_message(user.id, "assistant", chunk, db)

        # Decide whether this was a creation or modification (best-effort
        # signal for the follow-up LLM acknowledgment)
        text_lower = (text or "").lower()
        is_modification = any(kw in text_lower for kw in _MODIFICATION_KEYWORDS)
        verb = "updated" if is_modification else "ready"
        return (
            f"The training plan was just {verb} and sent to the user as a "
            "separate message. In your reply, briefly acknowledge it in 1 "
            "short sentence and invite them to tell you if they want to tweak anything. "
            "Do NOT repeat the plan."
        )
    except Exception as ex:
        print(f"[handle_plan_request ERROR] {ex}")
        return "Failed to generate training plan. Apologize briefly and ask them to try again."


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

        # Fetch latest recovery
        try:
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
    "PLAN_REQUEST": handle_plan_request,
    "STREAK_CHECK": handle_streak_check,
    "WHOOP_DATA": handle_whoop_data,
}


async def run_handlers(
    intents: list[str], user: User, text: str, db: AsyncSession
) -> str:
    """
    Run all matched handlers in parallel and return a combined context string
    to inject into the system prompt.
    """
    tasks = [
        _HANDLER_MAP[intent](user, text, db)
        for intent in intents
        if intent in _HANDLER_MAP
    ]
    if not tasks:
        return ""

    results = await asyncio.gather(*tasks, return_exceptions=True)
    lines = [r for r in results if isinstance(r, str) and r]
    return "\n".join(lines)
