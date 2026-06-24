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


def test_queued_notice_mentions_noon_mt():
    msg = replies.queued_notice()
    assert "noon MT" in msg


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
