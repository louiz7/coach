import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, Boolean, Text, DateTime, Date, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


# Project identifiers for shared Linq number routing
class ProjectEnum:
    HERCULES = "hercules"
    UNKNOWN = "unknown"


# Onboarding states for the iMessage chat funnel
class OnboardingState:
    # ── New conversational flow (7 steps) ────────────────────────────────────
    INFORM           = "INFORM"           # waiting for first name
    CAPTURE_GOAL     = "CAPTURE_GOAL"     # free-text goal extraction
    STATUS_QUO       = "STATUS_QUO"       # current training description
    CONSTRAINTS      = "CONSTRAINTS"      # injuries / equipment / preferences
    WHOOP_OR_BASICS  = "WHOOP_OR_BASICS"  # WHOOP connect OR manual age/weight/gender
    PLAN_REVIEW      = "PLAN_REVIEW"      # plan built, waiting for ok / change request
    CHALLENGE        = "CHALLENGE"        # 7-day challenge pitch

    # ── Terminal state ────────────────────────────────────────────────────────
    DONE = "DONE"

    # ── Legacy states — kept for backward compat, fast-forwarded to INFORM ───
    BETA_GATE             = "BETA_GATE"
    CHAT_NAME             = "CHAT_NAME"
    CHAT_GOAL             = "CHAT_GOAL"
    CHAT_SPORTS_FOCUS     = "CHAT_SPORTS_FOCUS"
    CHAT_STATUS           = "CHAT_STATUS"
    CHAT_CHALLENGE        = "CHAT_CHALLENGE"
    CHAT_STYLE            = "CHAT_STYLE"
    CHAT_INTENSITY        = "CHAT_INTENSITY"
    CHAT_BODY_METRICS     = "CHAT_BODY_METRICS"
    CHAT_INJURIES         = "CHAT_INJURIES"
    CHAT_CURRENT_SCHEDULE = "CHAT_CURRENT_SCHEDULE"
    CHAT_EQUIPMENT        = "CHAT_EQUIPMENT"
    CHAT_WHOOP_PROMPT     = "CHAT_WHOOP_PROMPT"
    AWAITING_PLAN_CONFIRM = "AWAITING_PLAN_CONFIRM"
    SPORTS_FOCUS_BACKFILL = "SPORTS_FOCUS_BACKFILL"
    CHAT_PITCH            = "CHAT_PITCH"
    FORM                  = "FORM"


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
    plan_sent_count = Column(Integer, default=0, nullable=False)
    current_schedule_notes = Column(Text, nullable=True)
    equipment_access = Column(String(30), nullable=True)
    last_morning_brief_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    persona = relationship("CoachPersona")
