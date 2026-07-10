# Kraken's Ledger

Ingestion backend for TLOPO_Tracker's automated, opt-in research-observation submissions (`main-debug` branch). Not part of the desktop app itself -- this is a separate service you deploy and point the tracker's Settings panel at.

Separate service from `loot_wiki_backend/` (the older, simpler per-chest drop-rate submission API). Kraken's Ledger stores the richer observation stream `main-debug` produces -- kill/loot linkage, capture quality, confidence tiers, category groups -- using the same normalized schema `TLOPO_Tracker/exporter.py`'s `export_to_sqlite()` writes locally, so a submission payload is literally `enrichment.enrich_events(session)`'s own output.

## What it stores

One row per observation (kill or loot event), one row per item within a loot event. No player names, character names, account info, or anything else identifying -- only:

- A random per-install `anon_id` (a UUID, distinct from `loot_wiki_anon_id` -- kept separate so the two systems' anonymized IDs aren't trivially cross-linkable)
- Session metadata (session ID, session start time)
- Per observation: event type, timestamp, target/enemy name + canonical enemy ID, enemy color (manually-entered, see `TLOPO_Tracker/loot_parser.py`), location (same), kill number, capture quality, chest type, gold, kill<->loot linkage
- Per item: name, rarity, category, category group, OCR confidence, confidence tier

See `app.py`'s `ObservationIn`/`ItemIn`/`BatchIn` models for the exact schema and validation rules.

## Running locally

```
pip install -r requirements.txt
uvicorn app:app --reload
```

Then `POST http://127.0.0.1:8000/submit_batch` with a JSON body matching `BatchIn`, or check `GET http://127.0.0.1:8000/health`.

## Idempotent submission model

The client (`TLOPO_Tracker/kraken_ledger_client.py`) resends the session's ENTIRE current event list on every submission (piggybacked on the tracker's 60s autosave tick) rather than tracking a "what's already been sent" delta. `/submit_batch` does `INSERT OR REPLACE` keyed by `observation_id`, so this is always safe to call repeatedly with overlapping data -- and tolerant of a dropped connection or app restart, since the next tick just resends everything.

**Known limitation**: this isn't bandwidth-optimal for a very long session (resending hundreds of events every 60s). Fine for the dataset sizes seen so far; incremental/delta submission would be a reasonable future optimization once this is proven out at scale.

## Deploying

Plain FastAPI app with a SQLite file for storage -- no external database or other services required.

### Current live deployment (AWS EC2, same instance as the loot wiki)

- **Instance**: `i-09c870b34f96c9941` (same box as `loot_wiki_backend`), Elastic IP `100.56.135.187`.
- **Port**: `8100` (loot-wiki owns 8000). `TLOPO_Tracker/tlopo_tracker.py`'s default `research_db_endpoint` points at `http://100.56.135.187:8100`.
- **Security group**: `sg-0f32e1ec292fb32d3` -- port 8100 open to `0.0.0.0/0` (added 2026-07-10, same shape as the existing port-8000 rule; SSH stays restricted).
- **Service**: systemd unit `/etc/systemd/system/kraken-ledger.service`, `Restart=always`, enabled on boot. Code at `/home/ec2-user/kraken_ledger_backend/` on the instance, own venv (separate from loot-wiki's).

**Known limitation**: plain HTTP, not HTTPS -- same tradeoff as `loot_wiki_backend`, low stakes since the data is already anonymized before it leaves the client.

**To redeploy after a code change**: `scp app.py` (and `requirements.txt` if it changed) to `/home/ec2-user/kraken_ledger_backend/` on the instance, re-run `venv/bin/pip install -r requirements.txt` if dependencies changed, then `sudo systemctl restart kraken-ledger`.

## Endpoints

- `POST /submit_batch` -- upsert a session's full observation stream. Rate-limited to 20/minute per IP.
- `GET /health` -- basic liveness check.
- `GET /overview` -- site-wide totals (sessions, installs, kills, chests, items, unique enemies, chest link-status breakdown).
- `GET /enemies` -- one row per enemy with aggregate kill/chest/session/item counts.
- `GET /enemies/{enemy_id}` -- full breakdown for one enemy: capture-quality distribution, kill<->loot link-status distribution, chest-type breakdown, item rarity distribution, top items with average OCR confidence, confidence-tier breakdown, recent session IDs.
- `GET /sessions?limit=&offset=` -- paginated session list (newest first).
- `GET /sessions/{session_id}` -- full kill/loot timeline for one session, items nested per observation.
- `GET /report.csv?scope=observations|items&enemy_id=&session_id=&event_type=` -- streamed CSV export for either raw observations or item rows joined with their parent observation's context, optionally filtered.

## Viewer (`static/`)

Added 2026-07-10 -- Kraken's Ledger was write-only until this point (no way to see submitted data short of querying the SQLite file directly). Same self-contained-static-HTML-calling-fetch() pattern as `loot_wiki_backend/static/`, four pages sharing one stylesheet (`static/common.css`):

- `/` (`index.html`) -- dashboard: overview stat tiles, sortable/filterable enemy table, report-builder form (scope/enemy/event-type filters -> CSV download).
- `/enemy_page/{enemy_id}` (`enemy.html`) -- everything `GET /enemies/{enemy_id}` returns, plus per-enemy CSV export links and a jump-off list to that enemy's recent sessions.
- `/sessions_page` (`sessions.html`) -- paginated session browser.
- `/session_page/{session_id}` (`session.html`) -- full event timeline for one session, plus CSV export links scoped to it.

Static assets are served from `/assets/*` (mounted via `StaticFiles`), kept distinct from the page routes (`/enemy_page/...` etc.) and the JSON API (`/enemies`, `/sessions`, ...) so none of the three route families can collide.

## Privacy notes

- No authentication, no accounts, no player-identifying fields anywhere in the schema.
- `anon_id` exists solely so submissions from one install can be told apart from independent ones in aggregate -- a random UUID with no link to a name, account, or IP retained in the stored rows.
- Off by default in the tracker; a player has to explicitly check "Share research observation data" in Settings and the endpoint has to be non-blank for anything to be sent.
