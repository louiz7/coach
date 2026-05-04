"""One-off: fix Elias's stuck onboarding, generate plan, send WHOOP link."""
import asyncio
from sqlalchemy import select
from app.database import async_session
from app.models.user import User, OnboardingState
from app.services.token import create_onboarding_token
from app.services import linq
from app.services.training_plan import generate_plan, chunk_plan_text
from app.services.memory import add_message
from app.config import settings


async def main():
    async with async_session() as db:
        r = await db.execute(select(User).where(User.phone == "+4917681341283"))
        user = r.scalar_one()
        print(f"Before: state={user.onboarding_state}, sports_focus={user.sports_focus!r}")

        # Fill missing sports_focus and mark fully onboarded
        user.sports_focus = "muscle building, strength training"
        user.onboarding_state = OnboardingState.DONE
        await db.commit()
        print("State -> DONE, sports_focus set")

        # Generate training plan
        print("Generating plan...")
        plan = await generate_plan(user, db)
        print(f"Plan generated ({len(plan.raw_text)} chars)")

        # Send plan in chunks
        chunks = chunk_plan_text(plan.raw_text)
        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(1.0)
            await linq.send_message(user.linq_chat_id, chunk)
            await add_message(user.id, "assistant", chunk, db)
        print(f"Plan sent in {len(chunks)} messages")

        # Send WHOOP connect link
        token = create_onboarding_token(user.phone)
        base_url = settings.ALLOWED_ORIGINS.split(",")[0].strip()
        whoop_url = f"{base_url}/whoop/connect?token={token}"
        whoop_msg = (
            "Das ist dein Plan Elias! \U0001f4aa\n\n"
            "Noch eine Sache \u2014 verbinde deinen WHOOP, damit ich dein Training "
            "an deine t\u00e4gliche Recovery anpassen kann:\n\n"
            f"{whoop_url}"
        )
        await linq.send_message(user.linq_chat_id, whoop_msg)
        await add_message(user.id, "assistant", whoop_msg, db)
        print("WHOOP link sent. Done.")


if __name__ == "__main__":
    asyncio.run(main())
