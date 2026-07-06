"""Tests for pure helpers in the Discord client module.

Covers _format_triage (the /triage message renderer: empty states, filtered
bullet lists, unfiltered state grouping and priority ordering, truncation),
_build_intents (privileged message_content intent must stay enabled), and
_log_task_exception (the done-callback that surfaces silent asyncio task
failures). Triage items only need a handful of attributes, so a small
dataclass stands in for the real rows.
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

from bot.discord_ingest.client import (
    _TRIAGE_MAX_ITEMS,
    _build_intents,
    _format_triage,
    _log_task_exception,
)


@dataclass
class Item:
    title: str = "Cool robot"
    thread_url: str | None = "https://discord.com/channels/1/2/3"
    author_display: str = "osi"
    submitted_rel: str = "2 days ago"
    state: str = "ready_to_queue"


def test_format_triage_empty_without_filter_says_all_clear():
    out = _format_triage([], channel_name="robots", filter_label=None)
    assert "All clear" in out
    assert "#robots" in out
    assert "matching" not in out


def test_format_triage_empty_with_filter_names_the_filter():
    out = _format_triage([], channel_name="robots", filter_label="queued")
    assert "All clear" in out
    assert "matching 'queued'" in out


def test_format_triage_filtered_renders_markdown_link_bullets():
    items = [Item(title="A robot", thread_url="https://x.test/t/1")]
    out = _format_triage(items, channel_name="robots", filter_label="queued")
    assert "[A robot](<https://x.test/t/1>)" in out
    assert out.count("•") == 1
    assert "osi" in out
    assert "2 days ago" in out


def test_format_triage_filtered_header_has_count_and_label():
    items = [Item(), Item(title="Another")]
    out = _format_triage(items, channel_name="robots", filter_label="queued")
    header = out.splitlines()[0]
    assert "#robots" in header
    assert "2 open" in header
    assert "queued" in header


def test_format_triage_missing_thread_url_renders_bare_title():
    items = [Item(title="No thread yet", thread_url=None)]
    out = _format_triage(items, channel_name="robots", filter_label="queued")
    assert "No thread yet" in out
    assert "[No thread yet]" not in out


def test_format_triage_unfiltered_groups_by_state_with_labels():
    items = [
        Item(title="one", state="ready_to_queue"),
        Item(title="two", state="awaiting_source"),
        Item(title="three", state="awaiting_source"),
    ]
    out = _format_triage(items, channel_name="robots", filter_label=None)
    assert "**Needs confirmation** (1)" in out
    assert "**Awaiting source** (2)" in out


def test_format_triage_unfiltered_orders_groups_by_state_priority():
    # Insert in reverse priority order; output must follow the label table order.
    items = [
        Item(title="queued item", state="queued"),
        Item(title="fresh item", state="intent_submitted"),
        Item(title="ready item", state="ready_to_queue"),
    ]
    out = _format_triage(items, channel_name="robots", filter_label=None)
    ready_pos = out.index("Needs confirmation")
    queued_pos = out.index("Queued")
    fresh_pos = out.index("Just submitted")
    assert ready_pos < queued_pos < fresh_pos


def test_format_triage_unknown_state_sorts_last_and_uses_raw_state_as_label():
    items = [
        Item(title="weird", state="totally_unknown"),
        Item(title="fresh", state="intent_submitted"),
    ]
    out = _format_triage(items, channel_name="robots", filter_label=None)
    assert "**totally_unknown** (1)" in out
    assert out.index("Just submitted") < out.index("totally_unknown")


def test_format_triage_truncates_past_max_items():
    items = [Item(title=f"item {i}") for i in range(_TRIAGE_MAX_ITEMS + 5)]
    out = _format_triage(items, channel_name="robots", filter_label="queued")
    assert "and 5 more" in out
    assert f"item {_TRIAGE_MAX_ITEMS - 1}" in out
    assert f"item {_TRIAGE_MAX_ITEMS}" not in out


def test_build_intents_enables_message_content():
    intents = _build_intents()
    assert intents.message_content is True


async def test_log_task_exception_completed_task_logs_nothing():
    async def ok() -> int:
        return 1

    task = asyncio.create_task(ok())
    await task
    with patch("bot.discord_ingest.client.log") as mock_log:
        _log_task_exception(task)
    mock_log.exception.assert_not_called()


async def test_log_task_exception_swallows_cancellation():
    task = asyncio.create_task(asyncio.sleep(60))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with patch("bot.discord_ingest.client.log") as mock_log:
        _log_task_exception(task)  # must not raise
    mock_log.exception.assert_not_called()


async def test_log_task_exception_logs_unhandled_exception():
    async def boom() -> None:
        raise ValueError("kaput")

    task = asyncio.create_task(boom(), name="boom-task")
    await asyncio.gather(task, return_exceptions=True)
    with patch("bot.discord_ingest.client.log") as mock_log:
        _log_task_exception(task)
    mock_log.exception.assert_called_once()
    assert "boom-task" in mock_log.exception.call_args.args
