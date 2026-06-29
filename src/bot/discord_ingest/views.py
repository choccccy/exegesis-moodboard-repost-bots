"""Discord UI button factories for submission interactions.

Each factory returns a View with timeout=None (persistent). Buttons carry
explicit custom_ids so on_interaction can route them after any restart.

Custom ID scheme (must stay stable — old buttons carry these forever):
  confirm:{submission_id}   — queue the submission
  cancel:{submission_id}    — cancel the submission
  meta_ok:{submission_id}   — confirm current link as best available
  graphic:{submission_id}   — mark as graphic/gore content
  pl_skip:{submission_id}   — skip YouTube playlist addition
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
    return view


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
