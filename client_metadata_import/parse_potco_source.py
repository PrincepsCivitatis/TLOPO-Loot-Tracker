"""
parse_potco_source.py
Parses the REAL POTCO source (Pirates-Online-Rewritten-Py3-master.zip)
into client_metadata.db, as the actual ground truth to cross-reference
TLOPO's recovered ("reported") AvatarTypes/EnemyGlobals data against.

Uses Python's `ast` module to parse pirates/pirate/AvatarTypes.py and
pirates/battle/EnemyGlobals.py WITHOUT executing them -- these files
import pandac.PandaModules and other engine modules that aren't
installed here, and running arbitrary third-party source is never
appropriate for a static-analysis pass anyway.

Evidence framing: rows loaded here get evidence_status="potco_verified"
-- deliberately NOT the bare word "verified" and NOT the TLOPO tier
vocabulary ("reported"/"inferred"/"unresolved"), because this describes
what POTCO's OWN source code says, directly and reproducibly, not a
claim about TLOPO. POTCO is a schema/comparison reference for TLOPO,
never proof of a TLOPO value (see this project's evidence-tier rule) --
"potco_verified" makes that distinction impossible to miss downstream.

AvatarTypes.py shape (the only two statement patterns this parses):
    GroupVar = tuple((AvatarType(base=Base, boss=x) for x in range(A, B)))
    Name1, Name2, ... = GroupVar     # or "Name1, = GroupVar" for one member
EnemyGlobals.py shape:
    __baseAvatarStats = {AvatarTypes.Symbol: [min_lvl, max_lvl, scale,
                                               height, radius, class, enabled], ...}

Usage:
    python parse_potco_source.py --potco-zip PATH [--db PATH]
"""

import argparse
import ast
import sqlite3
import tempfile
import zipfile
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent / "client_metadata.db"
POTCO_ARTIFACT_ID = "potco_pirates_online_rewritten_py3_master"
EVIDENCE_STATUS = "potco_verified"  # see module docstring

SCHEMA = """
CREATE TABLE IF NOT EXISTS potco_artifacts(
    artifact_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    imported_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS potco_avatar_type_groups(
    artifact_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    base_type TEXT,
    attrs TEXT,
    count INTEGER,
    members_json TEXT,
    evidence_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS potco_avatar_type_members(
    artifact_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    group_name TEXT,
    base_type TEXT,
    attrs TEXT,
    member_index_in_group INTEGER,
    is_boss_group INTEGER,
    evidence_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS potco_enemy_globals(
    artifact_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    min_level INTEGER,
    max_level INTEGER,
    scale REAL,
    height REAL,
    battle_radius REAL,
    monster_class TEXT,
    enabled INTEGER,
    evidence_status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_potco_atm_symbol ON potco_avatar_type_members(symbol);
CREATE INDEX IF NOT EXISTS idx_potco_eg_symbol ON potco_enemy_globals(symbol);
"""


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _keyword_value_name(kw: ast.keyword) -> str:
    """Best-effort source text for a keyword's value (a Name/Attribute/Constant)."""
    try:
        return ast.unparse(kw.value)
    except Exception:
        return "?"


def _is_group_definition(node: ast.Assign):
    """
    Matches: GroupVar = tuple((AvatarType(...) for x in range(...)))
    Returns (group_var_name, base_type_or_None, attrs_list, count) or None.
    """
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return None
    target_name = node.targets[0].id

    value = node.value
    if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "tuple"):
        return None
    if not value.args or not isinstance(value.args[0], ast.GeneratorExp):
        return None
    genexp = value.args[0]
    elt = genexp.elt
    if not (isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name) and elt.func.id == "AvatarType"):
        return None

    base_type = None
    attrs = []
    for kw in elt.keywords:
        attrs.append(kw.arg)
        if kw.arg == "base":
            base_type = _keyword_value_name(kw)

    count = None
    if genexp.generators:
        comp = genexp.generators[0]
        if isinstance(comp.iter, ast.Call) and isinstance(comp.iter.func, ast.Name) and comp.iter.func.id == "range":
            range_args = comp.iter.args
            try:
                nums = [ast.literal_eval(a) for a in range_args]
                if len(nums) == 1:
                    count = nums[0]
                elif len(nums) >= 2:
                    count = nums[1] - nums[0]
            except (ValueError, TypeError):
                count = None

    return target_name, base_type, attrs, count


def _is_member_unpack(node: ast.Assign, known_groups: dict):
    """
    Matches: Name1, Name2, ... = GroupVar (GroupVar already a known group).
    Returns (group_var_name, [member_names...]) or None.
    """
    if len(node.targets) != 1 or not isinstance(node.targets[0], (ast.Tuple, ast.List)):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id not in known_groups:
        return None
    members = [elt.id for elt in node.targets[0].elts if isinstance(elt, ast.Name)]
    if not members:
        return None
    return node.value.id, members


def parse_avatar_types(source_text: str):
    """Returns (groups: dict[group_name -> info], members: dict[group_name -> [symbols]])."""
    tree = ast.parse(source_text)
    groups = {}
    members = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        group_def = _is_group_definition(node)
        if group_def is not None:
            name, base_type, attrs, count = group_def
            groups[name] = {"base_type": base_type, "attrs": attrs, "count": count}
            continue
        unpack = _is_member_unpack(node, groups)
        if unpack is not None:
            group_name, member_names = unpack
            members.setdefault(group_name, []).extend(member_names)
    return groups, members


def parse_enemy_globals(source_text: str):
    """Returns dict[symbol -> (min_level, max_level, scale, height, battle_radius, monster_class, enabled)]."""
    tree = ast.parse(source_text)
    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != "__baseAvatarStats":
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key_node, val_node in zip(node.value.keys, node.value.values):
            if not (isinstance(key_node, ast.Attribute) and isinstance(key_node.value, ast.Name)
                    and key_node.value.id == "AvatarTypes"):
                continue
            symbol = key_node.attr
            if not isinstance(val_node, ast.List) or len(val_node.elts) < 7:
                continue
            elts = val_node.elts
            try:
                min_level = ast.literal_eval(elts[0])
                max_level = ast.literal_eval(elts[1])
                scale = ast.literal_eval(elts[2])
                height = ast.literal_eval(elts[3])
                battle_radius = ast.literal_eval(elts[4])
            except (ValueError, TypeError):
                continue
            monster_class = _keyword_value_name(ast.keyword(arg=None, value=elts[5]))
            try:
                enabled = ast.literal_eval(elts[6])
            except (ValueError, TypeError):
                enabled = None
            result[symbol] = (min_level, max_level, scale, height, battle_radius, monster_class, enabled)
    return result


def import_potco(potco_zip: Path, db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    sha256 = _sha256(potco_zip)
    size_bytes = potco_zip.stat().st_size

    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp)
        with zipfile.ZipFile(potco_zip) as zf:
            # Only extract the two files this parser needs -- read-only
            # against the zip, and avoids pulling out the whole (large,
            # binary-heavy) POTCO tree just to read two source files.
            names = [n for n in zf.namelist() if n.endswith(("pirate/AvatarTypes.py", "battle/EnemyGlobals.py"))]
            for n in names:
                zf.extract(n, extract_dir)

        avatar_types_path = next(extract_dir.rglob("AvatarTypes.py"))
        enemy_globals_path = next(extract_dir.rglob("EnemyGlobals.py"))

        groups, members = parse_avatar_types(avatar_types_path.read_text(encoding="utf-8"))
        enemy_globals = parse_enemy_globals(enemy_globals_path.read_text(encoding="utf-8"))

    cur = conn.cursor()
    for table in ("potco_avatar_type_groups", "potco_avatar_type_members", "potco_enemy_globals"):
        cur.execute(f"DELETE FROM {table} WHERE artifact_id = ?", (POTCO_ARTIFACT_ID,))

    for group_name, info in groups.items():
        member_list = members.get(group_name, [])
        is_boss_group = 1 if "boss" in (info["attrs"] or []) else 0
        cur.execute(
            """INSERT INTO potco_avatar_type_groups
               (artifact_id, group_name, base_type, attrs, count, members_json, evidence_status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (POTCO_ARTIFACT_ID, group_name, info["base_type"], ",".join(info["attrs"] or []),
             info["count"], ",".join(member_list), EVIDENCE_STATUS),
        )
        for idx, symbol in enumerate(member_list):
            cur.execute(
                """INSERT INTO potco_avatar_type_members
                   (artifact_id, symbol, group_name, base_type, attrs, member_index_in_group,
                    is_boss_group, evidence_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (POTCO_ARTIFACT_ID, symbol, group_name, info["base_type"],
                 ",".join(info["attrs"] or []), idx, is_boss_group, EVIDENCE_STATUS),
            )

    for symbol, stats in enemy_globals.items():
        cur.execute(
            """INSERT INTO potco_enemy_globals
               (artifact_id, symbol, min_level, max_level, scale, height, battle_radius,
                monster_class, enabled, evidence_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (POTCO_ARTIFACT_ID, symbol, *stats, EVIDENCE_STATUS),
        )

    cur.execute(
        """INSERT INTO potco_artifacts (artifact_id, filename, sha256, size_bytes, imported_at)
           VALUES (?, ?, ?, ?, strftime('%s','now'))
           ON CONFLICT(artifact_id) DO UPDATE SET
               filename=excluded.filename, sha256=excluded.sha256,
               size_bytes=excluded.size_bytes, imported_at=excluded.imported_at""",
        (POTCO_ARTIFACT_ID, potco_zip.name, sha256, size_bytes),
    )
    conn.commit()

    summary = {
        "groups": len(groups),
        "boss_groups": sum(1 for g in groups.values() if "boss" in (g["attrs"] or [])),
        "members": sum(len(m) for m in members.values()),
        "boss_members": sum(len(members.get(g, [])) for g, info in groups.items() if "boss" in (info["attrs"] or [])),
        "enemy_globals_rows": len(enemy_globals),
    }
    conn.close()
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--potco-zip", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    summary = import_potco(args.potco_zip, args.db)
    print(f"POTCO source: {args.potco_zip}")
    print(f"Database: {args.db}")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
