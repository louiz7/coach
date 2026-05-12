"""ICS calendar feed for a user's training plan.

GET /calendar/{token}.ics
  - token is the standard plan JWT (30-day TTL)
  - Returns a webcal-compatible ICS file with one VEVENT per training day,
    repeating weekly, starting from the Monday of the current week.
  - Since the ICS is generated live from plan_json, it always reflects
    the latest plan when the calendar app syncs.
"""

from datetime import date, timedelta, datetime, timezone
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from fastapi import Request
from sqlalchemy import select
import os

from app.database import async_session
from app.models.user import User
from app.models.training_plan import TrainingPlan
from app.services.token import verify_onboarding_token
from app.config import settings

router = APIRouter(tags=["calendar"])
_templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

_WEEKDAY_MAP = {
    "monday":    0,
    "tuesday":   1,
    "wednesday": 2,
    "thursday":  3,
    "friday":    4,
    "saturday":  5,
    "sunday":    6,
}


def _this_week_date(weekday_index: int) -> date:
    """Return the date of the given weekday (0=Mon) in the current ISO week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=weekday_index)


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold long lines per RFC 5545 (max 75 octets, continuation with CRLF + space)."""
    result = []
    while len(line.encode("utf-8")) > 75:
        chunk = line[:75]
        result.append(chunk)
        line = " " + line[75:]
    result.append(line)
    return "\r\n".join(result)


def _build_ics(user_name: str, plan_json: dict) -> str:
    days = plan_json.get("days", [])
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    events = []
    for day in days:
        day_name = str(day.get("day", "")).strip().lower()
        weekday_idx = _WEEKDAY_MAP.get(day_name)
        if weekday_idx is None:
            continue  # skip rest days or unknown names

        focus = day.get("focus", "Training")
        exercises = day.get("exercises", [])

        # Build description: list of exercises
        ex_lines = []
        for ex in exercises:
            name = ex.get("name", "")
            sets = ex.get("sets", "?")
            reps = ex.get("reps", "?")
            notes = ex.get("notes", "")
            line = f"• {name}: {sets}x{reps}"
            if notes:
                line += f" — {notes}"
            ex_lines.append(line)
        description = f"{focus}\\n\\n" + "\\n".join(ex_lines) if ex_lines else focus

        # Start: this week's occurrence of that weekday at 07:00 local (DATE only, all-day variant)
        start_date = _this_week_date(weekday_idx)
        dtstart = start_date.strftime("%Y%m%d")
        # End = next day (all-day event convention)
        dtend = (start_date + timedelta(days=1)).strftime("%Y%m%d")

        uid = f"kano-{day_name}-{user_name.lower().replace(' ', '')}@kano.fit"

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:Kano — {_ics_escape(focus)} 💪",
            f"DESCRIPTION:{_ics_escape(description)}",
            "RRULE:FREQ=WEEKLY",
            "END:VEVENT",
        ]
        events.append("\r\n".join(event_lines))

    cal = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Kano//Training Plan//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Kano — {_ics_escape(user_name)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        *events,
        "END:VCALENDAR",
    ])
    return cal


@router.get("/calendar/{token}")
async def calendar_landing(token: str, request: Request):
    """HTML landing page with OG tags for iMessage preview + auto-redirect to webcal://."""
    try:
        verify_onboarding_token(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired calendar link")

    base_url = settings.PUBLIC_BASE_URL.rstrip("/")
    page_url = f"{base_url}/calendar/{token}"
    webcal_url = f"webcal://{base_url.lstrip('https://').lstrip('http://')}/calendar/{token}.ics"

    return _templates.TemplateResponse(request, "calendar.html", {
        "base_url": base_url,
        "page_url": page_url,
        "webcal_url": webcal_url,
    })


@router.get("/calendar/{token}.ics")
async def get_calendar(token: str):
    try:
        payload = verify_onboarding_token(token)
        phone = payload.get("phone")
    except Exception:
        raise HTTPException(401, "Invalid or expired calendar link")

    async with async_session() as db:
        result = await db.execute(select(User).where(User.phone == phone))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(404, "User not found")

        plan_result = await db.execute(
            select(TrainingPlan)
            .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
            .order_by(TrainingPlan.created_at.desc())
        )
        plan = plan_result.scalars().first()
        if not plan or not plan.plan_json:
            raise HTTPException(404, "No active training plan")

    ics_content = _build_ics(user.name or "Athlete", plan.plan_json)

    return Response(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="kano-plan.ics"',
            "Cache-Control": "no-cache",
        },
    )
