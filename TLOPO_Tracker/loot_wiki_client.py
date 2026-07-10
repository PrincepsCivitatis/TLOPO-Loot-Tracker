"""
loot_wiki_client.py
Optional, opt-in submission of anonymized loot data to a community
drop-rate backend (see loot_wiki_backend/ at the repo root for the
server side). Entirely inert unless the user turns it on in Settings
and sets an endpoint.

Ported verbatim from main/experimental-alpha's own loot_wiki_client.py
(same format the loot-wiki backend already expects) so main-debug can
submit to both the loot wiki AND Kraken's Ledger (see
kraken_ledger_client.py) -- two separate opt-ins, two separate
datasets (per-chest simplified stats here vs. this branch's richer
per-kill/per-loot research stream there), never mixed into one
payload.

Uses only the standard library (urllib) rather than adding a new
dependency for one POST request. The actual network call always runs
on its own background thread and never raises back into the caller --
a network hiccup, a misconfigured endpoint, or the backend being
offline must never crash the tracker or block the GUI, since this is a
best-effort background feature layered on top of local tracking that
already works fine without it.
"""

import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Callable, List, Optional

SUBMIT_TIMEOUT_SECONDS = 5


def ensure_anon_id(settings: dict) -> str:
    """
    Returns the per-install anonymous ID, generating and saving one into
    `settings` the first time this is called. Never tied to a name,
    character, or account -- just a random UUID so the backend can tell
    repeated submissions from the same install apart from independent
    ones in aggregate. Caller is responsible for persisting `settings`
    afterward (e.g. via the app's own _save_settings) if a new ID was
    just generated.
    """
    anon_id = settings.get("loot_wiki_anon_id")
    if not anon_id:
        anon_id = str(uuid.uuid4())
        settings["loot_wiki_anon_id"] = anon_id
    return anon_id


def submit_chest_async(
    endpoint: str,
    anon_id: str,
    target: str,
    chest_type: str,
    items: List[dict],
    gold: int,
    kills_since_last_container: Optional[int],
    skull_chest_number: Optional[int],
    kill_tracking: str,
    on_error: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Fires off one chest's worth of anonymized loot data to `endpoint`
    (a loot_wiki_backend base URL) on a background thread. `items` is a
    list of {"name": str, "rarity": str|None} dicts. Silently does
    nothing if `endpoint` is empty (opt-in is on but no endpoint has
    been configured yet).

    `kills_since_last_container` is a DELTA -- kills on this target
    since the last container submitted for it in this session -- not a
    cumulative session total. This is what makes drop-rate aggregation
    across many different contributors' sessions correct: summing
    deltas from every submission ever received for a target gives the
    true total kills witnessed for it, with no session-boundary
    bookkeeping needed server-side. A cumulative per-session count
    would NOT sum safely across contributors (or even across one
    contributor's own session resets).

    `kill_tracking` is "auto" or "manual" -- whether this target's kill
    count is coming from the reliable boss health-bar auto-detector, or
    relies on the player remembering to click +1/+5/+10 themselves. This
    matters a lot for data quality: kills are the denominator every
    drop-rate calculation divides by, and undercounted manual clicks
    (easy to forget mid-farm) silently inflate every rate -- a target
    farmed with no kill tracking at all would make its containers look
    like they drop on ~100% of kills. Tagging provenance lets the
    backend/UI flag or discount manual-only data instead of presenting
    it with the same confidence as auto-tracked data.
    """
    if not endpoint:
        return

    payload = {
        "anon_id": anon_id,
        "target": target,
        "chest_type": chest_type,
        "items": items,
        "gold": gold,
        "kills_since_last_container": kills_since_last_container,
        "skull_chest_number": skull_chest_number,
        "kill_tracking": kill_tracking,
    }

    def _send():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                endpoint.rstrip("/") + "/submit",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT_SECONDS)
        except Exception as e:
            if on_error:
                try:
                    on_error(str(e))
                except Exception:
                    pass

    threading.Thread(target=_send, daemon=True).start()
