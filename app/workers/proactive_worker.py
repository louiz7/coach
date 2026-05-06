import asyncio
import os
from datetime import datetime, date as _date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from arq.cron import cron
from app.database import async_session
from app.services.proactive import get_idle_users, send_checkin


# Hour at which the morning brief is sent (in each user's local timezone).
# Cron fires every 30 min; each user gets at most ONE brief per day thanks
# to a Redis dedup key (whoop:morning_sent:{user_id}:{YYYY-MM-DD}).
_MORNING_HOUR = 8

# Hour at which the evening check-in is sent (local time).
# Skipped if the user already logged progress today.
_EVENING_HOUR = 20


async def run_proactive_checkins():
    """Run proactive check-ins for idle users. Called by scheduler."""
    async with async_session() as db:
        users = await get_idle_users(db)
        for user in users:
            try:
                await send_checkin(user, db)
            except Exception as e:
                print(f"Proactive check-in failed for {user.id}: {e}")
            await asyncio.sleep(0.5)  # rate limit


async def run_morning_brief():
    """Daily morning brief for ALL onboarded Hercules users.

    - Fires every 30 min via cron; only acts when a user's local hour matches
      _MORNING_HOUR (so each user is processed at most twice per day).
    - A Redis dedup key (whoop:morning_sent:{user_id}:{YYYY-MM-DD}) ensures
      every user gets exactly ONE brief per day even if the cron retries.
    - WHOOP-connected users get a recovery-aware brief.
    - Non-WHOOP users get a plan-only brief (today's planned workout).
    - Users with no active plan + no WHOOP are skipped silently.
    """
    from sqlalchemy import select
    from app.models.user import User
    from app.redis import redis_pool

    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                User.onboarding_complete == True,
                User.linq_chat_id.isnot(None),
            )
        )
        users = list(result.scalars().all())

    for user in users:
        try:
            tz_name = user.timezone or "Europe/Berlin"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("Europe/Berlin")

            local_now = datetime.now(tz)
            if local_now.hour != _MORNING_HOUR:
                continue

            # Per-day dedup — set inside _send_morning_brief on success
            today_key = f"whoop:morning_sent:{user.id}:{local_now.strftime('%Y-%m-%d')}"
            if await redis_pool.get(today_key):
                continue

            async with async_session() as db:
                await _send_morning_brief(user, db, today_key, local_now)

        except Exception as e:
            print(f"[morning_brief] failed for {user.id}: {e}")
        await asyncio.sleep(0.5)


async def _send_morning_brief(user, db, dedup_key: str, local_now: datetime) -> None:
    """Build + send a single morning brief for one user, then set dedup key."""
    from sqlalchemy import select
    from app.models.training_plan import TrainingPlan
    from app.services.training_plan import get_workout_for_today
    from app.services import linq as linq_svc
    from app.services.memory import add_message
    from app.redis import redis_pool
    from app.config import settings as cfg
    from openai import AsyncOpenAI

    # Active plan (optional)
    plan_result = await db.execute(
        select(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
        .order_by(TrainingPlan.created_at.desc())
    )
    active_plan = plan_result.scalars().first()
    today_workout = None
    if active_plan and active_plan.plan_json:
        today_workout = get_workout_for_today(
            active_plan.plan_json, local_now.strftime("%A")
        )

    # WHOOP recovery (optional)
    recovery_score: int | None = None
    hrv: float | None = None
    rhr: int | None = None
    if user.whoop_access_token:
        try:
            from app.api.whoop import _ensure_fresh_token
            from app.services import whoop as whoop_svc
            access_token = await _ensure_fresh_token(user, db)
            if access_token:
                # Prefer canonical "today's cycle" recovery; fall back to
                # the latest SCORED recovery only if cycle endpoint fails.
                rec = await whoop_svc.get_today_recovery(access_token)
                if rec is None:
                    rec = await whoop_svc.get_latest_recovery(access_token)
                if rec:
                    score = rec.get("score", {}) or {}
                    rs = score.get("recovery_score")
                    if rs is not None:
                        recovery_score = int(rs)
                        user.last_recovery_score = recovery_score
                        print(f"[morning_brief] WHOOP recovery for {user.name}: {recovery_score}% (score_state={rec.get('score_state')}, cycle_id={rec.get('cycle_id')})")
                    if score.get("hrv_rmssd_milli") is not None:
                        hrv = float(score["hrv_rmssd_milli"])
                        user.last_hrv = hrv
                    if score.get("resting_heart_rate") is not None:
                        rhr = int(score["resting_heart_rate"])
                    await db.commit()
        except Exception as ex:
            print(f"[morning_brief] WHOOP fetch failed for {user.name}: {ex}")

    # Skip silently if there's nothing to say
    if not today_workout and recovery_score is None and not active_plan:
        await redis_pool.set(dedup_key, "1", ex=86400)
        return

    # Skip rest days entirely — no morning brief on non-training days
    if active_plan and not today_workout:
        await redis_pool.set(dedup_key, "1", ex=86400)
        return

    # ── Recovery context ──────────────────────────────────────────────────
    if recovery_score is not None:
        if recovery_score >= 67:
            emoji = "🟢"
            intensity_guidance = "HIGH: great recovery — push hard today, top sets encouraged."
        elif recovery_score >= 34:
            emoji = "🟡"
            intensity_guidance = "MODERATE: decent recovery — keep planned loads but don't max out."
        else:
            emoji = "🔴"
            intensity_guidance = "LOW: poor recovery — reduce all loads by ~20-30%, cut sets by 1, prioritise form over weight. Consider swapping any heavy compound lifts for lighter accessory work or mobility."
        bio_bits = []
        if hrv:
            bio_bits.append(f"{hrv:.0f}ms HRV")
        if rhr:
            bio_bits.append(f"{rhr}bpm RHR")
        bio_str = f" ({', '.join(bio_bits)})" if bio_bits else ""
        recovery_line = f"WHOOP: {emoji} {recovery_score}%{bio_str} → intensity guidance: {intensity_guidance}"
    else:
        recovery_line = "No WHOOP data — use planned loads as written."
        intensity_guidance = None

    # ── Today's workout from plan ─────────────────────────────────────────
    if today_workout:
        ex_lines = []
        for ex in today_workout.get("exercises", []):
            sets = ex.get("sets", "?")
            reps = ex.get("reps", "?")
            rpe  = ex.get("rpe", "")
            rest = ex.get("rest_seconds", "")
            line = f"- {ex.get('name','')}: {sets}x{reps}"
            if rpe:
                line += f" @RPE{rpe}"
            if rest:
                line += f" ({rest}s rest)"
            ex_lines.append(line)
        workout_block = (
            f"Planned workout: {today_workout.get('day','')} — {today_workout.get('focus','')}\n"
            + "\n".join(ex_lines)
        )
        is_rest_day = False
    elif active_plan:
        workout_block = "Today is a rest day per their training plan."
        is_rest_day = True
    else:
        workout_block = "No active training plan."
        is_rest_day = True

    # ── Build prompt ──────────────────────────────────────────────────────
    user_ctx = (
        f"Athlete: {user.name} | Goal: {user.goal or 'general fitness'} | "
        f"Focus: {user.sports_focus or 'general'} | "
        f"Coach style: {user.coach_style or 'direct'} | "
        f"Intensity pref: {user.coach_intensity or 'moderate'}"
    )

    if not is_rest_day and today_workout:
        if recovery_score is not None and recovery_score < 34:
            adapt_note = "Since recovery is LOW, explicitly tell the athlete you've adjusted today's session for them (lighter loads, one less set) so they recover properly and come back stronger."
        elif recovery_score is not None and recovery_score >= 67:
            adapt_note = "Since recovery is HIGH, explicitly tell the athlete their body is primed and you've flagged where they can push a top set today."
        elif recovery_score is not None:
            adapt_note = "Briefly mention you've checked their WHOOP data and today's session reflects their current readiness."
        else:
            adapt_note = "Give one short motivational line to kick off the session."
        if recovery_score is not None:
            line1 = "Line 1 — Recovery status: one sentence with the recovery score + one-word readiness verdict.\n"
        else:
            line1 = "Line 1 — Morning greeting: one short energetic sentence to start the day.\n"
        adjust_instruction = (
            "TASK: Write a morning workout brief. Structure it exactly like this:\n"
            f"{line1}"
            f"Line 2 — Adaptation note: {adapt_note} Keep it to one short sentence, conversational.\n"
            "Line 3 — Today's session: name the focus and list the exercises with sets/reps. Keep it as a compact text list, no markdown bullets.\n"
            "Line 4 — One punchy coaching tip (max 1 sentence).\n\n"
            "Rules: No markdown. Max 6 lines total. Sound like a real coach texting, not a report. "
            "IMPORTANT: Do NOT mention WHOOP, recovery scores, or biometric data unless actual WHOOP data is provided above."
        )
    else:
        adjust_instruction = (
            "TASK: Write a 2-3 sentence morning message. "
            "If it's a rest day, acknowledge it and give one general recovery tip. "
            "No markdown. Sound like a real coach texting. "
            "IMPORTANT: Do NOT mention WHOOP or recovery scores — this athlete has no WHOOP connected."
        )

    prompt = (
        f"You are Hercules, a personal fitness coach messaging via iMessage.\n\n"
        f"{user_ctx}\n\n"
        f"{recovery_line}\n\n"
        f"{workout_block}\n\n"
        f"{adjust_instruction}"
    )

    try:
        client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.75,
        )
        message = response.choices[0].message.content.strip()
    except Exception as ex:
        print(f"[morning_brief] LLM failed for {user.name}: {ex}")
        return

    if user.linq_chat_id:
        # Split into 2 messages: recovery line + workout block (feels more natural)
        lines = message.split("\n", 1)
        if len(lines) == 2 and len(lines[0]) < 120:
            await linq_svc.send_message(user.linq_chat_id, lines[0].strip())
            await asyncio.sleep(1.5)
            await linq_svc.send_message(user.linq_chat_id, lines[1].strip())
            try:
                await add_message(user.id, "assistant", message, db)
            except Exception:
                pass
        else:
            await linq_svc.send_message(user.linq_chat_id, message)
            try:
                await add_message(user.id, "assistant", message, db)
            except Exception:
                pass
        print(f"[morning_brief] sent to {user.name} (recovery={recovery_score}, plan={today_workout is not None})")

    # Set dedup AFTER successful send
    await redis_pool.set(dedup_key, "1", ex=86400)


# For arq worker
async def proactive_task(ctx):
    await run_proactive_checkins()


async def morning_whoop_task(ctx):
    # Renamed conceptually — kept the function name for cron compatibility.
    # Sends ONE morning brief per user per day (with or without WHOOP data).
    await run_morning_brief()


async def evening_checkin_task(ctx):
    await run_evening_checkin()


async def weekly_coach_notes_task(ctx):
    """Sunday midnight: enrich coach_notes for active users via gpt-4o-mini."""
    from sqlalchemy import select
    from app.models.user import User
    from app.services.fitness_profile import enrich_profile_with_coach_notes

    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                User.onboarding_complete == True,
                User.linq_chat_id.isnot(None),
            )
        )
        users = list(result.scalars().all())

    for user in users:
        try:
            async with async_session() as db:
                added = await enrich_profile_with_coach_notes(user.id, db)
                if added:
                    print(f"[coach_notes] {user.name}: {added}")
        except Exception as e:
            print(f"[coach_notes] failed for {user.id}: {e}")
        await asyncio.sleep(1.0)


async def run_evening_checkin():
    """Evening check-in at 8 PM local time.

    - Asks how the workout went if today was a training day.
    - Prompts to log progress if no progress_entry exists for today.
    - Skipped entirely if user already messaged us in the past 2 hours
      (they're active — no need to interrupt).
    - Redis dedup key: evening_sent:{user_id}:{YYYY-MM-DD}
    """
    from sqlalchemy import select, func
    from app.models.user import User
    from app.models.progress_entry import ProgressEntry
    from app.redis import redis_pool

    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                User.onboarding_complete == True,
                User.linq_chat_id.isnot(None),
            )
        )
        users = list(result.scalars().all())

    for user in users:
        try:
            tz_name = user.timezone or "Europe/Berlin"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("Europe/Berlin")

            local_now = datetime.now(tz)
            if local_now.hour != _EVENING_HOUR:
                continue

            today_key = f"evening_sent:{user.id}:{local_now.strftime('%Y-%m-%d')}"
            if await redis_pool.get(today_key):
                continue

            async with async_session() as db:
                await _send_evening_checkin(user, db, today_key, local_now)

        except Exception as e:
            print(f"[evening_checkin] failed for {user.id}: {e}")
        await asyncio.sleep(0.5)


async def _send_evening_checkin(user, db, dedup_key: str, local_now: datetime) -> None:
    from sqlalchemy import select, func
    from app.models.progress_entry import ProgressEntry
    from app.models.training_plan import TrainingPlan
    from app.models.message import Message
    from app.services.training_plan import get_workout_for_today
    from app.services import linq as linq_svc
    from app.services.memory import add_message
    from app.redis import redis_pool
    from app.config import settings as cfg
    from openai import AsyncOpenAI

    today_str = local_now.strftime("%Y-%m-%d")
    weekday = local_now.strftime("%A")

    # Skip if user was active in the last 2 hours (they're already chatting)
    recent_msg = await db.execute(
        select(func.max(Message.created_at))
        .where(Message.user_id == user.id, Message.role == "user")
    )
    last_msg_at = recent_msg.scalar_one_or_none()
    if last_msg_at:
        diff = (local_now.replace(tzinfo=None) - last_msg_at).total_seconds()
        if diff < 7200:  # active within 2h — skip
            await redis_pool.set(dedup_key, "1", ex=86400)
            return

    # Check if user already logged progress today
    logged_today = await db.execute(
        select(func.count(ProgressEntry.id))
        .where(
            ProgressEntry.user_id == user.id,
            func.date(ProgressEntry.created_at) == today_str,
        )
    )
    has_log = (logged_today.scalar_one() or 0) > 0

    # Get today's planned workout (so we know if it was a training day)
    plan_result = await db.execute(
        select(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
    )
    active_plan = plan_result.scalars().first()
    today_workout = None
    if active_plan and active_plan.plan_json:
        today_workout = get_workout_for_today(active_plan.plan_json, weekday)

    is_training_day = today_workout is not None

    # Skip rest days entirely — no evening check-in on non-training days
    if not is_training_day:
        await redis_pool.set(dedup_key, "1", ex=86400)
        return

    # Build the prompt
    user_ctx = (
        f"Athlete: {user.name} | Goal: {user.goal or 'general fitness'} | "
        f"Coach style: {user.coach_style or 'direct'}"
    )

    if is_training_day and not has_log:
        task = (
            f"Today was a {weekday} training day ({today_workout.get('focus', 'workout')}). "
            "The athlete has NOT logged their workout yet. "
            "Write a short evening check-in (2-3 sentences max): "
            "ask how the session went, and nudge them to log it "
            "(e.g. 'just drop a quick note — what you lifted, how it felt'). "
            "Keep it casual, like a text from a coach. No markdown."
        )
    elif is_training_day and has_log:
        task = (
            f"Today was a {weekday} training day and the athlete already logged their workout. "
            "Write a short encouraging evening message (1-2 sentences): "
            "acknowledge they got it done and say something motivating about consistency. "
            "No markdown. No questions."
        )
    elif not is_training_day and not has_log:
        # Rest day — short recovery tip, no log nudge
        task = (
            "Today is a rest day. Write a very short message (1-2 sentences) "
            "reminding them rest is part of the plan and giving one recovery tip "
            "(sleep, nutrition, mobility). No markdown."
        )
    else:
        # Rest day but they logged something (extra activity) — acknowledge
        task = (
            "Today was a rest day but the athlete logged some activity. "
            "Write 1-2 sentences acknowledging it positively without overdoing it. No markdown."
        )

    prompt = (
        f"You are Hercules, a personal fitness coach messaging via iMessage.\n\n"
        f"{user_ctx}\n\n"
        f"{task}"
    )

    try:
        client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.8,
        )
        message = response.choices[0].message.content.strip()
    except Exception as ex:
        print(f"[evening_checkin] LLM failed for {user.name}: {ex}")
        return

    if user.linq_chat_id:
        await linq_svc.send_message(user.linq_chat_id, message)
        try:
            await add_message(user.id, "assistant", message, db)
        except Exception:
            pass
        print(f"[evening_checkin] sent to {user.name} (training_day={is_training_day}, logged={has_log})")

    await redis_pool.set(dedup_key, "1", ex=86400)


def _build_redis_settings():
    from arq.connections import RedisSettings
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    print(f"[arq worker] REDIS_URL = {redis_url}", flush=True)
    _url = redis_url.replace("redis://", "")
    _host, _port = (_url.split(":") + ["6379"])[:2]
    return RedisSettings(host=_host, port=int(_port))


_redis_settings = _build_redis_settings()


class WorkerSettings:
    functions = [proactive_task, morning_whoop_task, evening_checkin_task, weekly_coach_notes_task]
    cron_jobs = [
        # Proactive idle-user pings stay disabled for now (too chatty pre-beta).
        # cron(proactive_task, minute={0, 30}),
        #
        # Morning brief: cron fires twice an hour but each user is processed
        # at most ONCE per day thanks to the Redis dedup key set inside
        # _send_morning_brief. The local-hour filter (==8) gives us a 1-hour
        # window for the cron to land on each user's tz.
        cron(morning_whoop_task, minute={0, 30}),
        cron(evening_checkin_task, minute={0, 30}),
        cron(weekly_coach_notes_task, weekday=6, hour=0, minute=0),   # Sunday 00:00 UTC (no user-facing messages)
    ]
    redis_settings = _redis_settings


if __name__ == "__main__":
    # Fallback when invoked via `python -m`
    from arq import run_worker as _run_worker
    _run_worker(WorkerSettings)
