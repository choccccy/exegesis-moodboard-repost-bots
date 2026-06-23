from bot.state import (
    AltTextStatus,
    GraphicStatus,
    SubmissionState,
    SubmissionSnapshot,
    evaluate_state,
    missing_gaps,
    Gap,
)


def snap(**kw):
    base = dict(
        has_canonical_link=True,
        image_alt_statuses=[],
        graphic_status=GraphicStatus.UNKNOWN,
        graphic_classification_required=False,
    )
    base.update(kw)
    return SubmissionSnapshot(**base)


def test_ready_when_nothing_missing():
    assert evaluate_state(snap()) == SubmissionState.READY_TO_QUEUE
    assert missing_gaps(snap()) == []


def test_missing_source_blocks():
    s = snap(has_canonical_link=False)
    assert evaluate_state(s) == SubmissionState.AWAITING_SOURCE
    assert Gap.SOURCE in missing_gaps(s)


def test_missing_alt_text_blocks():
    s = snap(image_alt_statuses=[AltTextStatus.PROVIDED, AltTextStatus.NEEDED])
    assert evaluate_state(s) == SubmissionState.AWAITING_ALT_TEXT


def test_provided_alt_text_is_ready():
    s = snap(image_alt_statuses=[AltTextStatus.PROVIDED, AltTextStatus.PROVIDED])
    assert evaluate_state(s) == SubmissionState.READY_TO_QUEUE


def test_graphic_required_blocks_until_answered():
    s = snap(graphic_classification_required=True, graphic_status=GraphicStatus.UNKNOWN)
    assert evaluate_state(s) == SubmissionState.AWAITING_GRAPHIC_CLASSIFICATION
    s2 = snap(graphic_classification_required=True, graphic_status=GraphicStatus.NOT_GRAPHIC)
    assert evaluate_state(s2) == SubmissionState.READY_TO_QUEUE


def test_source_takes_precedence_over_alt_and_graphic():
    s = snap(
        has_canonical_link=False,
        image_alt_statuses=[AltTextStatus.NEEDED],
        graphic_classification_required=True,
    )
    assert evaluate_state(s) == SubmissionState.AWAITING_SOURCE
    assert missing_gaps(s) == [Gap.SOURCE, Gap.ALT_TEXT, Gap.GRAPHIC]


def test_missing_image_blocks_when_required():
    s = snap(needs_image=True, has_image=False)
    assert evaluate_state(s) == SubmissionState.AWAITING_IMAGE
    assert Gap.IMAGE in missing_gaps(s)


def test_image_satisfied_is_ready():
    assert evaluate_state(snap(needs_image=True, has_image=True)) == SubmissionState.READY_TO_QUEUE


def test_image_not_required_does_not_block():
    # e.g. a Bluesky-native repost: needs_image False even with no image
    assert evaluate_state(snap(needs_image=False, has_image=False)) == SubmissionState.READY_TO_QUEUE


def test_image_precedes_alt_text():
    s = snap(needs_image=True, has_image=False, image_alt_statuses=[AltTextStatus.NEEDED])
    assert evaluate_state(s) == SubmissionState.AWAITING_IMAGE
    assert missing_gaps(s) == [Gap.IMAGE, Gap.ALT_TEXT]


# --- metadata gap tests ---

def test_metadata_gap_fires_when_via_none():
    s = snap(needs_metadata=True, resolved_via="none")
    assert Gap.METADATA in missing_gaps(s)
    assert evaluate_state(s) == SubmissionState.AWAITING_BETTER_LINK


def test_metadata_gap_fires_when_resolved_via_is_none_sentinel():
    s = snap(needs_metadata=True, resolved_via=None)
    assert Gap.METADATA in missing_gaps(s)


def test_metadata_gap_suppressed_when_confirmed():
    s = snap(needs_metadata=True, resolved_via="none", metadata_confirmed=True)
    assert Gap.METADATA not in missing_gaps(s)


def test_metadata_gap_suppressed_for_images_kind():
    s = snap(needs_metadata=False, resolved_via="none")
    assert Gap.METADATA not in missing_gaps(s)


def test_metadata_gap_not_when_discord_fallback():
    s = snap(needs_metadata=True, resolved_via="discord")
    assert Gap.METADATA not in missing_gaps(s)


def test_metadata_gap_not_when_opengraph():
    s = snap(needs_metadata=True, resolved_via="opengraph")
    assert Gap.METADATA not in missing_gaps(s)


def test_metadata_before_image_in_gap_order():
    s = snap(needs_metadata=True, resolved_via="none", needs_image=True, has_image=False)
    gaps = missing_gaps(s)
    assert gaps.index(Gap.METADATA) < gaps.index(Gap.IMAGE)
    assert evaluate_state(s) == SubmissionState.AWAITING_BETTER_LINK


def test_metadata_gap_not_without_link():
    # No link → SOURCE gap fires, METADATA gap should not (nothing to resolve).
    s = snap(has_canonical_link=False, needs_metadata=True, resolved_via="none")
    assert Gap.SOURCE in missing_gaps(s)
    assert Gap.METADATA not in missing_gaps(s)


def test_source_precedes_metadata():
    s = snap(has_canonical_link=False, needs_metadata=True, resolved_via="none")
    assert missing_gaps(s)[0] == Gap.SOURCE
