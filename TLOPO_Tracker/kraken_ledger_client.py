"""
kraken_ledger_client.py
Optional, opt-in submission of a session's full research-observation
stream to Kraken's Ledger (see kraken_ledger_backend/ at the repo root
for the server side). Entirely inert unless the user turns it on in
Settings and sets an endpoint -- same privacy-first, off-by-default
pattern as loot_wiki_client.py's community drop-rate sharing.

Separate feature from loot_wiki_client.py: different opt-in setting,
different anon_id (research_db_anon_id, not loot_wiki_anon_id -- kept
distinct so the two systems' anonymized IDs aren't trivially
cross-linkable), different payload shape (the richer per-session
observation stream from enrichment.enrich_events(), not one submission
per chest).

Uses only the standard library (urllib), same as loot_wiki_client.py --
no new dependency for one POST request. The network call always runs on
its own background thread and never raises back into the caller -- a
network hiccup, a misconfigured endpoint, or the backend being offline
must never crash the tracker or block the GUI.
"""

import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Callable, List, Optional

SUBMIT_TIMEOUT_SECONDS = 8  # a full session's event list can be larger than one loot_wiki chest submission


def ensure_research_db_anon_id(settings: dict) -> str:
    """
    Returns the per-install anonymous ID for Kraken's Ledger, generating
    and saving one into `settings` the first time this is called. Never
    tied to a name, character, or account -- just a random UUID, and
    deliberately a DIFFERENT UUID than loot_wiki_anon_id (see module
    docstring). Caller is responsible for persisting `settings`
    afterward (e.g. via the app's own _save_settings) if a new ID was
    just generated.
    """
    anon_id = settings.get("research_db_anon_id")
    if not anon_id:
        anon_id = str(uuid.uuid4())
        settings["research_db_anon_id"] = anon_id
    return anon_id


def submit_batch_async(
    endpoint: str,
    anon_id: str,
    session_id: str,
    session_start: float,
    events: List[dict],
    on_error: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Fires off the session's CURRENT FULL enriched event list (see
    enrichment.enrich_events) to `endpoint`'s /submit_batch on a
    background thread. Silently does nothing if `endpoint` is empty
    (opt-in is on but no endpoint has been configured yet) or `events`
    is empty (nothing to send yet).

    Deliberately resends the whole list every call rather than tracking
    a "what's already been sent" delta -- the server upserts by
    observation_id, so this is safe to call repeatedly with overlapping
    data (idempotent), and tolerant of a dropped connection or app
    restart (the next call just resends everything). Not bandwidth-
    optimal for a very long session, but simple and robust; see
    kraken_ledger_backend/README.md's "Known limitations".
    """
    if not endpoint or not events:
        return

    payload = {
        "anon_id": anon_id,
        "session_id": session_id,
        "session_start": session_start,
        "events": events,
    }

    def _send():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                endpoint.rstrip("/") + "/submit_batch",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT_SECONDS) as resp:
                print(f"[TLOPO kraken-ledger] submitted {len(events)} event(s) for session "
                      f"{session_id!r} -> HTTP {resp.status}", flush=True)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            print(f"[TLOPO kraken-ledger] submit FAILED for session {session_id!r}: "
                  f"HTTP {e.code} {body}", flush=True)
            if on_error:
                try:
                    on_error(f"HTTP {e.code}: {body}")
                except Exception:
                    pass
        except Exception as e:
            print(f"[TLOPO kraken-ledger] submit FAILED for session {session_id!r}: {e}", flush=True)
            if on_error:
                try:
                    on_error(str(e))
                except Exception:
                    pass

    threading.Thread(target=_send, daemon=True).start()
