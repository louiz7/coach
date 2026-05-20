"""Central PostHog analytics helper.

All capture/identify calls in the codebase route through here so we have one
canonical person-properties shape, one place to add new fields, and one
thread-wrapped capture path (PostHog's sync client otherwise blocks the asyncio
loop arq runs on).

Person properties are STRUCTURED ONLY — free-text fields (goal, sports_focus,
injuries, current_schedule_notes, equipment_access) intentionally stay in our
DB and never leave it. Everything PostHog sees is enums, scalars, booleans,
timestamps.
"""
import threading
from typing import Any

import posthog

from app.models.user import User


_STRUCTURED_FIELDS = (
    "name",
    "phone",
    "email",
    "language",
    "timezone",
    "training_frequency",
    "age",
    "gender",
    "weight_kg",
    "height_cm",
    "coach_style",
    "coach_intensity",
    "challenge",
    "persona_id",
    "onboarding_state",
    "onboarding_complete",
    "created_at",
)


def _person_props(user: User, extra: dict | None = None) -> dict[str, Any]:
    """Build the canonical person-properties dict from a User row."""
    props: dict[str, Any] = {}
    for field in _STRUCTURED_FIELDS:
        val = getattr(user, field, None)
        if val is None:
            continue
        # UUIDs and datetimes need to be JSON-serializable for posthog
        if hasattr(val, "isoformat"):
            props[field] = val.isoformat()
        else:
            props[field] = str(val) if not isinstance(val, (int, float, bool, str)) else val
    if extra:
        props.update(extra)
    return props


def _run_in_thread(fn, **kwargs) -> None:
    """Fire-and-forget thread wrapper so PostHog's sync client never blocks
    the asyncio loop. Errors are swallowed — analytics should never break the
    user-facing flow."""
    try:
        threading.Thread(target=fn, kwargs=kwargs, daemon=True).start()
    except Exception:
        pass


def identify(user: User, extra: dict | None = None) -> None:
    """Set person properties on PostHog for this user."""
    _run_in_thread(
        posthog.identify,
        distinct_id=str(user.id),
        properties=_person_props(user, extra),
    )


def capture(event: str, user: User, properties: dict | None = None) -> None:
    """Identify the user, then capture an event keyed on their id.

    The identify-before-capture pattern keeps the person profile fresh: any
    field that just changed (e.g. training_frequency was just set in
    _handle_status_quo) becomes visible on the PostHog person page before the
    event itself is processed.
    """
    identify(user)
    _run_in_thread(
        posthog.capture,
        distinct_id=str(user.id),
        event=event,
        properties=properties or {},
    )


def capture_by_id(event: str, distinct_id: str, properties: dict | None = None) -> None:
    """Capture an event when we don't have a User object handy.

    Use sparingly — prefer capture(event, user) so person properties stay in
    sync. This exists for edge cases like background jobs that only have a
    distinct_id string.
    """
    _run_in_thread(
        posthog.capture,
        distinct_id=distinct_id,
        event=event,
        properties=properties or {},
    )
