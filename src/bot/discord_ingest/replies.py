"""Procedural reply text. Utilitarian and dead-simple - never chatty."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..moderation import GRAPHIC_NO_EMOJI, GRAPHIC_YES_EMOJI

METADATA_CONFIRM_EMOJI = "🔗"
CANCEL_EMOJI = "❌"
PLAYLIST_OPT_OUT_EMOJI = "⏹️"


def source_request() -> str:
    return "reply to this message with the source URL"


def image_request() -> str:
    return (
        "this link has no preview image, so the post would have no image. "
        "reply to this message attaching an image to use"
    )


def image_not_found() -> str:
    return "no image attached - reply again attaching at least one image"


def alt_text_request(filename: str) -> str:
    return f"reply to this message with the alt text for **{filename}**"


def graphic_request() -> str:
    return (
        f"react {GRAPHIC_YES_EMOJI} if this contains graphic/gore content, "
        f"{GRAPHIC_NO_EMOJI} if it's safe"
    )


def ready_confirmation() -> str:
    return "✓ all required info received - this submission is ready to queue"


def metadata_request(url: str) -> str:
    return (
        f"couldn't get any metadata from **{url}** - reply with a more embeddable "
        f"link, or react {METADATA_CONFIRM_EMOJI} to use it as-is (at least one image will be required)"
    )


def metadata_confirmed() -> str:
    return f"{METADATA_CONFIRM_EMOJI} noted - link confirmed as best available; at least one image must be attached"


def metadata_link_updated(new_url: str) -> str:
    return f"updated source to {new_url} - re-resolving metadata…"


def metadata_url_not_found() -> str:
    return f"no URL found - reply again with a link, or react {METADATA_CONFIRM_EMOJI} to use the existing one as-is"


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


def queued_notice(
    bluesky_handle: str | None = None,
    dashboard_url: str | None = None,
    youtube_playlist_id: str | None = None,
    videos_added: int = 0,
) -> str:
    if bluesky_handle:
        bsky_url = f"https://bsky.app/profile/{bluesky_handle}"
        first = f"Queued - will post to [{bluesky_handle} on Bluesky](<{bsky_url}>) at the next available slot ."
    else:
        first = "Queued - will post at the next available slot."
    parts = [first]
    if dashboard_url:
        parts.append(f"You can see what else is queued on the [dashboard](<{dashboard_url}>).")
    if videos_added > 0 and youtube_playlist_id:
        playlist_url = f"https://www.youtube.com/playlist?list={youtube_playlist_id}"
        parts.append(f"-# Also added to the [YouTube playlist](<{playlist_url}>).")
    return "\n".join(parts)


def publish_failed_notice(error: str | None) -> str:
    return (
        f"publish failed: {error or 'unknown error'}\n"
        "will retry automatically at the next available queue slot"
    )


def duplicate_warning(bsky_url: str) -> str:
    return (
        f"⚠ this URL was already posted: {bsky_url}\n"
        "proceeding anyway - remove 🦋 if this was a mistake"
    )


def thread_name(submission_id: int) -> str:
    # Discord caps thread names at 100 chars; this stays well under.
    return f"🦋 submission {submission_id}"


def thread_anchor(
    *,
    author_mention: str,
    curator_user_mentions: list[str],
    bot_mention: str = "The bot",
    board_display_name: str | None = None,
    bluesky_handle: str | None = None,
    youtube_playlist_id: str | None = None,
    content_title: str | None = None,
    dashboard_url: str | None = None,
) -> str:
    """Top-of-thread orientation message for the OP and curators."""
    if bluesky_handle:
        dest = f"[{bluesky_handle} on Bluesky](<https://bsky.app/profile/{bluesky_handle}>)"
    else:
        dest = "Bluesky"

    board_part = f" for the **{board_display_name}** moodboard" if board_display_name else ""

    if youtube_playlist_id:
        playlist_url = f"https://www.youtube.com/playlist?list={youtube_playlist_id}"
        playlist_part = f" and added to the [YouTube playlist](<{playlist_url}>)"
    else:
        playlist_part = ""

    parts = [
        f"🦋 {author_mention}, your post was picked up{board_part} and will be scheduled to post to {dest}{playlist_part}."
    ]
    if content_title:
        parts.append(f"📌 \"{content_title}\"")
    parts.append(
        f"\n{bot_mention} will ask a few questions below (alt text, content rating, etc.). "
        "You can answer them yourself, or wait for a curator - either works."
    )
    if dashboard_url:
        parts.append(f"\nYou can see what else is queued on the [dashboard](<{dashboard_url}>).")
    if curator_user_mentions:
        parts.append(f"-# Curators: {' '.join(curator_user_mentions)}")
    return "\n".join(parts)


def supplemental_image_request() -> str:
    return "📎 Reply to this message with any additional or supplemental images to include in the post."


def cancel_request() -> str:
    return f"react {CANCEL_EMOJI} here to cancel, or react {CANCEL_EMOJI} on the original post"


def source_cancel_confirmation(user_id: int) -> str:
    return f"<@{user_id}> cancelled this submission via ❌ on the source post - removed from queue"


def playlist_opt_out_prompt() -> str:
    return f"this will be added to the YouTube playlist - react {PLAYLIST_OPT_OUT_EMOJI} to skip it"


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
