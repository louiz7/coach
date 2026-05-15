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


_FOCUS_EMOJI = {
    "push":       "🏋️",
    "pull":       "🔙",
    "legs":       "🦵",
    "upper":      "💪",
    "lower":      "🦵",
    "full body":  "⚡",
    "full":       "⚡",
    "run":        "🏃",
    "running":    "🏃",
    "cardio":     "🏃",
    "hiit":       "🔥",
    "mobility":   "🧘",
    "yoga":       "🧘",
    "rest":       "😴",
}

def _focus_emoji(focus: str) -> str:
    f = focus.lower()
    for key, emoji in _FOCUS_EMOJI.items():
        if key in f:
            return emoji
    return "🏋️"


def _build_exercise_lines(exercises: list) -> list[str]:
    """Return bullet lines for each exercise — one line per exercise."""
    lines = []
    for ex in exercises:
        name = ex.get("name", "").strip()
        if not name:
            continue
        parts = []
        sets = ex.get("sets")
        reps = ex.get("reps")
        rpe  = ex.get("rpe")
        rest = ex.get("rest") or ex.get("rest_seconds")
        if sets and reps:
            parts.append(f"{sets}×{reps}")
        elif sets:
            parts.append(f"{sets} sets")
        if rpe:
            parts.append(f"RPE {rpe}")
        if rest:
            rest_str = str(rest)
            if rest_str.isdigit():
                rest_str = f"{rest_str}s rest"
            parts.append(rest_str)
        detail = "  " + "  ·  ".join(parts) if parts else ""
        notes = (ex.get("notes") or "").strip()
        line = f"• {name}"
        if detail:
            line += f"\n  {detail.strip()}"
        if notes:
            line += f"\n  ↳ {notes}"
        lines.append(line)
    return lines


def _build_ics(user_name: str, plan_json: dict) -> str:
    days = plan_json.get("days", [])
    plan_notes = (plan_json.get("notes") or "").strip()
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    total_days = len([d for d in days if _WEEKDAY_MAP.get(str(d.get("day", "")).strip().lower()) is not None])

    events = []
    training_day_num = 0
    for day in days:
        day_name = str(day.get("day", "")).strip().lower()
        weekday_idx = _WEEKDAY_MAP.get(day_name)
        if weekday_idx is None:
            continue

        focus = (day.get("focus") or "Training").strip()
        exercises = day.get("exercises") or []
        day_notes = (day.get("notes") or "").strip()
        is_rest = "rest" in focus.lower() and not exercises

        if not is_rest:
            training_day_num += 1

        emoji = _focus_emoji(focus)

        # Title: "Day N — Focus" like the screenshot
        summary = f"{emoji} Day {training_day_num} — {focus}" if not is_rest else f"😴 Rest — {focus}"

        # ── Description ──────────────────────────────────────────────
        lines: list[str] = []

        # Header line
        lines.append(f"Day {training_day_num} — {focus}")
        lines.append("─" * 30)
        lines.append("")

        if is_rest:
            lines.append("Active recovery day.")
            lines.append("Foam roll · light walk · stretch 10 min")
        else:
            # Warm-up block (generic unless plan_json has one)
            warmup = day.get("warmup") or day.get("warm_up")
            if warmup and isinstance(warmup, list):
                lines.append("WARM-UP:")
                for w in warmup:
                    lines.append(f"- {w}")
                lines.append("")

            # Main exercises
            ex_block = _build_exercise_lines(exercises)
            if ex_block:
                lines.append("EXERCISES:")
                lines.extend(ex_block)

            # Cool-down block
            cooldown = day.get("cooldown") or day.get("cool_down")
            if cooldown and isinstance(cooldown, list):
                lines.append("")
                lines.append("COOL-DOWN:")
                for c in cooldown:
                    lines.append(f"- {c}")

        if day_notes:
            lines.append("")
            lines.append(f"📝 {day_notes}")

        if plan_notes:
            lines.append("")
            lines.append(f"Plan note: {plan_notes}")

        # Join with real newlines — _ics_escape converts \n → \n (ICS spec)
        description = "\n".join(lines)

        start_date = _this_week_date(weekday_idx)
        dtstart = start_date.strftime("%Y%m%d")
        dtend = (start_date + timedelta(days=1)).strftime("%Y%m%d")
        uid = f"kano-{day_name}-{user_name.lower().replace(' ', '')}@kano.fit"

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{_ics_escape(summary)}",
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
