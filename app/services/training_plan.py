"""Training plan generation + modification.

- Uses the new beta-phase onboarding fields (goal, sports_focus, coach_style,
  coach_intensity, training_frequency, challenge).
- All output is English.
- If the user already has an active plan, the LLM is instructed to preserve
  what works and only patch what the user asked to change.
"""
import asyncio
import json
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.training_plan import TrainingPlan
from app.models.user import User
from app.services.research_rag import search_research, format_research_for_prompt


# Categories to pull from research_chunks when generating plans.
# rt_prescription = sets/reps/intensity prescription
# frequency_load_tempo_rest_failure = frequency, load, RPE, rest, failure
# mechanisms_hypertrophy = useful when goal involves muscle gain
_PLAN_RESEARCH_CATEGORIES = (
    "rt_prescription",
    "frequency_load_tempo_rest_failure",
    "mechanisms_hypertrophy",
)


def _build_research_queries(user: User, request_text: Optional[str]) -> list[tuple[str, Optional[str]]]:
    """Build (query, category_hint) tuples to pull diverse, relevant research."""
    goal = (user.goal or "general fitness").strip()
    sports = (user.sports_focus or "").strip()
    intensity = (user.coach_intensity or "moderate").strip()
    freq = user.training_frequency if user.training_frequency is not None else 3
    freq = max(freq, 2)

    q_prescription = (
        f"Optimal sets, reps, and RPE prescription for {goal}. "
        f"Training frequency {freq}x per week. "
        f"Intensity preference: {intensity}."
    )
    q_load = (
        f"Load, rest periods, and proximity to failure for {goal}"
        + (f" while focusing on {sports}" if sports else "")
        + "."
    )
    queries: list[tuple[str, Optional[str]]] = [
        (q_prescription, "rt_prescription"),
        (q_load, "frequency_load_tempo_rest_failure"),
    ]
    # If hypertrophy-related goal, also pull mechanism evidence
    goal_lower = goal.lower()
    if any(k in goal_lower for k in ("muscle", "hyper", "mass", "size", "build", "strength")):
        queries.append((f"Mechanisms driving hypertrophy and progressive overload for {goal}.", "mechanisms_hypertrophy"))
    # If user typed a free-text request (mod or preference), also search broad
    if request_text:
        queries.append((request_text, None))
    return queries


async def _gather_research_for_plan(
    user: User,
    request_text: Optional[str],
    db: AsyncSession,
    per_query_top_k: int = 2,
    overall_cap: int = 6,
) -> str:
    """Run research queries in parallel, dedupe, format for prompt."""
    queries = _build_research_queries(user, request_text)
    try:
        results = await asyncio.gather(
            *[search_research(q, db, category_hint=cat, top_k=per_query_top_k) for q, cat in queries],
            return_exceptions=True,
        )
    except Exception as e:
        print(f"[_gather_research_for_plan ERROR] {e}")
        return ""

    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        for chunk in res:
            key = (chunk.get("title", ""), chunk.get("chunk_text", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            merged.append(chunk)
    # Sort by similarity desc, cap
    merged.sort(key=lambda c: c.get("sim", 0), reverse=True)
    merged = merged[:overall_cap]
    return format_research_for_prompt(merged)


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
    if getattr(user, 'equipment_access', None):
        parts.append(f"Equipment access: {user.equipment_access}")
    if getattr(user, 'current_schedule_notes', None):
        parts.append(f"Current training routine: {user.current_schedule_notes}")
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
            if ex.get("rpe"):
                line += f" @RPE{ex['rpe']}"
            if ex.get("rest_seconds"):
                line += f" ({ex['rest_seconds']}s rest)"
            if ex.get("notes"):
                line += f" — {ex['notes']}"
            lines.append(line)
        lines.append("")
    
    # Add progression tips if provided
    if "progression_tips" in plan_data:
        lines.append("📈 Progression:\n")
        for tip in plan_data.get("progression_tips", []):
            lines.append(f"  • {tip}")
        lines.append("")
    
    # Add motivational closing if provided
    if "motivational_note" in plan_data:
        lines.append(f"💪 {plan_data.get('motivational_note', '')}")
    
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
    freq = user.training_frequency if user.training_frequency is not None else 3
    freq = max(freq, 2)  # minimum 2 training days

    # Pull scientific research relevant to this user's goal/intensity/sports
    research_block = await _gather_research_for_plan(user, request_text, db)

    system_msg = (
        "You are a world-class fitness coach generating a structured weekly "
        "training plan grounded in current sport-science research. "
        "Output ONLY valid JSON, nothing else.\n\n"
        f"USER PROFILE: {profile}\n\n"
    )

    if research_block:
        system_msg += research_block + "\n\n"
        system_msg += (
            "Use the SCIENTIFIC CONTEXT above to ground your decisions on:\n"
            "• exercise SELECTION (compound vs isolation, technique cues)\n"
            "• REP RANGES (e.g. ~5-8 for strength, ~6-12 for hypertrophy, ~12-20 for endurance)\n"
            "• SET VOLUME per muscle group per week\n"
            "• REST periods between sets\n"
            "• RPE / proximity to failure (most working sets RPE 7-9; reserve RPE 9-10 for top sets)\n"
            "• weekly FREQUENCY per muscle group\n"
            "Apply the evidence — don't quote it.\n\n"
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
        '"exercises":[{"name":"Bench Press","sets":4,"reps":"8-10","rpe":8,'
        '"rest_seconds":120,"notes":"Controlled tempo, 2s eccentric"}]}],'
        '"progression_tips":["..."],"motivational_note":"..."}\n\n'
        "PLAN REQUIREMENTS:\n"
        f"- Exactly {freq} training days per week (rest days not included).\n"
        "- Total ~1200-1500 words when rendered.\n"
        "- Each day has a clear focus and 5-7 exercises.\n"
        "- EVERY exercise MUST include: name, sets (int), reps (string like '8-10'), rpe (int 6-10), rest_seconds (int), notes.\n"
        "- Choose RPE based on the research above: compound lifts top sets RPE 8-9, accessory work RPE 7-8.\n"
        "- Include 1-2 progression tips per week (e.g. 'Week 3: add 2-3 reps to top sets').\n"
        "- Add 1 motivational closing line (e.g. 'Trust the process — consistency wins').\n"
        "- Tailor volume and intensity to the user's goal and intensity preference.\n"
        "- Match exercise selection to their sports focus.\n"
        "- Use English day names (Monday–Sunday).\n"
        "- Do NOT include macros, stretches, or theory — just the workout structure.\n"
        "- Return ONLY the JSON object — no prose, no code fences."
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

    # Normalize exercise names against MuscleWiki catalog (zero extra tokens)
    try:
        from app.services.exercise_normalizer import normalize_plan_exercises
        plan_data = await normalize_plan_exercises(plan_data)
    except Exception as _norm_err:
        print(f"[training_plan] exercise normalization skipped: {_norm_err}")

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


def render_today_workout(plan_json: dict, weekday: str = None) -> Optional[str]:
    """Return a formatted iMessage-ready string for today's workout, or None on rest day."""
    from datetime import datetime
    weekday = weekday or datetime.now().strftime("%A")  # e.g. "Monday"
    day = get_workout_for_today(plan_json, weekday)
    if not day:
        return None
    focus = day.get("focus", "")
    header = f"📅 {weekday}{(' — ' + focus) if focus else ''}"
    lines = [header]
    for ex in day.get("exercises", []):
        line = f"  • {ex.get('name', '')}: {ex.get('sets', '?')}x{ex.get('reps', '?')}"
        if ex.get("rpe"):
            line += f" @RPE{ex['rpe']}"
        if ex.get("rest_seconds"):
            line += f" ({ex['rest_seconds']}s rest)"
        if ex.get("notes"):
            line += f" — {ex['notes']}"
        lines.append(line)
    return "\n".join(lines)


def chunk_plan_text(text: str, max_len: int = 800) -> list[str]:
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
