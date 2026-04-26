import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from arq.cron import cron
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


def _build_redis_settings():
    import os
    from arq.connections import RedisSettings
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    print(f"[arq worker] REDIS_URL = {redis_url}", flush=True)
    _url = redis_url.replace("redis://", "")
    _host, _port = (_url.split(":") + ["6379"])[:2]
    return RedisSettings(host=_host, port=int(_port))


class WorkerSettings:
    functions = [proactive_task, morning_whoop_task, weekly_coach_notes_task]
    cron_jobs = [
        cron(proactive_task, minute={0, 30}),
        cron(morning_whoop_task, minute={0, 30}),
        cron(weekly_coach_notes_task, weekday=6, hour=0, minute=0),   # Sunday 00:00 UTC
    ]
    redis_settings = _build_redis_settings()


if __name__ == "__main__":
    # Fallback when invoked via `python -m`
    from arq import run_worker as _run_worker
    _run_worker(WorkerSettings)
