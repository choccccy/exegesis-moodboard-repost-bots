"""Tests for the bot status file writer/reader (bot.bot_status)."""

from __future__ import annotations

import json
import logging

import pytest

from bot import bot_status


@pytest.fixture
def status_dir(tmp_path):
    """Fresh status file per test; restores module globals and log handlers."""
    orig_file = bot_status._STATUS_FILE
    orig_status = bot_status._status
    http_logger = logging.getLogger("discord.http")
    orig_handlers = list(http_logger.handlers)

    bot_status.init(str(tmp_path))
    yield tmp_path

    bot_status._STATUS_FILE = orig_file
    bot_status._status = orig_status
    http_logger.handlers = orig_handlers


def _read_file(tmp_path) -> dict:
    return json.loads((tmp_path / "bot_status.json").read_text())


def _rate_limit_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="discord.http",
        level=logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_init_writes_status_file(status_dir):
    data = _read_file(status_dir)
    assert "started_at" in data
    assert data["rate_limit"] is None
    assert bot_status._STATUS_FILE == status_dir / "bot_status.json"


def test_init_adds_handler_to_discord_http_logger(status_dir):
    handlers = logging.getLogger("discord.http").handlers
    assert any(isinstance(h, bot_status._RateLimitHandler) for h in handlers)


def test_record_thread_count_persists(status_dir):
    bot_status.record_thread_count(42)
    assert _read_file(status_dir)["discord_active_threads"] == 42


def test_scan_started_adds_entry(status_dir):
    bot_status.scan_started(123, "robots", scan_type="manual")
    scans = _read_file(status_dir)["active_scans"]
    assert len(scans) == 1
    assert scans[0]["channel_id"] == 123
    assert scans[0]["channel_name"] == "robots"
    assert scans[0]["type"] == "manual"
    assert scans[0]["started_at"] > 0


def test_scan_started_dedupes_same_channel(status_dir):
    bot_status.scan_started(123, "robots")
    bot_status.scan_started(123, "robots")
    assert len(_read_file(status_dir)["active_scans"]) == 1


def test_scan_started_multiple_channels(status_dir):
    bot_status.scan_started(1, "a")
    bot_status.scan_started(2, "b")
    scans = _read_file(status_dir)["active_scans"]
    assert {s["channel_id"] for s in scans} == {1, 2}


def test_scan_finished_removes_only_that_channel(status_dir):
    bot_status.scan_started(1, "a")
    bot_status.scan_started(2, "b")
    bot_status.scan_finished(1)
    scans = _read_file(status_dir)["active_scans"]
    assert [s["channel_id"] for s in scans] == [2]


def test_scan_finished_on_empty_list_is_noop(status_dir):
    bot_status.scan_finished(999)
    assert _read_file(status_dir)["active_scans"] == []


def test_read_missing_file_returns_empty(tmp_path):
    assert bot_status.read(str(tmp_path / "nope")) == {}


def test_read_corrupt_json_returns_empty(tmp_path):
    (tmp_path / "bot_status.json").write_text("{not json!!")
    assert bot_status.read(str(tmp_path)) == {}


def test_read_round_trips_written_data(status_dir):
    bot_status.record_thread_count(7)
    data = bot_status.read(str(status_dir))
    assert data["discord_active_threads"] == 7
    assert "started_at" in data


def test_rate_limit_handler_records_retry_and_route(status_dir):
    msg = (
        "We are being rate limited. "
        "GET https://discord.com/api/v10/channels/123/messages responded with 429. "
        "Retrying in 12.5 seconds."
    )
    handler = bot_status._RateLimitHandler()
    handler.emit(_rate_limit_record(msg))
    rl = _read_file(status_dir)["rate_limit"]
    assert rl["retry_after"] == 12.5
    assert rl["route"] == "https://discord.com/api/v10/channels/123/messages"
    assert rl["until"] == pytest.approx(rl["last_seen_at"] + 12.5)


def test_rate_limit_handler_defaults_when_details_missing(status_dir):
    handler = bot_status._RateLimitHandler()
    handler.emit(_rate_limit_record("Global rate limit has been hit."))
    rl = _read_file(status_dir)["rate_limit"]
    assert rl["retry_after"] == 30.0
    assert rl["route"] == "unknown"


def test_rate_limit_handler_ignores_unrelated_messages(status_dir):
    handler = bot_status._RateLimitHandler()
    handler.emit(_rate_limit_record("Webhook fired successfully."))
    assert _read_file(status_dir)["rate_limit"] is None


def test_rate_limit_via_logger_emission(status_dir):
    logging.getLogger("discord.http").warning(
        "We are being rate limited. Retrying in 3.0 seconds."
    )
    rl = _read_file(status_dir)["rate_limit"]
    assert rl["retry_after"] == 3.0


def test_write_swallows_oserror_nonexistent_dir(status_dir):
    bot_status._STATUS_FILE = status_dir / "no" / "such" / "dir" / "s.json"
    bot_status._write()  # must not raise


def test_write_swallows_oserror_path_is_directory(status_dir):
    bot_status._STATUS_FILE = status_dir  # a directory, not a file
    bot_status._write()  # must not raise


def test_write_noop_when_uninitialised(status_dir):
    bot_status._STATUS_FILE = None
    bot_status._write()  # must not raise
