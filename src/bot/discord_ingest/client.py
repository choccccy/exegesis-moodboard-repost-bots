"""discord.py event wiring: reactions in, replies in, requests out."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
import httpx

from ..config import Settings
from ..db import session_scope
from ..moderation import GRAPHIC_NO_EMOJI, GRAPHIC_YES_EMOJI
from . import replies, service

log = logging.getLogger(__name__)


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # privileged - enable in the Dev Portal
    return intents


class RepostBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        super().__init__(intents=_build_intents())
        self.settings = settings
        self._http: httpx.AsyncClient | None = None
        self._watched_channels = {b.discord_channel_id for b in settings.boards}
        self._catchup_started = False

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
        # on_ready can fire again on reconnect; only run the catch-up scan once.
        if self.settings.catchup_enabled and not self._catchup_started:
            self._catchup_started = True
            asyncio.create_task(self._run_catchup())

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == getattr(self.user, "id", None):
            return  # ignore the bot's own reactions (incl. pre-seeded votes)
        emoji = str(payload.emoji)

        if emoji == self.settings.trigger_emoji:
            if payload.channel_id not in self._watched_channels:
                return
            message = await self._fetch_message(payload.channel_id, payload.message_id)
            if message is None:
                return
            async with session_scope() as session:
                await service.handle_reaction(
                    session, settings=self.settings, message=message, http_client=self.httpx_client
                )
            return

        if emoji in (GRAPHIC_YES_EMOJI, GRAPHIC_NO_EMOJI):
            # Graphic vote on a bot request message (lives in a thread). The DB
            # lookup by message id scopes this to our own prompts.
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return
            async with session_scope() as session:
                await service.handle_label_reaction(
                    session,
                    settings=self.settings,
                    channel=channel,
                    message_id=payload.message_id,
                    emoji=emoji,
                    member=payload.member,
                    user_id=payload.user_id,
                )

        if emoji == replies.METADATA_CONFIRM_EMOJI:
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return
            async with session_scope() as session:
                await service.handle_metadata_reaction(
                    session,
                    settings=self.settings,
                    channel=channel,
                    message_id=payload.message_id,
                    member=payload.member,
                    user_id=payload.user_id,
                )

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != self.settings.trigger_emoji:
            return
        if payload.channel_id not in self._watched_channels:
            return
        channel = await self._resolve_channel(payload.channel_id)
        if channel is None:
            return
        async with session_scope() as session:
            await service.handle_reaction_removed(
                session,
                settings=self.settings,
                channel=channel,
                channel_id=payload.channel_id,
                message_id=payload.message_id,
            )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id == getattr(self.user, "id", None):
            return  # never react to our own messages
        if message.reference is None:
            return  # only replies can answer a request
        if not self._is_watched_location(message.channel):
            return
        async with session_scope() as session:
            await service.handle_reply(
                session,
                settings=self.settings,
                message=message,
                http_client=self.httpx_client,
            )

    def _is_watched_location(self, channel) -> bool:
        """A watched channel, or a thread whose parent is a watched channel."""
        if channel.id in self._watched_channels:
            return True
        return (
            isinstance(channel, discord.Thread)
            and channel.parent_id in self._watched_channels
        )

    async def _resolve_channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("cannot access channel %s: %s", channel_id, exc)
                return None
        return channel

    async def _fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(message_id)  # type: ignore[union-attr]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("cannot fetch message %s: %s", message_id, exc)
            return None

    async def _run_catchup(self) -> None:
        """Reconcile 🦋 reactions missed while offline.

        For each watched channel, walk recent history (bounded by lookback window
        and message cap) and ingest any message currently bearing the trigger
        emoji. Idempotent: messages that already have a submission are skipped by
        the service layer, so re-scanning never duplicates or re-prompts.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.settings.catchup_lookback_hours
        )
        trigger = self.settings.trigger_emoji
        cap = self.settings.catchup_max_messages
        total = 0
        log.info("catch-up scan starting (lookback=%sh, cap=%s/channel)",
                 self.settings.catchup_lookback_hours, cap)

        for channel_id in self._watched_channels:
            channel = await self._resolve_channel(channel_id)
            if channel is None:
                continue
            scanned = 0
            try:
                async for message in channel.history(limit=cap):  # type: ignore[union-attr]
                    if message.created_at < cutoff:
                        break
                    scanned += 1
                    if not any(str(r.emoji) == trigger for r in message.reactions):
                        continue
                    async with session_scope() as session:
                        await service.handle_reaction(
                            session,
                            settings=self.settings,
                            message=message,
                            http_client=self.httpx_client,
                        )
                    total += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("catch-up scan failed for channel %s: %s", channel_id, exc)
                continue
            # Surface a bounded scan rather than implying full coverage.
            if scanned >= cap:
                log.warning(
                    "catch-up hit the %s-message cap on channel %s; 🦋 reactions on "
                    "older messages within the lookback window were not scanned",
                    cap, channel_id,
                )
        log.info("catch-up scan complete: processed %d butterflied message(s)", total)
