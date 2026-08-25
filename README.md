# Marginalia — content ingestion pipeline

Standalone, **offline** Python pipeline that prepares audiobook and Gutenberg content ahead
of time. It is not a live service: run it manually or via cron on your own
machine/server. Neither Spring Boot nor Flutter does audio processing — by
the time users press play, files are already clean.

What it does, per LibriVox audiobook:

1. **Match** it to a `gutenberg_catalog` row — by the Gutenberg id LibriVox
   itself links (`url_text_source`), else fuzzy title+author (rapidfuzz).
   Below the confidence threshold it never guesses: skip + log
   `needs manual match`.
2. **Download** each chapter MP3 from archive.org (resumable).
3. **Trim** the spoken LibriVox boilerplate — intro ("This is a LibriVox
   recording… read by…") and outro ("End of chapter X…") — found by
   transcribing the track with faster-whisper and scanning for markers.
4. **Normalize** loudness (EBU R128: `loudnorm=I=-16:TP=-1.5:LRA=11`) in the
   same ffmpeg pass; output `libmp3lame -q:a 4`.
5. **Upload** the cleaned MP3 (local dir or S3/MinIO) and record
   `audiobooks` / `audio_tracks` rows plus an `audio_processing_log` audit
   row (cut points, durations, validation status).

Safety: never trims more than 90 s off the front; if the resulting clip
would be under 5 s the original is kept untouched — both cases are stored
with `validation='failed'` so you review a small subset instead of
listening to everything.

Keep the narrator's name in any UI ("Narrated by X (LibriVox)") and don't
imply LibriVox endorses the app — recordings are public domain, editing and
commercial use are allowed.

## Setup

### Option 1: Run with Docker (Recommended)

You don't need to manually install Python, ffmpeg, or start the service every time. Just run with Docker:

```bash
# Start Postgres, MinIO, and Ingestion worker from repo root
docker compose up -d

# Or build & start only the ingestion worker from marginalia-ingestion/
docker compose up -d --build
```

In Docker Desktop, the `marginalia-ingestion` container will automatically run the background worker, polling and processing any uploaded books or Gutenberg ingestion jobs.

### Option 2: Local Python Setup

Requirements: Python 3.10+, **ffmpeg + ffprobe on PATH**, network access to
librivox.org / archive.org, and the Marginalia PostgreSQL database.

```bash
cd marginalia-ingestion
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt

# One-time DB migration (same database the backend uses):
psql "$DATABASE_URL" -f migrations/001_audiobooks.sql

cp .env.example .env    # then edit DATABASE_URL + storage settings
```

## Run against a 20-book sample

## Prepare Gutenberg reader copies

Apply the database migrations in order, then prepare one or more catalog books:

```bash
psql "$DATABASE_URL" -f migrations/002_ingestion_jobs.sql
psql "$DATABASE_URL" -f migrations/003_gutenberg_reader.sql
python -m ingestion.ingest_gutenberg 1342 1661
```

## Durable ingestion worker

The worker claims queued Gutenberg and uploaded-document jobs with row locks.
Failures retry with exponential backoff up to three attempts, then remain in
`failed` for review. Every lifecycle transition is recorded in
`ingestion_job_events`.

```bash
python -m ingestion.worker
```

For an operator check, process at most one queued job:

```bash
python -m ingestion.worker --once
```

## Nightly Home discovery snapshot

Apply `migrations/005_gutenberg_trending.sql`, then run the publisher once:

```bash
python -m ingestion.sync_gutenberg_trending
```

It fetches Project Gutenberg's official daily book and author rankings outside
the request path (with Gutendex as a fallback), enriches the top authors with
Wikipedia portraits, and replaces the book and author snapshot tables in one
PostgreSQL transaction. If a fetch or validation fails, the previous snapshot
remains available.

For a long-running scheduler, refresh every night at 02:15 UTC:

```bash
python -m ingestion.sync_gutenberg_trending --daily-at 02:15
```

Alternatively, invoke the one-shot command from cron or a platform scheduler.
The backend endpoints `/me/catalog/trending` and `/me/catalog/top-authors`
only read this PostgreSQL snapshot; Flutter never calls Gutendex or Wikipedia
while Home is loading.

The original Gutenberg download is never overwritten. The cleaned EPUB and
cover are written under versioned `gutenberg/{id}/` storage keys. Cleaning only
occurs when both official Project Gutenberg start and end markers are found.

```bash
# See what would match, without downloading anything:
python -m ingestion.ingest_librivox --limit 20 --dry-run

# The real thing (English only):
python -m ingestion.ingest_librivox --limit 20 --languages en

# GPU box:
python -m ingestion.ingest_librivox --limit 20 --device cuda

# Re-run only the tracks previously flagged validation='failed':
python -m ingestion.ingest_librivox --manual-review-only
```

Flags: `--limit N` · `--offset N` · `--languages en,fr,ar` · `--dry-run` ·
`--device cpu|cuda` · `--manual-review-only`.

Re-running is safe: audiobooks/tracks upsert on natural keys, and each
`(librivox_id, track_number)` log row doubles as the checkpoint — a crashed
run continues where it stopped. Failed tracks are *not* retried on normal
runs (only via `--manual-review-only`), so a bad file can't wedge the
pipeline.

## Review queue

```sql
SELECT librivox_id, track_number, cut_start, cut_end, detail
FROM audio_processing_log
WHERE validation = 'failed'
ORDER BY created_at DESC;
```

## Tests

The parts most likely to break — the boilerplate-marker cut logic and the
fuzzy matcher — are unit-tested with synthetic data (no audio, no DB):

```bash
python -m pytest tests -q
```

## Notes / deviations from the original spec

- The spec's `book` table is Marginalia's **`gutenberg_catalog`** (the synced
  Gutenberg index); `book.has_audio` became `gutenberg_catalog.has_audio`
  (added by the migration).
- **Chapter opening sentences aren't stored in Marginalia's schema**, so the
  cut-confirmation input (`book_opening`) is plumbed through
  `audio_cleaner.trim_track` but the orchestrator passes `None`. Marker-based
  cuts + the audit log are the active safety net; if chapter openings are
  ever ingested, pass them in `ingest_librivox._process_book`.
- `pydub` was dropped: durations come from `ffprobe` (loading hour-long MP3s
  into memory just to measure them is wasteful), and cutting/encoding is
  plain `ffmpeg`. Nothing else needed it.

## Follow-up (not part of this pipeline)

The Flutter app currently streams raw LibriVox files via a live backend
lookup. Once this pipeline has populated `audiobooks`/`audio_tracks`, the
backend should serve those cleaned tracks (and prefer them over the live
lookup) — that's a small Spring Boot endpoint + a client switch, tracked
separately.
