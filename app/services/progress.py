import json
import httpx
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models.progress_entry import ProgressEntry


async def parse_and_store_progress(user_id: UUID, text: str, db: AsyncSession) -> list[ProgressEntry]:
    """Use LLM to extract structured progress data from free text."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": (
                        "Extract workout/progress data from the user message. "
                        "Return a JSON array of objects. Each object: "
                        '{"category":"exercise|bodyweight|run|custom",'
                        '"label":"exercise name or metric",'
                        '"value":number,"unit":"kg|lbs|min|km|reps",'
                        '"sets":number_or_null,"reps":number_or_null,'
                        '"notes":"optional note or null"}\n'
                        "If no progress data found, return empty array []."
                        "Return ONLY valid JSON, nothing else."
                    )},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 300,
                "temperature": 0,
            },
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()

    try:
        # Strip markdown code block if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        entries_data = json.loads(raw)
    except (json.JSONDecodeError, IndexError):
        return []

    entries = []
    for e in entries_data:
        entry = ProgressEntry(
            user_id=user_id,
            category=e.get("category", "custom"),
            label=e.get("label", "unknown"),
            value=float(e.get("value", 0)),
            unit=e.get("unit", ""),
            sets=e.get("sets"),
            reps=e.get("reps"),
            notes=e.get("notes"),
        )
        db.add(entry)
        entries.append(entry)

    if entries:
        await db.commit()
    return entries
