# Deploying the Readsake ingestion worker

```
push to prod ─▶ GitHub Actions: test (pytest) ─▶ build image ─▶ ghcr.io/medktite/readsake-ingestion:<sha>
                                                                  │
                                    ssh medktite@ancora ◀─────────┘
                                    /opt/readsake/ingestion: docker compose pull && up -d
```

The worker runs next to the backend on the Ancora VPS, as its own compose
project (`readsake-ingestion`) in `/opt/readsake/ingestion`. It has no public
URL: it polls the `ingestion_jobs` table over the backend project's network
(`readsake_default`) and stores its output in Cloudflare R2. It reads the backend's
`/opt/readsake/.env` — no extra secrets file.

## One-time setup

Do the backend's `deploy/DEPLOY.md` first — the worker needs its db,
network and `.env` (including the R2 keys).

1. **GitHub secrets**: same four as the backend, in this repo's `production`
   environment (`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`).
   The same deploy key works for both repos.
2. **Schema**: this repo's migrations share the backend's database and run interleaved
   with the backend's — use the ordered list in the backend's `deploy/DEPLOY.md` (step 7).
3. **First deploy**: push to `prod` (or Actions → *Build & deploy ingestion* → Run
   workflow on `prod`).
4. **Home discovery**: the `discovery` service refreshes "Trending now" and "Top
   authors" nightly at 02:15 UTC but does not run on start. Publish the first
   snapshot by hand:
   ```bash
   ssh ancora 'cd /opt/readsake/ingestion && docker compose --env-file ../.env run --rm discovery python -m ingestion.sync_gutenberg_trending'
   ```

## Day to day

- Deploy = merge `main` into `prod` and push. Pushes to `main` and PRs run tests only.
- Logs: `ssh ancora 'docker logs -f readsake-ingestion-worker-1'`
- Discovery logs: `ssh ancora 'docker logs readsake-ingestion-discovery-1'`
- Roll back: `ssh ancora 'cd /opt/readsake/ingestion && INGESTION_TAG=<older sha> docker compose --env-file ../.env up -d'`
- Whisper model: `WHISPER_MODEL` in `/opt/readsake/.env` (default `base`). The first
  audiobook job downloads it into the `whisper_cache` volume.
