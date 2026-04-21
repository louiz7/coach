from pydantic import BaseModel
from typing import Optional
from uuid import UUID


class OnboardingQuiz(BaseModel):
    sport: str
    fitness_level: str  # beginner, intermediate, advanced
    goal: str  # weight_loss, muscle_gain, endurance, competition, general
    training_frequency: int
    injuries: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None


class PersonaSelect(BaseModel):
    persona_id: UUID


class PersonaResponse(BaseModel):
    id: UUID
    name: str
    description: str
    avatar_url: Optional[str] = None

    class Config:
        from_attributes = True
