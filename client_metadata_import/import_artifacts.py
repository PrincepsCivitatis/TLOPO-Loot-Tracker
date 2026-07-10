"""
import_artifacts.py
Phase A artifact intake for the TLOPO loot-intelligence-platform side
project (see TLOPO_Loot_Tracker_Experimental_Branch_Spec.md /
TLOPO_Reverse_Engineering_Bible.md). Standalone tool -- not part of the
TLOPO_Tracker app's runtime, not imported by it.

Every prior reverse-engineering extraction package (`tlopo_*.zip` under
--source-dir, default the user's Downloads folder) gets hashed and
recorded in `client_artifacts`, whether or not it's deeply parsed. The
two richest enemy/boss-relevant packages (v13 AvatarTypes/EnemyGlobals,
v4 concrete lists) additionally get their CSVs loaded into normalized
tables below, one row per symbol, every row carrying:
  - artifact_id: which package this came from (FK to client_artifacts)
  - evidence_status: always "reported" here -- this is a re-import of a
    PRIOR conversation's summary CSV, not an independent re-extraction
    from the raw TLOPO phase files/executable. Per this project's
    evidence-tier rule, "reported" must never be presented as "verified"
    downstream.
  - source_confidence_note: the package's OWN free-text confidence/why
    field, preserved verbatim and kept separate from evidence_status so
    a source author's self-assessed confidence ("medium-high") is never
    conflated with this project's independent evidence tier.

Idempotent: re-running fully replaces a given artifact_id's rows rather
than accumulating duplicates, so it's always safe to re-run after a new
package appears in the source directory.

Usage:
    python import_artifacts.py [--source-dir PATH] [--db PATH]
"""

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_SOURCE_DIR = Path.home() / "Downloads"
DEFAULT_DB_PATH = Path(__file__).parent / "client_metadata.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS client_artifacts(
    artifact_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    imported_at REAL NOT NULL,
    readme_text TEXT,
    parsed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS avatar_type_groups(
    artifact_id TEXT NOT NULL REFERENCES client_artifacts(artifact_id),
    section_const_index INTEGER,
    range_const_index INTEGER,
    group_name TEXT,
    base_type TEXT,
    attrs TEXT,
    count INTEGER,
    start_id TEXT,
    members_json TEXT,
    evidence_status TEXT NOT NULL,
    source_confidence_note TEXT
);

CREATE TABLE IF NOT EXISTS avatar_type_members(
    artifact_id TEXT NOT NULL REFERENCES client_artifacts(artifact_id),
    symbol TEXT,
    display_guess TEXT,
    group_name TEXT,
    base_type TEXT,
    attrs TEXT,
    member_index_in_group INTEGER,
    start_id TEXT,
    derived_id_guess TEXT,
    is_boss_group INTEGER,
    section_const_index INTEGER,
    evidence_status TEXT NOT NULL,
    source_confidence_note TEXT
);

CREATE TABLE IF NOT EXISTS enemy_globals(
    artifact_id TEXT NOT NULL REFERENCES client_artifacts(artifact_id),
    symbol TEXT,
    display_guess TEXT,
    min_level INTEGER,
    max_level INTEGER,
    avg_level REAL,
    scale REAL,
    height REAL,
    battle_radius REAL,
    monster_class TEXT,
    enabled INTEGER,
    enemyglobals_const_index INTEGER,
    evidence_status TEXT NOT NULL,
    source_confidence_note TEXT
);

CREATE TABLE IF NOT EXISTS named_enemy_candidates(
    artifact_id TEXT NOT NULL REFERENCES client_artifacts(artifact_id),
    symbol TEXT,
    display_guess TEXT,
    group_name TEXT,
    base_type TEXT,
    attrs TEXT,
    member_index_in_group INTEGER,
    start_id TEXT,
    derived_id_guess TEXT,
    is_boss_group INTEGER,
    section_const_index INTEGER,
    named_enemy_candidate INTEGER,
    min_level INTEGER,
    max_level INTEGER,
    avg_level REAL,
    monster_class TEXT,
    enabled INTEGER,
    level_source TEXT,
    evidence_status TEXT NOT NULL,
    source_confidence_note TEXT
);

CREATE TABLE IF NOT EXISTS boss_candidates_v4(
    artifact_id TEXT NOT NULL REFERENCES client_artifacts(artifact_id),
    symbol TEXT,
    display_guess TEXT,
    category TEXT,
    source_module TEXT,
    nearby_numbers_json TEXT,
    evidence_status TEXT NOT NULL,
    source_confidence_note TEXT
);

CREATE TABLE IF NOT EXISTS enemy_candidates_v4(
    artifact_id TEXT NOT NULL REFERENCES client_artifacts(artifact_id),
    symbol TEXT,
    display_guess TEXT,
    category TEXT,
    source_module TEXT,
    nearby_numbers_json TEXT,
    evidence_status TEXT NOT NULL,
    source_confidence_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_atg_artifact ON avatar_type_groups(artifact_id);
CREATE INDEX IF NOT EXISTS idx_atm_artifact ON avatar_type_members(artifact_id);
CREATE INDEX IF NOT EXISTS idx_atm_symbol ON avatar_type_members(symbol);
CREATE INDEX IF NOT EXISTS idx_eg_artifact ON enemy_globals(artifact_id);
CREATE INDEX IF NOT EXISTS idx_eg_symbol ON enemy_globals(symbol);
CREATE INDEX IF NOT EXISTS idx_nec_artifact ON named_enemy_candidates(artifact_id);
CREATE INDEX IF NOT EXISTS idx_nec_symbol ON named_enemy_candidates(symbol);
CREATE INDEX IF NOT EXISTS idx_bc4_artifact ON boss_candidates_v4(artifact_id);
CREATE INDEX IF NOT EXISTS idx_ec4_artifact ON enemy_candidates_v4(artifact_id);
"""

EVIDENCE_STATUS = "reported"  # see module docstring -- never "verified" here


def _slugify(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_readme_text(extract_dir: Path) -> str:
    for name in ("README.md", "README.txt", "summary.json"):
        candidate = extract_dir / name
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")
    return ""


def _int_or_none(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _float_or_none(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _bool_to_int(v):
    return 1 if str(v).strip().lower() in ("true", "1", "yes") else 0


def _read_csv_rows(path: Path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def parse_v13_package(conn: sqlite3.Connection, artifact_id: str, extract_dir: Path) -> bool:
    """
    tlopo_avatartypes_enemy_loot_v13_package.zip -- AvatarTypes groups/
    members, EnemyGlobals base stats, and the joined named-enemy view.
    Returns True if the expected files were found and parsed.
    """
    groups_path = extract_dir / "avatartypes_groups_v13.csv"
    members_path = extract_dir / "avatartypes_members_v13.csv"
    enemyglobals_path = extract_dir / "enemyglobals_base_stats_v13.csv"
    named_path = extract_dir / "named_enemy_rows_v13.csv"
    if not all(p.exists() for p in (groups_path, members_path, enemyglobals_path, named_path)):
        return False

    cur = conn.cursor()
    for table in ("avatar_type_groups", "avatar_type_members", "enemy_globals", "named_enemy_candidates"):
        cur.execute(f"DELETE FROM {table} WHERE artifact_id = ?", (artifact_id,))

    for row in _read_csv_rows(groups_path):
        cur.execute(
            """INSERT INTO avatar_type_groups
               (artifact_id, section_const_index, range_const_index, group_name, base_type,
                attrs, count, start_id, members_json, evidence_status, source_confidence_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id, _int_or_none(row.get("section_const_index")),
                _int_or_none(row.get("range_const_index")), row.get("group"), row.get("base_type"),
                row.get("attrs"), _int_or_none(row.get("count")), row.get("start_id"),
                row.get("members"), EVIDENCE_STATUS, None,
            ),
        )

    for row in _read_csv_rows(members_path):
        cur.execute(
            """INSERT INTO avatar_type_members
               (artifact_id, symbol, display_guess, group_name, base_type, attrs,
                member_index_in_group, start_id, derived_id_guess, is_boss_group,
                section_const_index, evidence_status, source_confidence_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id, row.get("symbol"), row.get("display_guess"), row.get("group"),
                row.get("base_type"), row.get("attrs"), _int_or_none(row.get("member_index_in_group")),
                row.get("start_id"), row.get("derived_id_guess"), _bool_to_int(row.get("is_boss_group")),
                _int_or_none(row.get("section_const_index")), EVIDENCE_STATUS, row.get("confidence"),
            ),
        )

    for row in _read_csv_rows(enemyglobals_path):
        cur.execute(
            """INSERT INTO enemy_globals
               (artifact_id, symbol, display_guess, min_level, max_level, avg_level, scale,
                height, battle_radius, monster_class, enabled, enemyglobals_const_index,
                evidence_status, source_confidence_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id, row.get("symbol"), row.get("display_guess"), _int_or_none(row.get("min_level")),
                _int_or_none(row.get("max_level")), _float_or_none(row.get("avg_level")),
                _float_or_none(row.get("scale")), _float_or_none(row.get("height")),
                _float_or_none(row.get("battle_radius")), row.get("monster_class"),
                _int_or_none(row.get("enabled")), _int_or_none(row.get("enemyglobals_const_index")),
                EVIDENCE_STATUS, row.get("confidence"),
            ),
        )

    for row in _read_csv_rows(named_path):
        cur.execute(
            """INSERT INTO named_enemy_candidates
               (artifact_id, symbol, display_guess, group_name, base_type, attrs,
                member_index_in_group, start_id, derived_id_guess, is_boss_group,
                section_const_index, named_enemy_candidate, min_level, max_level, avg_level,
                monster_class, enabled, level_source, evidence_status, source_confidence_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id, row.get("symbol"), row.get("display_guess"), row.get("group"),
                row.get("base_type"), row.get("attrs"), _int_or_none(row.get("member_index_in_group")),
                row.get("start_id"), row.get("derived_id_guess"), _bool_to_int(row.get("is_boss_group")),
                _int_or_none(row.get("section_const_index")), _bool_to_int(row.get("named_enemy_candidate")),
                _int_or_none(row.get("min_level")), _int_or_none(row.get("max_level")),
                _float_or_none(row.get("avg_level")), row.get("monster_class"),
                _int_or_none(row.get("enabled")), row.get("level_source"),
                EVIDENCE_STATUS, row.get("confidence"),
            ),
        )
    return True


def parse_v4_package(conn: sqlite3.Connection, artifact_id: str, extract_dir: Path) -> bool:
    """
    tlopo_concrete_lists_v4_package.zip -- an independent-ish symbolic
    boss/enemy candidate pass, kept in its own tables (not merged into
    the v13 tables) so the two passes can be cross-checked against each
    other rather than silently blended into one population.
    """
    boss_path = extract_dir / "boss_candidates_v4.csv"
    enemy_path = extract_dir / "enemy_candidates_v4.csv"
    if not all(p.exists() for p in (boss_path, enemy_path)):
        return False

    cur = conn.cursor()
    for table in ("boss_candidates_v4", "enemy_candidates_v4"):
        cur.execute(f"DELETE FROM {table} WHERE artifact_id = ?", (artifact_id,))

    for table, path in (("boss_candidates_v4", boss_path), ("enemy_candidates_v4", enemy_path)):
        for row in _read_csv_rows(path):
            cur.execute(
                f"""INSERT INTO {table}
                    (artifact_id, symbol, display_guess, category, source_module,
                     nearby_numbers_json, evidence_status, source_confidence_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id, row.get("symbol"), row.get("display_guess"), row.get("category"),
                    row.get("source_module"), row.get("nearby_numbers"),
                    EVIDENCE_STATUS, row.get("confidence"),
                ),
            )
    return True


PARSERS = [parse_v13_package, parse_v4_package]


def import_all(source_dir: Path, db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    zips = sorted(source_dir.glob("tlopo_*.zip"))
    summary = {"artifacts_found": len(zips), "artifacts_parsed": 0, "table_counts": {}}

    with tempfile.TemporaryDirectory() as tmp:
        for zip_path in zips:
            artifact_id = _slugify(zip_path.name)
            sha256 = _sha256(zip_path)
            size_bytes = zip_path.stat().st_size

            extract_dir = Path(tmp) / artifact_id
            extract_dir.mkdir(exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)  # read-only against zip_path itself

            readme_text = _read_readme_text(extract_dir)

            parsed = False
            for parser in PARSERS:
                if parser(conn, artifact_id, extract_dir):
                    parsed = True
                    break

            conn.execute(
                """INSERT INTO client_artifacts
                       (artifact_id, filename, sha256, size_bytes, imported_at, readme_text, parsed)
                   VALUES (?, ?, ?, ?, strftime('%s','now'), ?, ?)
                   ON CONFLICT(artifact_id) DO UPDATE SET
                       filename=excluded.filename, sha256=excluded.sha256,
                       size_bytes=excluded.size_bytes, imported_at=excluded.imported_at,
                       readme_text=excluded.readme_text, parsed=excluded.parsed""",
                (artifact_id, zip_path.name, sha256, size_bytes, readme_text, int(parsed)),
            )
            if parsed:
                summary["artifacts_parsed"] += 1

    conn.commit()

    for table in (
        "client_artifacts", "avatar_type_groups", "avatar_type_members",
        "enemy_globals", "named_enemy_candidates", "boss_candidates_v4", "enemy_candidates_v4",
    ):
        summary["table_counts"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    conn.close()
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    if not args.source_dir.is_dir():
        print(f"Source directory not found: {args.source_dir}", file=sys.stderr)
        sys.exit(1)

    summary = import_all(args.source_dir, args.db)

    print(f"Source: {args.source_dir}")
    print(f"Database: {args.db}")
    print(f"Artifacts found: {summary['artifacts_found']}")
    print(f"Artifacts deep-parsed: {summary['artifacts_parsed']}")
    print("Table row counts:")
    for table, count in summary["table_counts"].items():
        print(f"  {table}: {count}")


if __name__ == "__main__":
    main()
