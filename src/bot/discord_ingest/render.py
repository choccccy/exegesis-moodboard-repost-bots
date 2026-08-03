"""Render surface-agnostic component descriptors (`bot.components`) to Discord widgets.

This is the ONLY place buttons/selects/previews become `discord.*` objects, so the
curation core can traffic in plain descriptors. `action_id` is copied verbatim into
`custom_id`, preserving the `on_interaction` routing and every persisted button.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

from ..curation.components import Button, ButtonStyle, Component, ModalSpec, PreviewImage, Select

_STYLE = {
    ButtonStyle.PRIMARY: discord.ButtonStyle.primary,
    ButtonStyle.SECONDARY: discord.ButtonStyle.secondary,
    ButtonStyle.SUCCESS: discord.ButtonStyle.success,
    ButtonStyle.DANGER: discord.ButtonStyle.danger,
}


def render_components(components: Sequence[Component] | None) -> discord.ui.View | None:
    """Build a persistent (timeout=None) View from descriptors, or None if empty."""
    if not components:
        return None
    view = discord.ui.View(timeout=None)
    for comp in components:
        if isinstance(comp, Button):
            view.add_item(discord.ui.Button(
                style=_STYLE[comp.style],
                label=comp.label,
                emoji=comp.emoji,
                custom_id=comp.action_id if not comp.disabled else None,
                disabled=comp.disabled,
            ))
        elif isinstance(comp, Select):
            view.add_item(discord.ui.Select(
                custom_id=comp.action_id,
                placeholder=comp.placeholder,
                options=[
                    discord.SelectOption(label=o.label, value=o.value)
                    for o in comp.options
                ],
            ))
        else:  # pragma: no cover - Component union is exhaustively Button | Select
            continue
    return view


class _DescriptorModal(discord.ui.Modal):
    """A Discord modal built from a `ModalSpec`. Its `on_submit` collects the field
    values keyed by `action_id` and hands them to `_dispatch_modal_submit`, which
    routes by the modal's action_id prefix to the matching service call. Keeping the
    submit wiring here (the Discord adapter) is deliberate: the curation core only
    traffics in the plain `ModalSpec`. (Phase C will formalise this as an inbound
    event + HandlerOutcome; for now it stays a thin adapter dispatch.)"""

    def __init__(self, spec: ModalSpec) -> None:
        super().__init__(title=spec.title, custom_id=spec.action_id, timeout=None)
        self._action_id = spec.action_id
        self._inputs: dict[str, discord.ui.TextInput] = {}
        for f in spec.fields:
            item = discord.ui.TextInput(
                label=f.label[:45],  # Discord caps input labels at 45 chars
                placeholder=f.placeholder or None,
                custom_id=f.action_id,
                default=f.default,
                required=f.required,
                max_length=f.max_length,
                style=discord.TextStyle.paragraph if f.multiline else discord.TextStyle.short,
            )
            self.add_item(item)
            self._inputs[f.action_id] = item

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {action_id: inp.value for action_id, inp in self._inputs.items()}
        await _dispatch_modal_submit(self._action_id, values, interaction)


async def _dispatch_modal_submit(
    action_id: str, values: dict[str, str], interaction: discord.Interaction
) -> None:
    """Route a submitted modal to the curation call named by its action_id prefix."""
    from ..db import session_scope
    from ..curation import core

    if action_id.startswith("edit_post:"):
        submission_id = int(action_id.removeprefix("edit_post:"))
        alt_updates = {
            int(k.removeprefix("alt:")): v for k, v in values.items() if k.startswith("alt:")
        }
        async with session_scope() as session:
            await core.apply_post_edits(
                session,
                submission_id=submission_id,
                new_title=values.get("caption", ""),
                alt_updates=alt_updates,
                edited_by=interaction.user.id,
            )
        await interaction.response.send_message("Post updated.", ephemeral=True)
    elif action_id.startswith("edit_alt:"):
        attachment_id = int(action_id.removeprefix("edit_alt:"))
        async with session_scope() as session:
            await core.apply_single_alt(
                session,
                attachment_id=attachment_id,
                value=values.get("alt", ""),
                edited_by=interaction.user.id,
            )
        await interaction.response.send_message("Alt text updated.", ephemeral=True)


def render_modal(spec: ModalSpec) -> discord.ui.Modal:
    """Build a Discord modal from a surface-agnostic `ModalSpec`."""
    return _DescriptorModal(spec)


def render_preview(preview: PreviewImage) -> discord.File:
    """Build a discord.File media preview (resizing images to fit Discord's limit).

    Videos are uploaded as-is (the caller is responsible for keeping them under
    Discord's upload cap); only images go through the in-memory resize helper.
    """
    if preview.is_video:
        return discord.File(preview.local_path, filename=preview.filename)
    # Lazy import: the image-processing helper lives in service.py; importing it at
    # module load would create a render <-> service cycle.
    from .service import _discord_file_for_attachment

    return _discord_file_for_attachment(preview.local_path, preview.filename)
