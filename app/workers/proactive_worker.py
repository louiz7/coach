import asyncio
import os
from datetime import datetime, date as _date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from arq.cron import cron
from app.database import async_session
from app.services.proactive import get_idle_users, send_checkin


# Window (inclusive) in which the morning brief may be sent (local time).
# Cron fires every 30 min; dedup key ensures at most ONE brief per user per day.
# Tight catch-up window: 08:00–09:59 local. Wider windows risk re-sends if Redis
# is recreated mid-deploy (lost dedup keys → next cron tick re-fires to everyone).
_MORNING_HOUR_START = 8    # 08:00 local
_MORNING_HOUR_END   = 9    # catch-up until 09:59 local

# Window for evening check-in (local time). Same rationale — keep it tight.
_EVENING_HOUR_START = 19   # 19:00 local
_EVENING_HOUR_END   = 20   # catch-up until 20:59 local


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
    """Daily morning brief for ALL onboarded Kano users.

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

    from app.config import settings as cfg
    no_text = cfg.no_text_phones_set

    for user in users:
        try:
            # Skip users who have opted out of proactive texts
            if user.phone and user.phone in no_text:
                continue

            tz_name = user.timezone or "Europe/Berlin"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("Europe/Berlin")

            local_now = datetime.now(tz)
            # Catch-up window: 08:00 – 12:59 local. If the worker was down during
            # the original window we still deliver. Daily dedup key prevents dupes.
            if not (_MORNING_HOUR_START <= local_now.hour <= _MORNING_HOUR_END):
                continue

            # Per-day dedup using atomic SET NX claim. If another cron tick (or
            # a redeploy mid-window) already claimed today, we skip. The claim is
            # set BEFORE the send so even a slow/failed send can't be re-fired by
            # the next 30-min cron tick. TTL=24h covers the day.
            today_key = f"whoop:morning_sent:{user.id}:{local_now.strftime('%Y-%m-%d')}"
            claimed = await redis_pool.set(today_key, "1", ex=86400, nx=True)
            if not claimed:
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

    # Rest day — send a short recovery nudge instead of skipping
    if active_plan and not today_workout:
        rest_prompt = (
            f"You are Kano, a personal fitness coach messaging via iMessage.\n\n"
            f"Athlete: {user.name} | Goal: {user.goal or 'general fitness'} | "
            f"Focus: {user.sports_focus or 'general'}\n\n"
            "Today is a rest day for this athlete.\n\n"
            "TASK: Write a short rest-day morning message (2-3 sentences max).\n"
            "Cover 1-2 of these topics naturally: hydration (drink enough water), "
            "clean eating for recovery, sleep/rest quality, light mobility.\n"
            "Rules: no markdown, no bullet points, no emojis. "
            "Sound like a real coach texting, not a wellness app. Keep it brief and direct."
        )
        try:
            client = AsyncOpenAI(api_key=cfg.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
            resp = await client.chat.completions.create(
                model="deepseek/deepseek-v4-flash",
                messages=[{"role": "user", "content": rest_prompt}],
                max_tokens=120,
                temperature=0.75,
            )
            rest_msg = resp.choices[0].message.content.strip()
        except Exception as ex:
            print(f"[morning_brief] rest-day LLM failed for {user.name}: {ex}")
            rest_msg = f"Rest day today, {user.name}. Make sure you're drinking enough water and keeping your meals clean — recovery happens in the kitchen too."

        if user.linq_chat_id:
            await linq_svc.send_message(user.linq_chat_id, rest_msg)
            try:
                await add_message(user.id, "assistant", rest_msg, db)
            except Exception:
                pass
            print(f"[morning_brief] rest-day nudge sent to {user.name}")
        await redis_pool.set(dedup_key, "1", ex=86400)
        return

    # ── WHOOP connected but not yet synced today ──────────────────────────
    # Send the plan now so they're not waiting, nudge them to open WHOOP,
    # and park a pending key so the follow-up task can text them once WHOOP syncs.
    whoop_pending_key = f"whoop:pending_brief:{user.id}:{local_now.strftime('%Y-%m-%d')}"
    if user.whoop_access_token and recovery_score is None:
        # WHOOP connected but not synced yet — send plan link + nudge
        focus = today_workout.get("focus", "") if today_workout else ""
        day_name = today_workout.get("day", local_now.strftime("%A")).lower() if today_workout else local_now.strftime("%A").lower()

        early_prompt = (
            f"You are Kano, a personal fitness coach messaging via iMessage.\n\n"
            f"Athlete: {user.name} | Goal: {user.goal or 'general fitness'}\n"
            f"Today: {day_name} — {focus or 'rest day'}\n\n"
            "TASK: Write ONE short morning message (2-3 sentences max).\n"
            "Mention what type of day today is (e.g. leg day, upper body, rest day).\n"
            "Tell them their WHOOP hasn't synced yet and to open the app.\n"
            "Rules: no markdown, no bullet points, no exercise lists. Real coach texting tone."
        )
        try:
            client = AsyncOpenAI(api_key=cfg.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
            resp = await client.chat.completions.create(
                model="deepseek/deepseek-v4-flash",
                messages=[{"role": "user", "content": early_prompt}],
                max_tokens=120,
                temperature=0.75,
            )
            early_msg = resp.choices[0].message.content.strip()
        except Exception as ex:
            print(f"[morning_brief] early LLM failed for {user.name}: {ex}")
            early_msg = f"Morning {user.name}! {day_name.capitalize()} — {focus or 'rest day'} is on the card. Open your WHOOP so I can check your recovery."

        if user.linq_chat_id:
            await linq_svc.send_message(user.linq_chat_id, early_msg)
            try:
                await add_message(user.id, "assistant", early_msg, db)
            except Exception:
                pass

            # Message 2: plan link
            if today_workout and active_plan:
                from app.services.token import create_plan_token
                token = create_plan_token(user.phone)
                plan_url = f"{cfg.PUBLIC_BASE_URL.rstrip('/')}/plan?token={token}"
                await asyncio.sleep(1.2)
                await linq_svc.send_message(user.linq_chat_id, plan_url)
                try:
                    await add_message(user.id, "assistant", plan_url, db)
                except Exception:
                    pass

            print(f"[morning_brief] plan-only sent to {user.name} (WHOOP not synced yet)")

        await redis_pool.set(whoop_pending_key, "1", ex=43200)
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
        day_label = today_workout.get("day", local_now.strftime("%A")).lower()
        focus_label = today_workout.get("focus", "")
        workout_block = f"Today: {day_label} — {focus_label}"
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
        if recovery_score is not None:
            if recovery_score < 34:
                adapt_note = "Recovery is LOW — tell them in one short sentence to take it easier today and trust the process."
            elif recovery_score >= 67:
                adapt_note = "Recovery is HIGH — one short sentence, they're primed to push."
            else:
                adapt_note = "Recovery is MODERATE — one short sentence, stick to plan."
            adjust_instruction = (
                "TASK: Write ONE short morning message (3-4 sentences max).\n"
                f"Start with their WHOOP recovery score ({recovery_score}%) in natural language.\n"
                f"Mention what type of session today is (e.g. leg day, push day, upper body — use the workout info).\n"
                f"{adapt_note}\n"
                "Rules: no markdown, no bullet points, NO exercise lists. Real coach texting tone."
            )
        else:
            adjust_instruction = (
                "TASK: Write ONE short morning message (2-3 sentences max).\n"
                "Mention what type of session today is (e.g. leg day, push day, upper body — use the workout info).\n"
                "Add one short motivational line.\n"
                "Rules: no markdown, no bullet points, NO exercise lists. Real coach texting tone."
            )
    else:
        adjust_instruction = (
            "TASK: Write a 2-3 sentence morning message.\n"
            "If it's a rest day, acknowledge it and give one general recovery tip.\n"
            "Rules: no markdown, NO exercise lists. Real coach texting tone."
        )

    prompt = (
        f"You are Kano, a personal fitness coach messaging via iMessage.\n\n"
        f"{user_ctx}\n\n"
        f"{recovery_line}\n\n"
        f"{workout_block}\n\n"
        f"{adjust_instruction}"
    )

    try:
        client = AsyncOpenAI(api_key=cfg.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
        response = await client.chat.completions.create(
            model="deepseek/deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.75,
        )
        message = response.choices[0].message.content.strip()
    except Exception as ex:
        print(f"[morning_brief] LLM failed for {user.name}: {ex}")
        return

    if user.linq_chat_id:
        # Message 1: recovery + day focus
        await linq_svc.send_message(user.linq_chat_id, message)
        try:
            await add_message(user.id, "assistant", message, db)
        except Exception:
            pass

        # Message 2: plan link (only if there's a workout today)
        if today_workout and active_plan:
            from app.services.token import create_plan_token
            token = create_plan_token(user.phone)
            plan_url = f"{cfg.PUBLIC_BASE_URL.rstrip('/')}/plan?token={token}"
            await asyncio.sleep(1.2)
            await linq_svc.send_message(user.linq_chat_id, plan_url)
            try:
                await add_message(user.id, "assistant", plan_url, db)
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

    from app.config import settings as cfg
    no_text = cfg.no_text_phones_set

    print(f"[evening_checkin] checking {len(users)} users")
    for user in users:
        try:
            # Skip users who have opted out of proactive texts
            if user.phone and user.phone in no_text:
                continue

            tz_name = user.timezone or "Europe/Berlin"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("Europe/Berlin")

            local_now = datetime.now(tz)
            # Catch-up window: send any time from evening start through 23:59.
            # This way a worker restart inside the original window still delivers,
            # and the daily dedup key ensures users get at most one message per day.
            if not (_EVENING_HOUR_START <= local_now.hour <= _EVENING_HOUR_END):
                continue

            today_key = f"evening_sent:{user.id}:{local_now.strftime('%Y-%m-%d')}"
            # Atomic claim — see morning brief for rationale.
            claimed = await redis_pool.set(today_key, "1", ex=86400, nx=True)
            if not claimed:
                print(f"[evening_checkin] {user.name} already sent today, skipping")
                continue

            print(f"[evening_checkin] processing {user.name} (local={local_now.strftime('%H:%M')})")
            async with async_session() as db:
                await _send_evening_checkin(user, db, today_key, local_now)

        except Exception as e:
            print(f"[evening_checkin] failed for {user.id}: {e}")
        await asyncio.sleep(0.5)


async def _send_evening_checkin(user, db, dedup_key: str, local_now: datetime) -> None:
    from sqlalchemy import select, func
    from app.models.progress_entry import ProgressEntry
    from app.models.training_plan import TrainingPlan
    from app.services.training_plan import get_workout_for_today
    from app.services import linq as linq_svc
    from app.services.memory import add_message
    from app.redis import redis_pool
    from app.config import settings as cfg
    from openai import AsyncOpenAI

    today_str = local_now.strftime("%Y-%m-%d")
    weekday = local_now.strftime("%A")

    # Check if user already logged progress today
    from sqlalchemy import cast, Date
    logged_today = await db.execute(
        select(func.count(ProgressEntry.id))
        .where(
            ProgressEntry.user_id == user.id,
            cast(ProgressEntry.recorded_at, Date) == local_now.date(),
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
        # Rest day — short recovery tip
        task = (
            "Today is a rest day. Write a very short message (1-2 sentences) "
            "reminding them rest is part of the plan and giving one recovery tip "
            "(sleep, nutrition, mobility). No markdown."
        )
    else:
        # Rest day but they logged some extra activity — acknowledge
        task = (
            "Today was a rest day but the athlete logged some activity. "
            "Write 1-2 sentences acknowledging it positively without overdoing it. No markdown."
        )

    prompt = (
        f"You are Kano, a personal fitness coach messaging via iMessage.\n\n"
        f"{user_ctx}\n\n"
        f"{task}"
    )

    try:
        client = AsyncOpenAI(api_key=cfg.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
        response = await client.chat.completions.create(
            model="deepseek/deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.8,
        )
        message = response.choices[0].message.content.strip()
    except Exception as ex:
        print(f"[evening_checkin] LLM failed for {user.name}: {ex}")
        return

    if user.linq_chat_id:
        try:
            await linq_svc.send_message(user.linq_chat_id, message)
            print(f"[evening_checkin] sent to {user.name} (training_day={is_training_day}, logged={has_log})")
        except Exception as send_err:
            print(f"[evening_checkin] SEND FAILED for {user.name}: {send_err}")
            return  # do NOT set dedup so we retry next cron tick
        try:
            await add_message(user.id, "assistant", message, db)
        except Exception:
            pass
    else:
        print(f"[evening_checkin] {user.name} has no linq_chat_id, skipping")
        return

    await redis_pool.set(dedup_key, "1", ex=86400)


async def run_whoop_followup():
    """Runs every 30 min. For users who got a plan-only brief this morning
    (WHOOP was connected but not yet synced), check if WHOOP has synced.
    If yes → send a short recovery follow-up and clear the pending key."""
    from sqlalchemy import select
    from app.models.user import User
    from app.redis import redis_pool

    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                User.onboarding_complete == True,
                User.linq_chat_id.isnot(None),
                User.whoop_access_token.isnot(None),
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
            today_str = local_now.strftime("%Y-%m-%d")

            pending_key = f"whoop:pending_brief:{user.id}:{today_str}"
            sent_key = f"whoop:followup_sent:{user.id}:{today_str}"

            if not await redis_pool.get(pending_key):
                continue
            if await redis_pool.get(sent_key):
                continue

            # Try to fetch WHOOP data now
            recovery_score = None
            hrv = None
            rhr = None
            try:
                from app.api.whoop import _ensure_fresh_token
                from app.services import whoop as whoop_svc
                async with async_session() as db2:
                    access_token = await _ensure_fresh_token(user, db2)
                    if access_token:
                        rec = await whoop_svc.get_today_recovery(access_token)
                        if rec is None:
                            rec = await whoop_svc.get_latest_recovery(access_token)
                        if rec:
                            score = rec.get("score", {}) or {}
                            rs = score.get("recovery_score")
                            if rs is not None:
                                recovery_score = int(rs)
                            if score.get("hrv_rmssd_milli") is not None:
                                hrv = float(score["hrv_rmssd_milli"])
                            if score.get("resting_heart_rate") is not None:
                                rhr = int(score["resting_heart_rate"])
            except Exception as ex:
                print(f"[whoop_followup] fetch failed for {user.name}: {ex}")
                continue

            if recovery_score is None:
                continue  # still not synced — try again next 30 min

            # Build follow-up message
            if recovery_score >= 67:
                emoji = "🟢"; verdict = "full send"; tip = "Push your top sets today."
            elif recovery_score >= 34:
                emoji = "🟡"; verdict = "stay the course"; tip = "Stick to planned loads — no need to back off."
            else:
                emoji = "🔴"; verdict = "take it easier"; tip = "Drop loads ~20% and cut one set per exercise — you'll come back stronger for it."

            bio_bits = []
            if hrv: bio_bits.append(f"{hrv:.0f}ms HRV")
            if rhr: bio_bits.append(f"{rhr}bpm RHR")
            bio_str = f" ({', '.join(bio_bits)})" if bio_bits else ""

            followup_prompt = (
                f"You are Kano, a personal fitness coach messaging via iMessage.\n\n"
                f"Athlete: {user.name}\n"
                f"WHOOP just synced: {emoji} {recovery_score}%{bio_str} — verdict: {verdict}\n"
                f"Coaching tip: {tip}\n\n"
                "TASK: Write a very short follow-up text (2-3 sentences max). "
                "Tell them their WHOOP just synced, give the recovery verdict, and say what that means for today's session. "
                "Sound like a real coach texting — direct, no fluff, no markdown."
            )

            try:
                from app.config import settings as cfg
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=cfg.OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
                resp = await client.chat.completions.create(
                    model="deepseek/deepseek-v4-flash",
                    messages=[{"role": "user", "content": followup_prompt}],
                    max_tokens=120,
                    temperature=0.75,
                )
                followup_msg = resp.choices[0].message.content.strip()
            except Exception as ex:
                print(f"[whoop_followup] LLM failed for {user.name}: {ex}")
                followup_msg = f"Your WHOOP just synced — {emoji} {recovery_score}%{bio_str}. {tip}"

            from app.services import linq as linq_svc
            from app.services.memory import add_message
            if user.linq_chat_id:
                await linq_svc.send_message(user.linq_chat_id, followup_msg)
                async with async_session() as db3:
                    try:
                        await add_message(user.id, "assistant", followup_msg, db3)
                    except Exception:
                        pass
                print(f"[whoop_followup] sent to {user.name} (recovery={recovery_score}%)")

            # Clear pending, set sent to prevent double-fire
            await redis_pool.delete(pending_key)
            await redis_pool.set(sent_key, "1", ex=86400)

        except Exception as e:
            print(f"[whoop_followup] failed for {user.id}: {e}")
        await asyncio.sleep(0.5)


async def whoop_followup_task(ctx):
    await run_whoop_followup()


def _build_redis_settings():
    from arq.connections import RedisSettings
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    print(f"[arq worker] REDIS_URL = {redis_url}", flush=True)
    _url = redis_url.replace("redis://", "")
    _host, _port = (_url.split(":") + ["6379"])[:2]
    return RedisSettings(host=_host, port=int(_port))


_redis_settings = _build_redis_settings()


async def startup(ctx):
    """Warm caches on worker start."""
    try:
        from app.services.exercise_normalizer import warm_exercise_cache
        count = await warm_exercise_cache()
        print(f"[worker startup] MuscleWiki exercise cache warmed: {count} names")
    except Exception as e:
        print(f"[worker startup] MuscleWiki cache warm failed (non-fatal): {e}")


class WorkerSettings:
    on_startup = startup
    job_timeout = 25  # seconds — gives in-flight jobs time to finish before shutdown
    functions = [proactive_task, morning_whoop_task, evening_checkin_task, weekly_coach_notes_task, whoop_followup_task]
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
        # WHOOP follow-up: checks every 30 min if WHOOP has synced for users
        # who received a plan-only brief this morning (WHOOP was connected but
        # not yet synced at 8am). Fires until data arrives or 12h window expires.
        cron(whoop_followup_task, minute={0, 30}),
        cron(weekly_coach_notes_task, weekday=6, hour=0, minute=0),   # Sunday 00:00 UTC (no user-facing messages)
    ]
    redis_settings = _redis_settings


if __name__ == "__main__":
    # Fallback when invoked via `python -m`
    from arq import run_worker as _run_worker
    _run_worker(WorkerSettings)
