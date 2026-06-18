"""Procedural reply text. Utilitarian and dead-simple — never chatty."""

from __future__ import annotations


def source_request(mention: str) -> str:
    return f"{mention} reply to this message with the source URL"


def alt_text_request(mention: str, filename: str) -> str:
    return f"{mention} reply to this message with the alt text for **{filename}**"


def graphic_request(mention: str) -> str:
    return f"{mention} reply to this message with whether this should be marked graphic (yes/no)"


def ready_confirmation() -> str:
    return "✓ all required info received — this submission is ready to queue"


def source_not_found() -> str:
    return "couldn't find a URL in that reply — reply again with the source URL"


def graphic_not_understood() -> str:
    return "reply with a simple yes or no for graphic content"
