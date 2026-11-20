"""Suggest and approve playback starts for the original public audiobook files."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import audio_cleaner

MAX_START_MS = int(audio_cleaner.MAX_INTRO_TRIM_S * 1000)
PREVIEW_SECONDS = int(audio_cleaner.MAX_INTRO_TRIM_S) + 15


def _validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None:
        raise ValueError("Use the exact HTTPS audio URL supplied to Flutter.")
    return url


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def propose(url: str, opening: str, output: Path, device: str | None) -> dict:
    """Download only the opening preview; any proposed offset requires review."""
    _validate_url(url)
    if len(opening.split()) < 8:
        raise ValueError("Supply at least eight words of the track's opening narration.")
    with tempfile.TemporaryDirectory(prefix="readsake_audio_opening_") as directory:
        preview = Path(directory) / "opening.wav"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-rw_timeout", "15000000", "-i", url, "-t", str(PREVIEW_SECONDS),
                "-vn", "-ac", "1", "-ar", "16000", str(preview),
            ],
            check=True,
            timeout=90,
        )
        duration_ms = int(audio_cleaner.probe_duration(str(preview)) * 1000)
        segments = audio_cleaner.transcribe(str(preview), device)

    start, validation = audio_cleaner.find_intro_end(segments, opening)
    candidate_ms = max(0, int(start * 1000) - 400)
    report = {
        "audio_url": url,
        "opening_text": opening,
        "preview_duration_ms": duration_ms,
        "suggested_start_ms": (
            candidate_ms if validation == "ok" and candidate_ms < duration_ms else None
        ),
        "transcript": [
            {"start_ms": int(segment.start * 1000), "text": segment.text.strip()}
            for segment in segments
        ],
    }
    _write_json(output, report)
    return report


def approve(report_path: Path, manifest: Path, start_ms: int) -> None:
    """Record a human-reviewed offset without changing the source audio."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    url = _validate_url(report["audio_url"])
    duration_ms = report["preview_duration_ms"]
    if not 0 <= start_ms <= MAX_START_MS or start_ms >= duration_ms:
        raise ValueError("Verified start must be within the reviewed preview and 0–90 seconds.")

    manifest.parent.mkdir(parents=True, exist_ok=True)
    lock = manifest.with_name(manifest.name + ".lock")
    if report_path.resolve() == manifest.resolve():
        raise ValueError("Use a separate review file and playback manifest.")
    lock.touch(exist_ok=False)
    try:
        entries = (
            json.loads(manifest.read_text(encoding="utf-8"))
            if manifest.exists()
            else {}
        )
        if not isinstance(entries, dict):
            raise ValueError("Manifest must be a JSON object mapping audio URLs to milliseconds.")
        entries[url] = start_ms
        _write_json(manifest, entries)
    finally:
        lock.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    proposal = subcommands.add_parser("propose", help="Transcribe an opening for human review")
    proposal.add_argument("--url", required=True, help="Exact HTTPS track URL used by Flutter")
    proposal.add_argument("--opening", required=True, help="Known opening lines of this track")
    proposal.add_argument("--output", type=Path, required=True)
    proposal.add_argument("--device", choices=["cpu", "cuda"])

    approval = subcommands.add_parser("approve", help="Publish a manually checked start")
    approval.add_argument("--review", type=Path, required=True)
    approval.add_argument("--manifest", type=Path, required=True)
    approval.add_argument("--start-ms", type=int, required=True, help="Verified milliseconds, or 0 if clean")

    args = parser.parse_args(argv)
    try:
        if args.command == "propose":
            report = propose(args.url, args.opening, args.output, args.device)
            candidate = report["suggested_start_ms"]
            print(f"Review written to {args.output}. Suggested start (ms): {candidate}")
        else:
            approve(args.review, args.manifest, args.start_ms)
            print(f"Verified start saved to {args.manifest}.")
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
