"""
diff_tlopo_vs_potco.py
Compares TLOPO's reported named-enemy/boss data (imported by
import_artifacts.py, evidence_status="reported") against POTCO's actual
source (imported by parse_potco_source.py, evidence_status=
"potco_verified") to answer two things the user asked for:

  1. Which TLOPO-side named enemies/bosses have NO matching symbol in
     POTCO's AvatarTypes.py at all -- i.e. content TLOPO added that
     POTCO never had (matched on symbol name; a real rename would show
     up as a false "TLOPO-only" -- this is a first pass, not a proof).
  2. For symbols/base types that DO match, whether TLOPO's reported
     EnemyGlobals level ranges agree with POTCO's actual values -- a
     concrete way to judge how much of the "reported" TLOPO data is
     accurate versus a "sloppy fallback" (the user's framing).

This does NOT conclude a symbol is "definitely TLOPO-exclusive" -- it's
a first-pass name-based diff. A symbol absent from POTCO's boss-member
list could also mean: POTCO uses a different name for the same slot,
or my ast-based parser missed a definition pattern. Treat "TLOPO-only"
results as leads, not verified conclusions -- still no evidence_status
here is "verified" for TLOPO's own claims.

Results are persisted to table `tlopo_potco_boss_diff` (one row per
TLOPO reported boss symbol) so later work/queries don't need to re-run
this script -- idempotent, full table rewrite each run.

Usage:
    python diff_tlopo_vs_potco.py [--db PATH]
"""

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent / "client_metadata.db"

DIFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS tlopo_potco_boss_diff(
    symbol TEXT PRIMARY KEY,
    display_guess TEXT,
    tlopo_group_name TEXT,
    tlopo_base_type TEXT,
    tlopo_min_level INTEGER,
    tlopo_max_level INTEGER,
    match_status TEXT NOT NULL,      -- 'matched' or 'tlopo_only'
    potco_base_type_exists INTEGER,  -- NULL if match_status='matched'
    potco_min_level INTEGER,
    potco_max_level INTEGER,
    potco_enabled INTEGER,
    level_range_agreement TEXT,      -- 'agree' / 'disagree' / NULL (tlopo_only rows)
    note TEXT
);
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(DIFF_SCHEMA)
    cur = conn.cursor()

    potco_boss_symbols = {
        row[0] for row in cur.execute(
            "SELECT DISTINCT symbol FROM potco_avatar_type_members WHERE is_boss_group = 1"
        )
    }
    potco_enemy_globals = {
        row[0]: row[1:] for row in cur.execute(
            "SELECT symbol, min_level, max_level, monster_class, enabled FROM potco_enemy_globals"
        )
    }

    tlopo_rows = cur.execute(
        """SELECT symbol, display_guess, group_name, base_type, min_level, max_level, monster_class
           FROM named_enemy_candidates
           WHERE is_boss_group = 1
             AND artifact_id = 'tlopo_avatartypes_enemy_loot_v13_package'
           ORDER BY symbol"""
    ).fetchall()

    tlopo_only = []
    matched = []
    for symbol, display_guess, group_name, base_type, min_lvl, max_lvl, monster_class in tlopo_rows:
        if symbol in potco_boss_symbols:
            matched.append((symbol, display_guess, group_name, base_type, min_lvl, max_lvl))
        else:
            tlopo_only.append((symbol, display_guess, group_name, base_type, min_lvl, max_lvl))

    print(f"TLOPO reported boss-group symbols: {len(tlopo_rows)}")
    print(f"POTCO actual boss-group symbols:   {len(potco_boss_symbols)}")
    print(f"Matched by symbol name:            {len(matched)}")
    print(f"TLOPO-only (no POTCO symbol match): {len(tlopo_only)}")
    print()

    cur.execute("DELETE FROM tlopo_potco_boss_diff")

    print("=== TLOPO-only named bosses (candidates for TLOPO-added content) ===")
    for symbol, display_guess, group_name, base_type, min_lvl, max_lvl in tlopo_only:
        p_min = p_max = p_enabled = None
        base_exists = 0
        note = f"POTCO base type '{base_type}' not found at all" if base_type else "TLOPO row has no base_type"
        if base_type and base_type in potco_enemy_globals:
            p_min, p_max, p_class, p_enabled = potco_enemy_globals[base_type]
            base_exists = 1
            note = f"POTCO base type '{base_type}' EXISTS, level {p_min}-{p_max}, enabled={p_enabled} -- but no boss identity defined for it in POTCO"
        print(f"  {symbol} ({display_guess}) -- TLOPO group={group_name}, base_type={base_type}, level {min_lvl}-{max_lvl}  [{note}]")
        cur.execute(
            """INSERT INTO tlopo_potco_boss_diff
               (symbol, display_guess, tlopo_group_name, tlopo_base_type, tlopo_min_level, tlopo_max_level,
                match_status, potco_base_type_exists, potco_min_level, potco_max_level, potco_enabled,
                level_range_agreement, note)
               VALUES (?, ?, ?, ?, ?, ?, 'tlopo_only', ?, ?, ?, ?, NULL, ?)""",
            (symbol, display_guess, group_name, base_type, min_lvl, max_lvl, base_exists, p_min, p_max, p_enabled, note),
        )

    print()
    print("=== Matched bosses: TLOPO-reported vs POTCO-actual level range agreement ===")
    # POTCO sometimes gives the boss's OWN symbol a direct __baseAvatarStats
    # row (e.g. FrenchBossA/SpanishBossA), overriding its tier's base_type
    # row -- must check the boss symbol itself before falling back to
    # base_type, or a real POTCO override gets misread as the tier's
    # generic value (this was a bug in the first pass of this script).
    agree, disagree, unresolved = 0, 0, 0
    for symbol, display_guess, group_name, base_type, tlopo_min, tlopo_max in matched:
        if symbol in potco_enemy_globals:
            p_min, p_max, p_class, p_enabled = potco_enemy_globals[symbol]
            source_note = "boss's own POTCO row"
        else:
            p_min, p_max, p_class, p_enabled = potco_enemy_globals.get(base_type, (None, None, None, None))
            source_note = f"inherited from base_type={base_type}"
        if p_min is None and p_max is None:
            unresolved += 1
            agreement = "unresolved"
        elif (tlopo_min, tlopo_max) == (p_min, p_max):
            agree += 1
            agreement = "agree"
        else:
            disagree += 1
            agreement = "disagree"
        print(f"  [{agreement.upper()}] {symbol}: TLOPO reported {tlopo_min}-{tlopo_max} vs POTCO actual {p_min}-{p_max} ({source_note})")
        cur.execute(
            """INSERT INTO tlopo_potco_boss_diff
               (symbol, display_guess, tlopo_group_name, tlopo_base_type, tlopo_min_level, tlopo_max_level,
                match_status, potco_base_type_exists, potco_min_level, potco_max_level, potco_enabled,
                level_range_agreement, note)
               VALUES (?, ?, ?, ?, ?, ?, 'matched', 1, ?, ?, ?, ?, ?)""",
            (symbol, display_guess, group_name, base_type, tlopo_min, tlopo_max, p_min, p_max, p_enabled,
             agreement, source_note),
        )
    print()
    print(f"Level-range agreement on matched bosses: {agree}/{agree + disagree + unresolved} agree, "
          f"{disagree} disagree, {unresolved} unresolved (no POTCO stat row found for symbol or base_type)")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
