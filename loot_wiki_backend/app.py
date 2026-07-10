"""
app.py
Minimal backend for anonymized, opt-in TLOPO loot-drop submissions.

Accepts one submission per detected chest from consenting tracker
installs (see TLOPO_Tracker's Settings -> "Share anonymized loot data").
Deliberately has NO concept of player identity -- only a random,
per-install anon_id (not tied to a name/account) is accepted, purely so
duplicate/bursty submissions from the same install can be told apart
from independent ones in aggregate. There is no login, no session, and
nothing here can be traced back to a specific player or character.

Run locally for development:
    pip install -r requirements.txt
    uvicorn app:app --reload

See README.md for deployment notes.
"""

import difflib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

DB_PATH = Path(__file__).parent / "loot_wiki.db"
STATIC_DIR = Path(__file__).parent / "static"
KNOWN_ITEMS_PATH = Path(__file__).parent / "known_items.json"

# Admin token for /admin/* endpoints (see _require_admin) -- the port
# this backend listens on is open to the whole internet (clients need to
# reach it), so anything that can MUTATE data (merge/rename/move a
# cluster) needs its own gate, separate from the read-only public API.
# Auto-generated on first run and persisted to a local file that's
# gitignored (same pattern as the deploy SSH key -- never committed,
# never logged) rather than requiring the operator to set an environment
# variable by hand before the service will even start.
ADMIN_TOKEN_PATH = Path(__file__).parent / "admin_token.txt"
if ADMIN_TOKEN_PATH.exists():
    ADMIN_TOKEN = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
else:
    ADMIN_TOKEN = secrets.token_urlsafe(32)
    ADMIN_TOKEN_PATH.write_text(ADMIN_TOKEN, encoding="utf-8")

# A real, authoritative item-name list extracted from a public open-source
# rewrite of the original Pirates Online codebase (PLocalizerEnglish.py's
# ItemNames dict, plus the 52 playing-card names built from
# PlayingCardGlobals/PLocalizerEnglish's rank/suit tables) -- see
# known_items.json. This is a MUCH stronger correction signal than the
# self-learned item_clusters/item_variants tables below: those can only
# ever converge toward whichever spelling is most POPULAR among noisy OCR
# reads, which isn't necessarily the CORRECT one, whereas this list is
# ground truth. Not exhaustive -- it won't have anything TLOPO added
# itself beyond the base game (e.g. custom drops), and clothing dye colors
# aren't baked into a name here (the game applies color separately from a
# base piece like "Cotton Trousers") -- those fall through to the self-
# learned clustering below instead of forcing a bad match.
try:
    with open(KNOWN_ITEMS_PATH, "r", encoding="utf-8") as f:
        KNOWN_ITEMS = json.load(f)
except FileNotFoundError:
    KNOWN_ITEMS = []
KNOWN_ITEMS_BY_LOWER = {name.lower(): name for name in KNOWN_ITEMS}

# Stricter than FUZZY_MATCH_THRESHOLD (below) used for the self-learned
# clusters -- matching against ~1750 known items carries more chance of a
# coincidental high-similarity hit than matching against a small, per-
# target self-learned set, so this asks for higher confidence before
# trusting a fuzzy hit against the authoritative list.
KNOWN_ITEM_FUZZY_THRESHOLD = 0.88

# Loose sanity bounds -- not trying to validate against the exact known-
# boss/rarity lists client-side code uses (those can change independently
# of this backend), just rejecting obviously-garbage/abusive payloads.
MAX_NAME_LEN = 120
MAX_ITEMS_PER_SUBMISSION = 30
ALLOWED_CHEST_TYPES = {"pouch", "chest", "skull"}
ALLOWED_RARITIES = {"Crude", "Common", "Rare", "Famed", "Legendary", None}
ALLOWED_KILL_TRACKING = {"auto", "manual"}

# A rate above this is a mathematically implausible signal that kills
# are being undercounted for a target (TLOPO's kill model doesn't
# support a single kill reliably yielding more than one of the same
# container type) -- see /rates/{target}/containers, which surfaces a
# "warning" field rather than silently showing a >100% rate as if it
# were trustworthy.
IMPLAUSIBLE_RATE_THRESHOLD = 1.0

# Reference baseline: the item-rarity distribution PER CONTAINER TYPE
# coded into the original (pre-TLOPO) game's loot-drop formulas -- not
# reverse-engineered, not TLOPO's own numbers, just publicly-documented
# base-game drop math re-expressed here as plain percentages so a wiki
# reader can see "here's what the original formula said" next to
# "here's what TLOPO installs are actually observing" from real
# submissions. TLOPO is free to have retuned any of this since -- that
# divergence is exactly what the live/observed numbers are for.
POTCO_BASELINE_RARITY_BY_CONTAINER = {
    "pouch": {"Crude": 0.7549, "Common": 0.2, "Rare": 0.045, "Famed": 0.0001, "Legendary": 0.0},
    "chest": {"Crude": 0.41979, "Common": 0.48, "Rare": 0.1, "Famed": 0.0002, "Legendary": 0.00001},
    "skull": {"Crude": 0.0, "Common": 0.72415, "Rare": 0.25, "Famed": 0.025, "Legendary": 0.00085},
}


class ItemIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    rarity: Optional[str] = None

    @field_validator("rarity")
    @classmethod
    def rarity_must_be_known(cls, v):
        if v not in ALLOWED_RARITIES:
            raise ValueError(f"unknown rarity {v!r}")
        return v


class ItemStatsIn(BaseModel):
    anon_id: str
    item_name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    attack: Optional[int] = Field(default=None, ge=0, le=100_000)
    weapon_skill_name: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    weapon_skill_rank: Optional[int] = Field(default=None, ge=0, le=100)
    boosts: Optional[dict] = None
    level_requirement: Optional[int] = Field(default=None, ge=0, le=100)
    gold_value: Optional[int] = Field(default=None, ge=0, le=10_000_000)

    @field_validator("anon_id")
    @classmethod
    def anon_id_must_be_uuid(cls, v):
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("anon_id must be a valid UUID")
        return v

    @field_validator("boosts")
    @classmethod
    def boosts_must_be_small_and_numeric(cls, v):
        if v is None:
            return v
        if len(v) > 10:
            raise ValueError("too many boost entries")
        for key, val in v.items():
            if not isinstance(key, str) or len(key) > 40:
                raise ValueError("boost key must be a short string")
            if not isinstance(val, (int, float)) or not (-1000 <= val <= 1000):
                raise ValueError("boost value out of range")
        return v


class SubmissionIn(BaseModel):
    anon_id: str
    target: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    chest_type: str
    items: List[ItemIn] = Field(default_factory=list, max_length=MAX_ITEMS_PER_SUBMISSION)
    gold: int = Field(default=0, ge=0, le=1_000_000)
    # Kills SINCE THE LAST container submitted for this target in this
    # session (a delta), NOT a cumulative session total -- see
    # TLOPO_Tracker's loot_wiki_client.submit_chest_async docstring.
    # This is what lets /rates sum kills across every contributor's
    # submissions with a single SUM(), no session-boundary bookkeeping.
    kills_since_last_container: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    skull_chest_number: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    # "auto" (boss health-bar auto-detector) or "manual" (player clicked
    # +1/+5/+10 themselves) -- see TLOPO_Tracker's loot_wiki_client.
    # submit_chest_async docstring for why this matters: undercounted
    # manual kills silently inflate every drop rate, since kills are the
    # denominator. Required (not optional) so this can't be silently
    # omitted and treated as equally trustworthy as auto-tracked data.
    kill_tracking: str

    @field_validator("kill_tracking")
    @classmethod
    def kill_tracking_must_be_known(cls, v):
        if v not in ALLOWED_KILL_TRACKING:
            raise ValueError(f"kill_tracking must be one of {sorted(ALLOWED_KILL_TRACKING)}")
        return v

    @field_validator("anon_id")
    @classmethod
    def anon_id_must_be_uuid(cls, v):
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("anon_id must be a valid UUID")
        return v

    @field_validator("chest_type")
    @classmethod
    def chest_type_must_be_known(cls, v):
        if v not in ALLOWED_CHEST_TYPES:
            raise ValueError(f"chest_type must be one of {sorted(ALLOWED_CHEST_TYPES)}")
        return v

    @field_validator("target")
    @classmethod
    def target_no_control_chars(cls, v):
        # Rejects anything that isn't plain printable text -- a target
        # name is always short human-readable text, never a place for
        # control characters or the kind of payloads injection attempts
        # rely on.
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", v):
            raise ValueError("target contains invalid characters")
        return v.strip()


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="TLOPO Loot Wiki Backend")
app.state.limiter = limiter
# Uses slowapi's own handler (rather than a hand-rolled one) -- a custom
# handler registered via @app.exception_handler, combined with
# SlowAPIMiddleware, doesn't actually get invoked: SlowAPIMiddleware is
# Starlette BaseHTTPMiddleware-based and sits outside FastAPI's own
# exception-handling layer, so a RateLimitExceeded raised inside a route
# (via the @limiter.limit(...) decorator) propagates past the point
# where registered handlers are consulted, crashing with a 500
# (confirmed by actually exceeding the rate limit in testing -- this
# doesn't show up just from reading the code). The per-route decorator
# alone is sufficient; SlowAPIMiddleware isn't needed here.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _require_admin(x_admin_token: str = Header(default="")):
    # secrets.compare_digest instead of == -- a plain string comparison
    # short-circuits on the first mismatched byte, which leaks the
    # token's length/prefix via response-timing differences to anyone
    # probing this endpoint from the public internet.
    if not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Token header")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db():
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id TEXT NOT NULL,
                anon_id TEXT NOT NULL,
                target TEXT NOT NULL,
                chest_type TEXT NOT NULL,
                item_name TEXT,
                item_rarity TEXT,
                gold INTEGER NOT NULL DEFAULT 0,
                kills_since_last_container INTEGER,
                skull_chest_number INTEGER,
                kill_tracking TEXT,
                received_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target ON submissions(target)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item ON submissions(item_name, item_rarity)")

        # Item-name canonicalization (see _canonicalize_item_name) --
        # clusters raw OCR item-name spellings that are likely the same
        # real item together. `is_known` marks a cluster that was seeded
        # from KNOWN_ITEMS (the ground-truth item list) rather than
        # self-learned from submissions: its canonical_name is
        # authoritative and never gets outvoted by a more-popular OCR
        # misread, unlike a self-learned cluster (is_known = 0), whose
        # canonical_name tracks whichever spelling has been seen most
        # often so an early bad OCR read doesn't permanently win.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT NOT NULL,
                is_known INTEGER NOT NULL DEFAULT 0,
                stats_json TEXT,
                stats_confirmed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # CREATE TABLE IF NOT EXISTS is a no-op on a database from before
        # stats_json/stats_confirmed existed -- an already-deployed
        # item_clusters table needs those columns added explicitly, since
        # SQLite has no "ADD COLUMN IF NOT EXISTS". Checked via
        # pragma_table_info rather than try/except around ALTER TABLE so
        # this stays a clean no-op (not a caught-and-ignored error) on
        # every subsequent startup once the column already exists.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(item_clusters)")}
        if "stats_json" not in existing_cols:
            conn.execute("ALTER TABLE item_clusters ADD COLUMN stats_json TEXT")
        if "stats_confirmed" not in existing_cols:
            conn.execute("ALTER TABLE item_clusters ADD COLUMN stats_confirmed INTEGER NOT NULL DEFAULT 0")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_variants (
                raw_name TEXT PRIMARY KEY,
                cluster_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (cluster_id) REFERENCES item_clusters(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_variant_cluster ON item_variants(cluster_id)")

        # Raw candidate item-stat readings (Attack, weapon skill, stat
        # boosts, level requirement -- everything on the in-game item
        # detail card besides name/icon/rarity, which are already
        # covered by item_clusters). Unlike drop rates, stats are static
        # ground truth per item, not something to keep sampling forever
        # -- see _maybe_confirm_item_stats: once STATS_CONFIRM_THRESHOLD
        # independent submissions agree EXACTLY on the same payload for
        # an item, that payload gets written to item_clusters.stats_json
        # and stats_confirmed flips to 1, at which point /confirmed_items
        # tells every tracker install to stop scanning that item
        # entirely. Submissions that don't match an existing candidate
        # start a new one rather than being discarded, so a genuine
        # early misread doesn't block the correct reading from ever
        # accumulating its own threshold.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_stat_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_id INTEGER NOT NULL,
                anon_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                submitted_at REAL NOT NULL,
                FOREIGN KEY (cluster_id) REFERENCES item_clusters(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stat_submission_cluster ON item_stat_submissions(cluster_id, payload_json)")


_init_db()


# A brand-new raw spelling only joins an existing cluster above this
# similarity (difflib SequenceMatcher ratio, stdlib -- no new dependency
# for what's fundamentally a small, infrequent lookup). Picked
# conservatively from real OCR misreads seen in the wild (e.g. "Ten f
# Hearts" -> "Ten of Hearts" at ~0.92, "Buccaneer DDoll" -> "Buccaneer
# Doll" at ~0.93) while staying well clear of genuinely different same-
# template items (e.g. "Ten of Hearts" vs "Ten of Diamonds" scores well
# under this) -- false merges are worse than a few unmerged duplicates,
# since merging two real distinct items silently corrupts drop-rate math
# in a way a human reviewing the wiki has no way to notice or undo.
FUZZY_MATCH_THRESHOLD = 0.84
# Below this length, fuzzy matching is skipped entirely and a new
# spelling always starts (or exact-matches into) its own cluster --
# short strings can score a deceptively high similarity ratio against
# an unrelated short string by chance, and OCR garbage this short is
# more likely a fragment (nameplate bleed, a stray character) than a
# genuine item name worth clustering at all.
FUZZY_MIN_NAME_LEN = 4

# How many independent submissions must agree EXACTLY on the same item-
# stat payload before it's trusted as confirmed (see
# _maybe_confirm_item_stats). Low on purpose: unlike a drop-rate
# percentage, an item's Attack/skill/boosts/level-requirement are a
# fixed, single ground-truth value, not something that benefits from
# averaging many samples -- 2 independent players reading the same
# number off their own screen is already strong evidence, and asking
# for more just means more players wastefully re-scanning something
# already effectively certain.
STATS_CONFIRM_THRESHOLD = 2
# Payload keys accepted for an item-stat submission -- validated so a
# submission can't smuggle in arbitrary junk keys that would never
# match any other submission's payload and therefore could never reach
# STATS_CONFIRM_THRESHOLD, silently wasting rows forever.
ALLOWED_STAT_KEYS = {"attack", "weapon_skill_name", "weapon_skill_rank", "boosts", "level_requirement", "gold_value"}


def _normalize_item_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _match_known_item(normalized: str) -> Optional[str]:
    """
    Checks a normalized raw item name against KNOWN_ITEMS (the real,
    ground-truth item list -- see its module-level comment) and returns
    the official spelling if confident, else None. Exact (case-
    insensitive) match is tried first; a fuzzy pass only runs for names
    long enough that a coincidental high-similarity match against ~1750
    candidates is unlikely to be a false positive (see FUZZY_MIN_NAME_LEN,
    KNOWN_ITEM_FUZZY_THRESHOLD).
    """
    exact = KNOWN_ITEMS_BY_LOWER.get(normalized.lower())
    if exact is not None:
        return exact

    if len(normalized) < FUZZY_MIN_NAME_LEN:
        return None

    best_ratio = 0.0
    best_name = None
    normalized_lower = normalized.lower()
    for candidate in KNOWN_ITEMS:
        len_a, len_b = len(normalized), len(candidate)
        if abs(len_a - len_b) > max(2, 0.2 * max(len_a, len_b)):
            continue
        ratio = difflib.SequenceMatcher(None, normalized_lower, candidate.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best_name = ratio, candidate
    return best_name if best_ratio >= KNOWN_ITEM_FUZZY_THRESHOLD else None


def _canonicalize_item_name(conn: sqlite3.Connection, raw_name: str) -> str:
    """
    Maps a raw, possibly OCR-garbled item name to its best-guess correct
    spelling for storage -- this is what keeps a misread like "Ten f
    Hearts" from showing up on the wiki as a phantom item distinct from
    "Ten of Hearts".

    Two layers, tried in order:

    1. KNOWN_ITEMS -- a real, ground-truth item name list (see its
       module comment). An exact or high-confidence fuzzy match here is
       authoritative: the raw spelling is filed under that official name
       permanently, never subject to being outvoted by a more-popular
       OCR misread later. This covers the large majority of real items.

    2. A self-learning fallback (item_clusters/item_variants) for
       anything KNOWN_ITEMS doesn't cover -- TLOPO-specific items added
       since that list was captured, dye-colored clothing variants (the
       game applies color separately from a base piece name), or
       anything else novel. Spellings are clustered by fuzzy similarity
       (FUZZY_MATCH_THRESHOLD) and each cluster's canonical name is
       whichever spelling has been seen most often, so an early bad OCR
       read doesn't permanently win -- once the correctly-spelled
       version starts arriving more often, the cluster's canonical name
       flips to it.

    Either way, `submissions.item_name` stores whatever spelling was
    canonical AT INSERT TIME (kept as-is, never rewritten), but every
    read endpoint (see /rates/{target}/items, /loot_table/{target},
    /stats/{target}) resolves it back through item_variants/item_clusters
    to the cluster's CURRENT canonical name before grouping -- so a
    canonical-name flip corrects every past row's aggregation too, not
    just future submissions, with no backfill/migration needed.
    """
    normalized = _normalize_item_name(raw_name)

    existing = conn.execute(
        "SELECT cluster_id FROM item_variants WHERE raw_name = ?", (normalized,)
    ).fetchone()

    if existing is not None:
        cluster_id = existing[0]
        conn.execute("UPDATE item_variants SET count = count + 1 WHERE raw_name = ?", (normalized,))
        # A known-item cluster's canonical name is authoritative and was
        # already set when the cluster was created -- no popularity vote
        # needed, just return it directly.
        known_row = conn.execute(
            "SELECT canonical_name FROM item_clusters WHERE id = ? AND is_known = 1", (cluster_id,)
        ).fetchone()
        if known_row is not None:
            return known_row[0]
    else:
        known_name = _match_known_item(normalized)
        if known_name is not None:
            cluster_row = conn.execute(
                "SELECT id FROM item_clusters WHERE canonical_name = ? AND is_known = 1", (known_name,)
            ).fetchone()
            if cluster_row is not None:
                cluster_id = cluster_row[0]
            else:
                cursor = conn.execute(
                    "INSERT INTO item_clusters (canonical_name, is_known) VALUES (?, 1)", (known_name,)
                )
                cluster_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO item_variants (raw_name, cluster_id, count) VALUES (?, ?, 1)",
                (normalized, cluster_id),
            )
            return known_name

        cluster_id = None
        if len(normalized) >= FUZZY_MIN_NAME_LEN:
            best_ratio = 0.0
            best_id = None
            for row in conn.execute("SELECT id, canonical_name FROM item_clusters WHERE is_known = 0"):
                other = row[1]
                len_a, len_b = len(normalized), len(other)
                if abs(len_a - len_b) > max(2, 0.25 * max(len_a, len_b)):
                    continue  # too different in length to be worth an expensive ratio() call
                ratio = difflib.SequenceMatcher(None, normalized.lower(), other.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio, best_id = ratio, row[0]
            if best_ratio >= FUZZY_MATCH_THRESHOLD:
                cluster_id = best_id

        if cluster_id is None:
            cursor = conn.execute(
                "INSERT INTO item_clusters (canonical_name, is_known) VALUES (?, 0)", (normalized,)
            )
            cluster_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO item_variants (raw_name, cluster_id, count) VALUES (?, ?, 0)",
            (normalized, cluster_id),
        )
        conn.execute("UPDATE item_variants SET count = count + 1 WHERE raw_name = ?", (normalized,))

    # Only flip the cluster's canonical spelling once some variant
    # STRICTLY overtakes the current one in popularity -- not on ties,
    # and not toward "whichever is shorter". A shorter-wins tie-break
    # sounds reasonable but actively backfires on the single most common
    # OCR failure mode: a dropped character makes the WRONG spelling
    # shorter (e.g. "Ten f Hearts" vs "Ten of Hearts"). Staying put until
    # a variant clearly wins avoids that trap, and avoids canonical_name
    # flip-flopping between two equally-popular spellings from one
    # submission to the next.
    current_name = conn.execute(
        "SELECT canonical_name FROM item_clusters WHERE id = ?", (cluster_id,)
    ).fetchone()[0]
    current_count_row = conn.execute(
        "SELECT count FROM item_variants WHERE raw_name = ?", (current_name,)
    ).fetchone()
    current_count = current_count_row[0] if current_count_row else 0

    top = conn.execute(
        """
        SELECT raw_name, count FROM item_variants
        WHERE cluster_id = ?
        ORDER BY count DESC, raw_name ASC
        LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()

    if top is not None and top[1] > current_count:
        conn.execute(
            "UPDATE item_clusters SET canonical_name = ? WHERE id = ?", (top[0], cluster_id)
        )
        current_name = top[0]

    return current_name


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    """Serves the search/browse UI (static/index.html) -- a single
    self-contained page with no build step, calling this same API via
    fetch()."""
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/submit")
@limiter.limit("30/minute")
def submit(request: Request, submission: SubmissionIn):
    """
    Records one chest's worth of loot. A chest with zero items (e.g. a
    pouch that only had gold) is still stored as a single row with
    item_name=None, so it counts toward the denominator (total chests
    opened for that target) that drop-rate math needs.
    """
    now = time.time()
    # One submission_id shared by every row this chest produces, so a
    # chest with multiple items doesn't get double/triple-counted as
    # multiple chests when aggregating by chest -- see /stats, which
    # counts COUNT(DISTINCT submission_id), not COUNT(DISTINCT id).
    submission_id = str(uuid.uuid4())

    with _db() as conn:
        # Canonicalize each item's raw OCR name before it ever reaches
        # `submissions` (see _canonicalize_item_name) -- this is what
        # keeps an OCR misread like "Ten f Hearts" from being stored (and
        # later aggregated/displayed) as a phantom item distinct from
        # "Ten of Hearts".
        canonical_names = [_canonicalize_item_name(conn, item.name) for item in submission.items]

        rows = [
            (submission_id, submission.anon_id, submission.target, submission.chest_type,
             canonical_name, item.rarity, submission.gold,
             submission.kills_since_last_container, submission.skull_chest_number,
             submission.kill_tracking, now)
            for item, canonical_name in zip(submission.items, canonical_names)
        ] or [
            (submission_id, submission.anon_id, submission.target, submission.chest_type,
             None, None, submission.gold,
             submission.kills_since_last_container, submission.skull_chest_number,
             submission.kill_tracking, now)
        ]

        conn.executemany(
            """
            INSERT INTO submissions
                (submission_id, anon_id, target, chest_type, item_name, item_rarity,
                 gold, kills_since_last_container, skull_chest_number, kill_tracking, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    return {"status": "recorded", "items_recorded": len(rows)}


def _canonical_stat_payload(body: ItemStatsIn) -> str:
    """
    Normalized JSON string of a stat submission's payload fields, used
    as the exact-match key candidate readings are grouped by (see
    _maybe_confirm_item_stats) -- sorted keys and dropped None fields so
    two submissions that report the same values but happen to omit
    different optional fields as None still hash identically.
    """
    payload = {
        k: v
        for k, v in {
            "attack": body.attack,
            "weapon_skill_name": body.weapon_skill_name.strip() if body.weapon_skill_name else None,
            "weapon_skill_rank": body.weapon_skill_rank,
            "boosts": dict(sorted(body.boosts.items())) if body.boosts else None,
            "level_requirement": body.level_requirement,
            "gold_value": body.gold_value,
        }.items()
        if v is not None
    }
    return json.dumps(payload, sort_keys=True)


def _maybe_confirm_item_stats(conn: sqlite3.Connection, cluster_id: int) -> None:
    """
    After a new stat submission is recorded, checks whether any single
    payload for this item cluster has now reached STATS_CONFIRM_THRESHOLD
    independent (anon_id) submissions -- if so, locks that payload onto
    item_clusters.stats_json/stats_confirmed. Once locked, this never
    re-runs for that cluster's future submissions (see the early return
    in submit_item_stats) -- there's nothing to gain from continuing to
    accumulate rows for an item whose stats are already trusted.
    """
    rows = conn.execute(
        """
        SELECT payload_json, COUNT(DISTINCT anon_id) AS n
        FROM item_stat_submissions
        WHERE cluster_id = ?
        GROUP BY payload_json
        ORDER BY n DESC LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()
    if rows is not None and rows[1] >= STATS_CONFIRM_THRESHOLD:
        conn.execute(
            "UPDATE item_clusters SET stats_json = ?, stats_confirmed = 1 WHERE id = ?",
            (rows[0], cluster_id),
        )


@app.post("/submit_item_stats")
@limiter.limit("30/minute")
def submit_item_stats(request: Request, body: ItemStatsIn):
    """
    Records one player's OCR read of an item's detail card (Attack,
    weapon skill, stat boosts, level requirement, gold value) -- see
    STATS_CONFIRM_THRESHOLD's docstring for why this is a "confirm once,
    then stop" system rather than an ongoing-sample system like drop
    rates. Silently no-ops (still 200s) if this item is already
    confirmed, or if the submission carries no stat fields at all, so a
    tracker install can submit opportunistically without needing to
    check /confirmed_items itself first on every single call -- though
    it still should, to avoid the OCR/network work in the first place.
    """
    payload_json = _canonical_stat_payload(body)
    if payload_json == "{}":
        return {"status": "ignored", "reason": "no stat fields provided"}

    with _db() as conn:
        canonical_name = _canonicalize_item_name(conn, body.item_name)
        cluster_row = conn.execute(
            "SELECT id, stats_confirmed FROM item_clusters WHERE canonical_name = ?",
            (canonical_name,),
        ).fetchone()
        cluster_id = cluster_row[0]
        if cluster_row[1]:
            return {"status": "ignored", "reason": "already confirmed", "item_name": canonical_name}

        conn.execute(
            "INSERT INTO item_stat_submissions (cluster_id, anon_id, payload_json, submitted_at) VALUES (?, ?, ?, ?)",
            (cluster_id, body.anon_id, payload_json, time.time()),
        )
        _maybe_confirm_item_stats(conn, cluster_id)
        confirmed_now = conn.execute(
            "SELECT stats_confirmed FROM item_clusters WHERE id = ?", (cluster_id,)
        ).fetchone()[0]

    return {"status": "recorded", "item_name": canonical_name, "confirmed": bool(confirmed_now)}


@app.get("/confirmed_items")
@limiter.limit("60/minute")
def confirmed_items(request: Request):
    """
    Every item name with stats_confirmed = 1 -- the list a tracker
    install fetches (once at startup, cached, not per-poll) to decide
    which items it can skip scanning entirely. Deliberately just names,
    not the stats themselves -- a tracker doesn't need the confirmed
    VALUES to know to skip OCR, only the fact that it's already settled;
    the values are fetched per-item via /item/{name} same as everything
    else on the wiki.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        names = [
            row["canonical_name"]
            for row in conn.execute("SELECT canonical_name FROM item_clusters WHERE stats_confirmed = 1")
        ]
    return {"items": names}


@app.get("/stats/{target}")
@limiter.limit("60/minute")
def stats(request: Request, target: str):
    """
    Aggregate summary for one target: total chests opened by type, and
    a count of every named (Famed/Legendary) item seen, so a wiki page
    can compute e.g. "Miracle Water: 3 drops across 214 skull chests."
    Deliberately does not expose anon_id or per-submission rows -- only
    aggregate counts.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        chest_counts = {
            row["chest_type"]: row["n"]
            for row in conn.execute(
                """
                SELECT chest_type, COUNT(DISTINCT submission_id) AS n
                FROM submissions WHERE target = ? GROUP BY chest_type
                """,
                (target,),
            )
        }
        item_counts = [
            {"name": row["item_name"], "rarity": row["item_rarity"], "count": row["n"]}
            for row in conn.execute(
                """
                SELECT ic.canonical_name AS item_name, s.item_rarity AS item_rarity, COUNT(*) AS n
                FROM submissions s
                JOIN item_variants iv ON iv.raw_name = s.item_name
                JOIN item_clusters ic ON ic.id = iv.cluster_id
                WHERE s.target = ? AND s.item_name IS NOT NULL AND s.item_rarity IN ('Famed', 'Legendary')
                GROUP BY ic.canonical_name, s.item_rarity
                ORDER BY n DESC
                """,
                (target,),
            )
        ]

    if not chest_counts and not item_counts:
        raise HTTPException(status_code=404, detail="No data for this target yet")

    return {"target": target, "chests": chest_counts, "named_items": item_counts}


def _target_has_data(conn, target: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM submissions WHERE target = ? LIMIT 1", (target,)
    ).fetchone()
    return row is not None


def _total_kills(conn, target: str) -> int:
    """
    Sums kills_since_last_container across DISTINCT chests (not rows --
    a chest with N items has N rows sharing one submission_id, and the
    same kills value copied onto each of them, so summing raw rows
    would multiply it by item count). This is the one SUM() that makes
    the whole delta-based design work: it's correct regardless of how
    many different contributors' sessions fed into it, since each
    submission's delta only ever counts once here.
    """
    row = conn.execute(
        """
        SELECT SUM(kills_since_last_container) AS total FROM (
            SELECT DISTINCT submission_id, kills_since_last_container
            FROM submissions WHERE target = ?
        )
        """,
        (target,),
    ).fetchone()
    return row["total"] or 0


def _total_containers(conn, target: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT submission_id) AS n FROM submissions WHERE target = ?",
        (target,),
    ).fetchone()
    return row["n"] or 0


def _kill_tracking_breakdown(conn, target: str) -> dict:
    """
    How many distinct containers for this target were logged while kills
    were auto-tracked (the boss health-bar detector) versus manual-only
    (the player clicking +1/+5/+10 themselves). Manual counting is easy
    to under-report mid-farm, which understates total kills and
    silently inflates every rate that divides by it -- this breakdown
    lets a UI show a confidence caveat instead of presenting manual-only
    data with the same trust as auto-tracked data.
    """
    breakdown = {"auto": 0, "manual": 0, "unknown": 0}
    for row in conn.execute(
        """
        SELECT kill_tracking, COUNT(DISTINCT submission_id) AS n
        FROM submissions WHERE target = ? GROUP BY kill_tracking
        """,
        (target,),
    ):
        key = row["kill_tracking"] if row["kill_tracking"] in ("auto", "manual") else "unknown"
        breakdown[key] += row["n"]
    return breakdown


@app.get("/enemies")
@limiter.limit("60/minute")
def enemies(request: Request):
    """List every target with at least one submission, for a search/
    autocomplete UI to pull from."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        names = [
            row["target"]
            for row in conn.execute("SELECT DISTINCT target FROM submissions ORDER BY target")
        ]
    return {"enemies": names}


@app.get("/rates/{target}/containers")
@limiter.limit("60/minute")
def container_rates(request: Request, target: str):
    """
    Chance a kill on this target yields each container type, e.g.
    "0.64 chests per kill". total_kills is summed across every
    contributor's submissions for this target (see _total_kills).

    Includes a kill_tracking breakdown (see _kill_tracking_breakdown)
    and a `warning` field if any rate comes out above
    IMPLAUSIBLE_RATE_THRESHOLD -- TLOPO's kill model doesn't support a
    single kill reliably yielding more than one of the same container
    type, so a rate over 100% is a hard sign kills are being
    undercounted (most likely from manual-only tracking on a target
    that was farmed quickly), not a real drop rate.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        if not _target_has_data(conn, target):
            raise HTTPException(status_code=404, detail="No data for this target yet")

        total_kills = _total_kills(conn, target)
        containers = {}
        implausible = False
        for row in conn.execute(
            """
            SELECT chest_type, COUNT(DISTINCT submission_id) AS n
            FROM submissions WHERE target = ? GROUP BY chest_type
            """,
            (target,),
        ):
            count = row["n"]
            rate = (count / total_kills) if total_kills else None
            if rate is not None and rate > IMPLAUSIBLE_RATE_THRESHOLD:
                implausible = True
            containers[row["chest_type"]] = {"count": count, "rate_per_kill": rate}

        kill_tracking = _kill_tracking_breakdown(conn, target)

    result = {
        "target": target,
        "total_kills": total_kills,
        "containers": containers,
        "kill_tracking": kill_tracking,
    }
    if implausible or kill_tracking["manual"] > 0:
        result["warning"] = (
            "Some or all kills for this target were manually tracked, or a computed "
            "rate exceeds 100% -- kills may be undercounted, which inflates every rate "
            "below. Treat these numbers as a lower-confidence estimate."
        )
    return result


@app.get("/rates/{target}/rarities")
@limiter.limit("60/minute")
def rarity_rates(request: Request, target: str):
    """
    Distribution of item rarity across every container opened for this
    target (all container types combined -- see the module README for
    why a per-container-type breakdown isn't done yet).
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        if not _target_has_data(conn, target):
            raise HTTPException(status_code=404, detail="No data for this target yet")

        total_containers = _total_containers(conn, target)
        rarities = {}
        for row in conn.execute(
            """
            SELECT item_rarity, COUNT(*) AS n
            FROM submissions
            WHERE target = ? AND item_rarity IS NOT NULL
            GROUP BY item_rarity
            """,
            (target,),
        ):
            count = row["n"]
            rarities[row["item_rarity"]] = {
                "count": count,
                "rate_per_container": (count / total_containers) if total_containers else None,
            }

    return {"target": target, "total_containers": total_containers, "rarities": rarities}


def _rarity_by_container_breakdown(conn: sqlite3.Connection, target: Optional[str]) -> dict:
    """
    Shared aggregation behind /rates/{target}/rarities_by_container and
    /global_stats: rarity distribution PER container type (pouch/chest/
    skull), each paired with the POTCO_BASELINE_RARITY_BY_CONTAINER
    reference figure for the same container+rarity cell. `target=None`
    aggregates across every submission site-wide instead of scoping to
    one target -- same shape either way, so both endpoints can share
    this and a UI can render them with the same rendering code.
    """
    containers = {}
    for chest_type in ALLOWED_CHEST_TYPES:
        if target is not None:
            total_of_type = conn.execute(
                "SELECT COUNT(DISTINCT submission_id) AS n FROM submissions WHERE target = ? AND chest_type = ?",
                (target, chest_type),
            ).fetchone()["n"]
        else:
            total_of_type = conn.execute(
                "SELECT COUNT(DISTINCT submission_id) AS n FROM submissions WHERE chest_type = ?",
                (chest_type,),
            ).fetchone()["n"]
        if not total_of_type:
            continue

        if target is not None:
            rarity_rows = conn.execute(
                """
                SELECT item_rarity, COUNT(*) AS n
                FROM submissions
                WHERE target = ? AND chest_type = ? AND item_rarity IS NOT NULL
                GROUP BY item_rarity
                """,
                (target, chest_type),
            )
        else:
            rarity_rows = conn.execute(
                """
                SELECT item_rarity, COUNT(*) AS n
                FROM submissions
                WHERE chest_type = ? AND item_rarity IS NOT NULL
                GROUP BY item_rarity
                """,
                (chest_type,),
            )
        observed = {row["item_rarity"]: row["n"] / total_of_type for row in rarity_rows}

        baseline = POTCO_BASELINE_RARITY_BY_CONTAINER.get(chest_type, {})
        rarities = {}
        for rarity in ("Crude", "Common", "Rare", "Famed", "Legendary"):
            if rarity not in observed and rarity not in baseline:
                continue
            rarities[rarity] = {
                "observed": observed.get(rarity),
                "potco_baseline": baseline.get(rarity),
            }
        containers[chest_type] = {"total_containers": total_of_type, "rarities": rarities}

    return containers


@app.get("/rates/{target}/rarities_by_container")
@limiter.limit("60/minute")
def rarity_rates_by_container(request: Request, target: str):
    """
    Rarity distribution broken down PER container type -- unlike
    /rates/{target}/rarities, which blends every container type
    together, this is the apples-to-apples shape needed to compare
    "here's what the original base-game formula gives a Skull Chest"
    against "here's what TLOPO installs are actually seeing," rather
    than mixing pouch/chest/skull chest odds into one number.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        if not _target_has_data(conn, target):
            raise HTTPException(status_code=404, detail="No data for this target yet")
        containers = _rarity_by_container_breakdown(conn, target)

    if not containers:
        raise HTTPException(status_code=404, detail="No container-type data for this target yet")

    return {"target": target, "containers": containers}


@app.get("/global_stats")
@limiter.limit("60/minute")
def global_stats(request: Request):
    """
    Site-wide macro rates: the same rarity-by-container breakdown as
    /rates/{target}/rarities_by_container, but summed across every
    target instead of scoped to one -- one big TLOPO-vs-POTCO number
    per container type per rarity tier, using all loot data site-wide.
    Meant for the homepage, shown before any search, so a visitor gets
    an at-a-glance comparison without picking a target first.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        containers = _rarity_by_container_breakdown(conn, target=None)
        total_targets = conn.execute("SELECT COUNT(DISTINCT target) AS n FROM submissions").fetchone()["n"]
        total_containers_all = conn.execute(
            "SELECT COUNT(DISTINCT submission_id) AS n FROM submissions"
        ).fetchone()["n"]

    return {
        "containers": containers,
        "total_targets": total_targets,
        "total_containers": total_containers_all,
    }


@app.get("/items")
@limiter.limit("60/minute")
def items_list(request: Request):
    """List every known item's current canonical name, for a search/
    autocomplete UI to pull from -- the item-side counterpart to
    /enemies. Sourced from item_clusters directly (not a DISTINCT scan
    of submissions.item_name) so it reflects each cluster's CURRENT
    canonical spelling, same self-correcting property /rates and
    /loot_table already have."""
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        names = [
            row["canonical_name"]
            for row in conn.execute(
                """
                SELECT DISTINCT ic.canonical_name
                FROM item_clusters ic
                JOIN item_variants iv ON iv.cluster_id = ic.id
                JOIN submissions s ON s.item_name = iv.raw_name
                ORDER BY ic.canonical_name
                """
            )
        ]
    return {"items": names}


@app.get("/rates/{target}/items")
@limiter.limit("60/minute")
def item_rates(request: Request, target: str):
    """
    Per-item drop rate (any rarity, not just Famed/Legendary -- see
    /stats for the named-only summary) across every container opened
    for this target.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        if not _target_has_data(conn, target):
            raise HTTPException(status_code=404, detail="No data for this target yet")

        total_containers = _total_containers(conn, target)
        items = [
            {
                "name": row["item_name"],
                "rarity": row["item_rarity"],
                "count": row["n"],
                "rate_per_container": (row["n"] / total_containers) if total_containers else None,
            }
            for row in conn.execute(
                """
                SELECT ic.canonical_name AS item_name, s.item_rarity AS item_rarity, COUNT(*) AS n
                FROM submissions s
                JOIN item_variants iv ON iv.raw_name = s.item_name
                JOIN item_clusters ic ON ic.id = iv.cluster_id
                WHERE s.target = ? AND s.item_name IS NOT NULL
                GROUP BY ic.canonical_name, s.item_rarity
                ORDER BY n DESC
                """,
                (target,),
            )
        ]

    return {"target": target, "total_containers": total_containers, "items": items}


@app.get("/loot_table/{target}")
@limiter.limit("60/minute")
def loot_table(request: Request, target: str):
    """
    The possibility space for this target -- every distinct item ever
    observed, with no rate/count math -- i.e. "what CAN drop here" as
    opposed to "how often." Sorted by rarity tier so a UI can group
    Legendary-down-to-Crude without needing its own rarity ordering.
    """
    rarity_order = {"Legendary": 0, "Famed": 1, "Rare": 2, "Common": 3, "Crude": 4}
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        if not _target_has_data(conn, target):
            raise HTTPException(status_code=404, detail="No data for this target yet")

        rows = [
            {"name": row["item_name"], "rarity": row["item_rarity"]}
            for row in conn.execute(
                """
                SELECT DISTINCT ic.canonical_name AS item_name, s.item_rarity AS item_rarity
                FROM submissions s
                JOIN item_variants iv ON iv.raw_name = s.item_name
                JOIN item_clusters ic ON ic.id = iv.cluster_id
                WHERE s.target = ? AND s.item_name IS NOT NULL
                """,
                (target,),
            )
        ]
    rows.sort(key=lambda r: (rarity_order.get(r["rarity"], 99), r["name"]))

    return {"target": target, "items": rows}


@app.get("/item/{name}")
@limiter.limit("60/minute")
def item_detail(request: Request, name: str):
    """
    Single-item view: every target/container combination this item has
    ever been seen dropping from, aggregated across the whole wiki (not
    scoped to one target) -- the counterpart to /rates/{target}/items,
    which goes the other direction (one target, all its items). Powers
    a dedicated per-item page the same way a wiki like the OSRS Wiki
    gives each item its own page cataloging every monster/activity that
    drops it, rather than only listing items under each monster's page.

    `name` is resolved case-insensitively against item_clusters, so a
    link built from any known spelling variant (or the current
    canonical name) lands on the same page.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        cluster = conn.execute(
            "SELECT canonical_name FROM item_clusters WHERE LOWER(canonical_name) = LOWER(?)",
            (name,),
        ).fetchone()
        if cluster is None:
            raise HTTPException(status_code=404, detail="No data for this item yet")
        canonical_name = cluster["canonical_name"]

        raw_sources = [
            {"target": row["target"], "chest_type": row["chest_type"], "count": row["n"]}
            for row in conn.execute(
                """
                SELECT s.target AS target, s.chest_type AS chest_type, COUNT(*) AS n
                FROM submissions s
                JOIN item_variants iv ON iv.raw_name = s.item_name
                JOIN item_clusters ic ON ic.id = iv.cluster_id
                WHERE ic.canonical_name = ?
                GROUP BY s.target, s.chest_type
                ORDER BY n DESC
                """,
                (canonical_name,),
            )
        ]
        if not raw_sources:
            raise HTTPException(status_code=404, detail="No data for this item yet")

        # rate_per_container matches the convention /rates/{target}/items
        # already uses: this source's count divided by ALL containers
        # opened for that target (not just ones of this chest_type), so
        # it reads as "chance of finding it doing anything at this
        # target," not "chance within this specific container type."
        totals_by_target = {s["target"]: _total_containers(conn, s["target"]) for s in raw_sources}
        sources = [
            {
                **s,
                "rate_per_container": (s["count"] / totals_by_target[s["target"]]) if totals_by_target[s["target"]] else None,
            }
            for s in raw_sources
        ]

        # The rarity actually stored per-row can vary in principle (color
        # sampling misfires, or a target-specific quirk) -- report the
        # single most-common value seen as this item's rarity, same
        # "majority wins" approach as item-name canonicalization.
        rarity_row = conn.execute(
            """
            SELECT s.item_rarity AS rarity, COUNT(*) AS n
            FROM submissions s
            JOIN item_variants iv ON iv.raw_name = s.item_name
            JOIN item_clusters ic ON ic.id = iv.cluster_id
            WHERE ic.canonical_name = ? AND s.item_rarity IS NOT NULL
            GROUP BY s.item_rarity ORDER BY n DESC LIMIT 1
            """,
            (canonical_name,),
        ).fetchone()
        rarity = rarity_row["rarity"] if rarity_row else None

        cluster_row = conn.execute(
            "SELECT is_known, stats_json, stats_confirmed FROM item_clusters WHERE canonical_name = ?",
            (canonical_name,),
        ).fetchone()
        is_known = cluster_row["is_known"]
        stats = json.loads(cluster_row["stats_json"]) if cluster_row["stats_json"] else None

    total_seen = sum(s["count"] for s in sources)
    return {
        "name": canonical_name,
        "rarity": rarity,
        "is_known_item": bool(is_known),
        "total_seen": total_seen,
        "sources": sources,
        "stats": stats,
        "stats_confirmed": bool(cluster_row["stats_confirmed"]),
    }


@app.get("/item_page/{name}")
def item_page(name: str):
    """Serves the per-item detail page (static/item.html) -- a single
    self-contained page that calls /item/{name} via fetch()."""
    return FileResponse(STATIC_DIR / "item.html")


# ---------------------------------------------------------------------------
# Admin: browse/fix item-name canonicalization (see _canonicalize_item_name)
# ---------------------------------------------------------------------------
# All three routes below require X-Admin-Token (see _require_admin). Static
# UI at /admin (static/admin.html) -- the page itself is public, but every
# fetch() it makes carries the token from a password field, so the page
# loading isn't itself a data leak.


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/admin/clusters")
@limiter.limit("30/minute")
def admin_list_clusters(
    request: Request,
    q: Optional[str] = None,
    target: Optional[str] = None,
    _admin: None = Depends(_require_admin),
):
    """
    Every item-name cluster with its variant spellings and per-variant
    counts, for a human to scan for mistakes -- either a split (the same
    real item ended up in two+ separate clusters because a fuzzy match
    fell just short of threshold) or a false merge (two different real
    items ended up in the same cluster because their spellings happened
    to be similar enough). `q` filters by substring match against either
    the canonical name or any variant spelling, case-insensitive.
    `target`, if given, restricts results to clusters that actually have
    at least one submission recorded for that target -- i.e. "show me
    just what's on General Hex's page" rather than every item ever seen
    across the whole wiki.
    """
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        clusters = {
            row["id"]: {
                "id": row["id"],
                "canonical_name": row["canonical_name"],
                "is_known": bool(row["is_known"]),
                "variants": [],
                "total_count": 0,
            }
            for row in conn.execute("SELECT id, canonical_name, is_known FROM item_clusters")
        }
        for row in conn.execute("SELECT raw_name, cluster_id, count FROM item_variants ORDER BY count DESC"):
            cluster = clusters.get(row["cluster_id"])
            if cluster is None:
                continue  # orphaned variant row -- shouldn't happen, but don't 500 over it
            cluster["variants"].append({"raw_name": row["raw_name"], "count": row["count"]})
            cluster["total_count"] += row["count"]

        if target:
            target_cluster_ids = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT DISTINCT iv.cluster_id
                    FROM submissions s
                    JOIN item_variants iv ON iv.raw_name = s.item_name
                    WHERE s.target = ?
                    """,
                    (target,),
                )
            }
            clusters = {cid: c for cid, c in clusters.items() if cid in target_cluster_ids}

    result = list(clusters.values())
    if q:
        ql = q.strip().lower()
        result = [
            c for c in result
            if ql in c["canonical_name"].lower() or any(ql in v["raw_name"].lower() for v in c["variants"])
        ]
    result.sort(key=lambda c: -c["total_count"])
    return {"clusters": result}


class MergeIn(BaseModel):
    keep_cluster_id: int
    merge_cluster_id: int
    canonical_name: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    mark_known: Optional[bool] = None


@app.post("/admin/merge")
@limiter.limit("30/minute")
def admin_merge(request: Request, body: MergeIn, _admin: None = Depends(_require_admin)):
    """
    Fixes a SPLIT: two clusters that are actually the same real item
    (e.g. a fuzzy match against a raw OCR spelling fell just short of
    FUZZY_MATCH_THRESHOLD and started its own cluster instead of joining
    the right one). Every variant under `merge_cluster_id` is reassigned
    to `keep_cluster_id` and the now-empty cluster is deleted.
    `canonical_name` optionally overrides the kept cluster's name (else
    it's left as whatever `keep_cluster_id` already had); `mark_known`
    optionally locks the result as authoritative (see is_known) so it
    can't be outvoted by a future popular misread.
    """
    if body.keep_cluster_id == body.merge_cluster_id:
        raise HTTPException(status_code=400, detail="cannot merge a cluster into itself")

    with _db() as conn:
        conn.row_factory = sqlite3.Row
        keep = conn.execute(
            "SELECT id, canonical_name, is_known FROM item_clusters WHERE id = ?", (body.keep_cluster_id,)
        ).fetchone()
        merge = conn.execute(
            "SELECT id, canonical_name, is_known FROM item_clusters WHERE id = ?", (body.merge_cluster_id,)
        ).fetchone()
        if keep is None or merge is None:
            raise HTTPException(status_code=404, detail="cluster not found")

        # raw_name is globally unique (PRIMARY KEY on item_variants), so
        # reassigning cluster_id can never collide with an existing row --
        # each raw spelling only ever belongs to exactly one cluster.
        conn.execute(
            "UPDATE item_variants SET cluster_id = ? WHERE cluster_id = ?",
            (body.keep_cluster_id, body.merge_cluster_id),
        )
        conn.execute("DELETE FROM item_clusters WHERE id = ?", (body.merge_cluster_id,))

        new_name = (body.canonical_name or "").strip() or keep["canonical_name"]
        new_known = keep["is_known"] or merge["is_known"] if body.mark_known is None else body.mark_known
        conn.execute(
            "UPDATE item_clusters SET canonical_name = ?, is_known = ? WHERE id = ?",
            (new_name, 1 if new_known else 0, body.keep_cluster_id),
        )

    return {"status": "merged", "kept_cluster_id": body.keep_cluster_id, "canonical_name": new_name}


class RenameIn(BaseModel):
    cluster_id: int
    canonical_name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    mark_known: Optional[bool] = None


@app.post("/admin/rename")
@limiter.limit("30/minute")
def admin_rename(request: Request, body: RenameIn, _admin: None = Depends(_require_admin)):
    """
    Overrides a cluster's canonical spelling directly -- for a self-
    learned cluster that converged on a still-wrong spelling (e.g. two
    similarly-popular misreads), or to correct/relock a KNOWN_ITEMS
    match that was itself wrong. `mark_known` (if given) sets whether
    the result is locked as authoritative going forward.
    """
    with _db() as conn:
        row = conn.execute("SELECT id, is_known FROM item_clusters WHERE id = ?", (body.cluster_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="cluster not found")
        new_known = row[1] if body.mark_known is None else (1 if body.mark_known else 0)
        conn.execute(
            "UPDATE item_clusters SET canonical_name = ?, is_known = ? WHERE id = ?",
            (body.canonical_name.strip(), new_known, body.cluster_id),
        )
    return {"status": "renamed"}


class MoveVariantIn(BaseModel):
    raw_name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    target_cluster_id: Optional[int] = None
    new_canonical_name: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)


@app.post("/admin/move_variant")
@limiter.limit("30/minute")
def admin_move_variant(request: Request, body: MoveVariantIn, _admin: None = Depends(_require_admin)):
    """
    Fixes a FALSE MERGE: pulls one raw spelling out of whatever cluster
    it's currently in, either into an existing `target_cluster_id` (two
    genuinely different items were wrongly merged; separate this one back
    out) or into a brand-new cluster of its own named
    `new_canonical_name` (exactly one of the two must be given).
    """
    if (body.target_cluster_id is None) == (not body.new_canonical_name):
        raise HTTPException(
            status_code=400,
            detail="give exactly one of target_cluster_id or new_canonical_name",
        )

    with _db() as conn:
        variant = conn.execute(
            "SELECT raw_name FROM item_variants WHERE raw_name = ?", (body.raw_name,)
        ).fetchone()
        if variant is None:
            raise HTTPException(status_code=404, detail="variant not found")

        if body.target_cluster_id is not None:
            target = conn.execute(
                "SELECT id FROM item_clusters WHERE id = ?", (body.target_cluster_id,)
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail="target cluster not found")
            cluster_id = body.target_cluster_id
        else:
            cursor = conn.execute(
                "INSERT INTO item_clusters (canonical_name, is_known) VALUES (?, 0)",
                (body.new_canonical_name.strip(),),
            )
            cluster_id = cursor.lastrowid

        conn.execute(
            "UPDATE item_variants SET cluster_id = ? WHERE raw_name = ?", (cluster_id, body.raw_name)
        )

    return {"status": "moved", "raw_name": body.raw_name, "cluster_id": cluster_id}


class DeleteItemIn(BaseModel):
    cluster_id: int


@app.post("/admin/delete_item")
@limiter.limit("30/minute")
def admin_delete_item(request: Request, body: DeleteItemIn, _admin: None = Depends(_require_admin)):
    """
    Removes a cluster that isn't a real item at all -- pure OCR noise
    (nameplate bleed, a stray fragment) rather than a misspelling of
    something real, so there's no correct spelling to rename it to.

    Every `submissions` row currently pointing at one of this cluster's
    raw spellings has item_name/item_rarity set to NULL rather than
    being deleted outright -- the chest itself was still genuinely
    opened (it should keep counting toward that target's total
    containers/kills for rate math), it just didn't actually contain
    this fabricated item. This is the same state a chest with zero real
    items already stores (see /submit's docstring). The cluster and its
    variant rows are then removed entirely so it stops showing up
    anywhere.
    """
    with _db() as conn:
        cluster = conn.execute(
            "SELECT id FROM item_clusters WHERE id = ?", (body.cluster_id,)
        ).fetchone()
        if cluster is None:
            raise HTTPException(status_code=404, detail="cluster not found")

        raw_names = [
            row[0]
            for row in conn.execute(
                "SELECT raw_name FROM item_variants WHERE cluster_id = ?", (body.cluster_id,)
            )
        ]
        for raw_name in raw_names:
            conn.execute(
                "UPDATE submissions SET item_name = NULL, item_rarity = NULL WHERE item_name = ?",
                (raw_name,),
            )
        conn.execute("DELETE FROM item_variants WHERE cluster_id = ?", (body.cluster_id,))
        conn.execute("DELETE FROM item_clusters WHERE id = ?", (body.cluster_id,))

    return {"status": "deleted", "cluster_id": body.cluster_id, "cleared_variants": raw_names}
