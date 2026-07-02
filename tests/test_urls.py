from bot.discord_ingest.urls import extract_urls


def test_plain_url():
    assert extract_urls("check https://example.com out") == ["https://example.com"]


def test_trailing_punctuation_stripped():
    assert extract_urls("see https://example.com.") == ["https://example.com"]
    assert extract_urls("(https://example.com)") == ["https://example.com"]


def test_wikipedia_parentheses_preserved():
    # Closing ) is part of the path - must not be stripped.
    url = "https://en.wikipedia.org/wiki/Stanley_(vehicle)"
    assert extract_urls(url) == [url]
    assert extract_urls(f"check out {url} cool right") == [url]


def test_wikipedia_parentheses_in_prose():
    # URL in parenthetical prose: outer ) is punctuation, inner ) is part of path.
    url = "https://en.wikipedia.org/wiki/Stanley_(vehicle)"
    assert extract_urls(f"(see {url})") == [url]


def test_unbalanced_trailing_paren_stripped():
    # No ( in URL, trailing ) is punctuation.
    assert extract_urls("(https://example.com/foo)") == ["https://example.com/foo"]


def test_deduplication():
    url = "https://example.com"
    assert extract_urls(f"{url} {url}") == [url]
