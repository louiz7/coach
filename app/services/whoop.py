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
            f"{WHOOP_API_BASE}/v2/user/profile/basic",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_latest_recovery(access_token: str) -> Optional[dict]:
    """Fetch the most recent SCORED recovery record (v2 API).

    WHOOP returns records sorted by sleep start DESC. We must:
      1. Skip records with score_state != "SCORED" (PENDING_SCORE / UNSCORABLE
         records have no reliable recovery_score yet).
      2. Pull a few records (limit=5) so we don't hand back yesterday's score
         when today's is still calculating — we'd rather return None than a
         stale value.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WHOOP_API_BASE}/v2/recovery",
            params={"limit": 5},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return None
        records = resp.json().get("records", [])
        if not records:
            return None
        # First record is the most recent. Only return it if SCORED.
        latest = records[0]
        if latest.get("score_state") != "SCORED":
            return None
        return latest


async def get_today_recovery(access_token: str) -> Optional[dict]:
    """Fetch TODAY's recovery via the current cycle.

    More reliable than `get_latest_recovery` for the morning brief: WHOOP's
    "current cycle" represents today's physiological day, so its recovery
    cannot be a stale record from a previous day.

    Returns None if:
      • no current cycle yet,
      • cycle has no recovery (user didn't sleep / no sleep synced),
      • recovery score_state != SCORED (still calculating).
    """
    async with httpx.AsyncClient() as client:
        # 1) Get the latest cycle (sorted by start DESC per docs)
        resp = await client.get(
            f"{WHOOP_API_BASE}/v2/cycle",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return None
        cycles = resp.json().get("records", [])
        if not cycles:
            return None
        cycle_id = cycles[0].get("id")
        if not cycle_id:
            return None

        # 2) Get the recovery for that cycle
        resp = await client.get(
            f"{WHOOP_API_BASE}/v2/cycle/{cycle_id}/recovery",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return None
        rec = resp.json()
        if rec.get("score_state") != "SCORED":
            return None
        return rec


async def get_latest_sleep(access_token: str) -> Optional[dict]:
    """Fetch the most recent sleep record (v2 API)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WHOOP_API_BASE}/v2/activity/sleep",
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
