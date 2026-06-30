"""Discord UI button factories and modal classes for submission interactions.

Each factory returns a View with timeout=None (persistent). Buttons carry
explicit custom_ids so on_interaction can route them after any restart.

Custom ID scheme (must stay stable — old buttons carry these forever):
  confirm:{submission_id}   — queue the submission
  cancel:{submission_id}    — cancel the submission
  meta_ok:{submission_id}   — confirm current link as best available
  graphic:{submission_id}   — mark as graphic/gore content
  pl_skip:{submission_id}   — skip YouTube playlist addition
  edit:{submission_id}      — open edit-post-text modal
  edit_post:{submission_id} — modal custom_id for post text editing
"""

from __future__ import annotations

import discord


def make_cancel_view(submission_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.danger,
        label="Cancel submission",
        emoji="❌",
        custom_id=f"cancel:{submission_id}",
    ))
    return view


def make_confirm_view(submission_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.success,
        label="Queue for posting",
        emoji="✅",
        custom_id=f"confirm:{submission_id}",
    ))
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Edit before queuing",
        emoji="✏️",
        custom_id=f"edit:{submission_id}",
    ))
    return view


class PostEditModal(discord.ui.Modal, title="Edit post text"):
    caption_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Post text",
        placeholder="Caption / title in the Bluesky post",
        required=False,
        max_length=280,
        style=discord.TextStyle.paragraph,
        custom_id="caption",
    )

    def __init__(self, submission_id: int, current_title: str | None) -> None:
        super().__init__(custom_id=f"edit_post:{submission_id}")
        self.submission_id = submission_id
        self.caption_input.default = current_title or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..db import session_scope
        from . import service as _service
        async with session_scope() as session:
            await _service.apply_post_edits(
                session,
                submission_id=self.submission_id,
                new_title=self.caption_input.value,
            )
        await interaction.response.send_message("Post text updated.", ephemeral=True)


def make_metadata_confirm_view(submission_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Use link as-is",
        emoji="🔗",
        custom_id=f"meta_ok:{submission_id}",
    ))
    return view


def make_graphic_view(submission_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.danger,
        label="Mark as graphic content",
        emoji="🩸",
        custom_id=f"graphic:{submission_id}",
    ))
    return view


def make_playlist_skip_view(submission_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Skip playlist",
        emoji="⏹️",
        custom_id=f"pl_skip:{submission_id}",
    ))
    return view


def make_disabled_view(label: str) -> discord.ui.View:
    """A tombstone view: same position as the live button, now grayed out."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label=label,
        disabled=True,
    ))
    return view
