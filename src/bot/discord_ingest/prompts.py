"""Surface-agnostic component descriptors for the curation prompts.

The `bot.components` equivalents of the `views.make_*_view` factories. **No `discord`
import** - a per-surface adapter renders these. The `action_id`s reproduce the stable
`{prefix}:{submission_id}` custom_id scheme (see the docstring in `views.py`), so
Discord's on_interaction routing and every persisted button are unaffected.
"""

from __future__ import annotations

from ..components import Button, ButtonStyle, Component, Select, SelectOption


def cancel_components(submission_id: int) -> list[Component]:
    return [Button(label="Cancel submission", action_id=f"cancel:{submission_id}",
                   style=ButtonStyle.DANGER, emoji="❌")]


def confirm_components(submission_id: int, media_count: int = 0) -> list[Component]:
    out: list[Component] = [
        Button(label="Queue for posting", action_id=f"confirm:{submission_id}",
               style=ButtonStyle.SUCCESS, emoji="✅"),
        Button(label="Edit before queuing", action_id=f"edit:{submission_id}",
               style=ButtonStyle.SECONDARY, emoji="✏️"),
    ]
    # The edit modal fits caption + 4 images; a picker is only needed beyond that.
    if media_count > 4:
        out.append(Button(label="Edit alt text", action_id=f"alt_edit:{submission_id}",
                          style=ButtonStyle.SECONDARY, emoji="🖼️"))
    return out


def metadata_confirm_components(submission_id: int) -> list[Component]:
    return [Button(label="Use link as-is", action_id=f"meta_ok:{submission_id}",
                   style=ButtonStyle.SECONDARY, emoji="🔗")]


def graphic_components(submission_id: int) -> list[Component]:
    return [Button(label="Mark as graphic content", action_id=f"graphic:{submission_id}",
                   style=ButtonStyle.DANGER, emoji="🩸")]


def playlist_skip_components(submission_id: int) -> list[Component]:
    return [Button(label="Skip playlist", action_id=f"pl_skip:{submission_id}",
                   style=ButtonStyle.SECONDARY, emoji="⏹️")]


def source_note_confirm_components(submission_id: int) -> list[Component]:
    return [
        Button(label="Use as source", action_id=f"srcnote_ok:{submission_id}",
               style=ButtonStyle.SUCCESS, emoji="📄"),
        Button(label="Discard", action_id=f"srcnote_no:{submission_id}",
               style=ButtonStyle.SECONDARY, emoji="🗑️"),
    ]


def alt_picker_components(submission_id: int, media: list[tuple[int, str]]) -> list[Component]:
    """A dropdown of a submission's images/videos (attachment_id, filename); Discord
    caps a select at 25 options."""
    options = [
        SelectOption(label=(filename or f"attachment {att_id}")[:100], value=str(att_id))
        for att_id, filename in media[:25]
    ]
    return [Select(action_id=f"alt_pick:{submission_id}",
                   placeholder="pick an image to edit its alt text", options=options)]
