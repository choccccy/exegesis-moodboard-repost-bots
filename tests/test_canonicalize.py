from bot.canonicalize import canonicalize


def c(url: str) -> str:
    return canonicalize(url).canonical_url


def fam(url: str) -> str:
    return canonicalize(url).domain_family


def test_strips_utm_and_si_on_unknown_domain():
    assert c("https://example.com/a?utm_source=x&utm_medium=y&id=7") == (
        "https://example.com/a?id=7"
    )
    assert c("http://example.com/page?si=abcdef") == "https://example.com/page"


def test_youtube_normalizes_and_preserves_timestamp():
    assert c("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=42s") == (
        "https://youtu.be/dQw4w9WgXcQ?t=42s"
    )
    assert c("https://youtu.be/dQw4w9WgXcQ?si=trackme") == "https://youtu.be/dQw4w9WgXcQ"
    assert fam("https://youtu.be/dQw4w9WgXcQ") == "youtube"


def test_twitter_normalizes_to_twitter_com_and_drops_mirrors():
    assert c("https://x.com/user/status/123?s=20") == "https://twitter.com/user/status/123"
    assert c("https://fxtwitter.com/user/status/123") == "https://twitter.com/user/status/123"
    assert c("https://fixupx.com/user/status/123") == "https://twitter.com/user/status/123"
    assert fam("https://x.com/user/status/123") == "twitter"


def test_reddit_collapses_to_www():
    assert c("https://old.reddit.com/r/art/comments/abc/title/?utm_name=x") == (
        "https://www.reddit.com/r/art/comments/abc/title"
    )
    assert fam("https://old.reddit.com/r/art/") == "reddit"


def test_instagram_normalizes_post_path():
    assert c("https://instagram.com/p/ABC123/?igshid=zzz") == "https://www.instagram.com/p/ABC123/"
    assert c("https://www.instagram.com/reel/XYZ/") == "https://www.instagram.com/reel/XYZ/"


def test_wikipedia_mobile_to_desktop():
    assert c("https://en.m.wikipedia.org/wiki/Art") == "https://en.wikipedia.org/wiki/Art"
    assert fam("https://en.wikipedia.org/wiki/Art") == "wikipedia"


def test_bluesky_drops_query():
    assert c("https://bsky.app/profile/alice.bsky.social/post/abc?ref=x") == (
        "https://bsky.app/profile/alice.bsky.social/post/abc"
    )
    assert fam("https://bsky.app/profile/a/post/b") == "bluesky"


def test_artstation_and_deviantart():
    assert c("https://www.artstation.com/artwork/abc123?utm_source=x") == (
        "https://www.artstation.com/artwork/abc123"
    )
    assert c("https://alice.deviantart.com/art/Title-123?si=y") == (
        "https://alice.deviantart.com/art/Title-123"
    )


def test_bare_domain_gets_https():
    assert c("example.com/x").startswith("https://example.com/x")
