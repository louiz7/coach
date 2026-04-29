import httpx
import json
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models.user import User
from app.models.coach_persona import CoachPersona
from app.models.training_plan import TrainingPlan
from app.models.progress_entry import ProgressEntry
from app.services.memory import get_conversation


async def build_system_prompt(
    user: User,
    persona: CoachPersona,
    db: AsyncSession,
    user_message: Optional[str] = None,
) -> str:
    """Build the full system prompt with user context."""
    from app.services.fitness_profile import get_or_create_profile, format_profile_for_prompt
    from app.services.memory_search import search_memories, format_memories_for_prompt

    # Base persona prompt
    prompt = persona.system_prompt + "\n\n"

    # User profile context
    prompt += f"""USER PROFILE:
- Sport: {user.sport or 'not specified'}
- Level: {user.fitness_level or 'not specified'}
- Goal: {user.goal or 'not specified'}
- Sports they want to improve in: {user.sports_focus or 'not specified'}
- Training frequency: {user.training_frequency or 'not specified'}x/week
- Injuries/limitations: {user.injuries or 'none'}
- Age: {user.age or 'not specified'}, Gender: {user.gender or 'not specified'}
- Weight: {user.weight_kg or 'not specified'}kg, Height: {user.height_cm or 'not specified'}cm
- Language: {user.language or 'de'}
"""

    # Current training plan
    result = await db.execute(
        select(TrainingPlan)
        .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
    )
    plan = result.scalar_one_or_none()
    if plan:
        prompt += f"\nCURRENT TRAINING PLAN:\n{plan.raw_text}\n"

    # Smart fitness profile (rule-based, ~150 tokens)
    try:
        profile = await get_or_create_profile(user.id, db)
        profile_str = format_profile_for_prompt(profile)
        if profile_str:
            prompt += "\n" + profile_str + "\n"
    except Exception as e:
        print(f"[build_system_prompt profile ERROR] {e}")

    # Vector memory search — only when we have a user message
    if user_message:
        try:
            memories = await search_memories(user.id, user_message, db, top_k=4)
            mem_str = format_memories_for_prompt(memories)
            if mem_str:
                prompt += "\n" + mem_str + "\n"
        except Exception as e:
            print(f"[build_system_prompt memory ERROR] {e}")
    # Rules
    prompt += """
RULES:
- Reply in 1-2 short sentences. Like texting, not an essay.
- No markdown formatting. No bullet points. No headers.
- Use the user's language (detect from their messages, default German).
- Be motivating. Reference their actual progress data when relevant.
- If the user logs a workout, acknowledge it and give brief feedback.
- If asked about an exercise, explain briefly and practically.
- For nutrition, give simple actionable tips, no meal plans.
- Never reveal you are an AI. You are their personal coach.
- If the user asks to change their plan, confirm and do it.
"""
    return prompt


async def call_llm(system_prompt: str, conversation: list[dict], max_tokens: int = 300) -> str:
    """Call OpenAI GPT-4o and return the response text."""
    messages = [{"role": "system", "content": system_prompt}] + conversation

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.85,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def classify_intent(text: str) -> str:
    """Quick intent classification via LLM."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": (
                        "Classify the user message into exactly one category. "
                        "Reply with ONLY the category name, nothing else.\n"
                        "Categories: PROGRESS_LOG, PLAN_REQUEST, EXERCISE_QUESTION, "
                        "NUTRITION_QUESTION, GENERAL"
                    )},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 10,
                "temperature": 0,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
