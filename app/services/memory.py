import json
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.models.message import Message
from app.redis import redis_pool

CONV_PREFIX = "conv:"
CONV_TTL = 86400  # 24h
MAX_MESSAGES = 20


async def get_conversation(user_id: UUID, db: AsyncSession) -> list[dict]:
    """Get conversation history. Try Redis first, fallback to Postgres."""
    key = f"{CONV_PREFIX}{user_id}"
    cached = await redis_pool.lrange(key, 0, MAX_MESSAGES - 1)
    if cached:
        return [json.loads(m) for m in cached]

    # Hydrate from DB
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(desc(Message.created_at))
        .limit(MAX_MESSAGES)
    )
    messages = result.scalars().all()
    messages.reverse()
    history = [{"role": m.role, "content": m.text} for m in messages]

    # Cache in Redis
    if history:
        pipe = redis_pool.pipeline()
        await pipe.delete(key)
        for m in history:
            await pipe.rpush(key, json.dumps(m))
        await pipe.expire(key, CONV_TTL)
        await pipe.execute()

    return history


async def add_message(user_id: UUID, role: str, text: str, db: AsyncSession, linq_message_id: str = None):
    """Save message to DB and Redis."""
    msg = Message(user_id=user_id, role=role, text=text, linq_message_id=linq_message_id)
    db.add(msg)
    await db.commit()

    key = f"{CONV_PREFIX}{user_id}"
    await redis_pool.rpush(key, json.dumps({"role": role, "content": text}))
    await redis_pool.ltrim(key, -MAX_MESSAGES, -1)
    await redis_pool.expire(key, CONV_TTL)

    return msg
