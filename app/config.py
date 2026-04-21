from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://coach:coach@db:5433/fitness_coach"
    REDIS_URL: str = "redis://redis:6380"
    OPENAI_API_KEY: str = ""
    LINQ_API_TOKEN: str = ""
    LINQ_WEBHOOK_SECRET: str = ""
    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24h
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_ID: str = ""
    ALLOWED_ORIGINS: str = "https://hercules.chat"
    DEFAULT_LANGUAGE: str = "de"
    PROACTIVE_MAX_PER_DAY: int = 3
    PROACTIVE_IDLE_HOURS: int = 2
    LINQ_BASE_URL: str = "https://api.linqapp.com/api/partner/v3"

    class Config:
        env_file = ".env"


settings = Settings()
