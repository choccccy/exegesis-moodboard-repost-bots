"""Tests for the status-checklist renderer and the escape-hatch reply copy.

The checklist is the glanceable 'why isn't this queued' message; these pin its
per-requirement rendering (blocking vs optional, waived/skipped states, the
ready/blocked/terminal footers) and the waiver/skip notice copy.
"""

from __future__ import annotations

from bot.discord_ingest import replies
from bot.state import AltTextStatus, GraphicStatus, SubmissionSnapshot


def _snap(**kw) -> SubmissionSnapshot:
    base = dict(
        has_canonical_link=True,
        image_alt_statuses=[],
        graphic_status=GraphicStatus.UNKNOWN,
        graphic_classification_required=False,
    )
    base.update(kw)
    return SubmissionSnapshot(**base)


def test_checklist_ready_shows_queue_footer():
    out = replies.status_checklist(_snap(), ready=True, source_domain="artstation")
    assert "✅ source: artstation" in out
    assert "Ready to queue" in out


def test_checklist_source_needed_blocks():
    out = replies.status_checklist(_snap(has_canonical_link=False), ready=False)
    assert "⛔ source" in out
    assert "Not queued yet" in out
    assert "blocked on: source" in out


def test_checklist_source_waived():
    out = replies.status_checklist(
        _snap(has_canonical_link=False, source_waived=True), ready=True
    )
    assert "source: unknown (waived)" in out
    assert "Ready to queue" in out


def test_checklist_source_note():
    out = replies.status_checklist(
        _snap(has_canonical_link=False, source_note="Popular Mechanics, March 1965"), ready=True
    )
    assert "✅ source: Popular Mechanics, March 1965" in out
    assert "Ready to queue" in out


def test_checklist_source_note_truncated():
    out = replies.status_checklist(
        _snap(has_canonical_link=False, source_note="x" * 100), ready=True
    )
    assert "..." in out  # long notes are truncated in the checklist


def test_checklist_metadata_needed():
    out = replies.status_checklist(
        _snap(needs_metadata=True, resolved_via="none"), ready=False
    )
    assert "⛔ embed" in out


def test_checklist_metadata_ok_when_confirmed():
    out = replies.status_checklist(
        _snap(needs_metadata=True, resolved_via="none", metadata_confirmed=True), ready=True
    )
    assert "✅ embed metadata" in out


def test_checklist_image_needed_and_ok():
    blocked = replies.status_checklist(_snap(needs_image=True, has_image=False), ready=False)
    assert "⛔ image" in blocked
    ok = replies.status_checklist(_snap(needs_image=True, has_image=True), ready=True)
    assert "✅ image" in ok


def test_checklist_alt_text_states():
    needed = replies.status_checklist(
        _snap(image_alt_statuses=[AltTextStatus.NEEDED, AltTextStatus.PROVIDED]), ready=False
    )
    assert "⛔ alt text - needed for 1 of 2 image(s)" in needed

    skipped = replies.status_checklist(
        _snap(image_alt_statuses=[AltTextStatus.SKIPPED, AltTextStatus.PROVIDED]), ready=True
    )
    assert "✅ alt text (1 skipped)" in skipped

    done = replies.status_checklist(
        _snap(image_alt_statuses=[AltTextStatus.PROVIDED]), ready=True
    )
    assert "✅ alt text" in done and "skipped" not in done


def test_checklist_graphic_optional_and_set():
    optional = replies.status_checklist(
        _snap(graphic_classification_required=True), ready=True
    )
    assert "◽ graphic label (optional)" in optional

    marked = replies.status_checklist(
        _snap(graphic_classification_required=True, graphic_status=GraphicStatus.GRAPHIC),
        ready=True,
    )
    assert "✅ graphic label: graphic" in marked


def test_checklist_terminal_queued_footer():
    out = replies.status_checklist(_snap(), ready=True, terminal="queued")
    assert "✅ **Queued**" in out
    assert "use the button" not in out


def test_checklist_terminal_other_footer():
    out = replies.status_checklist(_snap(), ready=False, terminal="published to Bluesky")
    assert "published to Bluesky" in out


def test_checklist_no_source_domain():
    out = replies.status_checklist(_snap(), ready=True)
    assert "✅ source" in out


# --- escape-hatch copy ------------------------------------------------------


def test_source_request_with_waiver_points_to_slash_and_search():
    out = replies.source_request_with_waiver()
    assert "/no_source" in out              # the waiver is now a slash command, last resort
    assert "reverse-image-search" in out.lower()
    assert out.startswith("**reply with the source URL")


def test_no_source_marked_mentions_source_unknown():
    assert "source unknown" in replies.no_source_marked()


def test_source_note_confirm_quotes_the_note():
    out = replies.source_note_confirm("Popular Mechanics, March 1965")
    assert "Popular Mechanics, March 1965" in out
    assert "URL" in out


def test_source_note_confirm_truncates_long_note():
    assert "..." in replies.source_note_confirm("x" * 200)


def test_source_note_confirmed_and_rejected_copy():
    assert "Popular Mechanics" in replies.source_note_confirmed("Popular Mechanics")
    assert "source:" in replies.source_note_confirmed("anything")
    assert "URL" in replies.source_note_rejected()


def test_alt_text_skipped_all_counts():
    assert "1 image" in replies.alt_text_skipped_all(1)
    assert "3 images" in replies.alt_text_skipped_all(3)


def test_alt_text_request_hints_skip_alt():
    out = replies.alt_text_request("robot.jpg")
    assert "robot.jpg" in out
    assert "/skip_alt" in out  # quiet last-resort aside


# --- preview: sourceless media shows "source unknown" -----------------------


def test_preview_sourceless_media_shows_source_unknown():
    preview = replies.PostPreview(
        kind="images",
        title=None,
        links=[],
        images=[("pic.jpg", "a pic")],
        embed_title=None,
        embed_description=None,
        embed_has_thumb=False,
        board_name="robots",
    )
    pages = replies.format_post_preview(preview)
    assert any("source unknown" in p for p in pages)


def test_preview_source_note_shows_source_line():
    preview = replies.PostPreview(
        kind="images",
        title=None,
        links=[],
        images=[("pic.jpg", "a pic")],
        embed_title=None,
        embed_description=None,
        embed_has_thumb=False,
        board_name="robots",
        source_note="Popular Mechanics, March 1965",
    )
    pages = replies.format_post_preview(preview)
    assert any("source: Popular Mechanics, March 1965" in p for p in pages)
    assert not any("source unknown" in p for p in pages)  # note wins over the waived fallback


def test_preview_no_links_no_media_shows_none():
    preview = replies.PostPreview(
        kind="empty",
        title=None,
        links=[],
        images=[],
        embed_title=None,
        embed_description=None,
        embed_has_thumb=False,
        board_name="robots",
    )
    pages = replies.format_post_preview(preview)
    assert any("(none)" in p for p in pages)
