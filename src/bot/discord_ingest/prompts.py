"""Surface-agnostic component descriptors for the curation prompts.

Plain `bot.curation.components` descriptors (buttons/selects/modals); **no `discord` import** -
a per-surface adapter (`render.py`) renders them. Each `action_id` is copied verbatim
into a Discord `custom_id`, so `on_interaction` prefix-routing and every persisted
button keep working. The scheme (these prefixes must stay stable - old buttons carry
them forever):

  confirm:{submission_id}   - queue the submission
  cancel:{submission_id}    - cancel the submission
  meta_ok:{submission_id}   - confirm current link as best available
  graphic:{submission_id}   - mark as graphic/gore content
  pl_skip:{submission_id}   - skip YouTube playlist addition
  edit:{submission_id}      - open edit-post modal (caption + up to 4 images' alt)
  edit_post:{submission_id} - modal custom_id for the edit-post modal
  alt_edit:{submission_id}  - open the alt-text image picker (posts with >4 images)
  alt_pick:{submission_id}  - select custom_id for the alt-text image picker
  edit_alt:{attachment_id}  - modal custom_id for single-image alt editing
  srcnote_ok:{submission_id} / srcnote_no:{submission_id} - confirm/discard a non-URL source

(The source waiver and per-image alt skip are the /no_source and /skip_alt slash
commands, not buttons.)
"""

from __future__ import annotations

from ..curation.components import (
    Button,
    ButtonStyle,
    Component,
    ModalSpec,
    Select,
    SelectOption,
    TextField,
)


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


def disabled_components(label: str) -> list[Component]:
    """A tombstone: the live button(s) replaced by a single greyed-out button in the
    same position. The disabled button carries no action_id (it routes nowhere)."""
    return [Button(label=label, action_id="", disabled=True)]


def post_edit_modal(
    submission_id: int,
    current_title: str | None,
    media: list[tuple[int, str, str | None]] | None = None,
) -> ModalSpec:
    """Edit the caption plus up to 4 images' alt text in one modal. Discord allows 5
    inputs: the caption first, then one alt field per media attachment."""
    fields = [
        TextField(label="Post text", action_id="caption",
                  default=current_title or "", max_length=280,
                  placeholder="Caption / title in the Bluesky post"),
    ]
    for att_id, filename, current_alt in (media or [])[:4]:
        fields.append(TextField(
            label=f"alt: {filename}", action_id=f"alt:{att_id}",
            default=current_alt or "", max_length=2000,
            placeholder="describe this image for screen readers (blank = no alt)",
        ))
    return ModalSpec(title="Edit post", action_id=f"edit_post:{submission_id}", fields=fields)


def alt_edit_modal(attachment_id: int, filename: str, current_alt: str | None) -> ModalSpec:
    """Single-image alt-text editor, opened from the alt picker."""
    return ModalSpec(
        title="Edit alt text", action_id=f"edit_alt:{attachment_id}",
        fields=[TextField(
            label=f"alt: {filename}", action_id="alt",
            default=current_alt or "", max_length=2000,
            placeholder="describe this image for screen readers (blank = no alt)",
        )],
    )
