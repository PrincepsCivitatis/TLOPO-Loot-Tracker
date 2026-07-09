# TLOPO Loot Wiki Backend

Minimal backend for anonymized, opt-in loot-drop submissions from TLOPO_Tracker installs. Not part of the desktop app itself -- this is a separate service you deploy and point the tracker's Settings panel at.

## What it stores

One row per item in a submitted chest (a chest with no items, e.g. gold-only, still gets one row so it counts toward the "how many chests did we see" denominator). No player names, character names, account info, or anything else identifying -- only:

- A random per-install `anon_id` (a UUID, regenerated only if the user resets it -- never tied to a name or account)
- The target/boss name
- Chest type (pouch/chest/skull)
- Item name + rarity (if any)
- Gold amount
- Kills SINCE THE LAST container submitted for this target in this session (a delta, not a cumulative session total -- this is what makes drop-rate math correct across many different contributors' sessions with a single SUM(), no session-boundary bookkeeping needed) / skull-chest count at the time of the drop
- Whether that kill count came from the boss health-bar auto-detector or manual +1/+5/+10 clicks (`kill_tracking: "auto"|"manual"`) -- see "Data quality" below for why this matters

See `app.py`'s module docstring and `SubmissionIn` model for the exact schema and validation rules.

## Running locally

```
pip install -r requirements.txt
uvicorn app:app --reload
```

Then open `http://127.0.0.1:8000/` for the search/browse UI, `POST http://127.0.0.1:8000/submit` with a JSON body matching `SubmissionIn`, or check `GET http://127.0.0.1:8000/health`.

## Deploying

This is a plain FastAPI app with a SQLite file for storage -- no external database or other services required. It'll run anywhere that can run a Python process and expose a port: a small VPS, a free-tier host (Render, Railway, Fly.io, etc.), or your own machine if you're comfortable exposing a port.

Once deployed, put the base URL into the tracker's Settings panel (or `tlopo_tracker_settings.json`'s `loot_wiki_endpoint` key) so it knows where to send submissions. `TLOPO_Tracker/tlopo_tracker.py`'s default settings already prefill this with the live deployment below, so opting in is a single checkbox for anyone running an unmodified build.

### Current live deployment (AWS EC2, free tier)

- **Account**: `039914330303`, region `us-east-1`.
- **Instance**: `i-09c870b34f96c9941` (`t3.micro`, free-tier eligible), tagged `tlopo-loot-wiki-backend`.
- **Elastic IP**: `100.56.135.187` (stable -- won't change if the instance stops/restarts). The tracker's default `loot_wiki_endpoint` points at `http://100.56.135.187:8000`.
- **Security group**: `sg-0f32e1ec292fb32d3` -- port 8000 (the API) open to `0.0.0.0/0` since clients need to reach it; port 22 (SSH) restricted to whatever IP was used to set this up, not open broadly.
- **Service**: runs as a systemd unit (`/etc/systemd/system/loot-wiki.service`) under `ec2-user`, `Restart=always`, enabled on boot -- survives both crashes and instance reboots. Code lives at `/home/ec2-user/loot_wiki_backend/` on the instance, in its own venv.
- **SSH key**: `tlopo-loot-wiki` key pair, private half saved locally outside the repo (never commit an SSH private key).

**Known limitation**: this is plain HTTP, not HTTPS. The data itself is already anonymized/non-identifying, so the stakes of on-path interception are low, but it's still not encrypted in transit. Adding TLS (e.g. a Caddy or nginx reverse proxy with Let's Encrypt, or an AWS Application Load Balancer with an ACM certificate) is a reasonable follow-up rather than something this setup did from the start.

**To redeploy after a code change**: `scp` the updated `app.py`/`static/index.html` to `/home/ec2-user/loot_wiki_backend/` on the instance, then `sudo systemctl restart loot-wiki`. If `requirements.txt` changed, also re-run `venv/bin/pip install -r requirements.txt` first.

## Endpoints

- `GET /` -- the search/browse UI (`static/index.html`, a single self-contained page with no build step). Type an enemy name, pick from the autocomplete list, and see its container/rarity/item rates and loot table.
- `POST /submit` -- record one chest's contents. Rate-limited to 30/minute per IP.
- `GET /enemies` -- list every target with at least one submission, for the UI's search box.
- `GET /rates/{target}/containers` -- chance a kill yields each container type (pouch/chest/skull), plus total kills tracked for that target.
- `GET /rates/{target}/rarities` -- rarity distribution (Crude/Common/Rare/Famed/Legendary) across every container opened for that target.
- `GET /rates/{target}/items` -- per-item drop rate (any rarity) across every container opened for that target.
- `GET /loot_table/{target}` -- the possibility space: every distinct item ever observed for that target, no rate math -- "what CAN drop" rather than "how often."
- `GET /stats/{target}` -- older, simpler summary: chest counts and named (Famed/Legendary) item counts only. Kept alongside the `/rates` endpoints above rather than replaced.
- `GET /health` -- basic liveness check.

All `/rates`, `/loot_table`, and `/stats` endpoints are rate-limited to 60/minute per IP, and return 404 if nothing's been recorded for that target yet.

## Data quality: kills are the denominator

Every rate here divides by kills, so undercounted kills silently inflate every single rate -- a target farmed with no reliable kill tracking at all can look like it drops something on every single kill. `/rates/{target}/containers` guards against presenting this with false confidence:

- It reports a `kill_tracking` breakdown (how many containers were logged while kills were `auto`-detected vs `manual`-only).
- It sets a `warning` field if kill tracking was ever manual for this target, OR if any computed rate exceeds 100% -- TLOPO's kill model doesn't support a single kill reliably yielding more than one of the same container type, so a rate above 100% is a hard sign kills are being undercounted, not a real drop rate. The UI shows this as an orange banner rather than a clean-looking (but wrong) percentage.

Manual kill counting (the only option for non-boss enemies right now -- auto-detection is boss-only, see TLOPO_Tracker's issue #9) is inherently easy to under-report during fast farming, so treat `manual`-tagged data as a lower-confidence estimate until broader auto-detection exists.

## Privacy notes

- No authentication, no accounts, no player-identifying fields anywhere in the schema.
- `anon_id` exists solely so bursty/duplicate submissions from one install can be told apart from independent ones in aggregate -- it's a random UUID with no link to a name, account, or IP retained in the stored rows (the request's source IP is only used transiently for rate-limiting, never stored).
- Every aggregate endpoint (`/stats`, `/rates`, `/loot_table`) only ever returns counts/rates, never individual submission rows.
