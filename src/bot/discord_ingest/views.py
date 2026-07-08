"""Discord UI button factories and modal classes for submission interactions.

Each factory returns a View with timeout=None (persistent). Buttons carry
explicit custom_ids so on_interaction can route them after any restart.

Custom ID scheme (must stay stable — old buttons carry these forever):
  confirm:{submission_id}   — queue the submission
  cancel:{submission_id}    — cancel the submission
  meta_ok:{submission_id}   — confirm current link as best available
  graphic:{submission_id}   — mark as graphic/gore content
  pl_skip:{submission_id}   — skip YouTube playlist addition
  edit:{submission_id}      — open edit-post modal (caption + up to 4 images' alt)
  edit_post:{submission_id} — modal custom_id for the edit-post modal
  alt_edit:{submission_id}  — open the alt-text image picker (posts with >4 images)
  alt_pick:{submission_id}  — select custom_id for the alt-text image picker
  edit_alt:{attachment_id}  — modal custom_id for single-image alt editing
  alt_skip:{attachment_id}  — waive alt text for one attachment
  no_source:{submission_id} — waive the source requirement (no findable source)
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


def make_confirm_view(submission_id: int, media_count: int = 0) -> discord.ui.View:
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
    # The edit modal fits caption + 4 images; a picker is only needed beyond that.
    if media_count > 4:
        view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Edit alt text",
            emoji="🖼️",
            custom_id=f"alt_edit:{submission_id}",
        ))
    return view


def make_alt_picker_view(submission_id: int, media: list[tuple[int, str]]) -> discord.ui.View:
    """A dropdown of a submission's images/videos (attachment_id, filename); choosing
    one opens its alt-text modal. Discord caps a select at 25 options."""
    view = discord.ui.View(timeout=None)
    options = [
        discord.SelectOption(label=(filename or f"attachment {att_id}")[:100], value=str(att_id))
        for att_id, filename in media[:25]
    ]
    view.add_item(discord.ui.Select(
        custom_id=f"alt_pick:{submission_id}",
        placeholder="pick an image to edit its alt text",
        options=options,
    ))
    return view


class PostEditModal(discord.ui.Modal, title="Edit post"):
    """Edit the caption plus up to 4 images' alt text in one modal.

    Fields are built dynamically (Discord allows 5 inputs): the caption first, then one
    alt field per media attachment (image or video), pre-filled with its current alt.
    """

    def __init__(
        self,
        submission_id: int,
        current_title: str | None,
        media: list[tuple[int, str, str | None]] | None = None,
    ) -> None:
        super().__init__(custom_id=f"edit_post:{submission_id}")
        self.submission_id = submission_id
        self._caption = discord.ui.TextInput(
            label="Post text",
            placeholder="Caption / title in the Bluesky post",
            required=False,
            max_length=280,
            style=discord.TextStyle.paragraph,
            custom_id="caption",
            default=current_title or "",
        )
        self.add_item(self._caption)
        self._alt_inputs: list[tuple[int, discord.ui.TextInput]] = []
        for att_id, filename, current_alt in (media or [])[:4]:
            field = discord.ui.TextInput(
                label=f"alt: {filename}"[:45],
                placeholder="describe this image for screen readers (blank = no alt)",
                required=False,
                max_length=2000,
                style=discord.TextStyle.paragraph,
                custom_id=f"alt:{att_id}",
                default=current_alt or "",
            )
            self.add_item(field)
            self._alt_inputs.append((att_id, field))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..db import session_scope
        from . import service as _service
        alt_updates = {att_id: field.value for att_id, field in self._alt_inputs}
        async with session_scope() as session:
            await _service.apply_post_edits(
                session,
                submission_id=self.submission_id,
                new_title=self._caption.value,
                alt_updates=alt_updates,
                edited_by=interaction.user.id,
            )
        await interaction.response.send_message("Post updated.", ephemeral=True)


class AltEditModal(discord.ui.Modal, title="Edit alt text"):
    """Single-image alt-text editor, opened from the alt picker."""

    def __init__(self, attachment_id: int, filename: str, current_alt: str | None) -> None:
        super().__init__(custom_id=f"edit_alt:{attachment_id}")
        self.attachment_id = attachment_id
        self._alt = discord.ui.TextInput(
            label=f"alt: {filename}"[:45],
            placeholder="describe this image for screen readers (blank = no alt)",
            required=False,
            max_length=2000,
            style=discord.TextStyle.paragraph,
            custom_id="alt",
            default=current_alt or "",
        )
        self.add_item(self._alt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..db import session_scope
        from . import service as _service
        async with session_scope() as session:
            await _service.apply_single_alt(
                session,
                attachment_id=self.attachment_id,
                value=self._alt.value,
                edited_by=interaction.user.id,
            )
        await interaction.response.send_message("Alt text updated.", ephemeral=True)


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


def make_alt_skip_view(attachment_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Skip alt text",
        emoji="⏭️",
        custom_id=f"alt_skip:{attachment_id}",
    ))
    return view


def make_no_source_view(submission_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="No known source",
        emoji="🚫",
        custom_id=f"no_source:{submission_id}",
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
