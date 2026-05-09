"""One-shot: regenerate Robi's plan with 5 days and send him the link."""
import asyncio
import sys
sys.path.insert(0, "/app")

from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.services.training_plan import generate_plan
from app.services import linq
from app.services.memory import add_message
from app.services.token import create_plan_token
from app.config import settings


async def main():
    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == "4f6445ad-ec42-4e63-8b82-37d6394ad9b7"))
        user = result.scalar_one()

        print(f"Regenerating plan for {user.name} (freq={user.training_frequency})...")
        await generate_plan(user, db, user_request="5 days of training, push/pull split, no leg days (running instead), goal: gain muscle and weight")

        token = create_plan_token(user.phone)
        base = settings.ALLOWED_ORIGINS.split(",")[0].strip()
        plan_url = f"{base}/plan?token={token}"

        msg = f"Done — updated your plan to 5 days 💪\n{plan_url}"
        await linq.send_message(user.linq_chat_id, msg)
        await add_message(user.id, "assistant", msg, db)
        print("Sent!")


asyncio.run(main())
