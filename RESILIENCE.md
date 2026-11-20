# Gutenberg availability

Reader packages, extracted covers and original EPUB sources are stored in S3.
Existing reader packages stay available during Gutenberg outages. New imports
retry with exponential backoff and jitter (six attempts); the API applies a
15-minute cooldown before failed imports can be queued again. Source failures
are logged and persisted in ingestion_job_events. Alert on SOURCE_UNAVAILABLE
errors and growing pending/failed queues using your deployment log monitor.

Prepare popular titles before readers request them (run one scheduler instance):

```sh
docker compose run --rm ingestion python -m ingestion.worker --prepare-trending 24 --once
```

The regular worker processes the queued jobs. Run this after catalog sync; it
skips already prepared titles and active/recently failed jobs. Gutenberg must
recover before new source files can be acquired. Do not aggressively retry 403s.

Deploy the backend and ingestion worker together, then restart the Flutter app.
No schema migration is required. Rebuild the ingestion container to pick up
Python changes. Do not delete existing storage objects or recreate volumes.

The search API preserves local results and returns partialResults/message when
search providers fail or recent ingestion jobs report a source outage. It does
not probe Gutenberg on every query. Reader requests return 409 while preparing
and 503 with Retry-After during a known outage. Genuine missing books remain 404.

Stored covers use the public /covers/gutenberg/{id} endpoint; only a key from the
catalog can be read. Reader packages and raw EPUB sources stay private.

The outage signal reflects recent worker observations (15 minutes), not a global
Gutenberg status service. The first failed import establishes it. Configure
monitoring/alerts in the deployment platform; no external alert integration is
installed by these changes.
