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


def test_artstation():
    assert c("https://www.artstation.com/artwork/abc123?utm_source=x") == (
        "https://www.artstation.com/artwork/abc123"
    )


def test_deviantart_subdomain_to_www():
    # Old-style artist subdomains normalize to www.deviantart.com/artist/...
    assert c("https://alice.deviantart.com/art/Title-123?si=y") == (
        "https://www.deviantart.com/alice/art/Title-123"
    )
    assert fam("https://alice.deviantart.com/art/Title-123") == "deviantart"


def test_fixdeviantart_canonicalizes_to_deviantart():
    assert c("https://fixdeviantart.com/alice/art/Title-123") == (
        "https://www.deviantart.com/alice/art/Title-123"
    )
    assert fam("https://fixdeviantart.com/alice/art/Title-123") == "deviantart"


def test_girlcockx_canonicalizes_to_twitter():
    assert c("https://girlcockx.com/user/status/999") == "https://twitter.com/user/status/999"
    assert fam("https://girlcockx.com/user/status/999") == "twitter"


def test_xcancel_canonicalizes_to_twitter():
    assert c("https://xcancel.com/user/status/999") == "https://twitter.com/user/status/999"
    assert fam("https://xcancel.com/user/status/999") == "twitter"


def test_xbsky_canonicalizes_to_bsky_app():
    assert c("https://xbsky.app/profile/alice.bsky.social/post/abc") == (
        "https://bsky.app/profile/alice.bsky.social/post/abc"
    )
    assert fam("https://xbsky.app/profile/alice.bsky.social/post/abc") == "bluesky"


def test_kkinstagram_canonicalizes_to_instagram():
    assert c("https://kkinstagram.com/p/ABC123/") == "https://www.instagram.com/p/ABC123/"
    assert fam("https://kkinstagram.com/p/ABC123/") == "instagram"


def test_vxreddit_canonicalizes_to_reddit():
    assert c("https://vxreddit.com/r/art/comments/abc/title/") == (
        "https://www.reddit.com/r/art/comments/abc/title"
    )
    assert fam("https://vxreddit.com/r/art/comments/abc/title/") == "reddit"


def test_rxddit_canonicalizes_to_reddit():
    # rxddit is dead for fetching but historical posts should still canonicalize.
    assert c("https://rxddit.com/r/art/comments/abc/title/") == (
        "https://www.reddit.com/r/art/comments/abc/title"
    )
    assert fam("https://rxddit.com/r/art/comments/abc/title/") == "reddit"


def test_wikipedia_mobile_already_handled():
    # Regression: mobile wikipedia was already working, ensure it stays correct.
    assert c("https://en.m.wikipedia.org/wiki/Art") == "https://en.wikipedia.org/wiki/Art"
    assert fam("https://en.m.wikipedia.org/wiki/Art") == "wikipedia"


# --- heuristic path-pattern fallback ---


def test_heuristic_unknown_twitter_mirror():
    # Any unknown domain with /handle/status/ID path → twitter family + normalized URL.
    result = canonicalize("https://futuretwitter.example/someuser/status/12345678")
    assert result.domain_family == "twitter"
    assert result.canonical_url == "https://twitter.com/someuser/status/12345678"


def test_heuristic_unknown_reddit_mirror():
    result = canonicalize("https://newreddit.example/r/art/comments/abc123/my_post/")
    assert result.domain_family == "reddit"
    assert result.canonical_url == "https://www.reddit.com/r/art/comments/abc123/my_post"


def test_heuristic_unknown_instagram_mirror():
    result = canonicalize("https://insta-mirror.example/p/XYZCODE123/")
    assert result.domain_family == "instagram"
    assert result.canonical_url == "https://www.instagram.com/p/XYZCODE123/"


def test_heuristic_unknown_wikipedia_mirror():
    result = canonicalize("https://wiki-mirror.example/wiki/Robots")
    assert result.domain_family == "wikipedia"
    # Unknown mirror host is preserved (can't infer language prefix); path is kept.
    assert "/wiki/Robots" in result.canonical_url


def test_heuristic_unknown_artstation_mirror():
    result = canonicalize("https://art-mirror.example/artwork/cool-piece")
    assert result.domain_family == "artstation"
    assert result.canonical_url == "https://www.artstation.com/artwork/cool-piece"


def test_heuristic_unknown_youtube_shorts_mirror():
    result = canonicalize("https://yt-mirror.example/shorts/dQw4w9WgXcQ")
    assert result.domain_family == "youtube"
    assert result.canonical_url == "https://youtu.be/dQw4w9WgXcQ"


def test_heuristic_unknown_pixiv_mirror():
    result = canonicalize("https://pixiv-mirror.example/artworks/12345678")
    assert result.domain_family == "pixiv"
    assert result.canonical_url == "https://www.pixiv.net/artworks/12345678"


def test_heuristic_unknown_flickr_mirror():
    result = canonicalize("https://flickr-mirror.example/photos/someuser/9876543210")
    assert result.domain_family == "flickr"
    assert result.canonical_url == "https://www.flickr.com/photos/someuser/9876543210"


def test_pixiv_known_domain():
    assert c("https://pixiv.net/artworks/12345678?utm_source=x") == (
        "https://www.pixiv.net/artworks/12345678"
    )
    assert fam("https://pixiv.net/artworks/12345678") == "pixiv"


def test_flickr_known_domain():
    assert fam("https://flickr.com/photos/user/12345/") == "flickr"
    assert fam("https://flic.kr/p/ABCDEF") == "flickr"


def test_heuristic_no_false_positive():
    # Generic URL with non-matching path stays "other".
    result = canonicalize("https://example.com/some/random/page")
    assert result.domain_family == "other"


def test_heuristic_shorts_requires_11_char_id():
    # /shorts/ with wrong-length ID should not match youtube heuristic.
    result = canonicalize("https://unknown.example/shorts/tooshort")
    assert result.domain_family == "other"


def test_bare_domain_gets_https():
    assert c("example.com/x").startswith("https://example.com/x")
