"""Persona auto-assignment helper.

Used by both the legacy web /form-submit endpoint and the in-chat onboarding
flow so that the style/intensity → persona mapping lives in one place.

Persona names map 1:1 to the four `coach_style` answers from onboarding:
`high_energy`, `calm`, `drill_sergeant`, `humor`.
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coach_persona import CoachPersona
from app.models.user import User


STYLE_TO_PERSONA = {
    "high_energy":    "high_energy",
    "calm":           "calm",
    "drill_sergeant": "drill_sergeant",
    "humor":          "humor",
}


async def assign_persona_from_style(
    user: User,
    db: AsyncSession,
    coach_style: Optional[str],
    coach_intensity: Optional[str],
) -> Optional[CoachPersona]:
    """Pick a persona based on style/intensity and assign it to the user.

    - `coach_intensity == "maximum"` → forces "drill_sergeant"
    - else style → preferred persona via `STYLE_TO_PERSONA`
    - falls back to "high_energy", then to the first active persona

    Only sets `user.persona_id` if it's currently None. Caller must commit.
    Returns the resolved persona (or None if seeding is missing).
    """
    if user.persona_id:
        # already set; just return it
        res = await db.execute(
            select(CoachPersona).where(CoachPersona.id == user.persona_id)
        )
        return res.scalar_one_or_none()

    if coach_intensity == "maximum":
        persona_name = "drill_sergeant"
    else:
        persona_name = STYLE_TO_PERSONA.get(coach_style or "", "high_energy")

    res = await db.execute(
        select(CoachPersona).where(
            CoachPersona.name == persona_name,
            CoachPersona.is_active == True,  # noqa: E712
        )
    )
    persona = res.scalar_one_or_none()

    if not persona:
        # Last-resort fallback: any active persona, so we never stall onboarding.
        res = await db.execute(
            select(CoachPersona).where(CoachPersona.is_active == True).limit(1)  # noqa: E712
        )
        persona = res.scalar_one_or_none()

    if persona:
        user.persona_id = persona.id

    return persona
