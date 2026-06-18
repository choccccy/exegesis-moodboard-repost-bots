"""discord.py event wiring: reactions in, replies in, requests out."""

from __future__ import annotations

import logging

import discord
import httpx

from ..config import Settings
from ..db import session_scope
from . import service

log = logging.getLogger(__name__)


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # privileged — enable in the Dev Portal
    return intents


class RepostBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        super().__init__(intents=_build_intents())
        self.settings = settings
        self._http: httpx.AsyncClient | None = None
        self._watched_channels = {b.discord_channel_id for b in settings.boards}

    @property
    def httpx_client(self) -> httpx.AsyncClient:
        # NB: discord.Client owns `self.http`; ours must use a different name.
        assert self._http is not None, "http client not initialized"
        return self._http

    async def setup_hook(self) -> None:
        self._http = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        async with session_scope() as session:
            await service.sync_boards(session, self.settings)
        log.info("watching %d channel(s): %s", len(self._watched_channels), self._watched_channels)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        await super().close()

    async def on_ready(self) -> None:
        log.info("logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != self.settings.trigger_emoji:
            return
        if payload.channel_id not in self._watched_channels:
            return
        if payload.user_id == getattr(self.user, "id", None):
            return  # ignore the bot's own reactions

        message = await self._fetch_message(payload.channel_id, payload.message_id)
        if message is None:
            return
        async with session_scope() as session:
            await service.handle_reaction(
                session, settings=self.settings, message=message, http_client=self.httpx_client
            )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id == getattr(self.user, "id", None):
            return  # never react to our own messages
        if message.reference is None:
            return  # only replies can answer a request
        if message.channel.id not in self._watched_channels:
            return
        async with session_scope() as session:
            await service.handle_reply(session, settings=self.settings, message=message)

    async def _fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("cannot access channel %s: %s", channel_id, exc)
                return None
        try:
            return await channel.fetch_message(message_id)  # type: ignore[union-attr]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("cannot fetch message %s: %s", message_id, exc)
            return None
