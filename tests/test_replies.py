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


def test_publish_failed_notice_pings_curators():
    msg = replies.publish_failed_notice("boom", mention_user_ids=[184475973356355584, 42])
    assert "<@184475973356355584>" in msg and "<@42>" in msg
    assert msg.startswith("<@184475973356355584>")  # mentions lead so they ping


def test_publish_failed_notice_no_mentions_when_empty():
    assert not replies.publish_failed_notice("boom").startswith("<@")
    assert not replies.publish_failed_notice("boom", mention_user_ids=[]).startswith("<@")


def test_metadata_confirm_emoji_is_link():
    assert replies.METADATA_CONFIRM_EMOJI == "🔗"


def test_metadata_request_includes_url():
    msg = replies.metadata_request("https://example.com/foo")
    assert "https://example.com/foo" in msg
    assert "as-is" in msg


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


def test_confirmation_request():
    msg = replies.confirmation_request()
    assert "queue" in msg.lower()
    assert len(msg) > 0


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
        links=[("https://example.com/post", "external", "A Title")],
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
        links=[("https://example.com/post", "images", None)],
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


# ---------------------------------------------------------------------------
# duplicate notices, _paginate boundaries, format_post_preview branches
# ---------------------------------------------------------------------------


def test_duplicate_queued_with_thread_url():
    out = replies.duplicate_queued("https://discord.com/channels/1/2/3")
    assert "already queued" in out
    assert "https://discord.com/channels/1/2/3" in out


def test_duplicate_queued_without_thread_url():
    out = replies.duplicate_queued(None)
    assert "already queued" in out
    assert "https://" not in out


def test_duplicate_pending_with_thread_url():
    out = replies.duplicate_pending("https://discord.com/channels/1/2/3")
    assert "already being processed" in out
    assert "https://discord.com/channels/1/2/3" in out


def test_duplicate_pending_without_thread_url():
    out = replies.duplicate_pending(None)
    assert "already being processed" in out
    assert "https://" not in out


def test_paginate_single_short_page():
    pages = replies._paginate(["one", "two"])
    assert pages == ["one\ntwo"]


def test_paginate_empty_lines_returns_single_empty_page():
    assert replies._paginate([]) == [""]


def test_paginate_splits_at_limit_with_continuation_header():
    long_line = "x" * (replies._DISCORD_MSG_LIMIT - 10)
    pages = replies._paginate([long_line, "next-page-line"], header="-# (cont.)")
    assert len(pages) == 2
    assert pages[1].startswith("-# (cont.)")
    assert "next-page-line" in pages[1]


def test_paginate_hard_splits_overlong_single_line():
    monster = "y" * (replies._DISCORD_MSG_LIMIT * 2 + 50)
    pages = replies._paginate([monster], header="-# (cont.)")
    assert len(pages) >= 3
    assert all(len(page) <= replies._DISCORD_MSG_LIMIT for page in pages)
    joined = "".join(page.replace("-# (cont.)", "").replace("\n", "") for page in pages)
    assert joined.count("y") == len(monster)


def _preview(**kw):
    defaults = dict(
        kind="external",
        title="A title",
        links=[("https://example.com/a", "other", "A title")],
        images=[],
        embed_title="Embed Title",
        embed_description="Embed description",
        embed_has_thumb=True,
        board_name="robots",
    )
    defaults.update(kw)
    return replies.PostPreview(**defaults)


def test_preview_reply_to_url_shown():
    pages = replies.format_post_preview(_preview(reply_to_bsky_url="https://bsky.app/parent"))
    assert "reply-to: https://bsky.app/parent" in pages[0]


def test_preview_reply_to_pending_shown():
    pages = replies.format_post_preview(_preview(reply_to_pending=True))
    assert "parent queued" in pages[0]


def test_preview_record_kind():
    pages = replies.format_post_preview(_preview(
        kind="record",
        links=[("https://bsky.app/profile/x/post/y", "bluesky", None)],
    ))
    assert "embed.record:" in pages[0]
    assert "native repost" in pages[0]


def test_preview_video_kind_counts_thread_posts():
    pages = replies.format_post_preview(_preview(
        kind="video",
        videos=[("a.mp4", "first vid"), ("b.mp4", None)],
        images=[("pic.jpg", "a pic")],
    ))
    text = pages[0]
    # root video + 1 extra video reply + 1 image reply = 3 posts
    assert "**thread 1/3:**" in text
    assert "video reply" in text
    assert "image reply" in text
    assert '"first vid"' in text
    assert "(no alt text)" in text  # b.mp4 has no alt


def test_preview_extra_link_replies():
    pages = replies.format_post_preview(_preview(
        links=[
            ("https://example.com/a", "other", "First"),
            ("https://example.com/b", "other", "Second"),
        ],
    ))
    text = "\n".join(pages)
    assert "**thread 2/2:**" in text
    assert "link reply" in text
    assert "https://example.com/b" in text


def test_preview_labels_and_graphic_line():
    pages = replies.format_post_preview(_preview(labels=["sexual"], nsfw=True, graphic_status="graphic"))
    text = pages[0]
    assert "labels: sexual" in text
    assert "NSFW" in text
    assert "graphic: graphic" in text
