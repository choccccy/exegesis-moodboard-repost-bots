"""Tests for queue scheduling logic - pure functions only (no DB/Discord)."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.queue import daily_cap
from bot.scheduler import _next_fire_time, _mt_midnight


MT = ZoneInfo("America/Denver")

# ---------------------------------------------------------------------------
# daily_cap
# ---------------------------------------------------------------------------


class _FakeSettings:
    queue_fresh_daily_cap = 6
    queue_backlog_daily_cap = 3


def test_daily_cap_when_fresh_available():
    assert daily_cap(True, _FakeSettings()) == 6


def test_daily_cap_when_only_backlog():
    assert daily_cap(False, _FakeSettings()) == 3


# ---------------------------------------------------------------------------
# _next_fire_time  (hour boundaries in local timezone, start_hour=12)
# ---------------------------------------------------------------------------


def _mt(hour, minute=0, day=1):
    return datetime(2026, 1, day, hour, minute, 0, tzinfo=MT)


def test_next_fire_before_noon_snaps_to_noon():
    # 09:30 MT → next hour boundary = 10:00, which is < 12 → fire at 12:00
    result = _next_fire_time(_mt(9, 30), start_hour=12)
    assert result.hour == 12
    assert result.minute == 0


def test_next_fire_at_noon_snaps_to_1300():
    # Exactly noon: next hour boundary is 13:00, which is >= 12 → fire at 13:00
    result = _next_fire_time(_mt(12, 0), start_hour=12)
    assert result.hour == 13


def test_next_fire_after_noon():
    # 13:45 MT → next hour boundary = 14:00 >= 12 → fire at 14:00
    result = _next_fire_time(_mt(13, 45), start_hour=12)
    assert result.hour == 14
    assert result.minute == 0


def test_next_fire_early_morning_snaps_to_noon():
    # 00:15 MT → next hour = 01:00, < 12 → snap to 12:00 same day
    result = _next_fire_time(_mt(0, 15), start_hour=12)
    assert result.hour == 12
    assert result.day == 1


def test_next_fire_late_evening_wraps_to_next_day_noon():
    # 23:30 MT → next hour = 00:00 next day, < 12 → snap to 12:00 next day
    result = _next_fire_time(_mt(23, 30, day=1), start_hour=12)
    assert result.day == 2
    assert result.hour == 12


def test_next_fire_just_before_midnight():
    # 23:00 MT (exact hour boundary) → next = 00:00 next day, < 12 → snap to noon
    result = _next_fire_time(_mt(23, 0, day=1), start_hour=12)
    assert result.day == 2
    assert result.hour == 12


def test_next_fire_custom_start_hour():
    # start_hour=9: 08:30 → next boundary = 09:00, which equals start_hour → fires at 09:00
    result = _next_fire_time(_mt(8, 30), start_hour=9)
    assert result.hour == 9


def test_next_fire_seconds_and_microseconds_zeroed():
    result = _next_fire_time(_mt(10, 45), start_hour=12)
    assert result.second == 0
    assert result.microsecond == 0


# ---------------------------------------------------------------------------
# _mt_midnight
# ---------------------------------------------------------------------------


def test_mt_midnight_returns_utc_aware():
    now_utc = datetime(2026, 6, 1, 18, 30, tzinfo=timezone.utc)  # 12:30 MDT (UTC-6)
    midnight = _mt_midnight(now_utc, MT)
    assert midnight.tzinfo is not None
    # UTC offset for MDT is -6h; MT midnight on Jun 1 is 06:00 UTC
    assert midnight.hour == 6
    assert midnight.minute == 0


def test_mt_midnight_before_local_midnight():
    # 01:00 UTC on Jun 2 is still Jun 1 in MDT (UTC-6 → 19:00 Jun 1 MDT)
    now_utc = datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc)
    midnight = _mt_midnight(now_utc, MT)
    # MDT midnight Jun 1 = 06:00 UTC Jun 1
    assert midnight == datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
