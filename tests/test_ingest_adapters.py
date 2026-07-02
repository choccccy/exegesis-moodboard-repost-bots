"""Tests for the Discord → InboundMessage/InboundAttachment adapter layer.

These cover the conversion code that sits at the Discord boundary. A field
mapping mistake here (e.g. swapping thumbnail_url and image_url) would cause
silent data loss in ingestion, so each attribute is verified explicitly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord

from bot.discord_ingest.adapters import (
    discord_attachment_to_inbound,
    discord_embed_to_inbound,
    discord_message_to_inbound,
)
from bot.ingest.types import InboundAttachment, InboundEmbed, InboundMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _discord_embed(
    url: str | None = None,
    title: str | None = None,
    description: str | None = None,
    thumb_url: str | None = None,
    thumb_proxy: str | None = None,
    image_url: str | None = None,
    image_proxy: str | None = None,
    author_name: str | None = None,
) -> MagicMock:
    e = MagicMock(spec=discord.Embed)
    e.url = url
    e.title = title
    e.description = description
    if thumb_url or thumb_proxy:
        e.thumbnail = MagicMock()
        e.thumbnail.url = thumb_url
        e.thumbnail.proxy_url = thumb_proxy
    else:
        e.thumbnail = None
    if image_url or image_proxy:
        e.image = MagicMock()
        e.image.url = image_url
        e.image.proxy_url = image_proxy
    else:
        e.image = None
    if author_name:
        e.author = MagicMock()
        e.author.name = author_name
    else:
        e.author = None
    return e


def _discord_attachment(
    att_id: int = 1,
    url: str = "https://cdn.discord.com/a.jpg",
    proxy_url: str = "https://proxy.discord.com/a.jpg",
    content_type: str = "image/jpeg",
    filename: str = "a.jpg",
    description: str | None = None,
    width: int = 800,
    height: int = 600,
    spoiler: bool = False,
) -> MagicMock:
    a = MagicMock(spec=discord.Attachment)
    a.id = att_id
    a.url = url
    a.proxy_url = proxy_url
    a.content_type = content_type
    a.filename = filename
    a.description = description
    a.width = width
    a.height = height
    a.is_spoiler = MagicMock(return_value=spoiler)
    return a


def _discord_message(
    content: str = "",
    embeds: list | None = None,
    attachments: list | None = None,
    message_snapshots: list | None = None,
) -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.content = content
    m.embeds = embeds or []
    m.attachments = attachments or []
    m.message_snapshots = message_snapshots or []
    return m


# ---------------------------------------------------------------------------
# discord_embed_to_inbound
# ---------------------------------------------------------------------------

def test_embed_maps_url():
    e = _discord_embed(url="https://example.com/post")
    assert discord_embed_to_inbound(e).url == "https://example.com/post"


def test_embed_maps_title_and_description():
    e = _discord_embed(title="My Title", description="My Desc")
    result = discord_embed_to_inbound(e)
    assert result.title == "My Title"
    assert result.description == "My Desc"


def test_embed_maps_thumbnail_url_and_proxy():
    e = _discord_embed(thumb_url="https://t.jpg", thumb_proxy="https://tp.jpg")
    result = discord_embed_to_inbound(e)
    assert result.thumbnail_url == "https://t.jpg"
    assert result.thumbnail_proxy_url == "https://tp.jpg"


def test_embed_maps_image_url_and_proxy():
    e = _discord_embed(image_url="https://img.jpg", image_proxy="https://imgp.jpg")
    result = discord_embed_to_inbound(e)
    assert result.image_url == "https://img.jpg"
    assert result.image_proxy_url == "https://imgp.jpg"


def test_embed_thumbnail_none_when_absent():
    e = _discord_embed()  # no thumbnail
    result = discord_embed_to_inbound(e)
    assert result.thumbnail_url is None
    assert result.thumbnail_proxy_url is None


def test_embed_image_none_when_absent():
    e = _discord_embed()  # no image
    result = discord_embed_to_inbound(e)
    assert result.image_url is None
    assert result.image_proxy_url is None


def test_embed_maps_author_name():
    e = _discord_embed(author_name="Jane Artist")
    assert discord_embed_to_inbound(e).author_name == "Jane Artist"


def test_embed_author_none_when_absent():
    e = _discord_embed()
    assert discord_embed_to_inbound(e).author_name is None


def test_embed_thumbnail_not_confused_with_image():
    """thumbnail_url and image_url must not be swapped."""
    e = _discord_embed(
        thumb_url="https://thumb.jpg",
        thumb_proxy="https://thumb-proxy.jpg",
        image_url="https://image.jpg",
        image_proxy="https://image-proxy.jpg",
    )
    result = discord_embed_to_inbound(e)
    assert result.thumbnail_url == "https://thumb.jpg"
    assert result.thumbnail_proxy_url == "https://thumb-proxy.jpg"
    assert result.image_url == "https://image.jpg"
    assert result.image_proxy_url == "https://image-proxy.jpg"


# ---------------------------------------------------------------------------
# discord_attachment_to_inbound
# ---------------------------------------------------------------------------

def test_attachment_maps_id():
    a = _discord_attachment(att_id=999)
    assert discord_attachment_to_inbound(a).id == 999


def test_attachment_maps_url_and_proxy():
    a = _discord_attachment(url="https://cdn/f.jpg", proxy_url="https://proxy/f.jpg")
    result = discord_attachment_to_inbound(a)
    assert result.url == "https://cdn/f.jpg"
    assert result.proxy_url == "https://proxy/f.jpg"


def test_attachment_maps_content_type_and_filename():
    a = _discord_attachment(content_type="image/png", filename="robot.png")
    result = discord_attachment_to_inbound(a)
    assert result.content_type == "image/png"
    assert result.filename == "robot.png"


def test_attachment_maps_description():
    a = _discord_attachment(description="A chrome robot arm")
    assert discord_attachment_to_inbound(a).description == "A chrome robot arm"


def test_attachment_description_none():
    a = _discord_attachment(description=None)
    assert discord_attachment_to_inbound(a).description is None


def test_attachment_maps_dimensions():
    a = _discord_attachment(width=1920, height=1080)
    result = discord_attachment_to_inbound(a)
    assert result.width == 1920
    assert result.height == 1080


def test_attachment_maps_spoiler_true():
    a = _discord_attachment(spoiler=True)
    assert discord_attachment_to_inbound(a).spoiler is True


def test_attachment_maps_spoiler_false():
    a = _discord_attachment(spoiler=False)
    assert discord_attachment_to_inbound(a).spoiler is False


# ---------------------------------------------------------------------------
# discord_message_to_inbound
# ---------------------------------------------------------------------------

def test_message_maps_content():
    m = _discord_message(content="hello https://example.com")
    assert discord_message_to_inbound(m).content == "hello https://example.com"


def test_message_empty_content_becomes_empty_string():
    m = _discord_message(content="")
    assert discord_message_to_inbound(m).content == ""


def test_message_none_content_becomes_empty_string():
    m = _discord_message()
    m.content = None
    assert discord_message_to_inbound(m).content == ""


def test_message_maps_embeds():
    e = _discord_embed(url="https://example.com", title="T")
    m = _discord_message(embeds=[e])
    result = discord_message_to_inbound(m)
    assert len(result.embeds) == 1
    assert result.embeds[0].title == "T"


def test_message_maps_attachments():
    a = _discord_attachment(att_id=42, filename="photo.jpg")
    m = _discord_message(attachments=[a])
    result = discord_message_to_inbound(m)
    assert len(result.attachments) == 1
    assert result.attachments[0].id == 42
    assert result.attachments[0].filename == "photo.jpg"


def test_message_maps_snapshots():
    snap = MagicMock()
    snap.content = "https://example.com/forwarded"
    snap.embeds = []
    snap.attachments = []
    m = _discord_message(message_snapshots=[snap])
    result = discord_message_to_inbound(m)
    assert len(result.snapshots) == 1
    assert result.snapshots[0].content == "https://example.com/forwarded"


def test_message_snapshot_embeds_converted():
    snap_embed = _discord_embed(title="Snap Embed Title")
    snap = MagicMock()
    snap.content = ""
    snap.embeds = [snap_embed]
    snap.attachments = []
    m = _discord_message(message_snapshots=[snap])
    result = discord_message_to_inbound(m)
    assert result.snapshots[0].embeds[0].title == "Snap Embed Title"


def test_message_snapshot_attachments_converted():
    snap_att = _discord_attachment(att_id=77, filename="snap.jpg")
    snap = MagicMock()
    snap.content = ""
    snap.embeds = []
    snap.attachments = [snap_att]
    m = _discord_message(message_snapshots=[snap])
    result = discord_message_to_inbound(m)
    assert result.snapshots[0].attachments[0].id == 77


def test_message_no_snapshots_gives_empty_list():
    m = _discord_message()
    assert discord_message_to_inbound(m).snapshots == []


def test_message_no_embeds_gives_empty_list():
    m = _discord_message()
    assert discord_message_to_inbound(m).embeds == []


def test_message_no_attachments_gives_empty_list():
    m = _discord_message()
    assert discord_message_to_inbound(m).attachments == []
