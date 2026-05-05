"""Pydantic schemas for editable training plan persistence.

Validation goals:
- All strings have length caps (defense against giant payloads)
- Numerics are bounded (sets/RPE etc. can't be absurd)
- We do NOT allow extra unknown keys (extra='forbid')
- ORM persists via SQLAlchemy parameterized queries → SQL injection safe.
"""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class PlanExercise(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
    sets: int = Field(..., ge=1, le=20)
    # reps can be "8-10", "AMRAP", "12" → keep as string but capped
    reps: str = Field(..., min_length=1, max_length=20)
    rpe: Optional[float] = Field(None, ge=1, le=10)
    rest_seconds: Optional[int] = Field(None, ge=0, le=900)
    notes: Optional[str] = Field(default="", max_length=500)


class PlanDay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    day: str = Field(..., min_length=1, max_length=30)
    focus: Optional[str] = Field(default="", max_length=80)
    exercises: List[PlanExercise] = Field(..., max_length=30)


class PlanJSON(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    days: List[PlanDay] = Field(..., min_length=1, max_length=14)
    progression_tips: Optional[List[str]] = Field(default=None, max_length=20)
    motivational_note: Optional[str] = Field(default=None, max_length=500)


class PlanUpdateRequest(BaseModel):
    """PATCH body for /plan."""
    model_config = ConfigDict(extra="forbid")

    # Concurrency guard: caller must echo back the plan_id they were viewing.
    # If it no longer matches the current plan, we return 409.
    plan_id: str = Field(..., min_length=1, max_length=64)
    plan_json: PlanJSON


def render_raw_text_from_plan(plan: PlanJSON) -> str:
    """Re-render a human-readable raw_text snapshot after a user edit."""
    lines: List[str] = ["Your Training Plan:", ""]
    for d in plan.days:
        focus = f" — {d.focus}" if d.focus else ""
        lines.append(f"{d.day}{focus}")
        for ex in d.exercises:
            rpe = f" @RPE{ex.rpe}" if ex.rpe is not None else ""
            rest = f" · {ex.rest_seconds}s rest" if ex.rest_seconds else ""
            note = f" — {ex.notes}" if ex.notes else ""
            lines.append(f"  • {ex.name}: {ex.sets}×{ex.reps}{rpe}{rest}{note}")
        lines.append("")
    if plan.progression_tips:
        lines.append("Progression:")
        for t in plan.progression_tips:
            lines.append(f"  - {t}")
        lines.append("")
    if plan.motivational_note:
        lines.append(f"\"{plan.motivational_note}\"")
    return "\n".join(lines).strip()
