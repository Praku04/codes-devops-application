"""IST ISO serialization for session timestamps."""

from datetime import datetime, timezone

from app.db.session import _iso_ist


def test_iso_ist_naive_utc_converted():
    # Stored as UTC wall clock naive → shown as IST (+5:30)
    dt = datetime(2026, 7, 28, 6, 0, 0)
    assert _iso_ist(dt) == "2026-07-28T11:30:00+05:30"


def test_iso_ist_aware_utc_converted():
    dt = datetime(2026, 7, 28, 6, 0, 0, tzinfo=timezone.utc)
    assert _iso_ist(dt) == "2026-07-28T11:30:00+05:30"


def test_iso_ist_none():
    assert _iso_ist(None) is None
