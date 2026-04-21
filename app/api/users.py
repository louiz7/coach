from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.user import User
from app.schemas.user import UserProfile, UserUpdate
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/me", response_model=UserProfile)
async def get_me(user: User = Depends(get_current_user)):
    return user


@router.put("/me", response_model=UserProfile)
async def update_me(
    data: UserUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.name is not None:
        user.name = data.name
    if data.language is not None:
        user.language = data.language
    await db.commit()
    await db.refresh(user)
    return user
