"""
WHOOP API service — OAuth token management and API calls.
"""
import base64
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
from app.config import settings

WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE = "https://api.prod.whoop.com/developer"
WHOOP_REDIRECT_URI = "https://hercules.chat/whoop/callback"
SCOPES = (
    "read:recovery read:cycles read:sleep read:workout "
    "read:profile read:body_measurement offline"
)


def build_auth_url(state: str) -> str:
    """Build the WHOOP OAuth authorization URL."""
    params = {
        "client_id": settings.WHOOP_CLIENT_ID,
        "redirect_uri": WHOOP_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{WHOOP_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            WHOOP_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": WHOOP_REDIRECT_URI,
                "client_id": settings.WHOOP_CLIENT_ID,
                "client_secret": settings.WHOOP_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token using the refresh token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            WHOOP_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.WHOOP_CLIENT_ID,
                "client_secret": settings.WHOOP_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


def verify_webhook_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """
    Verify a WHOOP webhook signature.
    WHOOP signs: base64(HMAC-SHA256(timestamp + body, client_secret))
    """
    msg = timestamp.encode() + body
    expected = base64.b64encode(
        hmac.new(
            settings.WHOOP_CLIENT_SECRET.encode(),
            msg,
            hashlib.sha256,
        ).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


async def get_profile(access_token: str) -> dict:
    """Fetch the authenticated user's WHOOP profile (includes user_id)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WHOOP_API_BASE}/v1/user/profile/basic",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_latest_recovery(access_token: str) -> Optional[dict]:
    """Fetch the most recent recovery record."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WHOOP_API_BASE}/v1/recovery",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return None
        records = resp.json().get("records", [])
        return records[0] if records else None


async def get_sleep(access_token: str, sleep_id: str) -> Optional[dict]:
    """Fetch a specific sleep record by UUID (v2 API)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WHOOP_API_BASE}/v2/activity/sleep/{sleep_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json() if resp.status_code == 200 else None


async def get_workout(access_token: str, workout_id: str) -> Optional[dict]:
    """Fetch a specific workout record by UUID (v2 API)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WHOOP_API_BASE}/v2/activity/workout/{workout_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json() if resp.status_code == 200 else None
