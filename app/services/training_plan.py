import json
import httpx
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.config import settings
from app.models.user import User
from app.models.training_plan import TrainingPlan


async def generate_plan(user: User, db: AsyncSession, modification: str = None) -> TrainingPlan:
    """Generate a training plan via LLM and store it."""
    profile = (
        f"Sport: {user.sport}, Level: {user.fitness_level}, Goal: {user.goal}, "
        f"Frequency: {user.training_frequency}x/week, "
        f"Injuries: {user.injuries or 'none'}, "
        f"Age: {user.age}, Gender: {user.gender}, "
        f"Weight: {user.weight_kg}kg, Height: {user.height_cm}cm"
    )

    system_msg = (
        "Generate a weekly training plan as JSON.\n"
        f"User profile: {profile}\n"
    )
    if modification:
        system_msg += f"Modification request: {modification}\n"

    system_msg += (
        'Format: {"days":[{"day":"Monday","focus":"Chest/Triceps",'
        '"exercises":[{"name":"Bench Press","sets":4,"reps":"8-10",'
        '"rest_seconds":90,"notes":"Controlled negative"}]}]}\n'
        "Rules:\n"
        "- Match the user's sport, level, and goal\n"
        "- Respect injury limitations\n"
        f"- {user.training_frequency} training days per week\n"
        "- Include warmup notes\n"
        "- Be specific with rep ranges and rest times\n"
        "- Return ONLY valid JSON, nothing else."
    )

    async with httpx.AsyncClient(timeout=30) as client:
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
            },
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()

    # Parse JSON
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    plan_data = json.loads(raw)

    # Generate plain text version
    text_lines = ["Dein Trainingsplan:\n"]
    for day in plan_data.get("days", []):
        text_lines.append(f"{day['day']} — {day['focus']}")
        for ex in day.get("exercises", []):
            line = f"  {ex['name']}: {ex['sets']}x{ex['reps']}"
            if ex.get("rest_seconds"):
                line += f" ({ex['rest_seconds']}s Pause)"
            if ex.get("notes"):
                line += f" — {ex['notes']}"
            text_lines.append(line)
        text_lines.append("")
    raw_text = "\n".join(text_lines)

    # Deactivate old plans
    await db.execute(
        update(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
        .values(is_current=False)
    )

    # Store new plan
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
