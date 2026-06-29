"""Procedural reply text. Utilitarian and dead-simple - never chatty."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..moderation import GRAPHIC_YES_EMOJI

METADATA_CONFIRM_EMOJI = "🔗"
CANCEL_EMOJI = "❌"
PLAYLIST_OPT_OUT_EMOJI = "⏹️"
CONFIRMATION_EMOJI = "✅"


def source_request() -> str:
    return "**reply to this message** with the source URL"


def image_request() -> str:
    return "this link has no preview image. **reply to this message** attaching at least one image to use"


def image_not_found() -> str:
    return "no image attached - **reply again** attaching at least one image"


def media_not_found() -> str:
    return "no image or video attached - **reply again** attaching at least one"


def alt_text_request(filename: str) -> str:
    return f"**reply to this message** with the alt text for **{filename}**"


def graphic_request() -> str:
    return "does this post contain graphic or gore content?"


def confirmation_request(
    bluesky_handle: str | None = None,
    youtube_playlist_id: str | None = None,
) -> str:
    if bluesky_handle:
        dest = f"[{bluesky_handle} on Bluesky](<https://bsky.app/profile/{bluesky_handle}>)"
    else:
        dest = "Bluesky"
    playlist_part = " (and add to the YouTube playlist)" if youtube_playlist_id else ""
    return f"queue this for posting to {dest}{playlist_part}?"


def metadata_request(url: str) -> str:
    return (
        f"couldn't get metadata from **{url}** - **reply with a better link**, "
        f"or press **Use link as-is** below (at least one image will be required)"
    )


def metadata_confirmed() -> str:
    return f"{METADATA_CONFIRM_EMOJI} noted - link confirmed as best available; at least one image must be attached"


def metadata_link_updated(new_url: str) -> str:
    return f"updated source to {new_url} - re-resolving metadata…"


def metadata_url_not_found() -> str:
    return f"no URL found - **reply again** with a link, or **react {METADATA_CONFIRM_EMOJI}** to use the existing one as-is"


def source_not_found() -> str:
    return "no URL found in that reply - **reply again** with the source URL"


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
        first = f"Queued - will post to [{bluesky_handle} on Bluesky](<{bsky_url}>) at the next available slot."
    else:
        first = "Queued - will post at the next available slot."
    parts = [first]
    if dashboard_url:
        parts.append(f"You can see what else is queued on the [dashboard](<{dashboard_url}>).")
    if videos_added > 0 and youtube_playlist_id:
        playlist_url = f"https://www.youtube.com/playlist?list={youtube_playlist_id}"
        parts.append(f"-# Also added to the [YouTube playlist](<{playlist_url}>).")
    return "\n".join(parts)


def updated_notice() -> str:
    return "post updated - the new content will be included when published"


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


def duplicate_posted(bsky_url: str) -> str:
    return f"this has already been posted: {bsky_url}\nClosing this thread."


def duplicate_queued(thread_url: str | None) -> str:
    if thread_url:
        return f"this is already queued for posting: {thread_url}\nClosing this thread."
    return "this link is already queued for posting.\nClosing this thread."


def duplicate_pending(thread_url: str | None) -> str:
    if thread_url:
        return f"this link is already being processed: {thread_url}\nClosing this thread."
    return "this link is already being processed.\nClosing this thread."


def thread_name(submission_id: int) -> str:
    # Discord caps thread names at 100 chars; this stays well under.
    return f"🦋 submission {submission_id}"


def thread_anchor(
    *,
    author_mention: str,
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
    return "\n".join(parts)


def closing_notice(reason: str) -> str:
    return f"-# {reason} - closing thread"


def supplemental_image_request() -> str:
    return "🖼️ **reply to this message** with any additional images or videos to include in the post."


def supplemental_link_request() -> str:
    return "🔗 **reply to this message** with any additional links to include as thread replies in the post."


def supplemental_link_not_found() -> str:
    return "no URLs found - **reply again** with the link(s) to add"


def cancel_request() -> str:
    return f"or react {CANCEL_EMOJI} on the original post to cancel"


def source_cancel_confirmation(user_id: int) -> str:
    return f"<@{user_id}> cancelled this submission via ❌ on the source post - removed from queue"


def playlist_opt_out_prompt() -> str:
    return "this will be added to the YouTube playlist - press **Skip playlist** below to opt out"


# Human labels + atproto $type per embed mode.
_KIND_LABELS = {
    "external": ("external link card", "app.bsky.embed.external"),
    "images": ("image post", "app.bsky.embed.images"),
    "video": ("video post", "app.bsky.embed.video"),
    "record": ("Bluesky repost/quote", "app.bsky.embed.record"),
    "empty": ("(no source yet)", "-"),
}


@dataclass
class PostPreview:
    """Projection of a submission onto the Bluesky post record it would become.

    ``links`` is ordered (canonical_url, domain_family, resolved_title);
    ``images`` is uploaded (filename, alt).
    """

    kind: str  # external | images | video | record | empty
    title: str | None
    links: list[tuple[str, str, str | None]]  # (canonical_url, domain_family, resolved_title)
    images: list[tuple[str, str | None]]
    embed_title: str | None
    embed_description: str | None
    embed_has_thumb: bool
    resolved_via: str | None = None
    videos: list[tuple[str, str | None]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    board_name: str = ""
    nsfw: bool = False
    graphic_status: str = "unknown"
    image_satisfied: bool = True
    image_source: str = "n/a"
    reply_to_bsky_url: str | None = None
    reply_to_pending: bool = False


_DISCORD_MSG_LIMIT = 1900


def _paginate(lines: list[str], *, header: str = "") -> list[str]:
    """Split lines into Discord-safe pages (≤ _DISCORD_MSG_LIMIT chars each).

    Continuation pages start with `header` so the reader has context.
    Lines that individually exceed the limit are hard-split as a last resort.
    """
    pages: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        if current:
            pages.append("\n".join(current))
        current.clear()
        nonlocal current_len
        current_len = 0

    def _continuation_prefix() -> list[str]:
        return [header, ""] if header else []

    for line in lines:
        # Hard-split a single line that's too long to fit in any page.
        while len(line) > _DISCORD_MSG_LIMIT:
            chunk = line[:_DISCORD_MSG_LIMIT]
            line = line[_DISCORD_MSG_LIMIT:]
            if current:
                _flush()
                current.extend(_continuation_prefix())
                current_len = sum(len(l) + 1 for l in current)
            current.append(chunk)
            _flush()
            current.extend(_continuation_prefix())
            current_len = sum(len(l) + 1 for l in current)

        needed = len(line) + (1 if current else 0)
        if current and current_len + needed > _DISCORD_MSG_LIMIT:
            _flush()
            current.extend(_continuation_prefix())
            current_len = sum(len(l) + 1 for l in current)

        current.append(line)
        current_len += needed

    _flush()
    return pages or [""]


def format_post_preview(p: PostPreview) -> list[str]:
    """Return one or more Discord messages (≤ 1900 chars each) for the preview.

    When the submission will produce a Bluesky reply thread (multiple posts),
    each post gets its own labeled block so the curator can see exactly what
    will be posted. Single-post submissions get an unlabeled block.
    """
    human, atproto = _KIND_LABELS.get(p.kind, _KIND_LABELS["empty"])
    primary_url = p.links[0][0] if p.links else None

    # Calculate how many Bluesky posts this submission will produce.
    if p.kind == "video":
        extra_vids = max(len(p.videos) - 1, 0)
        has_img_reply = len(p.images) > 0
        extra_links = max(len(p.links) - 1, 0)
        total = 1 + extra_vids + (1 if has_img_reply else 0) + extra_links
    else:
        total = max(len(p.links), 1)

    def _thread_label(n: int) -> str:
        return f"**thread {n}/{total}:**" if total > 1 else ""

    lines: list[str] = ["🔎 **prospective Bluesky post**", ""]

    # --- Root post ---
    label = _thread_label(1)
    if label:
        lines.append(label)
    lines.append(f"type:  {human} ({atproto})")
    if p.reply_to_bsky_url:
        lines.append(f"reply-to: {p.reply_to_bsky_url}")
    elif p.reply_to_pending:
        lines.append("reply-to: (parent queued - will wait to publish)")

    lines.append("text:")
    if p.title:
        lines.append(f"  {p.title}")
    lines.append(f"  {primary_url}" if primary_url else "  (none)")

    if p.kind == "video":
        first_vid = p.videos[0] if p.videos else None
        if first_vid:
            filename, alt = first_vid
            alt_text = f'"{alt}"' if alt else "⚠ (no alt text)"
            lines.append(f"embed.video: {filename} - alt: {alt_text}")
    elif p.kind == "images":
        lines.append(f"embed.images ({len(p.images)}/4):")
        for i, (filename, alt) in enumerate(p.images, start=1):
            alt_text = f'"{alt}"' if alt else "⚠ (no alt text)"
            lines.append(f"  {i}. {filename} - alt: {alt_text}")
    elif p.kind == "external":
        lines.append("embed.external:")
        lines.append(f"  uri:    {primary_url}")
        lines.append(f"  title:  {p.embed_title or '(unresolved)'}")
        lines.append(f"  desc:   {p.embed_description or '(none)'}")
        lines.append(f"  thumb:  {'✓ image present' if p.embed_has_thumb else '⚠ MISSING'}")
        lines.append(f"  via:    {p.resolved_via or 'none'}")
    elif p.kind == "record":
        lines.append("embed.record:")
        lines.append(f"  uri:    {primary_url}  (native repost/quote of an existing post)")

    labels = ", ".join(p.labels) if p.labels else "none"
    lines.append(f"labels: {labels}  (board: {p.board_name}, {'NSFW' if p.nsfw else 'sfw'})")
    lines.append(f"graphic: {p.graphic_status}")
    lines.append(f"image check: {'✓' if p.image_satisfied else '⚠'} {p.image_source}")

    # --- Reply posts ---
    reply_num = 2

    if p.kind == "video":
        # Extra video replies (videos[1:])
        for filename, alt in p.videos[1:]:
            lines.append("")
            lines.append(_thread_label(reply_num))
            lines.append("type:  video reply")
            alt_text = f'"{alt}"' if alt else "⚠ (no alt text)"
            lines.append(f"  {filename} - alt: {alt_text}")
            reply_num += 1

        # Image reply (if images exist alongside videos)
        if p.images:
            lines.append("")
            lines.append(_thread_label(reply_num))
            lines.append(f"type:  image reply ({len(p.images)}/4):")
            for i, (filename, alt) in enumerate(p.images, start=1):
                alt_text = f'"{alt}"' if alt else "⚠ (no alt text)"
                lines.append(f"  {i}. {filename} - alt: {alt_text}")
            reply_num += 1

    # Extra link replies (apply to all kinds)
    for url, _family, title in p.links[1:]:
        lines.append("")
        lines.append(_thread_label(reply_num))
        lines.append("type:  link reply")
        lines.append("text:")
        if title:
            lines.append(f"  {title}")
        lines.append(f"  {url}")
        reply_num += 1

    return _paginate(lines, header="-# 🔎 (cont.)")
