"""discord.py event wiring: reactions in, replies in, requests out."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
import httpx

from sqlalchemy import select

from ..config import Settings
from ..db import session_scope
from ..models import Submission, SubmissionThread
from ..moderation import GRAPHIC_NO_EMOJI, GRAPHIC_YES_EMOJI
from ..state import SubmissionState
from . import replies, service
from .replies import CANCEL_EMOJI

log = logging.getLogger(__name__)

# Minimum gap between handle_reaction calls during catchup, to avoid hitting
# Discord's per-guild thread-creation rate limit (observed: ~5-min freeze after
# creating 19 threads in rapid succession).
_CATCHUP_INTER_MESSAGE_DELAY = 2.0  # seconds


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: surface silent asyncio task exceptions in the log."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("background task %s raised an unhandled exception", task.get_name())


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # privileged - enable in the Dev Portal
    return intents


class RepostBot(discord.Client):
    def __init__(self, settings: Settings, yt_client=None) -> None:
        from ..version import __version__
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"v{__version__}",
        )
        super().__init__(intents=_build_intents(), activity=activity)
        self.settings = settings
        self._yt_client = yt_client
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
            task = asyncio.create_task(self._run_catchup())
            task.add_done_callback(_log_task_exception)

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
                    session, settings=self.settings, message=message, http_client=self.httpx_client,
                    member=payload.member, user_id=payload.user_id,
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

        if emoji == CANCEL_EMOJI:
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return
            if payload.channel_id in self._watched_channels:
                # ❌ on the original source post - OP or curator can cancel
                thread_id = None
                async with session_scope() as session:
                    thread_id = await service.handle_source_cancel_reaction(
                        session,
                        settings=self.settings,
                        channel=channel,
                        message_id=payload.message_id,
                        member=payload.member,
                        user_id=payload.user_id,
                    )
                if thread_id is not None:
                    thread = await self._resolve_channel(thread_id)
                    if thread is not None:
                        await thread.send(replies.source_cancel_confirmation(payload.user_id))
            else:
                # ❌ on the bot's cancel-request message inside a thread
                async with session_scope() as session:
                    await service.handle_cancel_reaction(
                        session,
                        settings=self.settings,
                        channel=channel,
                        message_id=payload.message_id,
                        member=payload.member,
                        user_id=payload.user_id,
                    )

        if str(payload.emoji) == self.settings.playlist_emoji:
            board_cfg = self.settings.board_for_channel(payload.channel_id)
            if board_cfg is None or not board_cfg.youtube_playlist_id:
                return
            if not service._is_curator(payload.member, payload.user_id, board_cfg):
                return
            message = await self._fetch_message(payload.channel_id, payload.message_id)
            if message is None:
                return
            async with session_scope() as session:
                await service.handle_playlist_reaction(
                    session,
                    settings=self.settings,
                    message=message,
                    board_cfg=board_cfg,
                    yt_client=self._yt_client,
                    reactor_id=payload.user_id,
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
                user_id=payload.user_id,
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
                            skip_auth=True,
                        )
                    total += 1
                    await asyncio.sleep(_CATCHUP_INTER_MESSAGE_DELAY)
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
        asyncio.create_task(self._run_thread_catchup())

    async def _run_thread_catchup(self) -> None:
        """Reconcile missed reactions/replies in submission threads.

        For each thread whose submission is still awaiting curator input, walk
        the full thread history and replay:
          - Human reply messages (alt text, source, image, metadata answers)
          - Emoji reactions on bot messages (🩸/🕊️ graphic votes, 🔗 metadata confirm)

        All service handlers are idempotent via answered_at guards, so replaying
        already-processed events is safe.
        """
        _PENDING_STATES = (
            SubmissionState.INTENT_SUBMITTED.value,
            SubmissionState.AWAITING_SOURCE.value,
            SubmissionState.AWAITING_BETTER_LINK.value,
            SubmissionState.AWAITING_IMAGE.value,
            SubmissionState.AWAITING_ALT_TEXT.value,
            SubmissionState.AWAITING_GRAPHIC_CLASSIFICATION.value,
        )
        _VOTE_EMOJIS = {GRAPHIC_YES_EMOJI, GRAPHIC_NO_EMOJI}

        async with session_scope() as session:
            rows = await session.execute(
                select(SubmissionThread.thread_id, Submission.id.label("submission_id"))
                .join(
                    Submission,
                    (Submission.board_id == SubmissionThread.board_id)
                    & (Submission.source_discord_message_id == SubmissionThread.source_discord_message_id),
                )
                .where(Submission.state.in_(_PENDING_STATES))
            )
            pending = [(r.thread_id, r.submission_id) for r in rows]

        log.info("thread catch-up scan: %d pending thread(s)", len(pending))
        replayed = 0

        for thread_id, submission_id in pending:
            thread = await self._resolve_channel(thread_id)
            if thread is None:
                continue
            try:
                async for message in thread.history(limit=None, oldest_first=True):  # type: ignore[union-attr]
                    bot_id = getattr(self.user, "id", None)
                    if message.author.id == bot_id:
                        # Replay reactions on bot messages.
                        for reaction in message.reactions:
                            emoji = str(reaction.emoji)
                            if emoji in _VOTE_EMOJIS:
                                async for user in reaction.users():
                                    if user.id == bot_id:
                                        continue
                                    async with session_scope() as session:
                                        await service.handle_label_reaction(
                                            session,
                                            settings=self.settings,
                                            channel=thread,
                                            message_id=message.id,
                                            emoji=emoji,
                                            member=None,
                                            user_id=user.id,
                                        )
                                    replayed += 1
                            elif emoji == replies.METADATA_CONFIRM_EMOJI:
                                async for user in reaction.users():
                                    if user.id == bot_id:
                                        continue
                                    async with session_scope() as session:
                                        await service.handle_metadata_reaction(
                                            session,
                                            settings=self.settings,
                                            channel=thread,
                                            message_id=message.id,
                                            member=None,
                                            user_id=user.id,
                                        )
                                    replayed += 1
                            elif emoji == CANCEL_EMOJI:
                                async for user in reaction.users():
                                    if user.id == bot_id:
                                        continue
                                    async with session_scope() as session:
                                        await service.handle_cancel_reaction(
                                            session,
                                            settings=self.settings,
                                            channel=thread,
                                            message_id=message.id,
                                            member=None,
                                            user_id=user.id,
                                        )
                                    replayed += 1
                    elif message.reference is not None:
                        # Replay human replies (alt text, source, etc.).
                        async with session_scope() as session:
                            await service.handle_reply(
                                session,
                                settings=self.settings,
                                message=message,
                                http_client=self.httpx_client,
                            )
                        replayed += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("thread catch-up failed for thread %s: %s", thread_id, exc)
                continue

            # Ensure all missing request messages (including the cancel button) are present.
            try:
                async with session_scope() as session:
                    submission = await session.get(Submission, submission_id)
                    if submission is not None:
                        await service.recompute_and_request(
                            session, submission, settings=self.settings, destination=thread
                        )
            except Exception:
                log.exception("recompute_and_request failed for submission %s in thread %s", submission_id, thread_id)

        log.info("thread catch-up scan complete: replayed %d event(s)", replayed)
