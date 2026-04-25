import random
import httpx
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from app.config import settings
from app.models.user import User
from app.models.message import Message
from app.models.coach_persona import CoachPersona
from app.services import linq
from app.services.memory import add_message
from app.redis import redis_pool


async def get_idle_users(db: AsyncSession) -> list[User]:
    """Find users who haven't messaged in PROACTIVE_IDLE_HOURS and are under daily limit."""
    cutoff = datetime.utcnow() - timedelta(hours=settings.PROACTIVE_IDLE_HOURS)

    # Subquery: last message time per user
    last_msg_sq = (
        select(Message.user_id, func.max(Message.created_at).label("last_msg"))
        .group_by(Message.user_id)
        .subquery()
    )

    result = await db.execute(
        select(User)
        .join(last_msg_sq, User.id == last_msg_sq.c.user_id, isouter=True)
        .where(
            and_(
                User.onboarding_complete == True,
                User.is_active == True,
                User.linq_chat_id.isnot(None),
                # Last message older than idle threshold OR no messages at all
                (last_msg_sq.c.last_msg < cutoff) | (last_msg_sq.c.last_msg.is_(None)),
            )
        )
    )
    return list(result.scalars().all())


async def send_checkin(user: User, db: AsyncSession):
    """Send a proactive check-in to a user."""
    # Check daily limit
    key = f"proactive:{user.id}:{datetime.utcnow().strftime('%Y-%m-%d')}"
    count = await redis_pool.get(key)
    if count and int(count) >= settings.PROACTIVE_MAX_PER_DAY:
        return

    # Load persona
    result = await db.execute(
        select(CoachPersona).where(CoachPersona.id == user.persona_id)
    )
    persona = result.scalar_one_or_none()
    if not persona:
        return

    # Generate check-in message
    checkin_types = [
        "Send a short training day reminder based on the user's plan.",
        "Ask how yesterday's training went.",
        "Send a short motivational message.",
        "Send a quick nutrition tip relevant to their goal.",
    ]

    # Build WHOOP context if user has biometric data cached
    whoop_context = ""
    if user.last_recovery_score is not None:
        if user.last_recovery_score >= 67:
            whoop_emoji = "🟢"
        elif user.last_recovery_score >= 34:
            whoop_emoji = "🟡"
        else:
            whoop_emoji = "🔴"
        whoop_context = f"Latest WHOOP recovery: {whoop_emoji} {user.last_recovery_score}%"
        if user.last_hrv:
            whoop_context += f", HRV {user.last_hrv:.0f}ms"
        if user.last_sleep_performance is not None:
            whoop_context += f", sleep performance {user.last_sleep_performance}%"
        whoop_context = f"\nBiometric context: {whoop_context}"

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": (
                        f"{persona.system_prompt}\n\n"
                        f"User: {user.sport}, {user.fitness_level}, {user.goal}\n"
                        f"Language: {user.language}"
                        f"{whoop_context}\n\n"
                        f"Task: {random.choice(checkin_types)}\n"
                        "Write exactly 1 short sentence. No markdown."
                    )},
                ],
                "max_tokens": 80,
                "temperature": 0.9,
            },
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()

    # Send via Linq
    await linq.send_message(user.linq_chat_id, text)
    await add_message(user.id, "assistant", text, db)

    # Increment counter
    await redis_pool.incr(key)
    # TTL until midnight
    await redis_pool.expire(key, 86400)
