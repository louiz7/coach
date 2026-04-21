import asyncio
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


# For arq worker
async def proactive_task(ctx):
    await run_proactive_checkins()


class WorkerSettings:
    functions = [proactive_task]
    cron_jobs = [
        {"coroutine": "proactive_task", "minute": {0, 30}},  # every 30 min
    ]
    redis_settings = None  # set from env at runtime
