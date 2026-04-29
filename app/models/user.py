import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, Boolean, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


# Project identifiers for shared Linq number routing
class ProjectEnum:
    HERCULES = "hercules"
    UNKNOWN = "unknown"


# Onboarding states for the iMessage chat funnel
class OnboardingState:
    # Beta-phase chat onboarding
    BETA_GATE = "BETA_GATE"                    # Awaiting beta access code
    CHAT_NAME = "CHAT_NAME"                    # Awaiting user's first name
    CHAT_GOAL = "CHAT_GOAL"                    # Awaiting goal (A/B/C/D + free-text)
    CHAT_SPORTS_FOCUS = "CHAT_SPORTS_FOCUS"    # Free-text: sports to improve
    CHAT_STATUS = "CHAT_STATUS"                # Training frequency (A/B/C/D)
    CHAT_CHALLENGE = "CHAT_CHALLENGE"          # Biggest challenge (A/B/C/D)
    CHAT_STYLE = "CHAT_STYLE"                  # Coach style (A/B/C/D)
    CHAT_INTENSITY = "CHAT_INTENSITY"          # Coach intensity (A/B/C/D)
    CHAT_WHOOP_PROMPT = "CHAT_WHOOP_PROMPT"    # Connect WHOOP or skip
    SPORTS_FOCUS_BACKFILL = "SPORTS_FOCUS_BACKFILL"  # One-time backfill for existing users
    # Legacy / terminal
    CHAT_PITCH = "CHAT_PITCH"                  # legacy: yes/no after pitch (kept for back-compat)
    FORM = "FORM"                              # legacy: web form path
    DONE = "DONE"                              # Fully onboarded


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone = Column(String(20), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    sport = Column(String(50), nullable=True)
    fitness_level = Column(String(20), nullable=True)
    goal = Column(Text, nullable=True)
    sports_focus = Column(Text, nullable=True)            # free-text list of sports to improve in
    beta_unlocked = Column(Boolean, default=False, nullable=False)
    training_frequency = Column(Integer, nullable=True)
    injuries = Column(Text, nullable=True)
    age = Column(Integer, nullable=True)
    gender = Column(String(10), nullable=True)
    weight_kg = Column(Float, nullable=True)
    height_cm = Column(Float, nullable=True)
    persona_id = Column(UUID(as_uuid=True), ForeignKey("coach_personas.id"), nullable=True)
    linq_chat_id = Column(String(100), nullable=True)
    pool_number = Column(String(20), nullable=True)
    onboarding_complete = Column(Boolean, default=False)
    project = Column(String(20), default=ProjectEnum.UNKNOWN, nullable=False)
    onboarding_state = Column(String(50), nullable=True)
    coach_style = Column(String(30), nullable=True)       # high_energy, calm, drill_sergeant, humor
    coach_intensity = Column(String(20), nullable=True)   # easy, moderate, hard, maximum
    challenge = Column(String(30), nullable=True)         # motivation, dont_know, consistency, no_time, alone
    # WHOOP OAuth
    whoop_user_id = Column(String(50), nullable=True)
    whoop_access_token = Column(Text, nullable=True)
    whoop_refresh_token = Column(Text, nullable=True)
    whoop_token_expires_at = Column(DateTime, nullable=True)
    # WHOOP cached biometrics (updated on each webhook)
    last_recovery_score = Column(Integer, nullable=True)
    last_hrv = Column(Float, nullable=True)
    last_sleep_performance = Column(Integer, nullable=True)
    timezone = Column(String(50), default="Europe/Berlin", nullable=True)
    is_active = Column(Boolean, default=True)
    language = Column(String(5), default="de")
    created_at = Column(DateTime, default=datetime.utcnow)

    persona = relationship("CoachPersona")
