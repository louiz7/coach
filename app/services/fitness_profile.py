"""
Smart fitness profile — structured JSON document per user, kept small (<200 tokens)
via rule-based updates. Always injected into the system prompt.

Heavy lifting (semantic memory) is handled by memory_search.py.
GPT is only used for weekly coach_notes enrichment.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


EMPTY_PROFILE: dict[str, Any] = {
    "prs": {},                      # {"bench_press": {"value": 120, "unit": "kg", "date": "2026-04-20"}}
    "bodyweight": [],               # rolling list of last 5: {"value": 84.5, "date": "..."}
    "whoop_7d_avg_recovery": None,
    "whoop_recovery_history": [],   # rolling list of last 7 scores
    "whoop_trend": None,            # "improving" | "declining" | "stable"
    "avg_sleep_performance": None,
    "training_streak": 0,
    "last_workout_date": None,
    "total_workouts_logged": 0,
    "coach_notes": [],              # max 5 strings, oldest dropped
    "updated_at": None,
}


# ─── CRUD ────────────────────────────────────────────────────────────────────

async def get_or_create_profile(user_id: UUID, db: AsyncSession) -> dict:
    """Fetch the profile JSON for a user; create empty if missing."""
    row = await db.execute(
        text("SELECT profile FROM fitness_profiles WHERE user_id = :uid"),
        {"uid": str(user_id)},
    )
    found = row.first()
    if found:
        profile = found[0] if isinstance(found[0], dict) else json.loads(found[0])
        # Backfill any missing keys
        for k, v in EMPTY_PROFILE.items():
            profile.setdefault(k, v)
        return profile

    # Create
    await db.execute(
        text(
            "INSERT INTO fitness_profiles (user_id, profile) "
            "VALUES (:uid, CAST(:p AS jsonb)) ON CONFLICT (user_id) DO NOTHING"
        ),
        {"uid": str(user_id), "p": json.dumps(EMPTY_PROFILE)},
    )
    await db.commit()
    return dict(EMPTY_PROFILE)


async def save_profile(user_id: UUID, profile: dict, db: AsyncSession) -> None:
    profile["updated_at"] = datetime.utcnow().isoformat()
    await db.execute(
        text(
            "UPDATE fitness_profiles SET profile = CAST(:p AS jsonb), updated_at = NOW() "
            "WHERE user_id = :uid"
        ),
        {"uid": str(user_id), "p": json.dumps(profile)},
    )
    await db.commit()


# ─── RULE-BASED UPDATERS (no GPT, no tokens) ─────────────────────────────────

async def update_profile_from_workout(
    user_id: UUID,
    db: AsyncSession,
    *,
    label: str,
    value: float,
    unit: str,
    category: str = "exercise",
) -> None:
    """Called after a workout/exercise is logged."""
    profile = await get_or_create_profile(user_id, db)
    today = date.today()

    if category == "bodyweight":
        profile["bodyweight"].append({"value": value, "date": today.isoformat()})
        profile["bodyweight"] = profile["bodyweight"][-5:]
    else:
        # PR check
        existing = profile["prs"].get(label, {})
        existing_value = existing.get("value", 0)
        if value > existing_value:
            profile["prs"][label] = {
                "value": value,
                "unit": unit,
                "date": today.isoformat(),
            }
        # Cap PR list at 12 most recent
        if len(profile["prs"]) > 12:
            sorted_prs = sorted(
                profile["prs"].items(),
                key=lambda kv: kv[1].get("date", ""),
                reverse=True,
            )[:12]
            profile["prs"] = dict(sorted_prs)

    profile["total_workouts_logged"] = int(profile.get("total_workouts_logged", 0)) + 1

    # Streak: increment if last workout was today or yesterday, else reset to 1
    last = profile.get("last_workout_date")
    if last:
        try:
            last_d = date.fromisoformat(last)
            if last_d == today:
                pass  # same day, no change
            elif (today - last_d).days == 1:
                profile["training_streak"] = int(profile.get("training_streak", 0)) + 1
            else:
                profile["training_streak"] = 1
        except Exception:
            profile["training_streak"] = 1
    else:
        profile["training_streak"] = 1

    profile["last_workout_date"] = today.isoformat()
    await save_profile(user_id, profile, db)


async def update_profile_from_whoop_recovery(
    user_id: UUID,
    db: AsyncSession,
    recovery_score: int,
) -> None:
    """Update WHOOP rolling 7d average + trend."""
    profile = await get_or_create_profile(user_id, db)
    today_iso = date.today().isoformat()

    history = profile.get("whoop_recovery_history", [])
    # Replace today's entry if exists, else append
    history = [h for h in history if h.get("date") != today_iso]
    history.append({"value": recovery_score, "date": today_iso})
    history = history[-7:]
    profile["whoop_recovery_history"] = history

    if history:
        avg = sum(h["value"] for h in history) / len(history)
        profile["whoop_7d_avg_recovery"] = round(avg)

        # Trend: compare last 3 vs previous 3
        if len(history) >= 6:
            recent = sum(h["value"] for h in history[-3:]) / 3
            prev = sum(h["value"] for h in history[-6:-3]) / 3
            diff = recent - prev
            if diff > 5:
                profile["whoop_trend"] = "improving"
            elif diff < -5:
                profile["whoop_trend"] = "declining"
            else:
                profile["whoop_trend"] = "stable"

    await save_profile(user_id, profile, db)


async def update_profile_from_whoop_sleep(
    user_id: UUID,
    db: AsyncSession,
    sleep_performance: int,
) -> None:
    profile = await get_or_create_profile(user_id, db)
    # simple EMA
    current = profile.get("avg_sleep_performance")
    if current is None:
        profile["avg_sleep_performance"] = sleep_performance
    else:
        profile["avg_sleep_performance"] = round(0.7 * current + 0.3 * sleep_performance)
    await save_profile(user_id, profile, db)


# ─── PROMPT FORMATTING (~150 tokens) ─────────────────────────────────────────

def format_profile_for_prompt(profile: dict) -> str:
    """Render the profile to a compact string for system prompt injection."""
    lines = ["FITNESS PROFILE:"]

    # PRs (most recent 5)
    prs = profile.get("prs", {})
    if prs:
        sorted_prs = sorted(
            prs.items(), key=lambda kv: kv[1].get("date", ""), reverse=True
        )[:5]
        pr_strs = [
            f"{name} {data['value']}{data.get('unit', '')}"
            for name, data in sorted_prs
        ]
        lines.append(f"- PRs: {', '.join(pr_strs)}")

    # Bodyweight
    bw = profile.get("bodyweight", [])
    if bw:
        latest = bw[-1]
        trend = ""
        if len(bw) >= 2:
            diff = bw[-1]["value"] - bw[0]["value"]
            trend = f" ({'+' if diff >= 0 else ''}{diff:.1f}kg over last {len(bw)} entries)"
        lines.append(f"- Bodyweight: {latest['value']}kg{trend}")

    # WHOOP
    avg_rec = profile.get("whoop_7d_avg_recovery")
    if avg_rec is not None:
        trend = profile.get("whoop_trend", "stable")
        lines.append(f"- WHOOP 7d avg recovery: {avg_rec}% ({trend})")
    avg_sleep = profile.get("avg_sleep_performance")
    if avg_sleep is not None:
        lines.append(f"- Avg sleep performance: {avg_sleep}%")

    # Streak
    streak = profile.get("training_streak", 0)
    total = profile.get("total_workouts_logged", 0)
    if streak or total:
        lines.append(f"- Streak: {streak} day(s) | Total workouts logged: {total}")

    # Coach notes
    notes = profile.get("coach_notes", [])
    if notes:
        lines.append("- Coach notes: " + " | ".join(notes[-3:]))

    return "\n".join(lines) if len(lines) > 1 else ""


# ─── WEEKLY GPT ENRICHMENT ───────────────────────────────────────────────────

async def enrich_profile_with_coach_notes(
    user_id: UUID, db: AsyncSession
) -> Optional[str]:
    """
    Once a week: read last 50 messages, ask gpt-4o-mini to extract 1-2 behavioral
    insights, merge into coach_notes (capped at 5 most recent).
    """
    import httpx
    from app.config import settings
    from app.models.message import Message

    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(Message.created_at.desc())
        .limit(50)
    )
    msgs = list(result.scalars().all())[::-1]
    if len(msgs) < 5:
        return None

    transcript = "\n".join(
        f"{m.role.upper()}: {m.text[:200]}" for m in msgs
    )

    prompt = (
        "From the following coach<>user conversation, extract 1-2 SHORT behavioral "
        "insights about the user (training preferences, habits, recurring obstacles, "
        "what motivates them). Each insight: max 12 words. Reply with ONLY a JSON "
        'array of strings, e.g. ["prefers evening training", "skips cardio often"]. '
        "If nothing notable, reply with []."
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                json={
                    "model": "deepseek/deepseek-v4-flash",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": transcript},
                    ],
                    "max_tokens": 120,
                    "temperature": 0.3,
                },
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            # Strip markdown fences if any
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            new_notes = json.loads(raw)
            if not isinstance(new_notes, list):
                return None
    except Exception as e:
        print(f"[coach_notes enrich ERROR] {e}")
        return None

    if not new_notes:
        return None

    profile = await get_or_create_profile(user_id, db)
    existing = profile.get("coach_notes", [])
    # Merge, dedup, keep last 5
    combined = existing + [n for n in new_notes if n not in existing]
    profile["coach_notes"] = combined[-5:]
    await save_profile(user_id, profile, db)
    return ", ".join(new_notes)
