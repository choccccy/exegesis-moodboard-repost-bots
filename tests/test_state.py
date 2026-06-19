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
