"""Tests for the known-good mirror cheat-sheet + in-thread nag (#61)."""

from bot.curation import replies
from bot.mirrors import KNOWN_GOOD_MIRRORS, mirror_hint_for_url


def test_hint_suggests_mirror_for_bare_platform_url():
    hint = mirror_hint_for_url("https://www.tiktok.com/@user/video/123456789")
    assert hint is not None
    assert "tnktok.com" in hint
    assert "TikTok" in hint


def test_hint_quiet_when_already_using_mirror():
    assert mirror_hint_for_url("https://tnktok.com/@user/video/123456789") is None
    assert mirror_hint_for_url("https://fxtwitter.com/user/status/123") is None


def test_hint_suggests_mirror_for_pixiv_and_furaffinity():
    pixiv = mirror_hint_for_url("https://www.pixiv.net/en/artworks/12345678")
    assert pixiv is not None and "phixiv.net" in pixiv
    fa = mirror_hint_for_url("https://www.furaffinity.net/view/12345678")
    assert fa is not None and "fxfuraffinity.net" in fa


def test_hint_none_for_unknown_platform():
    assert mirror_hint_for_url("https://example.com/whatever") is None


def test_hint_none_for_bluesky_native_repost():
    # Bluesky is a native repost, not a mirror-backed embed - no hint.
    assert mirror_hint_for_url("https://bsky.app/profile/alice.bsky.social/post/abc") is None


def test_every_mirror_hint_is_quiet_on_its_own_host():
    for m in KNOWN_GOOD_MIRRORS:
        assert mirror_hint_for_url(f"https://{m.host}/x") is None


def test_metadata_request_appends_tip_when_present():
    tip = "tip: TikTok embeds better via the `tnktok.com` mirror - reply with that link"
    msg = replies.metadata_request("https://www.tiktok.com/x", mirror_tip=tip)
    assert tip in msg
    assert msg.splitlines()[-1] == f"-# {tip}"


def test_metadata_request_no_tip_by_default():
    msg = replies.metadata_request("https://example.com/x")
    assert "-#" not in msg
