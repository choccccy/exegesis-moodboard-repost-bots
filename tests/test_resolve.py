from bot.resolve.fetch import parse_html_metadata


def test_opengraph_preferred():
    html = """
    <html><head>
      <title>Fallback Title</title>
      <meta property="og:title" content="OG Title">
      <meta property="og:description" content="A description">
      <meta property="og:image" content="https://cdn.example.com/img.jpg">
    </head><body>x</body></html>
    """
    m = parse_html_metadata(html, "https://example.com/page")
    assert m.title == "OG Title"
    assert m.description == "A description"
    assert m.image_url == "https://cdn.example.com/img.jpg"
    assert m.via == "opengraph"


def test_falls_back_to_title_tag():
    html = "<html><head><title>  Just a Title  </title></head><body></body></html>"
    m = parse_html_metadata(html, "https://example.com/")
    assert m.title == "Just a Title"
    assert m.image_url is None
    assert m.via == "html"


def test_twitter_card_and_relative_image_resolved():
    html = """
    <head>
      <meta name="twitter:title" content="Tw Title">
      <meta name="twitter:image" content="/rel/pic.png">
    </head>
    """
    m = parse_html_metadata(html, "https://example.com/a/b")
    assert m.title == "Tw Title"
    # relative image resolved against the (final) base URL
    assert m.image_url == "https://example.com/rel/pic.png"


def test_no_metadata():
    m = parse_html_metadata("<html><body>nothing</body></html>", "https://example.com/")
    assert m.title is None
    assert m.via == "none"
