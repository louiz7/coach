"""
Onboarding token service.

Generates and verifies short-lived JWT tokens that identify which user
is filling out the web onboarding form. The token is embedded in the
/start?token=... URL sent via iMessage.

Fields may be extended later without breaking existing tokens.
"""

import jwt
from datetime import datetime, timedelta, timezone
from app.config import settings


def create_onboarding_token(phone: str, ttl_hours: int = 24) -> str:
    """
    Generate a signed JWT for the onboarding form URL.

    Payload:
        phone  – the user's phone number (primary key for lookup)
        type   – always "onboarding" (guards against token reuse)
        exp    – expiry (default 24 hours)
        iat    – issued-at
    """
    now = datetime.now(tz=timezone.utc)
    payload = {
        "phone": phone,
        "type": "onboarding",
        "iat": now,
        "exp": now + timedelta(hours=ttl_hours),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_onboarding_token(token: str) -> dict:
    """
    Verify and decode an onboarding token.

    Returns the decoded payload dict on success.
    Raises ValueError with a human-readable message on failure.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        raise ValueError("This link has expired. Please ask Hercules for a new one.")
    except jwt.InvalidTokenError as e:
        raise ValueError(f"Invalid token: {e}")

    if payload.get("type") != "onboarding":
        raise ValueError("Invalid token type.")

    return payload
