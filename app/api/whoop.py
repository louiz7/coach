"""
WHOOP integration routes:
  GET  /whoop/connect          — start OAuth flow (requires onboarding token)
  GET  /whoop/callback         — OAuth callback from WHOOP
  GET  /whoop/connected        — confirmation page after successful connect
  POST /api/v1/webhooks/whoop  — receive WHOOP webhook events
"""
import base64
import sys
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import OnboardingState, User
from app.services import linq as linq_svc
from app.services import whoop as whoop_svc
from app.services.onboarding_chat import _build_plan_and_advance
from app.services.token import verify_onboarding_token

router = APIRouter(tags=["whoop"])


def _log(msg: str) -> None:
    print(f"[WHOOP] {msg}", flush=True, file=sys.stderr)


# ─── OAuth Step 1: redirect user to WHOOP ────────────────────────────────────

@router.get("/whoop/connect")
async def whoop_connect(token: str, db: AsyncSession = Depends(get_db)):
    """Start WHOOP OAuth. Requires the user's onboarding JWT token in ?token=."""
    try:
        payload = verify_onboarding_token(token)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Fail loudly if WHOOP credentials aren't configured — much better than
    # silently sending an empty client_id and getting "invalid_client" from WHOOP.
    from app.config import settings
    if not settings.WHOOP_CLIENT_ID or not settings.WHOOP_CLIENT_SECRET:
        raise HTTPException(
            500,
            "WHOOP integration is not configured on the server "
            "(missing WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET).",
        )

    phone = payload["phone"]
    # Encode phone as URL-safe base64 state so we can recover it on callback
    state = base64.urlsafe_b64encode(phone.encode()).decode().rstrip("=")
    auth_url = whoop_svc.build_auth_url(state=state)
    print(f"[WHOOP] Redirecting to auth URL (client_id={settings.WHOOP_CLIENT_ID[:6]}…, state_len={len(state)})", flush=True)
    return RedirectResponse(auth_url)


# ─── OAuth Step 2: WHOOP redirects back here ─────────────────────────────────

@router.get("/whoop/callback")
async def whoop_callback(
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    """Exchange authorization code for tokens and store in DB."""
    # Decode phone from state
    try:
        # Re-pad base64 (we strip padding when building the URL)
        padded = state + "=" * (-len(state) % 4)
        phone = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception:
        raise HTTPException(400, "Invalid state parameter")

    # Exchange code for tokens
    try:
        token_data = await whoop_svc.exchange_code(code)
    except Exception as e:
        _log(f"Token exchange failed: {e}")
        raise HTTPException(500, "Failed to exchange WHOOP authorization code")

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    # Fetch WHOOP profile to get their integer user_id
    try:
        profile = await whoop_svc.get_profile(access_token)
        whoop_user_id = str(profile.get("user_id", ""))
    except Exception as e:
        _log(f"Profile fetch failed: {e}")
        whoop_user_id = ""

    # Persist to DB
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    user.whoop_user_id = whoop_user_id
    user.whoop_access_token = access_token
    user.whoop_refresh_token = refresh_token
    user.whoop_token_expires_at = expires_at
    await db.commit()

    _log(f"WHOOP connected: user={user.name} whoop_user_id={whoop_user_id}")

    # If user is mid-onboarding waiting for WHOOP, advance state and build plan
    if user.onboarding_state == OnboardingState.WHOOP_OR_BASICS and user.linq_chat_id:
        _log(f"User {user.name} in WHOOP_OR_BASICS — advancing to plan build")
        try:
            await _build_plan_and_advance(user, user.linq_chat_id, db)
        except Exception as e:
            _log(f"_build_plan_and_advance failed: {e}")
            # Fall back to standard confirmation so user isn't left in silence
            await linq_svc.send_message(
                user.linq_chat_id,
                (
                    f"🟢 WHOOP connected, {user.name}! "
                    "Building your plan now — hang tight 💪"
                ),
            )
    elif user.linq_chat_id:
        # Send confirmation iMessage
        await linq_svc.send_message(
            user.linq_chat_id,
            (
                f"🟢 WHOOP connected, {user.name}! "
                "I can now see your recovery, sleep, and workouts. "
                "I'll coach you based on your real biometric data 💪"
            ),
        )

    return RedirectResponse("/whoop/connected")


# ─── Confirmation page ────────────────────────────────────────────────────────

@router.get("/whoop/connected", response_class=HTMLResponse)
async def whoop_connected():
    return HTMLResponse("""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>WHOOP Connected — Kano</title>
  <link rel="icon" type="image/png" href="/static/images/favicon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/css/style.css">
  <style>
    body { display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
    .card { text-align:center; padding:3rem 2rem; max-width:400px; }
    .badge { font-size:3rem; margin-bottom:1.5rem; }
    h1 { font-size:1.75rem; margin-bottom:1rem; }
    p { color:#888; line-height:1.7; margin:0; }
  </style>
</head>
<body>
  <main>
    <div class="card">
      <div class="badge">🟢</div>
      <h1>WHOOP Connected!</h1>
      <p>Head back to iMessage — Kano will now coach you based on your real recovery, sleep, and workout data.</p>
    </div>
  </main>
</body>
</html>
""")


# ─── Webhook: receive WHOOP events ───────────────────────────────────────────

@router.post("/api/v1/webhooks/whoop")
async def whoop_webhook(request: Request, bg: BackgroundTasks):
    """
    Receive WHOOP webhook events.
    Returns 200 immediately and processes in the background.
    """
    body = await request.body()
    timestamp = request.headers.get("X-WHOOP-Signature-Timestamp", "")
    signature = request.headers.get("X-WHOOP-Signature", "")

    # Verify signature when headers are present
    if timestamp and signature:
        if not whoop_svc.verify_webhook_signature(timestamp, body, signature):
            _log("Invalid WHOOP webhook signature — rejected")
            raise HTTPException(401, "Invalid signature")

    import json
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    event_type = data.get("type", "")
    whoop_user_id = str(data.get("user_id", ""))
    event_id = str(data.get("id", ""))
    trace_id = data.get("trace_id", "")

    _log(f"event={event_type} whoop_user_id={whoop_user_id} id={event_id} trace={trace_id}")

    # Deduplicate by trace_id — WHOOP may retry the same event
    if trace_id:
        from app.redis import redis_pool
        dedup_key = f"whoop:dedup:{trace_id}"
        already = await redis_pool.get(dedup_key)
        if already:
            _log(f"Duplicate trace_id={trace_id}, skipping")
            return {"ok": True}
        await redis_pool.set(dedup_key, "1", ex=3600)  # 1h TTL

    bg.add_task(_handle_whoop_event, event_type, whoop_user_id, event_id, trace_id)
    return {"ok": True}


# ─── Background event handlers ───────────────────────────────────────────────

async def _handle_whoop_event(event_type: str, whoop_user_id: str, event_id: str, trace_id: str = "") -> None:
    try:
        from app.database import async_session
        async with async_session() as db:
            result = await db.execute(
                select(User).where(User.whoop_user_id == whoop_user_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                _log(f"No user found for whoop_user_id={whoop_user_id}")
                return

            access_token = await _ensure_fresh_token(user, db)
            if not access_token:
                _log(f"Could not get fresh token for {user.name}")
                return

            if event_type == "recovery.updated":
                await _handle_recovery(user, access_token, db)
            elif event_type == "sleep.updated":
                await _handle_sleep(user, access_token, event_id, db)
            elif event_type == "workout.updated":
                await _handle_workout(user, access_token, event_id)
            # *.deleted → no action needed

    except Exception as exc:
        _log(f"Error handling {event_type}: {exc}")


async def _ensure_fresh_token(user: User, db) -> str | None:
    """Return a valid access token, refreshing if it expires within 5 minutes."""
    now = datetime.utcnow()
    expires_at = user.whoop_token_expires_at

    needs_refresh = (
        not user.whoop_access_token
        or not expires_at
        or expires_at <= now + timedelta(minutes=5)
    )

    if needs_refresh and user.whoop_refresh_token:
        try:
            token_data = await whoop_svc.refresh_access_token(user.whoop_refresh_token)
            user.whoop_access_token = token_data["access_token"]
            user.whoop_refresh_token = token_data.get(
                "refresh_token", user.whoop_refresh_token
            )
            user.whoop_token_expires_at = now + timedelta(
                seconds=token_data.get("expires_in", 3600)
            )
            await db.commit()
            _log(f"Token refreshed for {user.name}")
        except Exception as e:
            _log(f"Token refresh failed for {user.name}: {e}")
            return None

    return user.whoop_access_token


async def _handle_recovery(user: User, access_token: str, db) -> None:
    """Fetch latest recovery and send a personalised coaching iMessage."""
    recovery = await whoop_svc.get_latest_recovery(access_token)
    if not recovery:
        return

    score_data = recovery.get("score", {})
    recovery_score = score_data.get("recovery_score")   # 0-100
    hrv = score_data.get("hrv_rmssd_milli")
    rhr = score_data.get("resting_heart_rate")

    if recovery_score is None:
        return

    # Cache biometrics on user
    user.last_recovery_score = int(recovery_score)
    if hrv is not None:
        user.last_hrv = float(hrv)
    await db.commit()

    # Smart memory: rule-based profile update + vector memory
    try:
        from app.services.fitness_profile import update_profile_from_whoop_recovery
        from app.services.memory_search import store_memory
        from datetime import date as _date
        await update_profile_from_whoop_recovery(user.id, db, int(recovery_score))
        mem = f"Recovery {int(recovery_score)}%"
        if hrv is not None:
            mem += f", HRV {hrv:.0f}ms"
        if rhr is not None:
            mem += f", resting HR {rhr}bpm"
        mem += f" on {_date.today().isoformat()}"
        await store_memory(user.id, mem, "recovery", db)
    except Exception as _e:
        _log(f"profile/memory recovery update failed: {_e}")

    # Mark that a morning message was already sent today — prevents sleep.updated
    # from sending a duplicate when both events fire for the same sleep
    from app.redis import redis_pool
    from datetime import date
    morning_key = f"whoop:morning_sent:{user.id}:{date.today().isoformat()}"
    await redis_pool.set(morning_key, "1", ex=86400)

    from openai import AsyncOpenAI
    from app.config import settings as cfg

    client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)

    # Determine status emoji
    if recovery_score >= 67:
        emoji = "🟢"
    elif recovery_score >= 34:
        emoji = "🟡"
    else:
        emoji = "🔴"

    hrv_str = f"{hrv:.0f}ms HRV" if hrv else ""
    rhr_str = f"{rhr}bpm resting HR" if rhr else ""
    bio_detail = ", ".join(filter(None, [hrv_str, rhr_str]))

    prompt = (
        f"You are Kano, a personal fitness coach on iMessage. Be very concise (2-3 sentences max).\n\n"
        f"User: {user.name} | Goal: {user.goal or 'general fitness'} | "
        f"Preferred intensity: {user.coach_intensity or 'moderate'} | "
        f"Coach style: {user.coach_style or 'direct'}\n\n"
        f"Today's WHOOP data: {emoji} Recovery {recovery_score}%"
        f"{f' ({bio_detail})' if bio_detail else ''}\n\n"
        f"Write a short morning coaching message. Start with the emoji and recovery score. "
        f"Give ONE specific training recommendation that matches their recovery. "
        f"Be direct and personal — no filler, no generic advice."
    )

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.7,
    )
    message = response.choices[0].message.content.strip()

    if user.linq_chat_id:
        await linq_svc.send_message(user.linq_chat_id, message)
        _log(f"Sent recovery message to {user.name} (score={recovery_score}%)")


async def _handle_sleep(user: User, access_token: str, sleep_id: str, db) -> None:
    """Fetch sleep data, cache performance score, send message ONLY if no recovery message today."""
    from app.redis import redis_pool
    from datetime import date

    # Skip if recovery.updated already triggered a morning message today
    morning_key = f"whoop:morning_sent:{user.id}:{date.today().isoformat()}"
    if await redis_pool.get(morning_key):
        _log(f"Skipping sleep message for {user.name} — recovery message already sent today")
        return

    sleep = await whoop_svc.get_sleep(access_token, sleep_id)
    if not sleep:
        return

    score_data = sleep.get("score", {})
    performance = score_data.get("sleep_performance_percentage")   # 0-100
    total_secs = score_data.get("total_in_bed_time_milli", 0) // 1000
    hours = total_secs // 3600
    mins = (total_secs % 3600) // 60
    respiratory_rate = score_data.get("respiratory_rate")

    # Cache sleep performance
    if performance is not None:
        user.last_sleep_performance = int(performance)
        await db.commit()

    # Smart memory: profile + vector memory
    if performance is not None:
        try:
            from app.services.fitness_profile import update_profile_from_whoop_sleep
            from app.services.memory_search import store_memory
            from datetime import date as _date
            await update_profile_from_whoop_sleep(user.id, db, int(performance))
            mem = f"Sleep {hours}h{mins}m, performance {int(performance)}% on {_date.today().isoformat()}"
            await store_memory(user.id, mem, "sleep", db)
        except Exception as _e:
            _log(f"profile/memory sleep update failed: {_e}")

    # Set morning-sent flag so no further duplicate today
    await redis_pool.set(morning_key, "1", ex=86400)

    from openai import AsyncOpenAI
    from app.config import settings as cfg

    client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)

    sleep_detail = f"{hours}h {mins}m sleep"
    if performance is not None:
        sleep_detail += f", {performance:.0f}% performance"
    if respiratory_rate:
        sleep_detail += f", {respiratory_rate:.1f} breaths/min"

    prompt = (
        f"You are Kano, a personal fitness coach on iMessage. Be very concise (2-3 sentences max).\n\n"
        f"User: {user.name} | Goal: {user.goal or 'general fitness'} | "
        f"Coach style: {user.coach_style or 'direct'}\n\n"
        f"Sleep data just recorded: {sleep_detail}\n\n"
        f"Write a short morning message about their sleep. Give ONE actionable tip for the day based on this sleep quality. "
        f"Be direct and personal — no filler."
    )

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=120,
        temperature=0.7,
    )
    message = response.choices[0].message.content.strip()

    if user.linq_chat_id:
        await linq_svc.send_message(user.linq_chat_id, message)
        _log(f"Sent sleep message to {user.name} (performance={performance}%)")


# WHOOP sport ID → display name (most common)
_SPORT_NAMES: dict[int, str] = {
    -1: "Activity", 0: "Running", 1: "Cycling", 16: "HIIT",
    44: "Weightlifting", 45: "CrossFit", 63: "Swimming",
    74: "Soccer", 93: "Basketball", 64: "Boxing", 71: "Yoga",
}


async def _handle_workout(user: User, access_token: str, workout_id: str) -> None:
    """Fetch workout details and send an acknowledgement iMessage."""
    workout = await whoop_svc.get_workout(access_token, workout_id)
    if not workout:
        return

    score_data = workout.get("score", {})
    strain = score_data.get("strain")
    avg_hr = score_data.get("average_heart_rate")
    max_hr = score_data.get("max_heart_rate")
    sport_id = workout.get("sport_id", -1)
    sport = _SPORT_NAMES.get(sport_id, "Workout")

    details = [sport]
    if strain is not None:
        details.append(f"strain {strain:.1f}/21")
    if avg_hr:
        details.append(f"avg HR {avg_hr}bpm")

    from openai import AsyncOpenAI
    from app.config import settings as cfg

    client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
    prompt = (
        f"You are Kano, a personal fitness coach on iMessage. 1-2 sentences max.\n\n"
        f"User: {user.name} | Goal: {user.goal or 'general fitness'}\n"
        f"Just completed: {', '.join(details)}\n\n"
        f"Write a brief, energetic acknowledgment. Be specific to what they did. No filler."
    )

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=80,
        temperature=0.7,
    )
    message = response.choices[0].message.content.strip()

    if user.linq_chat_id:
        await linq_svc.send_message(user.linq_chat_id, message)
        _log(f"Sent workout ack to {user.name} ({sport})")

    # Vector memory for the workout
    try:
        from app.services.memory_search import store_memory
        from datetime import date as _date
        # Need a db session here — _handle_workout currently doesn't take one, so create a fresh one
        from app.database import async_session
        async with async_session() as _db:
            mem = f"WHOOP {sport} workout, {', '.join(details[1:]) or 'no details'} on {_date.today().isoformat()}"
            await store_memory(user.id, mem, "workout", _db)
    except Exception as _e:
        _log(f"memory workout store failed: {_e}")
