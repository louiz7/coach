from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID


class UserSignup(BaseModel):
    phone: str
    email: Optional[EmailStr] = None
    password: str
    name: str


class UserLogin(BaseModel):
    email: str
    password: str


class UserProfile(BaseModel):
    id: UUID
    phone: str
    email: Optional[str] = None
    name: str
    sport: Optional[str] = None
    fitness_level: Optional[str] = None
    goal: Optional[str] = None
    training_frequency: Optional[int] = None
    injuries: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    onboarding_complete: bool = False
    language: str = "de"

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    name: Optional[str] = None
    language: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
