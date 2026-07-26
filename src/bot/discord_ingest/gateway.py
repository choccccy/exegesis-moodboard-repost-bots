"""Discord gateway for the interaction-handler port (surface-agnostic core, Phase C).

Translates a live `discord.Interaction` into a normalized `InteractionEvent` for a
curation handler, then performs the `HandlerOutcome` the handler returns against that
interaction. This is the ONLY place the migrated handlers touch Discord's
interaction-response API; the handlers themselves stay platform-agnostic.
"""

from __future__ import annotations

import logging

import discord

from ..curation.events import InteractionEvent
from ..curation.outcomes import Ack, HandlerOutcome, Noop, OpenModal, Tombstone
from . import prompts
from .discord_notifier import DiscordSurface
from .render import render_components, render_modal

log = logging.getLogger(__name__)


def surface_for(interaction: discord.Interaction) -> DiscordSurface:
    """Wrap the interaction's channel as a Surface. The client is attached so
    `clear_trigger` can reach the (different) source channel."""
    return DiscordSurface(interaction.channel, client=interaction.client)


def to_event(interaction: discord.Interaction, submission_id: int) -> InteractionEvent:
    """Normalize an inbound button/select interaction into an `InteractionEvent`."""
    user = interaction.user
    member = user if isinstance(user, discord.Member) else None
    values = tuple((interaction.data or {}).get("values") or ())
    return InteractionEvent(
        user_id=user.id, submission_id=submission_id, member=member, values=values
    )


async def perform(interaction: discord.Interaction, outcome: HandlerOutcome) -> None:
    """Carry out a handler's outcome against the live interaction."""
    if isinstance(outcome, Noop):
        return
    if isinstance(outcome, OpenModal):
        # A modal must be the interaction's first response - never after a defer.
        await interaction.response.send_modal(render_modal(outcome.spec))
        return
    if isinstance(outcome, Tombstone):
        # Disable the clicked message's controls in place. Best-effort: the message may
        # have been deleted, or we may lack perms - the state change already committed.
        try:
            await interaction.message.edit(
                view=render_components(prompts.disabled_components(outcome.label))
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("could not tombstone message %s: %s", interaction.message.id, exc)
        return
    if isinstance(outcome, Ack):
        kwargs: dict = {"ephemeral": outcome.ephemeral}
        view = render_components(outcome.components)
        if view is not None:
            kwargs["view"] = view
        # A handler may or may not have deferred; reply on whichever channel is open.
        if interaction.response.is_done():
            await interaction.followup.send(outcome.message, **kwargs)
        else:
            await interaction.response.send_message(outcome.message, **kwargs)
