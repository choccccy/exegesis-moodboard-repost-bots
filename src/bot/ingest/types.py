"""Platform-agnostic inbound message types.

These dataclasses are the internal representation of an incoming post,
independent of the transport (Discord, Matrix, …). Each platform adapter
converts its native event objects into these types before calling the
shared ingestion functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InboundEmbed:
    url: str | None = None
    title: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    thumbnail_proxy_url: str | None = None
    image_url: str | None = None
    image_proxy_url: str | None = None
    author_name: str | None = None


@dataclass
class InboundAttachment:
    id: int = 0
    url: str = ""
    proxy_url: str = ""
    content_type: str | None = None
    filename: str = ""
    description: str | None = None
    width: int | None = None
    height: int | None = None
    spoiler: bool = False


@dataclass
class InboundSnapshot:
    """A forwarded/quoted message snapshot."""
    content: str = ""
    embeds: list[InboundEmbed] = field(default_factory=list)
    attachments: list[InboundAttachment] = field(default_factory=list)


@dataclass
class InboundMessage:
    content: str = ""
    embeds: list[InboundEmbed] = field(default_factory=list)
    attachments: list[InboundAttachment] = field(default_factory=list)
    snapshots: list[InboundSnapshot] = field(default_factory=list)
