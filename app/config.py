from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://coach:coach@db:5432/fitness_coach"
    REDIS_URL: str = "redis://redis:6379"
    OPENAI_API_KEY: str = ""
    LINQ_API_TOKEN: str = ""
    LINQ_WEBHOOK_SECRET: str = ""
    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24h
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_ID: str = ""
    STRIPE_PAYMENT_LINK: str = ""  # Static Stripe Payment Link for iMessage paywall
    # Public base URL used in outbound links (WHOOP redirect, plan link, payment fallback,
    # sitemap, etc.). Override per-environment via env var. During the kano.fit DNS/SSL
    # cutover, set this to https://hercules.chat in the server .env, then flip to
    # https://kano.fit once DNS + cert are live — no code changes needed.
    PUBLIC_BASE_URL: str = "https://kano.fit"
    # Comma-separated list of allowed CORS origins. Includes both domains during cutover.
    ALLOWED_ORIGINS: str = "https://kano.fit,https://www.kano.fit,https://hercules.chat,https://www.hercules.chat"
    DEFAULT_LANGUAGE: str = "de"
    PROACTIVE_MAX_PER_DAY: int = 3
    PROACTIVE_IDLE_HOURS: int = 2
    LINQ_BASE_URL: str = "https://api.linqapp.com/api/partner/v3"
    WHOOP_CLIENT_ID: str = ""
    WHOOP_CLIENT_SECRET: str = ""
    # MuscleWiki API
    MUSCLEWIKI_API_KEY: str = ""
    # Beta phase
    BETA_MODE: bool = True
    BETA_CODE: str = "kano2026!"
    WHATSAPP_COMMUNITY_URL: str = "https://chat.whatsapp.com/LIOlg1tHtq07Vkebqazr6u?mode=hqctcli"

    class Config:
        env_file = ".env"


settings = Settings()
