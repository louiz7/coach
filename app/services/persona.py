"""Persona auto-assignment helper.

Kano now uses a SINGLE coaching persona ("calm" — supportive, grounded,
direct without being pushy). The legacy multi-persona picker has been retired.

This helper still exists so the old call sites in onboarding don't have to
change much; it always assigns the one active persona.
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coach_persona import CoachPersona
from app.models.user import User


async def assign_persona_from_style(
    user: User,
    db: AsyncSession,
    coach_style: Optional[str] = None,      # back-compat, ignored
    coach_intensity: Optional[str] = None,  # back-compat, ignored
) -> Optional[CoachPersona]:
    """Assign the single active Kano persona to the user.

    `coach_style` / `coach_intensity` are accepted for backwards compatibility
    with old call sites but no longer affect persona selection.
    """
    # If user already has a persona AND it's still active, keep it
    if user.persona_id:
        res = await db.execute(
            select(CoachPersona).where(CoachPersona.id == user.persona_id)
        )
        existing = res.scalar_one_or_none()
        if existing and existing.is_active:
            return existing
        # else fall through to reassign

    res = await db.execute(
        select(CoachPersona).where(CoachPersona.is_active == True).limit(1)  # noqa: E712
    )
    persona = res.scalar_one_or_none()
    if persona:
        user.persona_id = persona.id
    return persona
