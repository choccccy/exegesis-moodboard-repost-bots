"""Tests for pure helpers in src/bot/publish/__init__.py.

Complements tests/test_publish.py: covers _compress_for_bsky with real
Pillow-generated images, _error_detail, the remaining _append_tags /
at_uri_to_url / _post_text_and_facets / _determine_kind branches.
"""

from __future__ import annotations

import io
import os
from unittest.mock import MagicMock, patch

from PIL import Image

from bot.models import SubmissionLink
from bot.publish import (
    _BSKY_MAX_BLOB,
    _append_tags,
    _compress_for_bsky,
    _determine_kind,
    _error_detail,
    _post_text_and_facets,
    at_uri_to_url,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _noise_image_bytes(size: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
    """Random-noise image: incompressible, so PNG size ~= pixel count * depth."""
    channels = len(mode)
    img = Image.frombytes(mode, (size, size), os.urandom(size * size * channels))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _animated_gif_bytes(size=(24, 24), n_frames=3) -> bytes:
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", size, colors[i % len(colors)]) for i in range(n_frames)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def _link(domain_family: str = "artstation") -> SubmissionLink:
    link = MagicMock(spec=SubmissionLink)
    link.domain_family = domain_family
    return link


# ---------------------------------------------------------------------------
# _compress_for_bsky
# ---------------------------------------------------------------------------

def test_compress_small_static_image_reencoded_as_jpeg_under_limit():
    data = _noise_image_bytes(32)
    out = _compress_for_bsky(data)
    assert len(out) <= _BSKY_MAX_BLOB
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_compress_oversized_image_fits_under_limit():
    data = _noise_image_bytes(640)  # noise PNG ~= 640*640*3 bytes, well over 975k
    assert len(data) > _BSKY_MAX_BLOB
    out = _compress_for_bsky(data)
    assert len(out) <= _BSKY_MAX_BLOB
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_compress_rgba_converted_to_rgb_jpeg():
    data = _noise_image_bytes(32, mode="RGBA")
    out = _compress_for_bsky(data)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_compress_animated_gif_becomes_animated_webp():
    out = _compress_for_bsky(_animated_gif_bytes(n_frames=3))
    img = Image.open(io.BytesIO(out))
    assert img.format == "WEBP"
    assert getattr(img, "n_frames", 1) > 1


def test_compress_animated_gif_falls_back_to_static_jpeg_when_nothing_fits():
    with patch("bot.publish._BSKY_MAX_BLOB", 1):
        out = _compress_for_bsky(_animated_gif_bytes())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert getattr(img, "n_frames", 1) == 1


def test_compress_gives_up_after_resolution_halving():
    # An impossible limit exercises the halving loop and the final give-up return.
    data = _noise_image_bytes(300)
    with patch("bot.publish._BSKY_MAX_BLOB", 1):
        out = _compress_for_bsky(data)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.width <= 100  # halved 300 -> 150 -> 75 before giving up


# ---------------------------------------------------------------------------
# _error_detail
# ---------------------------------------------------------------------------

def test_error_detail_short_message_passthrough():
    assert _error_detail(ValueError("boom")) == "ValueError: boom"


def test_error_detail_collapses_whitespace():
    exc = RuntimeError("line one\n   line   two\ttabbed")
    assert _error_detail(exc) == "RuntimeError: line one line two tabbed"


def test_error_detail_truncates_long_messages():
    detail = _error_detail(Exception("x" * 500))
    assert len(detail) == 303  # noqa: PLR2004 - 300 chars + "..."
    assert detail.endswith("...")
    assert detail.startswith("Exception: ")


# ---------------------------------------------------------------------------
# _append_tags - budget exhaustion mid-list
# ---------------------------------------------------------------------------

def test_append_tags_stops_mid_list_when_budget_exhausted():
    text = "A" * 290
    text, facets = _append_tags(text, [], ["ab", "waytoolongtag"])
    # " #ab" (4 chars) fits at 294; " #waytoolongtag" (15 chars) would hit 309 > 300.
    assert "#ab" in text
    assert "#waytoolongtag" not in text
    assert len(facets) == 1
    assert facets[0].features[0].tag == "ab"


# ---------------------------------------------------------------------------
# at_uri_to_url - remaining branches
# ---------------------------------------------------------------------------

def test_at_uri_to_url_two_part_uri_returned_unchanged():
    uri = "at://did:plc:abc/app.bsky.feed.post"  # no rkey - too few parts
    assert at_uri_to_url(uri) == uri


def test_at_uri_to_url_non_post_collection_ignores_handle():
    uri = "at://did:plc:abc/app.bsky.feed.like/xyz"
    assert at_uri_to_url(uri, "robots.exegesis.space") == uri


# ---------------------------------------------------------------------------
# _post_text_and_facets - hashtag facets in the title
# ---------------------------------------------------------------------------

def test_post_text_hashtag_in_title_gets_tag_facet():
    url = "https://example.com/p"
    text, facets = _post_text_and_facets("cool #robots art", url)
    tag_facets = [f for f in facets if hasattr(f.features[0], "tag")]
    assert len(tag_facets) == 1
    assert tag_facets[0].features[0].tag == "robots"
    text_bytes = text.encode("utf-8")
    sl = tag_facets[0].index
    assert text_bytes[sl.byte_start:sl.byte_end] == b"#robots"


def test_post_text_url_anchor_not_treated_as_hashtag():
    # "#anchor" inside a URL-like token must not produce a tag facet.
    text, facets = _post_text_and_facets("see example.com/page#anchor here", "https://example.com")
    tag_facets = [f for f in facets if hasattr(f.features[0], "tag")]
    assert tag_facets == []


def test_post_text_no_title_has_single_link_facet():
    _, facets = _post_text_and_facets(None, "https://example.com")
    assert len(facets) == 1
    assert facets[0].features[0].uri == "https://example.com"


# ---------------------------------------------------------------------------
# _determine_kind - video branches
# ---------------------------------------------------------------------------

def test_kind_video_when_has_uploaded_video():
    assert _determine_kind([_link()], False, True) == "video"


def test_kind_video_takes_precedence_over_images():
    assert _determine_kind([_link()], True, True) == "video"


def test_kind_record_takes_precedence_over_video():
    assert _determine_kind([_link(domain_family="bluesky")], True, True) == "record"
