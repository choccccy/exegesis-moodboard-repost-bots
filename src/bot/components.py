"""Surface-agnostic UI component descriptors.

Plain data - **no `discord` import** - describing the interactive bits the curation
core wants on a message (buttons, a select, a modal, an image preview). A per-surface
adapter renders these to native widgets (Discord: see `discord_ingest/render.py`).

`action_id` is the stable routing key. For Discord it is copied verbatim into a
button/select `custom_id`, so the existing `{prefix}:{id}` scheme (see the docstring in
`discord_ingest/prompts.py`) and every persisted button keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ButtonStyle(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    SUCCESS = "success"
    DANGER = "danger"


@dataclass(frozen=True)
class Button:
    label: str
    action_id: str  # == the Discord custom_id, e.g. "confirm:42"
    style: ButtonStyle = ButtonStyle.SECONDARY
    emoji: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class SelectOption:
    label: str
    value: str


@dataclass(frozen=True)
class Select:
    action_id: str
    placeholder: str
    options: list[SelectOption]


# A component that can sit on a message.
Component = Button | Select


@dataclass(frozen=True)
class TextField:
    label: str
    action_id: str
    default: str = ""
    placeholder: str = ""
    required: bool = False
    max_length: int = 2000
    multiline: bool = True


@dataclass(frozen=True)
class ModalSpec:
    """A form to pop open in response to an action (Discord: a Modal)."""
    title: str
    action_id: str
    fields: list[TextField] = field(default_factory=list)


@dataclass(frozen=True)
class PreviewImage:
    """A local image to attach to a message (Discord: a discord.File)."""
    local_path: str
    filename: str
