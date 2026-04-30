"""Training plan generation + modification.

- Uses the new beta-phase onboarding fields (goal, sports_focus, coach_style,
  coach_intensity, training_frequency, challenge).
- All output is English.
- If the user already has an active plan, the LLM is instructed to preserve
  what works and only patch what the user asked to change.
"""
import json
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.training_plan import TrainingPlan
from app.models.user import User


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _profile_string(user: User) -> str:
    """Build a compact profile string from the user record."""
    parts = [
        f"Name: {user.name}",
        f"Goal: {user.goal or 'general fitness'}",
        f"Sports/activities to improve: {user.sports_focus or 'general fitness'}",
        f"Training frequency: {user.training_frequency or 3}x per week",
        f"Coach style preference: {user.coach_style or 'balanced'}",
        f"Intensity preference: {user.coach_intensity or 'moderate'}",
    ]
    if user.challenge:
        parts.append(f"Biggest challenge: {user.challenge}")
    if user.injuries:
        parts.append(f"Injuries / limitations: {user.injuries}")
    if user.age:
        parts.append(f"Age: {user.age}")
    if user.gender:
        parts.append(f"Gender: {user.gender}")
    if user.weight_kg:
        parts.append(f"Weight: {user.weight_kg}kg")
    if user.height_cm:
        parts.append(f"Height: {user.height_cm}cm")
    return " | ".join(parts)


def _render_plan_text(plan_data: dict) -> str:
    """Render plan JSON into a readable English iMessage-friendly text."""
    lines = ["Your Training Plan 💪\n"]
    for day in plan_data.get("days", []):
        focus = day.get("focus", "")
        header = f"📅 {day.get('day', '')}"
        if focus:
            header += f" — {focus}"
        lines.append(header)
        for ex in day.get("exercises", []):
            line = f"  • {ex.get('name', '')}: {ex.get('sets', '?')}x{ex.get('reps', '?')}"
            if ex.get("rest_seconds"):
                line += f" ({ex['rest_seconds']}s rest)"
            if ex.get("notes"):
                line += f" — {ex['notes']}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


async def _get_active_plan(user: User, db: AsyncSession) -> Optional[TrainingPlan]:
    result = await db.execute(
        select(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
        .order_by(TrainingPlan.created_at.desc())
    )
    return result.scalars().first()


# ─── Public API ──────────────────────────────────────────────────────────────

async def generate_plan(
    user: User,
    db: AsyncSession,
    user_request: Optional[str] = None,
    modification: Optional[str] = None,
) -> TrainingPlan:
    """Generate or modify a training plan via LLM and persist it.

    - If `user` already has an active plan, the prompt instructs the LLM to
      preserve what works and apply the user's request as a delta.
    - `user_request` (preferred) and the legacy `modification` kwarg are
      treated equivalently.
    """
    request_text = (user_request or modification or "").strip() or None
    existing = await _get_active_plan(user, db)
    is_modification = existing is not None

    profile = _profile_string(user)
    freq = user.training_frequency or 3

    system_msg = (
        "You are a world-class fitness coach generating a structured weekly "
        "training plan. Output ONLY valid JSON, nothing else.\n\n"
        f"USER PROFILE: {profile}\n\n"
    )

    if is_modification:
        try:
            existing_json = json.dumps(existing.plan_json, ensure_ascii=False)
        except Exception:
            existing_json = "{}"
        system_msg += (
            "MODIFY THE FOLLOWING EXISTING PLAN. Preserve the structure, "
            "exercises, and progression that already work. Only change what "
            "the user explicitly asks to change. Keep day count and overall "
            "philosophy intact unless they ask otherwise.\n\n"
            f"CURRENT PLAN:\n{existing_json}\n\n"
        )
        if request_text:
            system_msg += f"USER MODIFICATION REQUEST: {request_text}\n\n"
    else:
        if request_text:
            system_msg += f"USER PREFERENCE FOR THIS PLAN: {request_text}\n\n"

    system_msg += (
        "OUTPUT FORMAT (strict JSON):\n"
        '{"days":[{"day":"Monday","focus":"Push (Chest/Shoulders/Triceps)",'
        '"exercises":[{"name":"Bench Press","sets":4,"reps":"8-10",'
        '"rest_seconds":90,"notes":"Controlled tempo"}]}]}\n\n'
        "RULES:\n"
        f"- Exactly {freq} training days per week (the rest are rest days — do not include rest days in the JSON).\n"
        "- Tailor every choice to the user's goal and sports focus.\n"
        "- Match volume and intensity to the user's intensity preference.\n"
        "- Be specific with rep ranges and rest times.\n"
        "- Use English day names (Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday).\n"
        "- Each day must have a clear `focus` and at least 4 exercises.\n"
        "- Return ONLY the JSON object — no prose, no markdown code fences."
    )

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": "Generate the plan now."},
                ],
                "max_tokens": 2000,
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()

    # Strip code fences defensively (response_format should prevent this)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    plan_data = json.loads(raw)

    raw_text = _render_plan_text(plan_data)

    # Deactivate any current plan
    await db.execute(
        update(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
        .values(is_current=False)
    )

    # Insert new plan
    plan = TrainingPlan(
        user_id=user.id,
        plan_json=plan_data,
        raw_text=raw_text,
        is_current=True,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


def get_workout_for_today(plan_json: dict, weekday_name: str) -> Optional[dict]:
    """Return the day dict matching the given weekday name, or None (rest day)."""
    if not plan_json:
        return None
    for day in plan_json.get("days", []):
        if str(day.get("day", "")).strip().lower() == weekday_name.strip().lower():
            return day
    return None


def chunk_plan_text(text: str, max_len: int = 1200) -> list[str]:
    """Split a plan's raw_text into iMessage-friendly chunks at day boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    # Split on blank lines (which separate days in our renderer)
    blocks = text.split("\n\n")
    for block in blocks:
        candidate = (current + "\n\n" + block).strip() if current else block
        if len(candidate) > max_len and current:
            chunks.append(current.strip())
            current = block
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks
