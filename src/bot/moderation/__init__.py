"""NSFW/graphic classification helpers.

NSFW is board-level metadata (one moodboard is sexual-content NSFW); that flag is
carried forward to publishing but does not itself block in M1. Graphic/gore
status is not encoded in the source channels, so when an operator flags that it
must be resolved, the bot asks a human and stores a structured yes/no answer.
"""

from __future__ import annotations

from ..state import GraphicStatus

# Graphic yes/no is answered by reacting to the bot's request message.
GRAPHIC_YES_EMOJI = "🩸"  # mark graphic
GRAPHIC_NO_EMOJI = "🕊️"  # not graphic

def graphic_from_emoji(emoji: str) -> GraphicStatus | None:
    if emoji == GRAPHIC_YES_EMOJI:
        return GraphicStatus.GRAPHIC
    return None
