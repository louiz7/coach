"""Persona auto-assignment helper.

Used by both the legacy web /form-submit endpoint and the in-chat onboarding
flow so that the style/intensity → persona mapping lives in one place.
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coach_persona import CoachPersona
from app.models.user import User


_STYLE_TO_PERSONA = {
    "drill_sergeant": "Sergeant Max",
    "high_energy":    "Coach Alex",
    "humor":          "Coach Alex",
    "calm":           "Dr. Fit",
}


async def assign_persona_from_style(
    user: User,
    db: AsyncSession,
    coach_style: Optional[str],
    coach_intensity: Optional[str],
) -> Optional[CoachPersona]:
    """Pick a persona based on style/intensity and assign it to the user.

    - `coach_intensity == "maximum"` → forces "Sergeant Max"
    - else style → preferred persona via `_STYLE_TO_PERSONA`
    - falls back to first active persona

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
        preferred = "Sergeant Max"
    else:
        preferred = _STYLE_TO_PERSONA.get(coach_style or "")

    persona: Optional[CoachPersona] = None
    if preferred:
        res = await db.execute(
            select(CoachPersona).where(
                CoachPersona.name == preferred,
                CoachPersona.is_active == True,  # noqa: E712
            )
        )
        persona = res.scalar_one_or_none()

    if not persona:
        res = await db.execute(
            select(CoachPersona).where(CoachPersona.is_active == True).limit(1)  # noqa: E712
        )
        persona = res.scalar_one_or_none()

    if persona:
        user.persona_id = persona.id

    return persona
