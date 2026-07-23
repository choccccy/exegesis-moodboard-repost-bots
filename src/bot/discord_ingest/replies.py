"""Procedural reply text. Utilitarian and dead-simple - never chatty."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..moderation import GRAPHIC_YES_EMOJI
from ..state import AltTextStatus, Gap, GraphicStatus, missing_gaps

_GAP_LABEL = {
    Gap.SOURCE: "source",
    Gap.METADATA: "embed metadata",
    Gap.IMAGE: "image",
    Gap.ALT_TEXT: "alt text",
}

METADATA_CONFIRM_EMOJI = "🔗"
CANCEL_EMOJI = "❌"
PLAYLIST_OPT_OUT_EMOJI = "⏹️"
CONFIRMATION_EMOJI = "✅"


def source_request() -> str:
    return "**reply with the source URL.**"


def source_request_with_waiver() -> str:
    """Source prompt shown when the media could publish sourceless (media present)."""
    return (
        "**reply with the source URL.**\n"
        "-# truly untraceable (old scan, etc.)? reverse-image-search first "
        "(Google / TinEye / SauceNAO), then `/no_source` as a last resort."
    )


def no_source_marked() -> str:
    return 'marked as no known source - this post will publish with a "source unknown" note'


def _truncate_note(note: str, limit: int = 120) -> str:
    return note if len(note) <= limit else note[: limit - 3] + "..."


def source_note_confirm(note: str) -> str:
    """Ask whether a non-URL reply should be used as a free-text source."""
    return (
        f'that doesn\'t look like a URL. use "{_truncate_note(note)}" as the source? '
        "it will publish as **source: ...** instead of a link."
    )


def source_note_confirmed(note: str) -> str:
    return f'source set: "{_truncate_note(note)}" - this post will publish with a "source: ..." note'


def source_note_rejected() -> str:
    return "discarded - reply with the source URL, or the source text again to retry"


def alt_text_skipped_all(count: int) -> str:
    noun = "image" if count == 1 else "images"
    return f"⏭️ alt text skipped for {count} {noun} - this post will publish without descriptions"


def alt_text_overwritten(filename: str, previous: str | None) -> str:
    if previous:
        shown = previous if len(previous) <= 100 else previous[:97] + "..."
        return f'🔁 alt text for **{filename}** updated (was: "{shown}")'
    return f"🔁 alt text for **{filename}** updated"


def status_checklist(
    snap,
    *,
    ready: bool,
    source_domain: str | None = None,
    terminal: str | None = None,
) -> str:
    """Render the live 'post status' checklist for a submission thread.

    One glanceable message, edited in place as gaps are filled, that makes the
    limiting factor obvious. ``snap`` is a state.SubmissionSnapshot. ``terminal``
    (e.g. "queued") renders a final footer instead of the blocked/ready one.
    """
    lines: list[str] = ["**post status**"]

    if snap.has_canonical_link:
        lines.append(f"✅ source: {source_domain}" if source_domain else "✅ source")
    elif snap.source_note:
        lines.append(f"✅ source: {_truncate_note(snap.source_note, 60)}")
    elif snap.source_waived:
        lines.append("🚫 source: unknown (waived)")
    else:
        lines.append("⛔ source - reply with the source URL")

    if snap.needs_metadata:
        meta_ok = snap.metadata_confirmed or snap.resolved_via not in (None, "none")
        lines.append(
            "✅ embed metadata" if meta_ok
            else "⛔ embed - reply with a better link or attach an image"
        )

    if snap.needs_image:
        lines.append("✅ image" if snap.has_image else "⛔ image - attach at least one")

    statuses = snap.image_alt_statuses
    if statuses:
        needed = sum(1 for s in statuses if s == AltTextStatus.NEEDED)
        skipped = sum(1 for s in statuses if s == AltTextStatus.SKIPPED)
        if needed:
            lines.append(f"⛔ alt text - needed for {needed} of {len(statuses)} image(s)")
        else:
            lines.append(f"✅ alt text ({skipped} skipped)" if skipped else "✅ alt text")

    if snap.graphic_classification_required:
        if snap.graphic_status != GraphicStatus.UNKNOWN:
            lines.append(f"✅ graphic label: {snap.graphic_status.value}")
        else:
            lines.append("◽ graphic label (optional) - not set")

    lines.append("")
    if terminal == "queued":
        lines.append("✅ **Queued** - will post at the next available slot.")
    elif terminal:
        lines.append(f"-# {terminal}")
    elif ready:
        lines.append("✅ **Ready to queue** - use the button below.")
    else:
        blockers = [_GAP_LABEL[g] for g in missing_gaps(snap)]
        joined = ", ".join(blockers) if blockers else "nothing"
        lines.append(f"⛔ **Not queued yet** - blocked on: {joined}")

    return "\n".join(lines)


def image_request(source_unavailable: bool = False) -> str:
    if source_unavailable:
        return (
            "⚠️ this twitter post is age-restricted, so twitter won't provide me an image "
            "(it may look fine to you because you're logged in). the link stays as the source - "
            "**reply to this message** attaching the image(s) yourself, or use `/no_source` if "
            "there's nothing to post."
        )
    return "this link has no preview image. **reply to this message** attaching at least one image to use"


def image_not_found() -> str:
    return "no image attached - **reply again** attaching at least one image"


def media_not_found() -> str:
    return "no image or video attached - **reply again** attaching at least one"


def alt_text_request(filename: str) -> str:
    return (
        f"**reply with alt text for {filename}** - a short description for screen-reader users.\n"
        "-# can't caption these? `/skip_alt` waives alt for the whole post (last resort)."
    )


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


def queue_blocked_notice(gaps: str) -> str:
    return (
        f"⚠️ not queued - this submission still needs: {gaps}. "
        "Check the status checklist above; the Queue button reopens once everything's resolved."
    )


def publish_failed_notice(error: str | None, mention_user_ids: list[int] | None = None) -> str:
    mentions = " ".join(f"<@{uid}>" for uid in (mention_user_ids or []))
    prefix = f"{mentions} " if mentions else ""
    # Error in a fenced code block so it's easy to select and copy, and so any
    # backticks/markdown in the message render literally rather than as formatting.
    detail = (error or "unknown error").replace("```", "``​`")
    return (
        f"{prefix}⚠️ publish failed:\n"
        f"```\n{detail}\n```\n"
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
        f"\n-# {bot_mention} will ask below for anything missing (source, alt text, rating). "
        "Answer here, or leave it to a curator."
    )
    if dashboard_url:
        parts.append(f"\nYou can see what else is queued on the [dashboard](<{dashboard_url}>).")
    return "\n".join(parts)


def closing_notice(reason: str) -> str:
    return f"-# {reason} - closing thread"


def supplemental_image_request() -> str:
    return "-# 🖼️ reply here to add more images or videos to this post."


def supplemental_link_request() -> str:
    return "-# 🔗 reply here to add links as extra thread replies."


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
    source_note: str | None = None  # confirmed non-URL free-text source, if any


_DISCORD_MSG_LIMIT = 1900


def _paginate(lines: list[str], *, header: str = "") -> list[str]:
    """Split lines into Discord-safe pages (≤ _DISCORD_MSG_LIMIT chars each).

    Continuation pages start with `header` so the reader has context. The
    header counts against the page budget, so no page ever exceeds the limit -
    lines too long for the remaining space are hard-split as a last resort.
    """
    pages: list[str] = []
    current: list[str] = []
    current_len = 0  # length of "\n".join(current), counted conservatively

    def _start_continuation() -> None:
        nonlocal current, current_len
        if current:
            pages.append("\n".join(current))
        current = [header, ""] if header else []
        current_len = sum(len(l) + 1 for l in current)

    for line in lines:
        while True:
            room = _DISCORD_MSG_LIMIT - current_len - (1 if current else 0)
            if len(line) <= room:
                break
            if room > 20:  # worth filling the rest of this page before splitting
                current.append(line[:room])
                line = line[room:]
            _start_continuation()
        current.append(line)
        current_len += len(line) + 1

    if current:
        pages.append("\n".join(current))
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
    if primary_url:
        lines.append(f"  {primary_url}")
    elif p.source_note:
        lines.append(f"  source: {p.source_note}")  # non-URL free-text source
    elif p.images or p.videos:
        lines.append("  source unknown")  # sourceless media post (source waived)
    else:
        lines.append("  (none)")

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
