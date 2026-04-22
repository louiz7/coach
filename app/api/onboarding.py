from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Any
from app.database import get_db
from app.models.user import User, OnboardingState
from app.models.coach_persona import CoachPersona
from app.schemas.onboarding import OnboardingQuiz, PersonaSelect, PersonaResponse
from app.schemas.user import UserProfile
from app.utils.auth import get_current_user
from app.services import linq
from app.services.token import verify_onboarding_token
from app.services.training_plan import generate_plan

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


# ---------------------------------------------------------------------------
# Token-based form submission (no login required — identified by iMessage token)
# ---------------------------------------------------------------------------

class TokenFormSubmit(BaseModel):
    """
    Flexible schema for the iMessage-to-form onboarding submission.

    Only `token` is strictly required — everything else is optional for now
    so the frontend can evolve without breaking the backend.
    Known fields are applied when present; unknown extra fields are ignored.
    Once the frontend is finalised, add validation here.
    """

    token: str

    # Profile fields — all optional until frontend is locked
    sport: str | None = None
    fitness_level: str | None = None
    goal: str | None = None
    training_frequency: int | None = None
    injuries: str | None = None
    age: int | None = None
    gender: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None

    model_config = {"extra": "allow"}  # accept unknown fields gracefully


class TokenFormResponse(BaseModel):
    status: str
    message: str
    user_id: str


@router.post("/form-submit", response_model=TokenFormResponse)
async def form_submit(
    data: TokenFormSubmit,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit onboarding form data identified by the token sent in the iMessage link.

    The token encodes the user's phone number — no login/password required at
    this stage.  All profile fields are optional so the frontend can change
    the form without needing a backend deploy.
    """
    # Verify token and extract phone number
    try:
        payload = verify_onboarding_token(data.token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    phone = payload["phone"]

    # Look up user by phone
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please start the chat again.")

    # Apply whichever fields the frontend sent — skip None values
    KNOWN_FIELDS = [
        "sport", "fitness_level", "goal", "training_frequency",
        "injuries", "age", "gender", "weight_kg", "height_cm",
    ]
    for field in KNOWN_FIELDS:
        value = getattr(data, field, None)
        if value is not None:
            setattr(user, field, value)

    # Mark form as completed
    user.onboarding_state = OnboardingState.DONE

    await db.commit()
    await db.refresh(user)

    return TokenFormResponse(
        status="success",
        message="Profile saved! We'll be in touch via iMessage shortly.",
        user_id=str(user.id),
    )


@router.get("/verify-token")
async def verify_token(token: str, db: AsyncSession = Depends(get_db)):
    """
    Verify an onboarding token and return basic user info to pre-fill the form.
    Called by the frontend when the /start page loads.
    """
    try:
        payload = verify_onboarding_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    phone = payload["phone"]
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    return {
        "valid": True,
        "name": user.name,
        "goal": user.goal,
        "phone": user.phone,
    }


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
