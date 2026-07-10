"""
Guest Scheduling
================

The one read a guest gets about the owner: **availability, never the calendar**.

`owner_availability` answers "when could I meet the owner?" from Google's
free/busy API — which returns busy *intervals* and nothing else, so there are no
titles, attendees, or locations to leak even in principle. The tool reduces those
intervals to open windows inside business hours in the owner's timezone and hands
back a short list. A guest books time the same way they'd book with a good chief
of staff: hear the open slots, then leave a meeting request (`submit_update`) the
owner confirms. Guests never hold a calendar write — the owner (or their gated
`update_calendar`) puts things on the calendar.

Fail closed: when Google isn't configured (or the token is unusable) the tool is
never added to the toolset at all — see `guest_scheduling_tools`.
"""

from datetime import date, datetime, time, timedelta
from os import getenv
from zoneinfo import ZoneInfo

from agno.run import RunContext
from agno.tools import tool
from agno.utils.log import log_warning

from app.settings import owner_timezone

# Business-hours window (owner-local) that availability is reported within.
_DAY_START = time(9, 0)
_DAY_END = time(18, 0)

# Bounds on how far out a guest can look, and the smallest slot worth offering.
_DEFAULT_DAYS = 5
_MAX_DAYS = 14
_MIN_SLOT = timedelta(minutes=30)


def _fetch_busy(window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]] | None:
    """The owner's busy intervals in the window, from Google's free/busy API.

    Returns `None` on any failure (unusable token, API error) so callers can
    answer "couldn't check" instead of guessing. Only intervals come back from
    this API — never event contents.
    """
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        from agents.sources import calendar_token_path

        creds = Credentials.from_authorized_user_file(calendar_token_path())
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        response = (
            service.freebusy()
            .query(
                body={
                    "timeMin": window_start.isoformat(),
                    "timeMax": window_end.isoformat(),
                    "items": [{"id": "primary"}],
                }
            )
            .execute()
        )
        busy = response["calendars"]["primary"].get("busy", [])
        return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy]
    except Exception as exc:
        log_warning(f"owner_availability: free/busy lookup failed: {exc}")
        return None


def _free_windows(day_start: datetime, day_end: datetime, busy: list[tuple[datetime, datetime]]) -> list[str]:
    """The open windows between `busy` intervals inside one business day."""
    windows: list[str] = []
    cursor = day_start
    for b_start, b_end in sorted(busy):
        if b_end <= day_start or b_start >= day_end:
            continue
        if b_start - cursor >= _MIN_SLOT:
            windows.append(f"{cursor:%H:%M}–{min(b_start, day_end):%H:%M}")
        cursor = max(cursor, b_end)
    if day_end - cursor >= _MIN_SLOT:
        windows.append(f"{cursor:%H:%M}–{day_end:%H:%M}")
    return windows


@tool
def owner_availability(run_context: RunContext, start_date: str = "", days: int = _DEFAULT_DAYS) -> str:
    """When the owner has open time — free windows only, never calendar contents.

    Use this when someone wants to meet or book time with the owner. Returns the
    owner's open windows (business hours, owner's local time) for the coming
    days. It reveals only free/busy — no event names, attendees, or details.

    Args:
        start_date: First day to check, as YYYY-MM-DD. Defaults to today.
        days: How many days to look ahead (1-14, default 5).
    """
    tz = ZoneInfo(owner_timezone())
    try:
        first_day = date.fromisoformat(start_date) if start_date else datetime.now(tz).date()
    except ValueError:
        return f"I couldn't read {start_date!r} as a date — give it as YYYY-MM-DD."
    days = max(1, min(int(days), _MAX_DAYS))

    window_start = datetime.combine(first_day, _DAY_START, tz)
    window_end = datetime.combine(first_day + timedelta(days=days - 1), _DAY_END, tz)
    busy = _fetch_busy(window_start, window_end)
    if busy is None:
        return "I couldn't check the owner's availability right now — leave your ask as an update instead."

    lines: list[str] = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        if day.weekday() >= 5:  # weekends aren't offered
            continue
        day_start = datetime.combine(day, _DAY_START, tz)
        day_end = datetime.combine(day, _DAY_END, tz)
        if day_end <= datetime.now(tz):  # today, already over
            continue
        start_from = max(day_start, datetime.now(tz)) if day == datetime.now(tz).date() else day_start
        windows = _free_windows(start_from, day_end, [(s.astimezone(tz), e.astimezone(tz)) for s, e in busy])
        label = f"{day:%a %Y-%m-%d}"
        lines.append(f"- {label}: {', '.join(windows) if windows else 'no open windows'}")

    zone = f"{window_start:%Z}" or owner_timezone()
    if not lines:
        return "No business days in that window — try a different start date."
    return f"The owner's open windows ({zone}, business hours):\n" + "\n".join(lines)


def guest_scheduling_tools() -> list:
    """The scheduling tools a guest gets — `[owner_availability]` iff the
    calendar is connected, `[]` otherwise (fail closed: no Google, no tool)."""
    if getenv("GOOGLE_CLIENT_ID") and getenv("GOOGLE_CLIENT_SECRET"):
        return [owner_availability]
    return []
