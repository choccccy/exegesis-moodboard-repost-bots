"""Tests for pure helpers in src/bot/discord_ingest/service.py.

Covers _triage_relative, _queue_action, the Discord file conversion helpers
(_discord_file_for_attachment / _discord_file_for_animated_gif) with real
Pillow-generated images, and the _primary_link / _image_status preview helpers.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import discord
from PIL import Image

from bot.discord_ingest.service import (
    _discord_file_for_animated_gif,
    _discord_file_for_attachment,
    _image_status,
    _primary_link,
    _queue_action,
    _triage_relative,
)
from bot.models import Attachment, SubmissionLink
from bot.state import SubmissionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _link(resolved_image_path: str | None = None, resolved_via: str | None = None) -> SubmissionLink:
    link = MagicMock(spec=SubmissionLink)
    link.canonical_url = "https://example.com/post"
    link.resolved_image_path = resolved_image_path
    link.resolved_via = resolved_via
    return link


def _att(is_image: bool = False, is_video: bool = False) -> Attachment:
    att = MagicMock(spec=Attachment)
    att.is_image = is_image
    att.is_video = is_video
    return att


def _write_animated_gif(path, size=(24, 24), n_frames=3) -> None:
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", size, colors[i % len(colors)]) for i in range(n_frames)]
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)


# ---------------------------------------------------------------------------
# _triage_relative
# ---------------------------------------------------------------------------

def test_triage_relative_none_is_question_mark():
    assert _triage_relative(None) == "?"


def test_triage_relative_just_now():
    dt = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert _triage_relative(dt) == "just now"


def test_triage_relative_minutes():
    dt = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert _triage_relative(dt) == "5m ago"


def test_triage_relative_hours():
    dt = datetime.now(timezone.utc) - timedelta(hours=3)
    assert _triage_relative(dt) == "3h ago"


def test_triage_relative_days():
    dt = datetime.now(timezone.utc) - timedelta(days=2)
    assert _triage_relative(dt) == "2d ago"


def test_triage_relative_naive_datetime_treated_as_utc():
    # SQLite returns naive datetimes; they must be interpreted as UTC.
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    assert _triage_relative(naive) == "2h ago"


# ---------------------------------------------------------------------------
# _queue_action
# ---------------------------------------------------------------------------

def test_queue_action_none_when_not_ready():
    assert _queue_action(
        SubmissionState.AWAITING_SOURCE.value, SubmissionState.AWAITING_SOURCE
    ) == "none"


def test_queue_action_fresh_from_pre_queue_state():
    assert _queue_action(
        SubmissionState.AWAITING_ALT_TEXT.value, SubmissionState.READY_TO_QUEUE
    ) == "fresh"


def test_queue_action_silent_when_already_ready():
    assert _queue_action(
        SubmissionState.READY_TO_QUEUE.value, SubmissionState.READY_TO_QUEUE
    ) == "silent"


def test_queue_action_none_for_terminal_states():
    for terminal in (
        SubmissionState.QUEUED.value,
        SubmissionState.PUBLISHED.value,
        SubmissionState.PUBLISH_FAILED.value,
    ):
        assert _queue_action(terminal, SubmissionState.READY_TO_QUEUE) == "none"


# ---------------------------------------------------------------------------
# _discord_file_for_attachment
# ---------------------------------------------------------------------------

def test_attachment_static_png_kept_as_is(tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGB", (10, 10), (200, 30, 30)).save(path, format="PNG")

    f = _discord_file_for_attachment(str(path), "pic.png")

    assert isinstance(f, discord.File)
    assert f.filename == "pic.png"
    out = Image.open(f.fp)
    assert out.format == "PNG"
    assert out.size == (10, 10)


def test_attachment_oversized_image_resized_to_preview_max(tmp_path):
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 400), (10, 10, 10)).save(path, format="PNG")

    f = _discord_file_for_attachment(str(path), "big.png")

    out = Image.open(f.fp)
    assert max(out.size) <= 1920
    # Aspect ratio roughly preserved (10:1).
    assert out.size[0] == 1920


def test_attachment_unknown_format_palette_flattened_to_jpeg(tmp_path):
    # BMP is not in the allowlist, so fmt falls back to JPEG; the palette (P)
    # mode must be flattened to RGB before JPEG encoding.
    path = tmp_path / "pal.bmp"
    Image.new("RGB", (12, 12), (5, 5, 5)).convert("P").save(path, format="BMP")

    f = _discord_file_for_attachment(str(path), "pal.bmp")

    out = Image.open(f.fp)
    assert out.format == "JPEG"
    assert out.mode == "RGB"


def test_attachment_animated_gif_delegates_to_webp(tmp_path):
    path = tmp_path / "anim.gif"
    _write_animated_gif(path)

    f = _discord_file_for_attachment(str(path), "anim.gif")

    assert f.filename == "anim.webp"
    out = Image.open(f.fp)
    assert out.format == "WEBP"


# ---------------------------------------------------------------------------
# _discord_file_for_animated_gif
# ---------------------------------------------------------------------------

def test_animated_gif_converted_to_animated_webp(tmp_path):
    path = tmp_path / "anim.gif"
    _write_animated_gif(path, n_frames=3)

    with Image.open(path) as img:
        f = _discord_file_for_animated_gif(img, "anim.gif")

    assert f.filename == "anim.webp"
    out = Image.open(f.fp)
    assert out.format == "WEBP"
    assert getattr(out, "n_frames", 1) > 1


def test_animated_gif_falls_back_to_static_jpeg_when_nothing_fits(tmp_path):
    path = tmp_path / "anim.gif"
    _write_animated_gif(path)

    with patch("bot.discord_ingest.service._DISCORD_MAX_BYTES", 1):
        with Image.open(path) as img:
            f = _discord_file_for_animated_gif(img, "anim.gif")

    assert f.filename == "anim.jpg"
    out = Image.open(f.fp)
    assert out.format == "JPEG"
    assert getattr(out, "n_frames", 1) == 1


def test_animated_gif_filename_without_extension_gets_suffix(tmp_path):
    path = tmp_path / "anim.gif"
    _write_animated_gif(path)

    with Image.open(path) as img:
        f = _discord_file_for_animated_gif(img, "noext")

    assert f.filename == "noext.webp"


def test_animated_gif_oversized_frames_are_downscaled(tmp_path):
    path = tmp_path / "wide.gif"
    _write_animated_gif(path, size=(2400, 240))

    with Image.open(path) as img:
        f = _discord_file_for_animated_gif(img, "wide.gif")

    out = Image.open(f.fp)
    assert max(out.size) <= 1920


# ---------------------------------------------------------------------------
# _primary_link / _image_status
# ---------------------------------------------------------------------------

def test_primary_link_returns_first_or_none():
    first, second = _link(), _link()
    assert _primary_link([first, second]) is first
    assert _primary_link([]) is None


def test_image_status_record_kind_is_na():
    ok, source = _image_status("record", [], [_link()])
    assert ok
    assert source.startswith("n/a")


def test_image_status_video_kind_counts_videos():
    atts = [_att(is_video=True), _att(is_video=True), _att(is_image=True)]
    ok, source = _image_status("video", atts, [])
    assert ok
    assert "2 video(s)" in source


def test_image_status_uploaded_images_counted():
    atts = [_att(is_image=True), _att(is_image=True)]
    ok, source = _image_status("images", atts, [_link()])
    assert ok
    assert "2 uploaded image(s)" in source


def test_image_status_external_thumbnail_mentions_resolver():
    links = [_link(resolved_image_path="/data/thumb.jpg", resolved_via="opengraph")]
    ok, source = _image_status("external", [], links)
    assert ok
    assert "thumbnail" in source
    assert "opengraph" in source


def test_image_status_no_image_at_all_fails():
    ok, source = _image_status("external", [], [_link(resolved_image_path=None)])
    assert not ok
    assert "no image" in source
