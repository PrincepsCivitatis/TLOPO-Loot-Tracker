"""
enrichment.py
Read-time join/derivation layer for TLOPO Loot Tracker observations.

Session.raw_events (see session.py) is an append-only, immutable log of
what was actually observed -- nothing in it is ever rewritten after the
fact. Two kinds of extra field don't belong there, though:

  1. Fields that can only be known by looking ACROSS events (e.g. "did
     this kill ever get a loot event linked to it, or was it a Missed
     kill with no container" -- TLOPO has no guaranteed drop, so this is
     genuinely unknowable at write time).
  2. Fields that need an external data source this repo doesn't have --
     the separate reverse-engineered TLOPO client database (Boss Group,
     Enemy Base Type, expected drop rates, etc.).

Both are computed here, at read/export time, from a session's raw_events
-- never stored back onto the session itself. Exporters (exporter.py)
call enrich_events() once and iterate the result instead of
session.raw_events directly.
"""

from typing import Dict, List, Optional

from loot_parser import RARITY_ORDER, canonical_enemy_id
from stats import rate_with_ci

# Drop-in point for the recovered TLOPO client database. Empty today --
# every field below exports as blank until that database (or a JSON/CSV
# built from it) gets loaded into this dict, keyed by canonical enemy ID
# (loot_parser.canonical_enemy_id() of the display name -- e.g.
# "cicatriz", not "Cicatriz" -- so spelling/alias variants of the same
# enemy still join correctly; see loot_parser.KNOWN_BOSS_NAMES for the
# display-name pool these IDs are derived from). A value need only
# include the fields it actually has; anything missing just stays blank
# on export. No export code needs to change when this gets populated --
# that's the whole point of this module existing separately from
# exporter.py.
#
# Every entry MUST also carry the three CLIENT_DB_PROVENANCE_FIELDS
# below. This project's source docs (the loot-tracker experimental-
# branch spec and the TLOPO reverse-engineering bible) are explicit that
# reverse-engineered client data is not authoritative until independently
# verified, and that no export may silently present a reported/inferred
# value as fact. Concretely: evidence_status must be one of "verified"
# (reproduced from a retained artifact), "reported" (claimed in prior
# research but not independently reproduced here), "inferred"
# (structural guess, not a direct extraction), or "unresolved". Do not
# add an entry without evidence_status -- exporter.py surfaces it
# wherever a CLIENT_DB_FIELDS value is shown so a reader never mistakes
# "reported" for "verified".
CLIENT_DB_LOOKUP: Dict[str, dict] = {}

CLIENT_DB_FIELDS = [
    "boss_group", "enemy_base_type", "expected_rare_chest_pct",
    "enemy_family", "known_level_range", "known_boss_status",
]

CLIENT_DB_PROVENANCE_FIELDS = ["evidence_status", "evidence_source", "evidence_notes"]

# Max seconds apart a kill event and a loot event can be to link. Real
# session data (TLOPO_Session_2026-07-09_23-13) showed genuine matches
# 6-8s apart (HEALTH_DEFEATED_CONFIRM_SECONDS debounce on the kill side,
# plus normal poll/OCR lag on the loot side) versus 39s+ for cases the
# old "just link to whichever kill was seen last" logic wrongly matched
# -- 12s leaves comfortable margin above the observed real matches while
# staying far below the observed bad ones.
MATCH_WINDOW_SECONDS = 12.0


def _parse_hms_seconds(timestamp: str) -> Optional[float]:
    """"HH:MM:SS" -> seconds since midnight, or None if unparseable."""
    try:
        h, m, s = timestamp.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def enrich_events(session) -> List[dict]:
    """
    Returns a NEW list of event dicts (shallow copies of
    session.raw_events, each with extra derived keys merged in) -- never
    mutates session.raw_events itself.

    Adds to every event:
      - the 6 CLIENT_DB_FIELDS plus the 3 CLIENT_DB_PROVENANCE_FIELDS,
        from CLIENT_DB_LOOKUP.get(canonical_enemy_id(target), {}) (all
        None today -- see the module docstring).

    Adds to "chest" events:
      - "associated_kill_number": alias of the event's own kill_number,
        for exporters that want the reader-friendlier name.
      - "linked_kill_observation_id": the observation_id of the NEAREST
        (by timestamp, either direction) "kill" event for the same
        target that is (a) within MATCH_WINDOW_SECONDS and (b) not
        already linked to a different loot event -- None if no such
        kill exists, rather than force-matching a distant one. Once a
        kill is linked, it's never reused for another loot event (a
        greedy nearest-first assignment: loot events are matched in
        chronological order, each claiming the closest still-available
        kill within the window). Best-effort only -- kill detection
        (health-bar) and loot detection (popup OCR) are independent
        detectors in detector.py, so even a correct match by proximity
        isn't a guaranteed causal link, just the most plausible one.
      - "link_status": "linked" (a unique nearest kill was found),
        "unlinked" (no candidate kill within MATCH_WINDOW_SECONDS), or
        "ambiguous" (two or more unused candidate kills are exactly
        tied for nearest -- linked_kill_observation_id still picks one,
        chronologically-first, but callers that care about linkage
        confidence should treat an "ambiguous" link as lower-trust than
        "linked"). There is no "manual" status yet -- that needs a GUI
        relinking workflow this repo doesn't have.

    Adds to "kill" events:
      - "capture_quality": "Has Loot" if some loot event linked back to
        this kill (via linked_kill_observation_id above), else "Missed"
        -- i.e. this kill produced no logged container at all, which is
        real, expected TLOPO behavior (no guaranteed drop), not an OCR
        problem.
    """
    events = [dict(e) for e in session.raw_events]

    for event in events:
        enemy_id = canonical_enemy_id(event.get("target") or "")
        event["enemy_id"] = enemy_id
        client_fields = CLIENT_DB_LOOKUP.get(enemy_id, {})
        for field_name in CLIENT_DB_FIELDS + CLIENT_DB_PROVENANCE_FIELDS:
            event[field_name] = client_fields.get(field_name)

    # kills_by_target: target -> list of (index into `events`, timestamp
    # in seconds, used: bool), in stream order. A list (not a dict keyed
    # by observation_id) since we need to scan all of a target's kills
    # to find the nearest one for each loot event.
    kills_by_target: Dict[str, List[list]] = {}
    for i, event in enumerate(events):
        if event.get("event_type") == "kill":
            target = event.get("target")
            kills_by_target.setdefault(target, []).append(
                [i, _parse_hms_seconds(event.get("timestamp")), False]
            )

    linked_kill_ids = set()

    for event in events:
        if event.get("event_type") != "chest":
            continue
        event["associated_kill_number"] = event.get("kill_number")
        event["linked_kill_observation_id"] = None
        event["link_status"] = "unlinked"

        loot_ts = _parse_hms_seconds(event.get("timestamp"))
        if loot_ts is None:
            continue
        candidates = kills_by_target.get(event.get("target"), [])
        best = None  # (abs_time_diff, candidate_record)
        tie_count = 0
        for record in candidates:
            idx, kill_ts, used = record
            if used or kill_ts is None:
                continue
            diff = abs(kill_ts - loot_ts)
            if diff > MATCH_WINDOW_SECONDS:
                continue
            if best is None or diff < best[0]:
                best = (diff, record)
                tie_count = 1
            elif diff == best[0]:
                tie_count += 1

        if best is not None:
            record = best[1]
            record[2] = True  # mark this kill used -- never reused for another loot event
            kill_observation_id = events[record[0]].get("observation_id")
            event["linked_kill_observation_id"] = kill_observation_id
            event["link_status"] = "ambiguous" if tie_count > 1 else "linked"
            linked_kill_ids.add(kill_observation_id)

    for event in events:
        if event.get("event_type") == "kill":
            oid = event.get("observation_id")
            event["capture_quality"] = "Has Loot" if oid in linked_kill_ids else "Missed"

    return events


def compute_session_statistics(session) -> dict:
    """
    Session-wide rate statistics with Wilson-interval confidence bounds
    (see stats.py) -- the "Observed Statistics" block the collaborator
    asked for. Splits the three distinct probabilities they called out:
    P(any container | kill), P(container type | container), and
    P(rarity tier | kill), rather than collapsing them into one number.

    Returns a dict of independent metric blocks (each is a rate_with_ci()
    dict, or a plain count/percent for the container-type distribution)
    -- never raises on a zero-kill session; every rate degrades to the
    None/no-CI case from stats.rate_with_ci in that case, not a ZeroDivisionError.
    """
    total = session.session_totals()
    kills = total.kills
    containers = total.pouches + total.chests + total.skull_chests

    container_dist = {}
    for label, count in [("pouch", total.pouches), ("chest", total.chests), ("skull", total.skull_chests)]:
        container_dist[label] = {
            "count": count,
            "pct_of_containers": (count / containers * 100.0) if containers else None,
        }

    rarity_rates = {
        rarity: rate_with_ci(total.rarity_counts.get(rarity, 0), kills)
        for rarity in RARITY_ORDER
    }

    return {
        "kills": kills,
        "containers": containers,
        "container_rate": rate_with_ci(containers, kills),
        "container_distribution": container_dist,
        "skull_chest_rate": rate_with_ci(total.skull_chests, kills),
        "rarity_rates": rarity_rates,
    }
