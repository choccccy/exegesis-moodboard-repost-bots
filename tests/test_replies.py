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
    msg = replies.metadata_request("@user", "https://example.com/foo")
    assert "https://example.com/foo" in msg
    assert "@user" in msg
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


def test_publish_failed_notice_mentions_slot():
    msg = replies.publish_failed_notice(None)
    assert "slot" in msg.lower()


def test_source_request_mentions_user():
    msg = replies.source_request("@alice")
    assert "@alice" in msg


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
