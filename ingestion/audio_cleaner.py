"""Trim LibriVox boilerplate and normalize loudness — the pipeline's core.

Every LibriVox track opens with spoken boilerplate ("This is a LibriVox
recording. All LibriVox recordings are in the public domain… read by…") and
often closes with an outro ("End of chapter X, read by…"). This module
transcribes each track with faster-whisper, finds where the real book text
begins/ends, cuts that range with ffmpeg, and applies EBU R128 loudness
normalization in the same pass.

The marker/cut logic is pure (list-of-segments in, seconds out) so it can be
unit-tested without any audio. Heavy dependencies (faster-whisper) are
imported lazily; ffmpeg/ffprobe are invoked as system binaries.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

log = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────

INTRO_MARKERS = (
    "librivox recording",
    "public domain",
    "librivox.org",
    "read by",
    "recording by",
    "for more information",
    "volunteer",
)

OUTRO_MARKERS = (
    "end of",
    "read by",
    "librivox recording",
)

MAX_INTRO_TRIM_S = 90.0   # never cut more than this off the front
OUTRO_WINDOW_S = 40.0     # only look for an outro in the last N seconds
MIN_CLIP_S = 5.0          # a shorter result means the cuts are wrong
OPENING_MATCH_THRESHOLD = 80  # rapidfuzz partial_ratio, 0–100


@dataclass(frozen=True)
class Segment:
    """One transcribed span — mirrors faster-whisper's segment shape."""

    start: float
    end: float
    text: str


# ── Pure cut logic (unit-tested, no audio needed) ───────────────────────────


def _is_boilerplate(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in markers)


def find_intro_end(
    segments: Sequence[Segment],
    book_opening: Optional[str] = None,
) -> tuple[float, str]:
    """Where the real book text begins.

    Skips leading segments that contain intro boilerplate. When the book's
    known opening sentence is provided, the cut must be CONFIRMED by fuzzy-
    matching a segment against it — cut at that segment's start; if nothing
    in the scan window ever matches, keep the marker-based cut but flag
    validation='failed' so a human reviews it.

    Returns (cut_start_seconds, validation) where validation is 'ok'|'failed'.
    """
    if not segments:
        return 0.0, "failed"

    cut = 0.0
    for seg in segments:
        if seg.start >= MAX_INTRO_TRIM_S:
            break
        if _is_boilerplate(seg.text, INTRO_MARKERS):
            cut = seg.end
        else:
            break  # first real-sounding leading segment ends the intro

    validation = "ok"
    if book_opening:
        from rapidfuzz import fuzz

        opening = book_opening.lower().strip()
        confirmed: Optional[float] = None
        for seg in segments:
            if seg.start > MAX_INTRO_TRIM_S:
                break
            if fuzz.partial_ratio(opening, seg.text.lower()) >= OPENING_MATCH_THRESHOLD:
                confirmed = seg.start
                break
        if confirmed is not None:
            cut = confirmed
        else:
            validation = "failed"

    if cut >= MAX_INTRO_TRIM_S:  # hitting the clamp at all means "review me"
        cut = MAX_INTRO_TRIM_S
        validation = "failed"
    return cut, validation


def find_outro_start(
    segments: Sequence[Segment],
    total_duration: float,
) -> float:
    """Where the outro begins — or the full duration when there is none.

    Only the last OUTRO_WINDOW_S seconds are scanned; an "end of…"/"read
    by…" in the body of a chapter must not truncate it.
    """
    window_start = total_duration - OUTRO_WINDOW_S
    for seg in segments:
        if seg.start < window_start:
            continue
        if _is_boilerplate(seg.text, OUTRO_MARKERS):
            return seg.start
    return total_duration


def resolve_cuts(
    cut_start: float,
    cut_end: float,
    total_duration: float,
    validation: str,
) -> tuple[float, float, str, bool]:
    """Final safety clamp. Returns (start, end, validation, keep_original).

    A resulting clip shorter than MIN_CLIP_S means the markers misfired —
    keep the original untouched and flag it for review.
    """
    if cut_end - cut_start < MIN_CLIP_S:
        return 0.0, total_duration, "failed", True
    return cut_start, cut_end, validation, False


# ── Whisper (loaded once, reused across every file) ─────────────────────────

_model = None


def get_model(device: Optional[str] = None):
    """The shared faster-whisper model — loading it is the slow part."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        name = os.getenv("WHISPER_MODEL", "base")
        dev = device or os.getenv("WHISPER_DEVICE", "cpu")
        compute = "int8" if dev == "cpu" else "float16"
        log.info("Loading Whisper model '%s' on %s (%s)…", name, dev, compute)
        _model = WhisperModel(name, device=dev, compute_type=compute)
    return _model


def transcribe(path: str, device: Optional[str] = None) -> list[Segment]:
    model = get_model(device)
    raw_segments, _info = model.transcribe(path, vad_filter=True)
    return [Segment(s.start, s.end, s.text) for s in raw_segments]


# ── ffmpeg / ffprobe ────────────────────────────────────────────────────────

LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def probe_duration(path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def _ffmpeg_cut(
    in_path: str,
    out_path: str,
    start: float,
    end: float,
    normalize: bool,
) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", in_path,
           "-ss", f"{start:.2f}", "-to", f"{end:.2f}"]
    if normalize:
        cmd += ["-af", LOUDNORM]
    cmd += ["-codec:a", "libmp3lame", "-q:a", "4", out_path]
    subprocess.run(cmd, check=True)


# ── The public entrypoint ───────────────────────────────────────────────────


def trim_track(
    in_path: str,
    out_path: str,
    book_opening: Optional[str] = None,
    normalize: bool = True,
    device: Optional[str] = None,
) -> dict:
    """Clean one track: transcribe → find cuts → cut+normalize → audit dict."""
    original_duration = probe_duration(in_path)
    segments = transcribe(in_path, device)

    cut_start, validation = find_intro_end(segments, book_opening)
    cut_end = find_outro_start(segments, original_duration)
    cut_start, cut_end, validation, keep_original = resolve_cuts(
        cut_start, cut_end, original_duration, validation
    )

    if keep_original:
        # Cuts misfired — ship the untouched original, flagged for review.
        shutil.copyfile(in_path, out_path)
        normalized = False
    else:
        _ffmpeg_cut(in_path, out_path, cut_start, cut_end, normalize)
        normalized = normalize

    return {
        "input": in_path,
        "output": out_path,
        "original_duration": round(original_duration, 2),
        "cut_start": round(cut_start, 2),
        "cut_end": round(cut_end, 2),
        "new_duration": round(
            original_duration if keep_original else cut_end - cut_start, 2
        ),
        "normalized": normalized,
        "validation": validation,
    }
