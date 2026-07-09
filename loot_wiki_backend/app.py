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

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

DB_PATH = Path(__file__).parent / "loot_wiki.db"
STATIC_DIR = Path(__file__).parent / "static"

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


class ItemIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    rarity: Optional[str] = None

    @field_validator("rarity")
    @classmethod
    def rarity_must_be_known(cls, v):
        if v not in ALLOWED_RARITIES:
            raise ValueError(f"unknown rarity {v!r}")
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


_init_db()


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
    rows = [
        (submission_id, submission.anon_id, submission.target, submission.chest_type,
         item.name, item.rarity, submission.gold,
         submission.kills_since_last_container, submission.skull_chest_number,
         submission.kill_tracking, now)
        for item in submission.items
    ] or [
        (submission_id, submission.anon_id, submission.target, submission.chest_type,
         None, None, submission.gold,
         submission.kills_since_last_container, submission.skull_chest_number,
         submission.kill_tracking, now)
    ]

    with _db() as conn:
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
                SELECT item_name, item_rarity, COUNT(*) AS n
                FROM submissions
                WHERE target = ? AND item_name IS NOT NULL AND item_rarity IN ('Famed', 'Legendary')
                GROUP BY item_name, item_rarity
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
                SELECT item_name, item_rarity, COUNT(*) AS n
                FROM submissions
                WHERE target = ? AND item_name IS NOT NULL
                GROUP BY item_name, item_rarity
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
                SELECT DISTINCT item_name, item_rarity
                FROM submissions WHERE target = ? AND item_name IS NOT NULL
                """,
                (target,),
            )
        ]
    rows.sort(key=lambda r: (rarity_order.get(r["rarity"], 99), r["name"]))

    return {"target": target, "items": rows}
