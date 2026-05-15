"""Image-based food calorie analysis service.

Sends a food photo URL (from Linq webhook media part) to gpt-4o-mini via
OpenRouter vision API, parses the structured JSON response, persists a
FoodLogEntry, and returns a context dict for the intent handler.
"""
import json
import httpx
from datetime import datetime, date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


async def analyze_food_image(
    image_url: str,
    user_id: UUID,
    db: AsyncSession,
    caption: str = "",
    language: str = "en",
) -> dict:
    """Call gpt-4o-mini (vision) via OpenRouter to analyse a food photo.

    Returns a dict:
        {
            "description": str,
            "meal_type": str,          # breakfast|lunch|dinner|snack|unknown
            "estimated_calories": int,
            "items": [{"name": str, "calories": int}, ...],
        }
    Also persists a FoodLogEntry row.
    """
    from app.models.food_log import FoodLogEntry

    lang_note = "Reply in German." if language == "de" else "Reply in English."

    prompt = (
        "You are a nutrition expert. Analyse this food photo and estimate the calories.\n"
        f"{lang_note}\n\n"
        "Reply with ONLY a valid JSON object (no markdown, no extra text):\n"
        "{\n"
        '  "description": "1-sentence description of what is on the plate",\n'
        '  "meal_type": "breakfast|lunch|dinner|snack|unknown",\n'
        '  "estimated_calories": <integer — total calories for the whole serving>,\n'
        '  "items": [\n'
        '    {"name": "item name", "calories": <integer>},\n'
        "    ...\n"
        "  ]\n"
        "}\n\n"
        "Be specific but concise. Always provide a numeric estimate, even if uncertain."
    )
    if caption:
        prompt += f"\n\nUser caption: {caption}"

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }
    ]

    result: dict = {
        "description": "Could not analyse image",
        "meal_type": "unknown",
        "estimated_calories": 0,
        "items": [],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                json={
                    "model": "openai/gpt-4o-mini",
                    "messages": messages,
                    "max_tokens": 400,
                    "temperature": 0.2,
                },
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()

        # Strip markdown fences defensively
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
            raw = raw.rsplit("```", 1)[0].strip()

        parsed = json.loads(raw)
        result.update(parsed)
    except Exception as e:
        print(f"[food_log.analyze_food_image LLM ERROR] {e}")

    # Persist to DB
    try:
        entry = FoodLogEntry(
            user_id=user_id,
            image_url=image_url,
            description=result.get("description"),
            estimated_calories=int(result.get("estimated_calories") or 0),
            items_json=json.dumps(result.get("items", [])),
            meal_type=result.get("meal_type", "unknown"),
            recorded_at=datetime.utcnow(),
        )
        db.add(entry)
        await db.commit()
    except Exception as e:
        print(f"[food_log.analyze_food_image DB ERROR] {e}")

    return result


async def get_today_food_summary(user_id: UUID, db: AsyncSession) -> str | None:
    """Return a compact summary of today's logged meals for system-prompt injection."""
    try:
        from sqlalchemy import select, func
        from app.models.food_log import FoodLogEntry

        today = date.today()
        result = await db.execute(
            select(FoodLogEntry)
            .where(
                FoodLogEntry.user_id == user_id,
                func.date(FoodLogEntry.recorded_at) == today,
            )
            .order_by(FoodLogEntry.recorded_at)
        )
        entries = result.scalars().all()
        if not entries:
            return None

        total_kcal = sum(e.estimated_calories or 0 for e in entries)
        meals = []
        for e in entries:
            if e.description:
                kcal = e.estimated_calories or 0
                meals.append(f"  • {e.description} (~{kcal} kcal)")

        lines = [f"Today's food log — {total_kcal} kcal total:"]
        lines.extend(meals)
        return "\n".join(lines)
    except Exception as e:
        print(f"[food_log.get_today_food_summary ERROR] {e}")
        return None
