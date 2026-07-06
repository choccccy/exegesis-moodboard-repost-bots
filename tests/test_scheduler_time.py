"""Tests for the scheduler's pure time helpers.

Covers _next_fire_time (next top-of-hour gated by a daily start hour) and
_mt_midnight (start of the current local day as a UTC-aware datetime).
These are pure functions, so we pass in explicit datetimes rather than
patching the clock. _mt_midnight is checked against America/Denver in both
DST (UTC-6) and standard time (UTC-7) to pin the offset handling.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.scheduler import _mt_midnight, _next_fire_time

DENVER = ZoneInfo("America/Denver")


def test_next_fire_before_start_hour_snaps_to_start_hour():
    now = datetime(2026, 7, 1, 3, 15, tzinfo=DENVER)
    result = _next_fire_time(now, 9)
    assert result == datetime(2026, 7, 1, 9, 0, tzinfo=DENVER)


def test_next_fire_after_start_hour_goes_to_next_top_of_hour():
    now = datetime(2026, 7, 1, 14, 45, tzinfo=DENVER)
    result = _next_fire_time(now, 9)
    assert result == datetime(2026, 7, 1, 15, 0, tzinfo=DENVER)


def test_next_fire_zeroes_minute_second_microsecond():
    now = datetime(2026, 7, 1, 10, 37, 22, 123456, tzinfo=DENVER)
    result = _next_fire_time(now, 9)
    assert (result.minute, result.second, result.microsecond) == (0, 0, 0)


def test_next_fire_one_hour_before_start_hour_lands_on_start_hour():
    now = datetime(2026, 7, 1, 8, 0, tzinfo=DENVER)
    result = _next_fire_time(now, 9)
    assert result == datetime(2026, 7, 1, 9, 0, tzinfo=DENVER)


def test_next_fire_at_hour_23_rolls_to_next_day_start_hour():
    # +1h moves the date forward to hour 0; 0 < start_hour, so the hour is
    # replaced with start_hour on that already-rolled next day.
    now = datetime(2026, 7, 1, 23, 30, tzinfo=DENVER)
    result = _next_fire_time(now, 9)
    assert result == datetime(2026, 7, 2, 9, 0, tzinfo=DENVER)


def test_next_fire_at_hour_23_with_start_hour_zero_is_next_day_midnight():
    now = datetime(2026, 7, 1, 23, 30, tzinfo=DENVER)
    result = _next_fire_time(now, 0)
    assert result == datetime(2026, 7, 2, 0, 0, tzinfo=DENVER)


def test_mt_midnight_summer_denver_is_six_utc():
    # July: America/Denver observes DST (UTC-6), so local midnight is 06:00 UTC.
    now_utc = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    result = _mt_midnight(now_utc, DENVER)
    assert result == datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_mt_midnight_winter_denver_is_seven_utc():
    # January: standard time (UTC-7), so local midnight is 07:00 UTC.
    now_utc = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    result = _mt_midnight(now_utc, DENVER)
    assert result == datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_mt_midnight_uses_local_day_when_utc_date_is_ahead():
    # 03:00 UTC on July 15 is still the evening of July 14 in Denver, so the
    # midnight returned belongs to the local (previous) day.
    now_utc = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)
    result = _mt_midnight(now_utc, DENVER)
    assert result == datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)
