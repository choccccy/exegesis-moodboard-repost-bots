"""Procedural reply text. Utilitarian and dead-simple - never chatty."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..moderation import GRAPHIC_NO_EMOJI, GRAPHIC_YES_EMOJI

METADATA_CONFIRM_EMOJI = "🔗"


def source_request(mention: str) -> str:
    return f"{mention} reply to this message with the source URL"


def image_request(mention: str) -> str:
    return (
        f"{mention} this link has no preview image, so the post would have no image. "
        "reply to this message attaching an image to use"
    )


def image_not_found() -> str:
    return "no image attached - reply again attaching at least one image"


def alt_text_request(mention: str, filename: str) -> str:
    return f"{mention} reply to this message with the alt text for **{filename}**"


def graphic_request(mention: str) -> str:
    return (
        f"{mention} react {GRAPHIC_YES_EMOJI} if this contains graphic/gore content, "
        f"{GRAPHIC_NO_EMOJI} if it's safe"
    )


def ready_confirmation() -> str:
    return "✓ all required info received - this submission is ready to queue"


def metadata_request(mention: str, url: str) -> str:
    return (
        f"{mention} couldn't get any metadata from **{url}** — reply with a more embeddable "
        f"link, or react {METADATA_CONFIRM_EMOJI} to use it as-is (at least one image will be required)"
    )


def metadata_confirmed() -> str:
    return f"{METADATA_CONFIRM_EMOJI} noted — link confirmed as best available; at least one image must be attached"


def metadata_link_updated(new_url: str) -> str:
    return f"updated source to {new_url} — re-resolving metadata…"


def metadata_url_not_found() -> str:
    return f"no URL found — reply again with a link, or react {METADATA_CONFIRM_EMOJI} to use the existing one as-is"


def source_not_found() -> str:
    return "couldn't find a URL in that reply - reply again with the source URL"


def graphic_not_understood() -> str:
    return "reply with a simple yes or no for graphic content"


def reaction_removed() -> str:
    return "🦋 removed, deleting prospective post"


def cannot_remove_published(bsky_url: str) -> str:
    return (
        f"this post has already been published to Bluesky: {bsky_url}\n"
        "removing the 🦋 won't un-publish it - contact an admin if you need it taken down"
    )


def published_notice(bsky_url: str) -> str:
    return f"posted to Bluesky: {bsky_url}"


def reposted_notice(bsky_url: str) -> str:
    return f"reposted to Bluesky: {bsky_url}"


def queued_notice() -> str:
    return "queued — will post at the next available slot (noon MT or later, up to 6/day fresh · 3/day backlog)"


def publish_failed_notice(error: str | None) -> str:
    return (
        f"publish failed: {error or 'unknown error'}\n"
        "will retry automatically at the next available queue slot"
    )


def duplicate_warning(bsky_url: str) -> str:
    return (
        f"⚠ this URL was already posted: {bsky_url}\n"
        "proceeding anyway — remove 🦋 if this was a mistake"
    )


def thread_name(submission_id: int) -> str:
    # Discord caps thread names at 100 chars; this stays well under.
    return f"🦋 submission {submission_id}"


def thread_anchor(*, poster_mention: str, curator_mentions: list[str]) -> str:
    """Top-of-thread message: pulls people into the private thread.

    Mentioning the poster adds them to the private thread; curator role mentions
    notify the (Manage-Threads-visible) curators. The original message is
    forwarded separately so curators can see the content inline.
    """
    lines = [f"🦋 new submission from {poster_mention}"]
    if curator_mentions:
        lines.append(f"{' '.join(curator_mentions)} - help curate this repost")
    return "\n".join(lines)


# Human labels + atproto $type per embed mode.
_KIND_LABELS = {
    "external": ("external link card", "app.bsky.embed.external"),
    "images": ("image post", "app.bsky.embed.images"),
    "record": ("Bluesky repost/quote", "app.bsky.embed.record"),
    "empty": ("(no source yet)", "-"),
}


@dataclass
class PostPreview:
    """Projection of a submission onto the Bluesky post record it would become.

    This is a verification preview, not the real publish (that's M2). ``links`` is
    ordered (canonical_url, domain_family); ``images`` is uploaded (filename, alt).
    """

    kind: str  # external | images | record | empty
    title: str | None
    links: list[tuple[str, str]]
    images: list[tuple[str, str | None]]
    embed_title: str | None
    embed_description: str | None
    embed_has_thumb: bool
    resolved_via: str | None = None
    labels: list[str] = field(default_factory=list)
    board_name: str = ""
    nsfw: bool = False
    graphic_status: str = "unknown"
    image_satisfied: bool = True
    image_source: str = "n/a"


def format_post_preview(p: PostPreview) -> str:
    human, atproto = _KIND_LABELS.get(p.kind, _KIND_LABELS["empty"])
    primary = p.links[0][0] if p.links else None

    lines: list[str] = ["🔎 **prospective Bluesky post**", ""]
    lines.append(f"type:  {human} ({atproto})")

    # text: source title (when known) followed by the primary canonical URL.
    lines.append("text:")
    if p.title:
        lines.append(f"  {p.title}")
    lines.append(f"  {primary}" if primary else "  (none)")

    # embed block depends on the chosen mode.
    if p.kind == "images":
        lines.append(f"embed.images ({len(p.images)}/4):")
        for i, (filename, alt) in enumerate(p.images, start=1):
            alt_text = f'"{alt}"' if alt else "⚠ (no alt text)"
            lines.append(f"  {i}. {filename} - alt: {alt_text}")
    elif p.kind == "external":
        lines.append("embed.external:")
        lines.append(f"  uri:    {primary}")
        lines.append(f"  title:  {p.embed_title or '(unresolved)'}")
        lines.append(f"  desc:   {p.embed_description or '(none)'}")
        lines.append(f"  thumb:  {'✓ image present' if p.embed_has_thumb else '⚠ MISSING'}")
        lines.append(f"  via:    {p.resolved_via or 'none'}")
    elif p.kind == "record":
        lines.append("embed.record:")
        lines.append(f"  uri:    {primary}  (native repost/quote of an existing post)")

    labels = ", ".join(p.labels) if p.labels else "none"
    lines.append(f"labels: {labels}  (board: {p.board_name}, {'NSFW' if p.nsfw else 'sfw'})")
    lines.append(f"graphic: {p.graphic_status}")

    # thread structure: first canonical link is the root, the rest become replies.
    extra = max(len(p.links) - 1, 0)
    lines.append("thread: " + (f"1 root + {extra} repl{'y' if extra == 1 else 'ies'}" if extra else "single post"))

    lines.append(f"image check: {'✓' if p.image_satisfied else '⚠'} {p.image_source}")

    text = "\n".join(lines)
    if len(text) > 1900:  # Discord hard-caps messages at 2000 chars.
        text = text[:1900] + "\n… (truncated)"
    return text
