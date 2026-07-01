"""Discord implementation of the Notifier protocol."""

from __future__ import annotations

import discord

from ..notifier import NullNotifier, SentMessage


class DiscordNotifier:
    """Wraps a Discord channel/thread so the service layer receives a Notifier.

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
