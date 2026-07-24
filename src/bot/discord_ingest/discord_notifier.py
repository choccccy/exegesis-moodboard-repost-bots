"""Discord implementations of the outbound Surface/Notifier port."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from ..components import Button, ButtonStyle, Component, PreviewImage
from ..notifier import SentMessage
from .render import render_components, render_preview

log = logging.getLogger(__name__)


class DiscordNotifier:
    """Legacy narrow adapter (send + archive) still used by the scheduler/publish path.

    The lazy import of _archive_thread_after_delay inside archive() breaks the
    circular import that would arise if service.py imported this module at the
    top level while this module imported from service.py.
    """

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self._channel = channel

    async def send(self, content: str | None = None, **kwargs) -> SentMessage:
        return await self._channel.send(content, **kwargs)

    async def archive(self, notice: str) -> None:
        if isinstance(self._channel, discord.Thread):
            from .service import _archive_thread_after_delay
            _archive_thread_after_delay(self._channel, notice=notice)


class DiscordSurface:
    """Full `Surface` adapter over a Discord thread/channel.

    Wraps the destination channel plus a client reference (for `clear_trigger`, which
    acts on a *different*, source channel). All Discord-specific rendering is delegated
    to `render.py`; thread-lifecycle/reaction helpers are lazy-imported from `service`
    to avoid an import cycle (service imports this module's factory at the boundary).
    """

    def __init__(
        self,
        channel: discord.abc.Messageable,
        *,
        client: discord.Client | None = None,
    ) -> None:
        self._channel = channel
        self._client = client

    async def send(
        self,
        content: str | None = None,
        *,
        components: Sequence[Component] | None = None,
        preview: PreviewImage | None = None,
    ) -> SentMessage:
        kwargs: dict = {}
        view = render_components(components)
        if view is not None:
            kwargs["view"] = view
        if preview is not None:
            kwargs["file"] = render_preview(preview)
        return await self._channel.send(content, **kwargs)

    async def edit(
        self,
        message_id: int,
        *,
        content: str | None = None,
        components: Sequence[Component] | None = None,
    ) -> bool:
        try:
            await self._channel.get_partial_message(message_id).edit(
                content=content, view=render_components(components)
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.debug("edit failed for message %s: %s", message_id, exc)
            return False

    async def disable_components(self, message_id: int, label: str) -> bool:
        tombstone = [Button(label=label, action_id="", style=ButtonStyle.SECONDARY, disabled=True)]
        try:
            await self._channel.get_partial_message(message_id).edit(
                view=render_components(tombstone)
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.debug("disable_components failed for message %s: %s", message_id, exc)
            return False

    async def edit_or_none(
        self,
        message_id: int,
        content: str,
        components: Sequence[Component] | None = None,
    ) -> bool:
        try:
            await self._channel.get_partial_message(message_id).edit(
                content=content, view=render_components(components)
            )
        except discord.NotFound:
            return False  # deleted - caller reposts fresh
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("edit_or_none transient failure for message %s: %s", message_id, exc)
        return True  # edited, or a transient error that shouldn't respawn it

    async def message_exists(self, message_id: int) -> bool:
        try:
            await self._channel.fetch_message(message_id)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True  # cannot verify; assume it exists

    async def archive(self, notice: str | None = None) -> None:
        if isinstance(self._channel, discord.Thread):
            from .service import _archive_thread
            await _archive_thread(self._channel, notice=notice)

    def archive_after_delay(self, notice: str | None = None) -> None:
        if isinstance(self._channel, discord.Thread):
            from .service import _archive_thread_after_delay
            _archive_thread_after_delay(self._channel, notice=notice)

    async def unarchive(self) -> None:
        if isinstance(self._channel, discord.Thread):
            from .service import _unarchive_thread
            await _unarchive_thread(self._channel)

    async def clear_trigger(self, source_channel_id: int, source_message_id: int, emoji: str) -> None:
        if self._client is None:
            log.debug("clear_trigger skipped: no client on surface")
            return
        source_channel = self._client.get_channel(source_channel_id)
        if source_channel is None:
            try:
                source_channel = await self._client.fetch_channel(source_channel_id)
            except (discord.Forbidden, discord.HTTPException):
                return
        from .service import _clear_trigger_reaction
        await _clear_trigger_reaction(source_channel, source_message_id, emoji)
