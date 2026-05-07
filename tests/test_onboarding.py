"""Tests for the onboarding state machine (app/services/onboarding_chat.py).

Runs with:
    PYTHONPATH=. pytest tests/test_onboarding.py -v

All external I/O is mocked; SQLAlchemy is not needed — we use a plain FakeUser
dataclass so there is zero mock-state bleed between tests.
"""

import asyncio
import sys
import types
import uuid
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ── Lightweight stubs registered BEFORE any app import ───────────────────────

def _pkg(name: str, path: str | None = None) -> types.ModuleType:
    m = types.ModuleType(name)
    if path is not None:
        m.__path__ = [path]
    sys.modules.setdefault(name, m)
    return m


# openai
openai_stub = _pkg("openai")
openai_stub.AsyncOpenAI = MagicMock()

# app / app.database
_pkg("app", path="/Users/louizel-hosri/Desktop/coach/app")
app_db = _pkg("app.database")
app_db.Base = object  # plain object so 'class User(Base)' doesn't pull in mocks

# app.config
app_cfg = _pkg("app.config")
settings_obj = MagicMock()
settings_obj.OPENAI_API_KEY = "sk-test"
settings_obj.ALLOWED_ORIGINS = "https://example.com"
app_cfg.settings = settings_obj

# app.redis
app_redis = _pkg("app.redis")
redis_pool_mock = AsyncMock()
app_redis.redis_pool = redis_pool_mock

# app.services (package) + stubs for each submodule
svc_pkg = _pkg("app.services", path="/Users/louizel-hosri/Desktop/coach/app/services")

linq_mock_send = AsyncMock()
linq_mock_card = AsyncMock()
linq_stub = _pkg("app.services.linq")
linq_stub.send_message = linq_mock_send
linq_stub.share_contact_card = linq_mock_card

add_message_mock = AsyncMock()
mem_stub = _pkg("app.services.memory")
mem_stub.add_message = add_message_mock

assign_persona_mock = AsyncMock()
persona_stub = _pkg("app.services.persona")
persona_stub.assign_persona_from_style = assign_persona_mock

token_stub = _pkg("app.services.token")
token_stub.create_onboarding_token = MagicMock(return_value="tok123")
token_stub.create_plan_token = MagicMock(return_value="plantok")

gen_plan_mock = AsyncMock()
tp_stub = _pkg("app.services.training_plan")
tp_stub.generate_plan = gen_plan_mock

# app.models.user — import REAL module (OnboardingState only; skip User ORM)
from app.models.user import OnboardingState  # noqa: E402

# Import module under test
import app.services.onboarding_chat as oc  # noqa: E402


# ── FakeUser — plain dataclass, no SQLAlchemy ─────────────────────────────────

@dataclass
class FakeUser:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    name: str = "Alex"
    phone: str = "+4915712345678"
    goal: Optional[str] = None
    sports_focus: Optional[str] = None
    training_frequency: Optional[int] = None
    current_schedule_notes: Optional[str] = None
    injuries: Optional[str] = None
    equipment_access: Optional[str] = None
    age: Optional[int] = None
    weight_kg: Optional[float] = None
    gender: Optional[str] = None
    onboarding_state: str = OnboardingState.INFORM
    onboarding_complete: bool = False
    linq_chat_id: str = "chat-99"


class FakeDB:
    def __init__(self):
        self.committed = 0

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        pass


CHAT = "chat-99"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def reset_mocks():
    linq_mock_send.reset_mock()
    linq_mock_card.reset_mock()
    add_message_mock.reset_mock()
    gen_plan_mock.reset_mock()
    gen_plan_mock.side_effect = None
    redis_pool_mock.get.reset_mock()
    redis_pool_mock.incr.reset_mock()
    redis_pool_mock.expire.reset_mock()
    redis_pool_mock.get.return_value = None


def sent_texts() -> list[str]:
    return [c.args[1] for c in linq_mock_send.call_args_list]


# ── 1. INFORM: valid name ─────────────────────────────────────────────────────

def test_inform_valid_name():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.INFORM)
    db = FakeDB()

    run(oc._handle_inform(user, CHAT, "Louis", db))

    assert user.name == "Louis"
    assert user.onboarding_state == OnboardingState.CAPTURE_GOAL
    assert db.committed == 1
    assert linq_mock_send.call_count >= 3  # name confirm + 2 pitch messages


# ── 2. INFORM: single-char name — reprompt, no advance ───────────────────────

def test_inform_name_too_short():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.INFORM)
    db = FakeDB()

    run(oc._handle_inform(user, CHAT, "A", db))

    assert user.onboarding_state == OnboardingState.INFORM
    assert db.committed == 0
    assert linq_mock_send.call_count == 1


# ── 3. CAPTURE_GOAL: LLM returns valid goal ───────────────────────────────────

def test_capture_goal_valid():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.CAPTURE_GOAL)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={
        "goal": "run a half marathon", "sports_focus": "running", "valid": True
    })):
        run(oc._handle_capture_goal(user, CHAT, "I want to run a half marathon", db))

    assert user.goal == "run a half marathon"
    assert user.sports_focus == "running"
    assert user.onboarding_state == OnboardingState.STATUS_QUO
    assert db.committed == 1


# ── 4. CAPTURE_GOAL: LLM invalid → first reask (no advance) ──────────────────

def test_capture_goal_invalid_reask():
    reset_mocks()
    redis_pool_mock.get.return_value = None  # 0 reasks so far
    user = FakeUser(onboarding_state=OnboardingState.CAPTURE_GOAL)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={"valid": False})):
        run(oc._handle_capture_goal(user, CHAT, "blah blah", db))

    assert user.onboarding_state == OnboardingState.CAPTURE_GOAL
    assert db.committed == 0
    redis_pool_mock.incr.assert_called_once()


# ── 5. CAPTURE_GOAL: LLM invalid second time → accept and advance ─────────────

def test_capture_goal_invalid_accept_after_reask():
    reset_mocks()
    redis_pool_mock.get.return_value = b"1"  # already re-asked once
    user = FakeUser(onboarding_state=OnboardingState.CAPTURE_GOAL)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={"valid": False})):
        run(oc._handle_capture_goal(user, CHAT, "I dunno, just fitness", db))

    assert user.onboarding_state == OnboardingState.STATUS_QUO
    assert user.goal == "I dunno, just fitness"
    assert db.committed == 1


# ── 6. STATUS_QUO: valid extraction ──────────────────────────────────────────

def test_status_quo_valid():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.STATUS_QUO)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={
        "training_frequency": 3, "schedule_summary": "gym 3x lifting", "valid": True
    })):
        run(oc._handle_status_quo(user, CHAT, "I go to the gym 3 times a week", db))

    assert user.training_frequency == 3
    assert "gym" in (user.current_schedule_notes or "")
    assert user.onboarding_state == OnboardingState.CONSTRAINTS
    assert db.committed == 1


# ── 7. CONSTRAINTS: "none" shortcut — LLM not called ─────────────────────────

def test_constraints_none():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.CONSTRAINTS)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock()) as mock_llm:
        run(oc._handle_constraints(user, CHAT, "none", db))
        mock_llm.assert_not_called()

    assert user.onboarding_state == OnboardingState.WHOOP_OR_BASICS
    assert db.committed == 1


# ── 8. CONSTRAINTS: LLM extracts injuries + equipment ────────────────────────

def test_constraints_with_injuries():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.CONSTRAINTS)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={
        "injuries": "bad knee", "equipment": "gym", "notes": "", "valid": True
    })):
        run(oc._handle_constraints(user, CHAT, "bad knee, access to a gym", db))

    assert user.injuries == "bad knee"
    assert user.equipment_access == "gym"
    assert user.onboarding_state == OnboardingState.WHOOP_OR_BASICS


# ── 9. WHOOP_OR_BASICS: valid basics → delegates to _build_plan_and_advance ───

def test_whoop_or_basics_valid():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.WHOOP_OR_BASICS)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={
        "age": 25, "weight_kg": 80.0, "gender": "male", "valid": True
    })), patch.object(oc, "_build_plan_and_advance", new=AsyncMock()) as mock_build:
        run(oc._handle_whoop_or_basics(user, CHAT, "25, 80 kg, male", db))
        mock_build.assert_called_once_with(user, CHAT, db)

    assert user.age == 25
    assert user.weight_kg == 80.0
    assert user.gender == "male"


# ── 10. WHOOP_OR_BASICS: invalid → reask once, no plan build ─────────────────

def test_whoop_or_basics_invalid_reask():
    reset_mocks()
    redis_pool_mock.get.return_value = None
    user = FakeUser(onboarding_state=OnboardingState.WHOOP_OR_BASICS)
    db = FakeDB()

    with patch.object(oc, "_llm_extract", new=AsyncMock(return_value={"valid": False})), \
         patch.object(oc, "_build_plan_and_advance", new=AsyncMock()) as mock_build:
        run(oc._handle_whoop_or_basics(user, CHAT, "idk", db))
        mock_build.assert_not_called()

    redis_pool_mock.incr.assert_called_once()


# ── 11. _build_plan_and_advance: success path ────────────────────────────────

def test_build_plan_and_advance_success():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.WHOOP_OR_BASICS)
    db = FakeDB()
    gen_plan_mock.return_value = None

    run(oc._build_plan_and_advance(user, CHAT, db))

    assert user.onboarding_state == OnboardingState.PLAN_REVIEW
    assert user.onboarding_complete is True
    texts = sent_texts()
    assert any("plantok" in t or "plan" in t.lower() for t in texts)
    linq_mock_card.assert_called_once_with(CHAT)


# ── 12. _build_plan_and_advance: plan gen fails → graceful fallback ───────────

def test_build_plan_and_advance_failure():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.WHOOP_OR_BASICS)
    db = FakeDB()
    gen_plan_mock.side_effect = RuntimeError("DB exploded")

    run(oc._build_plan_and_advance(user, CHAT, db))

    assert user.onboarding_complete is True
    texts = sent_texts()
    assert any("trouble" in t.lower() or "plan" in t.lower() for t in texts)


# ── 13. PLAN_REVIEW: no modification keyword → goes straight to challenge ─────

def test_plan_review_no_modification():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.PLAN_REVIEW)
    db = FakeDB()

    run(oc._handle_plan_review(user, CHAT, "looks good!", db))

    assert user.onboarding_state == OnboardingState.CHALLENGE
    texts = sent_texts()
    assert any("you in" in t.lower() for t in texts)


# ── 14. CHALLENGE: affirmative response → DONE ───────────────────────────────

def test_challenge_yes():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.CHALLENGE)
    db = FakeDB()

    run(oc._handle_challenge(user, CHAT, "yes!", db))

    assert user.onboarding_state == OnboardingState.DONE
    texts = sent_texts()
    assert any("🔥" in t or "check-in" in t.lower() for t in texts)


# ── 15. CHALLENGE: negative response → DONE with graceful ack ────────────────

def test_challenge_no():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.CHALLENGE)
    db = FakeDB()

    run(oc._handle_challenge(user, CHAT, "nope", db))

    assert user.onboarding_state == OnboardingState.DONE
    texts = sent_texts()
    assert any("no worries" in t.lower() for t in texts)


# ── 16. Legacy state → restart INFORM flow ───────────────────────────────────

def test_legacy_state_restarts():
    reset_mocks()
    user = FakeUser(onboarding_state=OnboardingState.BETA_GATE)
    db = FakeDB()

    run(oc.handle(user, CHAT, "hey", db))

    assert user.onboarding_state == OnboardingState.INFORM
    assert db.committed >= 1
    assert linq_mock_send.call_count >= 1
