import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.database import async_session
from app.services.proactive import get_idle_users, send_checkin


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


async def run_morning_whoop_pull():
    """
    Every 30 min: for each WHOOP-connected user whose local time is 8:00–8:29 AM,
    fetch latest recovery from WHOOP and send a morning briefing.
    Acts as a reconciliation fallback in case the recovery.updated webhook was missed.
    """
    from sqlalchemy import select
    from app.models.user import User
    from app.redis import redis_pool
    from app.api.whoop import _ensure_fresh_token, _handle_recovery

    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                User.whoop_access_token.isnot(None),
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
            if local_now.hour != 8:
                continue

            # Guard: only send once per day per user
            today_key = f"whoop:morning_sent:{user.id}:{local_now.strftime('%Y-%m-%d')}"
            async with async_session() as db:
                if await redis_pool.get(today_key):
                    continue

                access_token = await _ensure_fresh_token(user, db)
                if not access_token:
                    continue

                print(f"[MORNING PULL] Fetching recovery for {user.name} (local={local_now.strftime('%H:%M')} {tz_name})")
                await _handle_recovery(user, access_token, db)

        except Exception as e:
            print(f"Morning WHOOP pull failed for {user.id}: {e}")
        await asyncio.sleep(0.5)


# For arq worker
async def proactive_task(ctx):
    await run_proactive_checkins()


async def morning_whoop_task(ctx):
    await run_morning_whoop_pull()


class WorkerSettings:
    functions = [proactive_task, morning_whoop_task]
    cron_jobs = [
        {"coroutine": "proactive_task", "minute": {0, 30}},        # every 30 min
        {"coroutine": "morning_whoop_task", "minute": {0, 30}},    # every 30 min, checks local time internally
    ]
    redis_settings = None  # set from env at runtime
