import httpx
import json
from datetime import date
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


def _get_today_workout(plan) -> Optional[str]:
    """Extract today's workout block from plan_json using weekday-name matching."""
    try:
        from app.services.training_plan import get_workout_for_today
        weekday = date.today().strftime("%A")  # e.g. "Monday"
        day = get_workout_for_today(plan.plan_json, weekday)
        if not day:
            return None
        label = day.get("focus") or day.get("day") or weekday
        exercises = day.get("exercises") or []
        if not exercises:
            return f"{label}: rest day"
        lines = [label + ":"]
        for ex in exercises[:8]:
            name = ex.get("name") or ex.get("exercise") or ""
            sets = ex.get("sets", "")
            reps = ex.get("reps", "")
            note = f"{sets}x{reps}" if sets and reps else ""
            lines.append(f"  {name} {note}".strip())
        return "\n".join(lines)
    except Exception:
        return None


async def build_system_prompt(
    user: User,
    persona: CoachPersona,
    db: AsyncSession,
    user_message: Optional[str] = None,
    intents: Optional[list[str]] = None,
) -> str:
    """Build the full system prompt with user context."""
    from app.services.fitness_profile import get_or_create_profile, format_profile_for_prompt
    from app.services.memory_search import search_memories, format_memories_for_prompt
    from app.services.research_rag import search_research, format_research_for_prompt

    # Base persona prompt
    prompt = persona.system_prompt + "\n\n"

    # ── User profile (compact one-liner, ~60 tokens) ─────────────────────
    profile_parts = [
        user.goal or "general fitness",
        user.sports_focus or "general",
        user.fitness_level or "intermediate",
        f"{user.training_frequency or '?'}x/week",
        f"{user.coach_intensity or 'moderate'} intensity",
    ]
    if user.equipment_access:
        profile_parts.append(user.equipment_access)
    if user.age or user.weight_kg or user.height_cm:
        metrics = " ".join(filter(None, [
            f"{user.age}yo" if user.age else None,
            f"{user.weight_kg}kg" if user.weight_kg else None,
            f"{user.height_cm}cm" if user.height_cm else None,
        ]))
        if metrics:
            profile_parts.append(metrics)
    prompt += "USER: " + " | ".join(profile_parts) + "\n"
    if user.injuries:
        prompt += f"Injuries: {user.injuries}\n"
    if user.current_schedule_notes:
        prompt += f"Current routine: {user.current_schedule_notes[:300]}\n"

    # WHOOP biometrics (cached from last webhook)
    if user.whoop_access_token:
        whoop_lines = []
        if user.last_recovery_score is not None:
            if user.last_recovery_score >= 67:
                emoji = "🟢"
            elif user.last_recovery_score >= 34:
                emoji = "🟡"
            else:
                emoji = "🔴"
            whoop_lines.append(f"- Recovery: {emoji} {user.last_recovery_score}%")
        if user.last_hrv is not None:
            whoop_lines.append(f"- HRV: {user.last_hrv:.0f}ms")
        if user.last_sleep_performance is not None:
            whoop_lines.append(f"- Sleep performance: {user.last_sleep_performance}%")
        if whoop_lines:
            prompt += "\nLATEST WHOOP DATA (use this when the user asks about their data/recovery/sleep):\n"
            prompt += "\n".join(whoop_lines) + "\n"
        else:
            prompt += "\nWHOOP CONNECTED: Yes — but no biometric data cached yet (data arrives via webhooks when user syncs their WHOOP).\n"

    # ── Training plan — only inject when the message is plan-related ───────
    _PLAN_INTENTS = {"PLAN_REQUEST", "MODIFY_PLAN", "VIEW_PLAN", "NEW_PLAN", "PROGRESS_LOG", "WHOOP_DATA", "PERFORMANCE_DATA"}
    if intents and _PLAN_INTENTS.intersection(intents):
        result = await db.execute(
            select(TrainingPlan)
            .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
        )
        plan = result.scalar_one_or_none()
        if plan:
            # Try to inject today's workout only (~80 tokens) instead of full plan
            today_block = _get_today_workout(plan)
            if today_block:
                prompt += f"\nTODAY'S WORKOUT:\n{today_block}\n"
            else:
                # Fallback: full plan but capped at 1200 chars
                prompt += f"\nCURRENT TRAINING PLAN (summary):\n{plan.raw_text[:1200]}\n"

    # ── Recent workout history — inject whenever user asks about past performance ──
    # Without this the LLM has no data and says "no workouts logged" even when
    # progress_entries rows exist in the DB.
    _HISTORY_INTENTS = {"PERFORMANCE_DATA", "PROGRESS_LOG"}
    if intents and _HISTORY_INTENTS.intersection(intents):
        try:
            from datetime import datetime, timedelta
            from collections import defaultdict
            cutoff = datetime.utcnow() - timedelta(days=30)
            pe_result = await db.execute(
                select(ProgressEntry)
                .where(
                    ProgressEntry.user_id == user.id,
                    ProgressEntry.category == "exercise",
                    ProgressEntry.recorded_at >= cutoff,
                )
                .order_by(ProgressEntry.recorded_at.desc())
                .limit(60)
            )
            entries = pe_result.scalars().all()
            if entries:
                # Group by date, then by exercise within each date
                by_date: dict = defaultdict(lambda: defaultdict(list))
                for e in entries:
                    day = e.recorded_at.strftime("%Y-%m-%d") if e.recorded_at else "unknown"
                    key = e.label or "?"
                    val = str(e.value or "")
                    if e.unit:
                        val += e.unit
                    if e.sets and e.reps:
                        val += f" {e.sets}x{e.reps}"
                    by_date[day][key].append(val)

                lines = ["RECENT WORKOUT HISTORY (from logs):"]
                for day in sorted(by_date.keys(), reverse=True)[:7]:  # last 7 distinct days
                    lines.append(f"  {day}:")
                    for exname, vals in by_date[day].items():
                        lines.append(f"    {exname}: {', '.join(vals)}")
                prompt += "\n" + "\n".join(lines) + "\n"
        except Exception as e:
            print(f"[build_system_prompt progress_entries ERROR] {e}")

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

    # Scientific RAG — only on EXERCISE_QUESTION / NUTRITION_QUESTION to keep
    # token cost minimal. ~3 chunks × ~400 words ≈ 600-900 tokens injected.
    if user_message and intents:
        rag_intents = {"EXERCISE_QUESTION", "NUTRITION_QUESTION"}
        if rag_intents.intersection(intents):
            try:
                # Categories are topic-based (mechanisms_hypertrophy,
                # rt_prescription, …, protein_intake). Pure nutrition asks
                # are best served by the protein_intake corpus; exercise
                # asks should search across all training-related corpora.
                # Letting cosine similarity decide is robust → no hint.
                category_hint = None
                if "NUTRITION_QUESTION" in intents and "EXERCISE_QUESTION" not in intents:
                    category_hint = "protein_intake"
                research = await search_research(
                    user_message, db, category_hint=category_hint, top_k=2
                )
                rag_str = format_research_for_prompt(research)
                if rag_str:
                    prompt += "\n" + rag_str + "\n"
            except Exception as e:
                print(f"[build_system_prompt research ERROR] {e}")
    # Daily food log summary (only if user has logged food today)
    try:
        from app.services.food_log import get_today_food_summary
        food_summary = await get_today_food_summary(user.id, db)
        if food_summary:
            prompt += f"\n{food_summary}\n"
    except Exception as e:
        print(f"[build_system_prompt food_log ERROR] {e}")

    # Rules
    prompt += """
CAPABILITIES — things you CAN already do (never deny these):
- Log workouts: when the user mentions completing an exercise (sets, reps, weight, distance) you HAVE already logged it automatically. Confirm it was saved.
- Track progress over time: every logged workout is stored. They can ask "what did I bench last time" and you'll retrieve it.
- Build and update training plans: you can create or modify their plan instantly via text. After doing so, always share the plan URL.
- Send their plan as a calendar: they can add it to Apple/Google calendar. Tell them to say "send calendar link".
- Analyse food photos: they can send a photo of any meal and you'll estimate the calories and log it.
- Track daily calorie intake: all food photos are logged. You'll reference today's total in coaching.
- WHOOP integration: if connected, you see live recovery, HRV, and sleep data.
- Remember past conversations: key moments are stored in memory and recalled when relevant.

FEATURES NOT YET AVAILABLE (mention warmly if asked, never be dismissive):
- Connecting other wearables (Garmin, Apple Watch, etc.) — coming soon.
- Automatic workout detection from wearables — coming soon.
- Macro/calorie targets per meal or daily goals — not yet implemented.

RULES:
1. Max 1-2 short sentences per reply. Texting, not an essay. If you need more, use [MSG] to split into separate bubbles.
2. Lowercase always.
3. No markdown. No bullet points. No headers. No dashes (— - –).
4. Detect and match the user's language.
5. Be direct and motivating. Reference their actual data when relevant.
6. When the user logs a workout, ALWAYS confirm it was saved (e.g. "logged") and give one line of feedback.
7. Exercise questions: brief, practical, one tip max.
8. Nutrition: simple actionable tip, no meal plans.
9. Never reveal you are an AI.
10. Max one question per message, never stack multiple questions.
11. No opening fluff: no "of course!", "sure!", "great question!", "absolutely!".
12. If something is wrong, correct it. Don't just validate.
13. No safety disclaimers for normal fitness or nutrition questions.
14. No excessive hedging.
15. Non-fitness topics: decline warmly in one sentence.
16. If the CONTEXT block says "PLAN UPDATED" or "NEW PLAN CREATED" — you already did the action. Confirm and include the URL. Never say you "can't modify plans through text".
17. If the CONTEXT block says "User just logged" — confirm it was saved. Never say you can only remember it "for this conversation".
18. Never be rude or dismissive. If a feature isn't available yet, say so kindly.
19. Never contradict a CONTEXT or INSTRUCTION block — those override anything said earlier in the conversation.
20. If asked about pricing, cost, or subscription: Kano costs €3.49/week, billed weekly. 7-day free trial, no charge upfront. Cancel anytime. Never state any other price.
21. If the user asks about cancelling, cancelling their plan, stopping texts, unsubscribing, or leaving: do NOT explain how to cancel or say "you can cancel by...". Simply say "i'll send you the link" and NOTHING else. The system handles it separately.
"""
    return prompt


async def call_llm(system_prompt: str, conversation: list[dict], max_tokens: int = 300) -> str:
    """Call OpenAI GPT-4o and return the response text."""
    messages = [{"role": "system", "content": system_prompt}] + conversation

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            json={
                "model": "deepseek/deepseek-v4-flash",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.85,
            },
        )
        if response.status_code != 200:
            print(f"[call_llm] OpenRouter error {response.status_code}: {response.text[:500]}", flush=True)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


