# client_metadata_import

Phase A artifact intake for the TLOPO loot-intelligence-platform side project (see the loot-tracker experimental-branch spec and the TLOPO reverse-engineering bible). Standalone tool, not part of `TLOPO_Tracker/`'s runtime.

## What this is

Every prior reverse-engineering extraction package (`tlopo_*.zip`, normally sitting in the user's Downloads folder) gets hashed and recorded in `client_artifacts`, whether or not its contents are deeply parsed yet. The two enemy/boss-relevant packages currently parsed:

- **v13** (`tlopo_avatartypes_enemy_loot_v13_package.zip`) — AvatarTypes groups/members, EnemyGlobals base stats, and the joined named-enemy candidate view. This is the source of the "102 groups / 366 members / 63 boss-group members / 205 EnemyGlobals rows" figures cited in both spec documents.
- **v4** (`tlopo_concrete_lists_v4_package.zip`) — a separate, independent-ish symbolic boss/enemy candidate pass. Kept in its own tables (`boss_candidates_v4`, `enemy_candidates_v4`) rather than merged into the v13 tables, so the two passes can be cross-checked against each other instead of silently blended into one population.

All other packages in Downloads (dropglobals_focus, dropinfo_commondrops_hunt, global_loot_analysis, static_route, etc.) get a `client_artifacts` row (hash + README text preserved) but `parsed = 0` — their data isn't loaded into any table yet. Each covers different non-enemy data (drop-rate tables, item pools, phase-directory internals) and is its own future pass.

## The evidence-tier rule

**Nothing in this database is `verified`.** Every row from a parsed package carries `evidence_status = "reported"` — this tool re-imports a *prior conversation's own summary CSVs*, it does not independently re-extract anything from the raw TLOPO phase files or executable. A package's own self-assessed confidence text (e.g. "medium-high") is preserved separately as `source_confidence_note` and must never be conflated with `evidence_status` — a package calling itself "high confidence" does not make its evidence_status "verified".

Before treating anything in this database as ground truth: (1) check `evidence_status`, (2) trace back to the source artifact via `artifact_id` → `client_artifacts`, (3) remember the actual arbiter is the growing observed-gameplay dataset (Kraken's Ledger), not this recovered client data — see the spec's hypothesis H1.

## Usage

```
python import_artifacts.py [--source-dir PATH] [--db PATH]
```

Defaults: `--source-dir` is the user's Downloads folder, `--db` is `./client_metadata.db`.

Idempotent — re-running fully replaces each artifact's previously-imported rows (`DELETE ... WHERE artifact_id = ?` then re-insert), so it's always safe to re-run after a new package shows up in the source directory, and running it twice in a row does not duplicate rows.

## Known gap

The raw POTCO source (`BossNPCList.py`, `AvatarTypes.py`, `EnemyGlobals.py` with real display names) is **not** among these packages — the one POTCO-side output that exists (`potco_boss_keys_vs_tlopo_assets.csv` in `tlopo_potco_crossreference_package.zip`) contains POTCO source-control revision keys, not boss/enemy names, and matched zero TLOPO assets. The TLOPO-vs-POTCO named-enemy/boss master list (enemies TLOPO added that POTCO never had) is blocked on the user re-supplying the actual POTCO source — not yet attempted here.
