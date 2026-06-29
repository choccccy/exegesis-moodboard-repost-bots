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
from ..moderation import GRAPHIC_YES_EMOJI
from ..state import SubmissionState
from . import replies, service
from .replies import CANCEL_EMOJI

log = logging.getLogger(__name__)

# Minimum gap between handle_reaction calls during catchup, to avoid hitting
# Discord's per-guild thread-creation rate limit (observed: ~5-min freeze after
# creating 19 threads in rapid succession).
_CATCHUP_INTER_MESSAGE_DELAY = 2.0       # seconds; existing submission, thread reused
_CATCHUP_NEW_THREAD_DELAY = 6.0          # seconds; new thread created, respect rate limit


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
            retry_task = asyncio.create_task(self._run_threadless_retry_loop())
            retry_task.add_done_callback(_log_task_exception)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Route button clicks to service handlers by custom_id prefix."""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id: str = (interaction.data or {}).get("custom_id", "")
        async with session_scope() as session:
            if custom_id.startswith("cancel:"):
                await service.handle_cancel_button(
                    session, interaction, int(custom_id.removeprefix("cancel:")), self.settings
                )
            elif custom_id.startswith("confirm:"):
                await service.handle_confirm_button(
                    session, interaction, int(custom_id.removeprefix("confirm:")),
                    self.settings, self._yt_client,
                )
            elif custom_id.startswith("meta_ok:"):
                await service.handle_metadata_confirm_button(
                    session, interaction, int(custom_id.removeprefix("meta_ok:")),
                    self.settings, self._yt_client,
                )
            elif custom_id.startswith("graphic:"):
                await service.handle_graphic_button(
                    session, interaction, int(custom_id.removeprefix("graphic:")),
                    self.settings, self._yt_client,
                )
            elif custom_id.startswith("pl_skip:"):
                await service.handle_playlist_skip_button(
                    session, interaction, int(custom_id.removeprefix("pl_skip:")),
                    self.settings, self._yt_client,
                )
            else:
                await interaction.response.defer()

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
                    member=payload.member, user_id=payload.user_id, yt_client=self._yt_client,
                    bot_id=getattr(self.user, "id", None),
                )
            return

        if emoji == GRAPHIC_YES_EMOJI:
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
                    yt_client=self._yt_client,
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
                    yt_client=self._yt_client,
                )

        if emoji == CANCEL_EMOJI:
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return
            if payload.channel_id in self._watched_channels:
                # ❌ on the original source post - OP or curator can cancel
                async with session_scope() as session:
                    thread_id, cancelled_sub, removed_videos = await service.handle_source_cancel_reaction(
                        session,
                        settings=self.settings,
                        channel=channel,
                        message_id=payload.message_id,
                        member=payload.member,
                        user_id=payload.user_id,
                        yt_client=self._yt_client,
                    )
                if thread_id is not None:
                    thread = await self._resolve_channel(thread_id)
                    if thread is not None:
                        if cancelled_sub:
                            await thread.send(replies.source_cancel_confirmation(payload.user_id))
                            if isinstance(thread, discord.Thread):
                                await service._archive_thread(thread, notice=replies.closing_notice("submission cancelled"))
                        for video_id in removed_videos:
                            await thread.send(
                                f"<@{payload.user_id}> removed https://youtu.be/{video_id} from the playlist via ❌ on the source post"
                            )
            else:
                # ❌ on a bot message inside a thread - submission cancel button
                async with session_scope() as session:
                    await service.handle_cancel_reaction(
                        session,
                        settings=self.settings,
                        channel=channel,
                        message_id=payload.message_id,
                        member=payload.member,
                        user_id=payload.user_id,
                    )

        if emoji == replies.PLAYLIST_OPT_OUT_EMOJI:
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return
            async with session_scope() as session:
                await service.handle_playlist_opt_out(
                    session,
                    message_id=payload.message_id,
                    user_id=payload.user_id,
                    member=payload.member,
                    channel=channel,
                    settings=self.settings,
                    yt_client=self._yt_client,
                )

        if emoji == replies.CONFIRMATION_EMOJI:
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return
            async with session_scope() as session:
                await service.handle_confirmation_reaction(
                    session,
                    settings=self.settings,
                    channel=channel,
                    message_id=payload.message_id,
                    member=payload.member,
                    user_id=payload.user_id,
                    yt_client=self._yt_client,
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
                        new_thread = await service.handle_reaction(
                            session,
                            settings=self.settings,
                            message=message,
                            http_client=self.httpx_client,
                            skip_auth=True,
                            bot_id=getattr(self.user, "id", None),
                        )
                    total += 1
                    delay = _CATCHUP_NEW_THREAD_DELAY if new_thread else _CATCHUP_INTER_MESSAGE_DELAY
                    await asyncio.sleep(delay)
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
        _VOTE_EMOJIS = {GRAPHIC_YES_EMOJI}

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
                            session, submission, settings=self.settings, destination=thread,
                            bot_id=getattr(self.user, "id", None),
                        )
            except Exception:
                log.exception("recompute_and_request failed for submission %s in thread %s", submission_id, thread_id)

        log.info("thread catch-up scan complete: replayed %d event(s)", replayed)
        service._fire_and_forget(self._archive_queued_threads())

    async def _archive_queued_threads(self) -> None:
        """On startup, archive any QUEUED or PUBLISHED threads that haven't been closed yet.

        Threads transitioned more than 15 min ago get archived immediately. Threads
        transitioned recently (bot restarted mid-window) get a shortened delay for the
        remaining time. QUEUED threads that still have a pending playlist add are skipped.

        Critically: channel resolution (fetch_channel) is deferred into each background
        task so this loop never makes Discord API calls. Bulk fetch_channel calls were
        the source of guild-wide rate-limit penalties on startup.
        """
        async with session_scope() as session:
            rows = await session.execute(
                select(
                    SubmissionThread.thread_id,
                    Submission.updated_at,
                    Submission.board_id,
                    Submission.source_discord_message_id,
                    Submission.channel_id,
                    Submission.playlist_skipped,
                    Submission.state,
                )
                .join(
                    Submission,
                    (Submission.board_id == SubmissionThread.board_id)
                    & (Submission.source_discord_message_id == SubmissionThread.source_discord_message_id),
                )
                .where(Submission.state.in_([
                    SubmissionState.QUEUED.value,
                    SubmissionState.PUBLISHED.value,
                ]))
            )
            queued = [
                (r.thread_id, r.updated_at, r.board_id, r.source_discord_message_id, r.channel_id, r.playlist_skipped, r.state)
                for r in rows
            ]

        log.info("queued thread archival scan: %d thread(s) to evaluate", len(queued))
        now = datetime.now(timezone.utc)
        # Stagger threads that are immediately due to avoid bursting thread.edit() calls.
        immediate_stagger = 0.0
        _ARCHIVE_STAGGER = 2.0  # seconds between immediately-due archives
        scheduled = 0

        for thread_id, queued_at, board_id, source_msg_id, channel_id, playlist_skipped, state in queued:
            # Published threads are always closeable; queued ones need the playlist check.
            if state == SubmissionState.QUEUED.value:
                async with session_scope() as session:
                    board_cfg = self.settings.board_for_channel(channel_id)
                    if not await service._playlist_close_ready(
                        session, board_id, source_msg_id, board_cfg,
                        playlist_skipped=playlist_skipped,
                    ):
                        log.debug("skipping thread %s - playlist add still pending", thread_id)
                        continue

            if queued_at is not None:
                if queued_at.tzinfo is None:
                    queued_at = queued_at.replace(tzinfo=timezone.utc)
                elapsed = (now - queued_at).total_seconds()
                remaining = max(0.0, service._THREAD_CLOSE_DELAY - elapsed)
            else:
                remaining = 0.0

            if remaining == 0.0:
                remaining = immediate_stagger
                immediate_stagger += _ARCHIVE_STAGGER

            reason = "queued" if state == SubmissionState.QUEUED.value else "published to Bluesky"
            # Channel resolution happens inside the background task after the delay,
            # so this loop makes zero Discord API calls (no rate-limit burst).
            service._fire_and_forget(self._archive_thread_by_id(
                thread_id, remaining, notice=replies.closing_notice(reason),
            ))
            scheduled += 1

        log.info("queued thread archival scan: scheduled %d archive(s)", scheduled)

    async def _archive_thread_by_id(self, thread_id: int, delay: float, *, notice: str | None = None) -> None:
        """Archive a thread by ID, after `delay` seconds.

        Channel resolution is deferred until after the delay so the caller
        can schedule many archives without making any immediate Discord API calls.
        """
        if delay > 0:
            await asyncio.sleep(delay)
        thread = await self._resolve_channel(thread_id)
        if thread is None:
            log.debug("archive skipped: thread %s not found", thread_id)
            return
        if isinstance(thread, discord.Thread) and thread.archived:
            log.debug("archive skipped: thread %s already archived", thread_id)
            return
        if notice and isinstance(thread, discord.Thread):
            try:
                await thread.send(notice)
            except Exception:
                pass
        try:
            await thread.edit(archived=True)  # type: ignore[union-attr]
            log.debug("archived thread %s", thread_id)
        except Exception:
            log.warning("failed to archive thread %s", thread_id, exc_info=True)

    async def _run_threadless_retry_loop(self) -> None:
        """Loop forever, retrying thread creation for submissions that missed it.

        Thread creation can fail fast due to our 15-second timeout when Discord is
        rate limiting. Those submissions are recorded in the DB but have no Discord
        thread. This loop catches them every 3 minutes and tries again.
        """
        _RETRY_INTERVAL = 3 * 60  # seconds between scans
        _THREAD_DELAY = 10.0      # seconds between thread creations within one scan

        _TERMINAL_STATES = (
            SubmissionState.PUBLISHED.value,
            SubmissionState.PUBLISH_FAILED.value,
        )

        while True:
            await asyncio.sleep(_RETRY_INTERVAL)
            try:
                async with session_scope() as session:
                    rows = await session.execute(
                        select(Submission)
                        .outerjoin(
                            SubmissionThread,
                            (SubmissionThread.board_id == Submission.board_id)
                            & (SubmissionThread.source_discord_message_id == Submission.source_discord_message_id),
                        )
                        .where(SubmissionThread.thread_id.is_(None))
                        .where(~Submission.state.in_(_TERMINAL_STATES))
                    )
                    threadless = list(rows.scalars())

                if not threadless:
                    continue

                log.info("threadless retry: %d submission(s) without Discord threads", len(threadless))
                for sub in threadless:
                    message = await self._fetch_message(sub.channel_id, sub.source_discord_message_id)
                    if message is None:
                        log.warning("threadless retry: source message %s not found for submission %s", sub.source_discord_message_id, sub.id)
                        continue
                    async with session_scope() as session:
                        fresh = await session.get(Submission, sub.id)
                        if fresh is None or fresh.state in _TERMINAL_STATES:
                            continue
                        thread, _ = await service._ensure_thread(session, self.settings, message, fresh, post_anchor=True, bot_id=getattr(self.user, "id", None))
                        if thread is not None:
                            await service.recompute_and_request(session, fresh, settings=self.settings, destination=thread, bot_id=getattr(self.user, "id", None))
                            log.info("threadless retry: created thread for submission %s", fresh.id)
                    await asyncio.sleep(_THREAD_DELAY)
            except Exception:
                log.exception("threadless retry loop encountered an error")
