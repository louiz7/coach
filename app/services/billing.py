from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from app.models.subscription import Subscription


async def check_subscription(user_id: UUID, db: AsyncSession) -> bool:
    """Check if user has an active subscription."""
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return False
    if sub.status in ("active", "trialing"):
        if sub.current_period_end and sub.current_period_end > datetime.utcnow():
            return True
        if sub.status == "active":
            return True
    return False
