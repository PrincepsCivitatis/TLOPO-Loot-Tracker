"""
verify_enrichment_slice.py
Ad-hoc, no-pytest verification script for the CLIENT_DB_LOOKUP
provenance/evidence fields and link_status added to enrichment.py and
exporter.py. Not part of the app's runtime; a standalone check, same
"tools/" convention as color_sampler.py.

Builds a synthetic Cicatriz session (no OCR/screen capture involved) --
the vertical-slice test case named in TLOPO_Loot_Tracker_Experimental_
Branch_Spec.md section 16 -- covering:
  - a normal kill -> loot pair within the match window ("linked")
  - a kill with no loot at all ("Missed" capture quality)
  - a loot event with no candidate kill in range ("unlinked")
  - two kills equidistant from one loot event ("ambiguous")
and one CLIENT_DB_LOOKUP entry (evidence_status="reported", for this
script's run only -- not left in the module) to confirm evidence fields
flow through enrich_events() and both exporters without error.

Run directly: `python tools/verify_enrichment_slice.py` from the repo
root (needs the project venv active, same as the app itself).
"""

import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loot_parser import ChestResult, LootItem
from session import Session
import enrichment
from enrichment import enrich_events, CLIENT_DB_LOOKUP
import exporter


def _kill(session, hms, target="Cicatriz"):
    session.set_target(target)
    session.raw_events.append({
        "observation_id": session._next_observation_id(),
        "event_type": "kill",
        "timestamp": hms,
        "target": target,
        "enemy_color": None,
        "location": None,
        "kill_number": session.targets[target].kills + 1,
    })
    session.targets[target].kills += 1


def _loot(session, hms, target="Cicatriz", chest_type="Chest!"):
    result = ChestResult(
        chest_type=chest_type,
        items=[LootItem(name="Test Sword", rarity="Rare", name_confidence=99.0)],
        gold=10, timestamp=hms, target=target,
        kill_number=session.targets[target].kills,
    )
    session.log_chest(result)


def build_session() -> Session:
    s = Session()

    # Case 1: clean linked pair, 5s apart (inside MATCH_WINDOW_SECONDS).
    _kill(s, "12:00:00")
    _loot(s, "12:00:05")

    # Case 2: a kill with no loot at all -- stays "Missed".
    _kill(s, "12:01:00")

    # Case 3: a loot event with no kill anywhere near it -- "unlinked".
    _loot(s, "12:05:00")

    # Case 4: two kills exactly equidistant (10s) from one loot event --
    # "ambiguous". Both must be unused going into this loot event.
    _kill(s, "12:10:00")
    _kill(s, "12:10:20")
    _loot(s, "12:10:10")

    return s


def main():
    # Evidence-tagged entry for this run only -- CLIENT_DB_LOOKUP starts
    # and (after this script exits) stays empty in the real module; no
    # unverified RE-bible data gets left behind.
    CLIENT_DB_LOOKUP["cicatriz"] = {
        "boss_group": "CaptBrineyBosses",
        "enemy_base_type": "CaptBriney",
        "evidence_status": "reported",
        "evidence_source": "TLOPO_Reverse_Engineering_Bible.md section 6.3",
        "evidence_notes": "Unverified prior-conversation claim; not independently reproduced.",
    }

    session = build_session()
    enriched = enrich_events(session)

    chests = [e for e in enriched if e["event_type"] == "chest"]
    kills = [e for e in enriched if e["event_type"] == "kill"]

    assert len(chests) == 3, f"expected 3 loot events, got {len(chests)}"
    assert chests[0]["link_status"] == "linked", chests[0]
    assert chests[1]["link_status"] == "unlinked", chests[1]
    assert chests[2]["link_status"] == "ambiguous", chests[2]
    print("link_status: linked / unlinked / ambiguous all correct")

    assert kills[1]["capture_quality"] == "Missed", kills[1]
    print("capture_quality on the loot-less kill: Missed (correct)")

    for e in enriched:
        if e.get("target") == "Cicatriz":
            assert e["evidence_status"] == "reported", e
            assert e["boss_group"] == "CaptBrineyBosses", e
    print("evidence_status/boss_group flow through enrich_events() correctly")

    # exporter.py round-trip -- both the text export's evidence-status
    # annotation and the SQLite schema's new columns.
    with tempfile.TemporaryDirectory() as tmp:
        txt_path = exporter.export_to_text(session, folder=tmp)
        with open(txt_path, encoding="utf-8") as f:
            txt = f.read()
        assert "(reported)" in txt, "expected an evidence-status-annotated line in the text export"
        assert "Link Status: linked" in txt
        assert "Link Status: unlinked" in txt
        assert "Link Status: ambiguous" in txt
        print("text export: evidence-status annotation and Link Status lines present")

        db_path = exporter.export_to_sqlite(session, folder=tmp)
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version")
            assert cur.fetchone()[0] == 3, "expected schema v3"
            cur.execute("SELECT evidence_status, evidence_source FROM enemies WHERE enemy_id='cicatriz'")
            row = cur.fetchone()
            assert row == ("reported", "TLOPO_Reverse_Engineering_Bible.md section 6.3"), row
            cur.execute("SELECT link_status FROM loot_events ORDER BY observation_id")
            statuses = [r[0] for r in cur.fetchall()]
            assert statuses == ["linked", "unlinked", "ambiguous"], statuses
        finally:
            conn.close()
        print("sqlite export: schema v3, evidence columns, and link_status column all correct")

    # Confirm the empty-lookup case (the common case today) still renders
    # clean -- no evidence-status noise when there's nothing to annotate.
    CLIENT_DB_LOOKUP.clear()
    plain_session = Session()
    _kill(plain_session, "09:00:00")
    _loot(plain_session, "09:00:03")
    with tempfile.TemporaryDirectory() as tmp:
        txt_path = exporter.export_to_text(plain_session, folder=tmp)
        with open(txt_path, encoding="utf-8") as f:
            txt = f.read()
        assert "(reported)" not in txt and "evidence status unknown" not in txt
        assert "Link Status: linked" in txt
    print("empty CLIENT_DB_LOOKUP case: no evidence-status noise, output unchanged")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
