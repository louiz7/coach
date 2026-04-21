from pydantic import BaseModel
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class ExerciseSchema(BaseModel):
    name: str
    sets: int
    reps: str
    rest_seconds: Optional[int] = None
    notes: Optional[str] = None


class DaySchema(BaseModel):
    day: str
    focus: str
    exercises: List[ExerciseSchema]


class PlanSchema(BaseModel):
    days: List[DaySchema]


class TrainingPlanResponse(BaseModel):
    id: UUID
    plan_json: dict
    raw_text: str
    is_current: bool
    created_at: datetime

    class Config:
        from_attributes = True
