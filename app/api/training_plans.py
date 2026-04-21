from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.user import User
from app.models.training_plan import TrainingPlan
from app.schemas.training_plan import TrainingPlanResponse
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/v1/training-plan", tags=["training-plan"])


@router.get("", response_model=TrainingPlanResponse)
async def get_current_plan(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TrainingPlan).where(
            TrainingPlan.user_id == user.id,
            TrainingPlan.is_current == True,
        )
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(404, "No active training plan")
    return plan
