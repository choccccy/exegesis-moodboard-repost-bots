"""Discord → platform-agnostic type adapters.

Call these at the Discord boundary (handle_reaction, handle_reply, _ensure_thread)
to convert Discord event objects into InboundMessage/InboundAttachment/InboundEmbed
before passing them into shared ingestion functions.
"""

from __future__ import annotations

import discord

from ..ingest.types import (
    InboundAttachment,
    InboundEmbed,
    InboundMessage,
    InboundSnapshot,
)


def discord_embed_to_inbound(embed: discord.Embed) -> InboundEmbed:
    return InboundEmbed(
        url=embed.url,
        title=embed.title,
        description=embed.description,
        thumbnail_url=embed.thumbnail.url if embed.thumbnail else None,
        thumbnail_proxy_url=embed.thumbnail.proxy_url if embed.thumbnail else None,
        image_url=embed.image.url if embed.image else None,
        image_proxy_url=embed.image.proxy_url if embed.image else None,
        author_name=embed.author.name if embed.author else None,
    )


def discord_attachment_to_inbound(att: discord.Attachment) -> InboundAttachment:
    return InboundAttachment(
        id=att.id,
        url=att.url,
        proxy_url=att.proxy_url,
        content_type=att.content_type,
        filename=att.filename,
        description=att.description,
        width=att.width,
        height=att.height,
        spoiler=att.is_spoiler(),
    )


def _discord_snapshot_to_inbound(snap) -> InboundSnapshot:
    return InboundSnapshot(
        content=getattr(snap, "content", "") or "",
        embeds=[discord_embed_to_inbound(e) for e in getattr(snap, "embeds", [])],
        attachments=[
            discord_attachment_to_inbound(a) for a in getattr(snap, "attachments", [])
        ],
    )


def discord_message_to_inbound(msg: discord.Message) -> InboundMessage:
    return InboundMessage(
        content=msg.content or "",
        embeds=[discord_embed_to_inbound(e) for e in msg.embeds],
        attachments=[discord_attachment_to_inbound(a) for a in msg.attachments],
        snapshots=[
            _discord_snapshot_to_inbound(snap)
            for snap in getattr(msg, "message_snapshots", [])
        ],
    )
