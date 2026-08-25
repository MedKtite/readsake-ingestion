"""Marker/cut logic — pure functions, synthetic segments, no audio needed."""

from ingestion.audio_cleaner import (
    MAX_INTRO_TRIM_S,
    Segment,
    find_intro_end,
    find_outro_start,
    resolve_cuts,
)

INTRO = [
    Segment(0.0, 6.0, "This is a LibriVox recording."),
    Segment(6.0, 12.0, "All LibriVox recordings are in the public domain."),
    Segment(12.0, 18.0, "For more information please visit librivox.org."),
    Segment(18.0, 24.0, "Read by John Doe."),
    Segment(24.0, 40.0, "Chapter 1. It is a truth universally acknowledged,"
                        " that a single man in possession of a good fortune"),
    Segment(40.0, 55.0, "must be in want of a wife."),
]


def test_intro_skips_boilerplate():
    cut, validation = find_intro_end(INTRO)
    assert cut == 24.0
    assert validation == "ok"


def test_intro_without_boilerplate_cuts_nothing():
    segments = [Segment(0.0, 10.0, "Call me Ishmael."),
                Segment(10.0, 20.0, "Some years ago, never mind how long")]
    cut, validation = find_intro_end(segments)
    assert cut == 0.0
    assert validation == "ok"


def test_intro_confirmed_by_book_opening():
    cut, validation = find_intro_end(
        INTRO, book_opening="It is a truth universally acknowledged")
    assert cut == 24.0  # start of the matching segment
    assert validation == "ok"


def test_intro_flags_when_opening_never_matches():
    cut, validation = find_intro_end(
        INTRO, book_opening="Call me Ishmael, some years ago")
    assert validation == "failed"
    assert cut == 24.0  # marker-based cut is kept anyway


def test_intro_never_trims_more_than_90s():
    endless = [
        Segment(i * 10.0, (i + 1) * 10.0, "read by a volunteer for librivox.org")
        for i in range(12)  # boilerplate until 120s
    ]
    cut, validation = find_intro_end(endless)
    assert cut == MAX_INTRO_TRIM_S
    assert validation == "failed"


def test_empty_transcript_is_flagged():
    cut, validation = find_intro_end([])
    assert cut == 0.0
    assert validation == "failed"


def test_outro_detected_in_final_window():
    segments = [
        Segment(0.0, 500.0, "story text"),
        Segment(500.0, 580.0, "more story text"),
        Segment(582.0, 600.0, "End of chapter four, read by John Doe."),
    ]
    assert find_outro_start(segments, total_duration=600.0) == 582.0


def test_outro_marker_outside_window_is_ignored():
    segments = [
        # "end of" spoken mid-chapter must NOT truncate the track.
        Segment(100.0, 130.0, "and that was the end of the affair"),
        Segment(130.0, 600.0, "the story continues to the last second"),
    ]
    assert find_outro_start(segments, total_duration=600.0) == 600.0


def test_no_outro_returns_full_duration():
    segments = [Segment(0.0, 600.0, "story text")]
    assert find_outro_start(segments, total_duration=600.0) == 600.0


def test_resolve_cuts_keeps_original_when_clip_too_short():
    start, end, validation, keep = resolve_cuts(
        cut_start=598.0, cut_end=600.0, total_duration=600.0, validation="ok")
    assert keep is True
    assert validation == "failed"
    assert (start, end) == (0.0, 600.0)


def test_resolve_cuts_passes_sane_range_through():
    start, end, validation, keep = resolve_cuts(
        cut_start=24.0, cut_end=590.0, total_duration=600.0, validation="ok")
    assert keep is False
    assert validation == "ok"
    assert (start, end) == (24.0, 590.0)
