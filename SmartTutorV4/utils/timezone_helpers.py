"""IST (Asia/Kolkata) timezone helpers for SmartTutor."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    # Windows Python needs the `tzdata` package for IANA zones.
    try:
        import tzdata  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Timezone data is missing. Install with: pip install tzdata"
        ) from exc
    IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Return the current time in IST (Asia/Kolkata)."""
    return datetime.now(IST)


def format_ist_iso(dt: datetime | None) -> str | None:
    """Serialize a datetime for API/JSON use with explicit IST offset."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat(timespec="seconds")


def format_ist_display(dt: datetime | None, fallback: str = "") -> str:
    """Human-readable IST timestamp for PDFs and reports."""
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
