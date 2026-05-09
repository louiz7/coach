"""One-off: notify all WHOOP-connected users that they need to reconnect
after a WHOOP app credential rotation, then clear their stale tokens.

Run inside the api container:
    docker compose exec -T api python -m scripts.whoop_reconnect_blast
"""
import asyncio
from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.services import linq
from app.services.token import create_onboarding_token


MSG_TEMPLATE = (
    "Quick heads-up: I had to migrate to a new WHOOP integration, so your "
    "current connection has been reset. Tap the link to reconnect (1 min) "
    "so I can keep adapting your plan to your recovery 🟢\n\n"
    "{url}"
)


async def main() -> None:
    async with async_session() as db:
        result = await db.execute(
            select(User).where(
                User.whoop_access_token.isnot(None),
                User.linq_chat_id.isnot(None),
                User.is_active == True,
            )
        )
        users = list(result.scalars().all())
        print(f"Found {len(users)} WHOOP-connected users to notify")

        for user in users:
            try:
                token = create_onboarding_token(user.phone)
                from app.config import settings
                url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/whoop/connect?token={token}"
                msg = MSG_TEMPLATE.format(url=url)
                await linq.send_message(user.linq_chat_id, msg)
                print(f"  ✓ messaged {user.name} ({user.phone})")
            except Exception as e:
                print(f"  ✗ failed for {user.name} ({user.phone}): {e}")
                # Don't clear tokens if we couldn't reach them
                continue

            # Clear stale tokens so the old app's keys can't accidentally be used
            user.whoop_access_token = None
            user.whoop_refresh_token = None
            user.whoop_user_id = None
            user.whoop_token_expires_at = None
            user.last_recovery_score = None
            user.last_hrv = None
            user.last_sleep_performance = None
            await db.commit()
            await asyncio.sleep(0.5)  # rate-limit linq sends

        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
