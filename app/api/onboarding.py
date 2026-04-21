from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.user import User
from app.models.coach_persona import CoachPersona
from app.schemas.onboarding import OnboardingQuiz, PersonaSelect, PersonaResponse
from app.schemas.user import UserProfile
from app.utils.auth import get_current_user
from app.services import linq
from app.services.training_plan import generate_plan

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


@router.post("/quiz", response_model=UserProfile)
async def submit_quiz(
    data: OnboardingQuiz,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user.sport = data.sport
    user.fitness_level = data.fitness_level
    user.goal = data.goal
    user.training_frequency = data.training_frequency
    user.injuries = data.injuries
    user.age = data.age
    user.gender = data.gender
    user.weight_kg = data.weight_kg
    user.height_cm = data.height_cm
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/personas", response_model=list[PersonaResponse])
async def list_personas(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CoachPersona).where(CoachPersona.is_active == True)
    )
    return result.scalars().all()


@router.post("/select-persona", response_model=UserProfile)
async def select_persona(
    data: PersonaSelect,
    bg: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify persona exists
    result = await db.execute(
        select(CoachPersona).where(CoachPersona.id == data.persona_id)
    )
    persona = result.scalar_one_or_none()
    if not persona:
        raise HTTPException(404, "Persona not found")

    user.persona_id = data.persona_id
    user.onboarding_complete = True

    # Assign a Linq Blue number
    numbers = await linq.list_phone_numbers()
    if not numbers:
        raise HTTPException(500, "No phone numbers available")
    user.pool_number = numbers[0]["phone_number"]

    await db.commit()
    await db.refresh(user)

    # Background: create chat, set contact card, generate plan, send welcome
    bg.add_task(
        _complete_onboarding,
        str(user.id),
        user.pool_number,
        user.phone,
        persona.name,
        persona.avatar_url or "",
    )

    return user


async def _complete_onboarding(
    user_id: str, pool_number: str, user_phone: str, persona_name: str, avatar_url: str
):
    """Background task: set up contact card, create chat, generate plan, send welcome."""
    from app.database import async_session
    from app.models.user import User
    from app.services.memory import add_message
    from sqlalchemy import select
    import asyncio

    # Set contact card
    name_parts = persona_name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    try:
        await linq.setup_contact_card(pool_number, first_name, last_name, avatar_url)
    except Exception:
        pass  # non-critical

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()

        # Create chat with welcome message
        welcome = f"Hey {user.name}! Ich bin {persona_name}, dein neuer Coach 💪 Lass uns loslegen!"
        try:
            chat = await linq.create_chat(pool_number, user_phone, welcome)
            user.linq_chat_id = chat.get("id")
            await db.commit()

            # Share contact card
            if user.linq_chat_id:
                await linq.share_contact_card(user.linq_chat_id)

            await add_message(user.id, "assistant", welcome, db)
        except Exception as e:
            print(f"Failed to create chat: {e}")
            return

        # Generate training plan
        try:
            plan = await generate_plan(user, db)
            # Send plan summary (split into chunks)
            plan_lines = plan.raw_text.split("\n\n")
            for chunk in plan_lines:
                chunk = chunk.strip()
                if chunk:
                    await asyncio.sleep(1.5)
                    await linq.send_message(user.linq_chat_id, chunk)
                    await add_message(user.id, "assistant", chunk, db)

            await asyncio.sleep(1)
            outro = "Das ist dein Plan! Du kannst ihn jederzeit anpassen — schreib mir einfach 🙌"
            await linq.send_message(user.linq_chat_id, outro)
            await add_message(user.id, "assistant", outro, db)
        except Exception as e:
            print(f"Failed to generate plan: {e}")
