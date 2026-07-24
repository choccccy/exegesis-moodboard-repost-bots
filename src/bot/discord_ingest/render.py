"""Render surface-agnostic component descriptors (`bot.components`) to Discord widgets.

This is the ONLY place buttons/selects/previews become `discord.*` objects, so the
curation core can traffic in plain descriptors. `action_id` is copied verbatim into
`custom_id`, preserving the `on_interaction` routing and every persisted button.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

from ..components import Button, ButtonStyle, Component, PreviewImage, Select

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
    return view


def render_preview(preview: PreviewImage) -> discord.File:
    """Build a discord.File image preview (resizing to fit Discord's limit)."""
    # Lazy import: the image-processing helper lives in service.py; importing it at
    # module load would create a render <-> service cycle.
    from .service import _discord_file_for_attachment

    return _discord_file_for_attachment(preview.local_path, preview.filename)
