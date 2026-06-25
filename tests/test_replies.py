"""Tests for reply text functions (non-Discord, pure string output)."""

from bot.discord_ingest import replies


def test_duplicate_warning_includes_url():
    msg = replies.duplicate_warning("https://bsky.app/profile/foo/post/123")
    assert "https://bsky.app/profile/foo/post/123" in msg
    assert "⚠" in msg


def test_duplicate_warning_mentions_butterfly():
    msg = replies.duplicate_warning("https://bsky.app/profile/foo/post/123")
    assert "🦋" in msg


def test_publish_failed_notice_includes_error():
    msg = replies.publish_failed_notice("connection timeout")
    assert "connection timeout" in msg


def test_publish_failed_notice_unknown_error():
    msg = replies.publish_failed_notice(None)
    assert "unknown error" in msg


def test_publish_failed_notice_mentions_retry():
    msg = replies.publish_failed_notice(None)
    assert "retry" in msg.lower()


def test_metadata_confirm_emoji_is_link():
    assert replies.METADATA_CONFIRM_EMOJI == "🔗"


def test_metadata_request_includes_url():
    msg = replies.metadata_request("https://example.com/foo")
    assert "https://example.com/foo" in msg
    assert replies.METADATA_CONFIRM_EMOJI in msg


def test_metadata_confirmed_includes_emoji():
    msg = replies.metadata_confirmed()
    assert replies.METADATA_CONFIRM_EMOJI in msg


def test_metadata_link_updated_includes_url():
    msg = replies.metadata_link_updated("https://fxtwitter.com/user/status/123")
    assert "https://fxtwitter.com/user/status/123" in msg


def test_queued_notice_contains_queued():
    msg = replies.queued_notice()
    assert "queued" in msg.lower()



def test_queued_notice_with_bluesky_handle():
    msg = replies.queued_notice(bluesky_handle="robots.exegesis.space")
    assert "robots.exegesis.space" in msg
    assert "bsky.app" in msg


def test_queued_notice_playlist_only_when_videos_added():
    msg_no = replies.queued_notice(youtube_playlist_id="PLfoo", videos_added=0)
    assert "playlist" not in msg_no
    msg_yes = replies.queued_notice(youtube_playlist_id="PLfoo", videos_added=1)
    assert "playlist" in msg_yes
    assert "youtube.com" in msg_yes


def test_publish_failed_notice_mentions_slot():
    msg = replies.publish_failed_notice(None)
    assert "slot" in msg.lower()


def test_source_request_is_string():
    msg = replies.source_request()
    assert "source" in msg.lower() and "url" in msg.lower()


def test_ready_confirmation():
    msg = replies.ready_confirmation()
    assert "ready" in msg.lower()


def test_published_notice_includes_url():
    msg = replies.published_notice("https://bsky.app/profile/x/post/1")
    assert "https://bsky.app/profile/x/post/1" in msg


def test_reposted_notice_includes_url():
    msg = replies.reposted_notice("https://bsky.app/profile/x/post/1")
    assert "https://bsky.app/profile/x/post/1" in msg


def test_cannot_remove_published_includes_url():
    msg = replies.cannot_remove_published("https://bsky.app/profile/x/post/1")
    assert "https://bsky.app/profile/x/post/1" in msg


def test_supplemental_image_request_is_string():
    msg = replies.supplemental_image_request()
    assert isinstance(msg, str)
    assert len(msg) > 0
    assert "image" in msg.lower()


# ---------------------------------------------------------------------------
# _paginate
# ---------------------------------------------------------------------------

def test_paginate_short_content_single_page():
    pages = replies._paginate(["line one", "line two", "line three"])
    assert len(pages) == 1
    assert pages[0] == "line one\nline two\nline three"


def test_paginate_splits_at_line_boundary():
    limit = 20
    # Each line is 10 chars; two fit (10 + 1 + 10 = 21 > 20), so split after first.
    lines = ["0123456789", "0123456789", "short"]
    pages = replies._paginate(lines)
    for page in pages:
        assert len(page) <= replies._DISCORD_MSG_LIMIT


def test_paginate_continuation_header_appears():
    # Force a split by using a tiny effective limit via many lines.
    long_lines = ["x" * 60] * 40  # 40 lines * 60 chars + newlines > 1900
    pages = replies._paginate(long_lines, header="(cont.)")
    assert len(pages) >= 2
    for page in pages[1:]:
        assert page.startswith("(cont.)")


def test_paginate_no_page_exceeds_limit():
    lines = ["a" * 100] * 25  # 2500 chars total, needs splitting
    pages = replies._paginate(lines)
    for page in pages:
        assert len(page) <= replies._DISCORD_MSG_LIMIT


def test_paginate_empty_lines_preserved():
    pages = replies._paginate(["header", "", "body"])
    assert len(pages) == 1
    assert "\n\n" in pages[0]


def test_format_post_preview_returns_list():
    p = replies.PostPreview(
        kind="external",
        title="A Title",
        links=[("https://example.com/post", "external")],
        images=[],
        embed_title="A Title",
        embed_description="A description",
        embed_has_thumb=True,
        resolved_via="opengraph",
        board_name="robots",
        nsfw=False,
    )
    result = replies.format_post_preview(p)
    assert isinstance(result, list)
    assert len(result) >= 1
    assert all(len(page) <= replies._DISCORD_MSG_LIMIT for page in result)


def test_format_post_preview_overflow_spans_multiple_pages():
    # A preview with many images and very long alt text should spill across pages.
    many_images = [(f"image_{i:02d}.jpg", "a" * 200) for i in range(20)]
    p = replies.PostPreview(
        kind="images",
        title="A very long title " * 10,
        links=[("https://example.com/post", "images")],
        images=many_images,
        embed_title=None,
        embed_description=None,
        embed_has_thumb=False,
        board_name="robots",
        nsfw=False,
    )
    result = replies.format_post_preview(p)
    assert len(result) >= 2
    assert all(len(page) <= replies._DISCORD_MSG_LIMIT for page in result)
    assert any("cont." in page for page in result[1:])
