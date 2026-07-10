"""
app.py
Kraken's Ledger -- ingestion backend for TLOPO_Tracker's automated,
opt-in research-observation submissions (main-debug branch).

Separate from loot_wiki_backend/ (the older, simpler per-chest drop-rate
submission service). Kraken's Ledger stores the RICHER observation
stream main-debug produces -- kill/loot linkage, capture quality,
confidence tiers, category groups -- using the same normalized schema
exporter.export_to_sqlite() writes locally, so a submission payload is
literally enrichment.enrich_events(session)'s own output.

Deliberately has NO concept of player identity -- only a random,
per-install anon_id (not tied to a name/account, and distinct from
loot_wiki's own anon_id so the two systems' anonymized IDs aren't
trivially cross-linkable), purely so bursty/duplicate submissions from
one install can be told apart from independent ones in aggregate. There
is no login, no session, and nothing here can be traced back to a
specific player or character.

Run locally for development:
    pip install -r requirements.txt
    uvicorn app:app --reload

See README.md for deployment notes.
"""

import csv
import io
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

DB_PATH = Path(__file__).parent / "kraken_ledger.db"
STATIC_DIR = Path(__file__).parent / "static"

# Loose sanity bounds -- not trying to validate against the exact
# known-boss/rarity/category lists client-side code uses (those can
# change independently of this backend), just rejecting obviously
# garbage/abusive payloads. Same defensive posture as loot_wiki_backend.
MAX_NAME_LEN = 200
MAX_ITEMS_PER_EVENT = 30
MAX_EVENTS_PER_BATCH = 500
ALLOWED_EVENT_TYPES = {"kill", "chest"}


class ItemIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    rarity: Optional[str] = None
    category: Optional[str] = None
    category_group: Optional[str] = None
    name_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    confidence_tier: Optional[str] = None


class ObservationIn(BaseModel):
    observation_id: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    event_type: str
    timestamp: Optional[str] = None
    target: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    enemy_id: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    enemy_color: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    location: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    kill_number: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    capture_quality: Optional[str] = None
    chest_type: Optional[str] = None
    gold: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    associated_kill_number: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    linked_kill_observation_id: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    link_status: Optional[str] = None  # "linked" / "unlinked" / "ambiguous" -- see enrichment.enrich_events
    session_id: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)  # chest correlation id
    items: List[ItemIn] = Field(default_factory=list, max_length=MAX_ITEMS_PER_EVENT)

    @field_validator("event_type")
    @classmethod
    def event_type_must_be_known(cls, v):
        if v not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"event_type must be one of {sorted(ALLOWED_EVENT_TYPES)}")
        return v

    @field_validator("target", "enemy_color", "location")
    @classmethod
    def no_control_chars(cls, v):
        if v is not None and re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", v):
            raise ValueError("field contains invalid characters")
        return v.strip() if v else v


class BatchIn(BaseModel):
    anon_id: str
    session_id: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    session_start: float
    events: List[ObservationIn] = Field(default_factory=list, max_length=MAX_EVENTS_PER_BATCH)

    @field_validator("anon_id")
    @classmethod
    def anon_id_must_be_uuid(cls, v):
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("anon_id must be a valid UUID")
        return v


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Kraken's Ledger")
app.state.limiter = limiter
# See loot_wiki_backend/app.py's identical comment -- SlowAPIMiddleware
# doesn't actually invoke a custom exception handler for RateLimitExceeded
# raised inside a route; slowapi's own handler is required.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Prefixed "/assets" (not "/static") so it can never collide with a
# future plain-name API route the way mounting at "/static" or "/" would.
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent-write behavior for many simultaneous submitters
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions(
                session_id TEXT PRIMARY KEY,
                anon_id TEXT NOT NULL,
                session_start REAL,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS observations(
                observation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                event_type TEXT NOT NULL,
                timestamp TEXT,
                target TEXT,
                enemy_id TEXT,
                enemy_color TEXT,
                location TEXT,
                kill_number INTEGER,
                capture_quality TEXT,
                chest_type TEXT,
                gold INTEGER,
                associated_kill_number INTEGER,
                linked_kill_observation_id TEXT,
                link_status TEXT,
                chest_correlation_id TEXT,
                received_at REAL NOT NULL
            )
        """)
        # Migration for DBs created before link_status existed -- ADD
        # COLUMN has no "IF NOT EXISTS" guard before SQLite 3.35, and the
        # instance's exact version isn't guaranteed, so just swallow the
        # "duplicate column" error on a re-run instead of version-sniffing.
        try:
            conn.execute("ALTER TABLE observations ADD COLUMN link_status TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id TEXT NOT NULL REFERENCES observations(observation_id),
                item_name TEXT,
                rarity TEXT,
                category TEXT,
                category_group TEXT,
                name_confidence REAL,
                confidence_tier TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_enemy ON observations(enemy_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_obs ON items(observation_id)")


_init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/submit_batch")
@limiter.limit("20/minute")
def submit_batch(request: Request, batch: BatchIn):
    """
    Upserts a session's current full observation stream. Idempotent by
    design: the client resends its ENTIRE current event list on every
    autosave tick rather than tracking what's already been sent, so this
    always does INSERT OR REPLACE keyed by observation_id/session_id --
    safe to call repeatedly with overlapping data, safe against a
    dropped connection or client restart (the next tick just resends
    everything). See TLOPO_Tracker's kraken_ledger_client.py.
    """
    now = time.time()

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, anon_id, session_start, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (batch.session_id, batch.anon_id, batch.session_start, now, now),
        )

        for event in batch.events:
            conn.execute(
                """
                INSERT OR REPLACE INTO observations
                    (observation_id, session_id, event_type, timestamp, target, enemy_id,
                     enemy_color, location, kill_number, capture_quality, chest_type, gold,
                     associated_kill_number, linked_kill_observation_id, link_status,
                     chest_correlation_id, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.observation_id, batch.session_id, event.event_type, event.timestamp,
                    event.target, event.enemy_id, event.enemy_color, event.location,
                    event.kill_number, event.capture_quality, event.chest_type, event.gold,
                    event.associated_kill_number, event.linked_kill_observation_id,
                    event.link_status, event.session_id, now,
                ),
            )
            # Replace this observation's items wholesale rather than trying
            # to diff -- simplest correct behavior for a resend of the same
            # (possibly item-list-unchanged, possibly amended) observation.
            conn.execute("DELETE FROM items WHERE observation_id = ?", (event.observation_id,))
            if event.items:
                conn.executemany(
                    """
                    INSERT INTO items
                        (observation_id, item_name, rarity, category, category_group,
                         name_confidence, confidence_tier)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (event.observation_id, item.name, item.rarity, item.category,
                         item.category_group, item.name_confidence, item.confidence_tier)
                        for item in event.items
                    ],
                )

    return {"status": "recorded", "events_recorded": len(batch.events)}


# ---------------------------------------------------------------------------
# Read/browse API + static viewer -- added 2026-07-10. Kraken's Ledger
# previously had no way to see submitted data at all (write-only via
# /submit_batch); this is the read side, following the same "self-
# contained static HTML page calling this API via fetch()" pattern as
# loot_wiki_backend/.
# ---------------------------------------------------------------------------

RARITY_ORDER = ["Legendary", "Famed", "Rare", "Common", "Crude"]


def _row_to_dict(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def _enemy_display_name(conn, enemy_id: str) -> str:
    """Enemy_id has no separate name table server-side -- just the most
    recently-seen `target` OCR string reported for it."""
    row = conn.execute(
        "SELECT target FROM observations WHERE enemy_id = ? AND target IS NOT NULL "
        "ORDER BY received_at DESC LIMIT 1",
        (enemy_id,),
    ).fetchone()
    return row[0] if row else enemy_id


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/enemy_page/{enemy_id}")
def enemy_page(enemy_id: str):
    return FileResponse(STATIC_DIR / "enemy.html")


@app.get("/sessions_page")
def sessions_page():
    return FileResponse(STATIC_DIR / "sessions.html")


@app.get("/session_page/{session_id}")
def session_page(session_id: str):
    return FileResponse(STATIC_DIR / "session.html")


@app.get("/overview")
def overview():
    with _db() as conn:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_installs = conn.execute("SELECT COUNT(DISTINCT anon_id) FROM sessions").fetchone()[0]
        total_kills = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE event_type = 'kill'"
        ).fetchone()[0]
        total_chests = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE event_type = 'chest'"
        ).fetchone()[0]
        total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        unique_enemies = conn.execute(
            "SELECT COUNT(DISTINCT enemy_id) FROM observations WHERE enemy_id IS NOT NULL"
        ).fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(received_at), MAX(received_at) FROM observations"
        ).fetchone()
        link_stats = conn.execute(
            """
            SELECT
                SUM(CASE WHEN link_status = 'linked' THEN 1 ELSE 0 END),
                SUM(CASE WHEN link_status = 'ambiguous' THEN 1 ELSE 0 END),
                SUM(CASE WHEN link_status = 'unlinked' OR link_status IS NULL THEN 1 ELSE 0 END)
            FROM observations WHERE event_type = 'chest'
            """
        ).fetchone()
    return {
        "total_sessions": total_sessions,
        "total_installs": total_installs,
        "total_kills": total_kills,
        "total_chests": total_chests,
        "total_items": total_items,
        "unique_enemies": unique_enemies,
        "first_received_at": date_range[0],
        "last_received_at": date_range[1],
        "chest_link_status": {
            "linked": link_stats[0] or 0,
            "ambiguous": link_stats[1] or 0,
            "unlinked": link_stats[2] or 0,
        },
    }


@app.get("/enemies")
def list_enemies():
    """One row per enemy_id with aggregate counts -- feeds the dashboard
    table. Kept as a single GROUP BY query (not N+1 per-enemy queries)
    since this runs on every dashboard load."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT
                o.enemy_id,
                COUNT(DISTINCT CASE WHEN o.event_type = 'kill' THEN o.observation_id END) AS kills,
                COUNT(DISTINCT CASE WHEN o.event_type = 'chest' THEN o.observation_id END) AS chests,
                COUNT(DISTINCT CASE WHEN o.event_type = 'kill' AND o.capture_quality = 'Has Loot'
                      THEN o.observation_id END) AS kills_with_loot,
                COUNT(DISTINCT CASE WHEN o.event_type = 'chest' AND o.link_status = 'linked'
                      THEN o.observation_id END) AS chests_linked,
                COUNT(DISTINCT o.session_id) AS sessions,
                COUNT(DISTINCT i.id) AS items_recorded
            FROM observations o
            LEFT JOIN items i ON i.observation_id = o.observation_id
            WHERE o.enemy_id IS NOT NULL
            GROUP BY o.enemy_id
            ORDER BY kills DESC, chests DESC
            """
        ).fetchall()
        enemies = []
        for row in rows:
            d = dict(zip(
                ["enemy_id", "kills", "chests", "kills_with_loot", "chests_linked", "sessions", "items_recorded"],
                row,
            ))
            d["display_name"] = _enemy_display_name(conn, d["enemy_id"])
            enemies.append(d)
    return {"enemies": enemies}


@app.get("/enemies/{enemy_id}")
def enemy_detail(enemy_id: str):
    with _db() as conn:
        display_name = _enemy_display_name(conn, enemy_id)
        totals = conn.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN event_type = 'kill' THEN observation_id END),
                COUNT(DISTINCT CASE WHEN event_type = 'chest' THEN observation_id END),
                COUNT(DISTINCT session_id)
            FROM observations WHERE enemy_id = ?
            """,
            (enemy_id,),
        ).fetchone()
        if totals[0] == 0 and totals[1] == 0:
            raise HTTPException(status_code=404, detail="No data recorded for this enemy_id")

        capture_quality = conn.execute(
            """
            SELECT capture_quality, COUNT(*) FROM observations
            WHERE enemy_id = ? AND event_type = 'kill'
            GROUP BY capture_quality
            """,
            (enemy_id,),
        ).fetchall()

        link_status = conn.execute(
            """
            SELECT COALESCE(link_status, 'unlinked'), COUNT(*) FROM observations
            WHERE enemy_id = ? AND event_type = 'chest'
            GROUP BY 1
            """,
            (enemy_id,),
        ).fetchall()

        chest_types = conn.execute(
            """
            SELECT chest_type, COUNT(*) FROM observations
            WHERE enemy_id = ? AND event_type = 'chest' AND chest_type IS NOT NULL
            GROUP BY chest_type
            """,
            (enemy_id,),
        ).fetchall()

        rarities = conn.execute(
            """
            SELECT i.rarity, COUNT(*) FROM items i
            JOIN observations o ON o.observation_id = i.observation_id
            WHERE o.enemy_id = ? AND i.rarity IS NOT NULL
            GROUP BY i.rarity
            """,
            (enemy_id,),
        ).fetchall()

        top_items = conn.execute(
            """
            SELECT i.item_name, i.rarity, COUNT(*) AS drops,
                   AVG(i.name_confidence) AS avg_confidence
            FROM items i
            JOIN observations o ON o.observation_id = i.observation_id
            WHERE o.enemy_id = ? AND i.item_name IS NOT NULL
            GROUP BY i.item_name, i.rarity
            ORDER BY drops DESC
            LIMIT 100
            """,
            (enemy_id,),
        ).fetchall()

        confidence_tiers = conn.execute(
            """
            SELECT i.confidence_tier, COUNT(*) FROM items i
            JOIN observations o ON o.observation_id = i.observation_id
            WHERE o.enemy_id = ? AND i.confidence_tier IS NOT NULL
            GROUP BY i.confidence_tier
            """,
            (enemy_id,),
        ).fetchall()

        recent_sessions = conn.execute(
            """
            SELECT DISTINCT session_id FROM observations
            WHERE enemy_id = ? ORDER BY session_id DESC LIMIT 20
            """,
            (enemy_id,),
        ).fetchall()

    return {
        "enemy_id": enemy_id,
        "display_name": display_name,
        "total_kills": totals[0],
        "total_chests": totals[1],
        "total_sessions": totals[2],
        "capture_quality": dict(capture_quality),
        "link_status": dict(link_status),
        "chest_types": dict(chest_types),
        "rarities": dict(rarities),
        "confidence_tiers": dict(confidence_tiers),
        "top_items": [
            {"item_name": r[0], "rarity": r[1], "drops": r[2], "avg_confidence": r[3]}
            for r in top_items
        ],
        "recent_sessions": [r[0] for r in recent_sessions],
    }


@app.get("/sessions")
def list_sessions(limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        rows = conn.execute(
            """
            SELECT
                s.session_id, s.anon_id, s.session_start, s.first_seen_at, s.last_seen_at,
                COUNT(DISTINCT CASE WHEN o.event_type = 'kill' THEN o.observation_id END) AS kills,
                COUNT(DISTINCT CASE WHEN o.event_type = 'chest' THEN o.observation_id END) AS chests,
                COUNT(DISTINCT o.enemy_id) AS enemies_encountered
            FROM sessions s
            LEFT JOIN observations o ON o.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.last_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    sessions = [
        {
            "session_id": r[0], "anon_id": r[1], "session_start": r[2],
            "first_seen_at": r[3], "last_seen_at": r[4],
            "kills": r[5], "chests": r[6], "enemies_encountered": r[7],
        }
        for r in rows
    ]
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


@app.get("/sessions/{session_id}")
def session_detail(session_id: str):
    with _db() as conn:
        session_row = conn.execute(
            "SELECT session_id, anon_id, session_start, first_seen_at, last_seen_at "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session_row is None:
            raise HTTPException(status_code=404, detail="Unknown session_id")

        obs_rows = conn.execute(
            """
            SELECT observation_id, event_type, timestamp, target, enemy_id, enemy_color,
                   location, kill_number, capture_quality, chest_type, gold,
                   associated_kill_number, linked_kill_observation_id, link_status
            FROM observations WHERE session_id = ?
            ORDER BY received_at ASC
            """,
            (session_id,),
        ).fetchall()

        obs_cols = ["observation_id", "event_type", "timestamp", "target", "enemy_id",
                    "enemy_color", "location", "kill_number", "capture_quality", "chest_type",
                    "gold", "associated_kill_number", "linked_kill_observation_id", "link_status"]
        events = []
        for row in obs_rows:
            event = dict(zip(obs_cols, row))
            item_rows = conn.execute(
                "SELECT item_name, rarity, category, category_group, name_confidence, confidence_tier "
                "FROM items WHERE observation_id = ?",
                (event["observation_id"],),
            ).fetchall()
            event["items"] = [
                {
                    "item_name": r[0], "rarity": r[1], "category": r[2],
                    "category_group": r[3], "name_confidence": r[4], "confidence_tier": r[5],
                }
                for r in item_rows
            ]
            events.append(event)

    return {
        "session_id": session_row[0],
        "anon_id": session_row[1],
        "session_start": session_row[2],
        "first_seen_at": session_row[3],
        "last_seen_at": session_row[4],
        "events": events,
    }


@app.get("/report.csv")
def report_csv(
    scope: str = "observations",
    enemy_id: Optional[str] = None,
    session_id: Optional[str] = None,
    event_type: Optional[str] = None,
):
    """
    Generates a downloadable CSV for either raw observations or the item
    rows joined with their parent observation's context, optionally
    filtered by enemy/session/event type -- the "generate reports" half
    of the viewer. Streams straight from a cursor rather than building
    the whole table in memory first, so a large export doesn't spike
    server memory.
    """
    if scope not in ("observations", "items"):
        raise HTTPException(status_code=400, detail="scope must be 'observations' or 'items'")
    if event_type is not None and event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"event_type must be one of {sorted(ALLOWED_EVENT_TYPES)}")

    conditions, params = [], []
    if enemy_id:
        conditions.append("o.enemy_id = ?")
        params.append(enemy_id)
    if session_id:
        conditions.append("o.session_id = ?")
        params.append(session_id)
    if event_type:
        conditions.append("o.event_type = ?")
        params.append(event_type)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    if scope == "observations":
        columns = [
            "observation_id", "session_id", "event_type", "timestamp", "target", "enemy_id",
            "enemy_color", "location", "kill_number", "capture_quality", "chest_type", "gold",
            "associated_kill_number", "linked_kill_observation_id", "link_status",
            "chest_correlation_id", "received_at",
        ]
        query = f"SELECT {', '.join('o.' + c for c in columns)} FROM observations o {where_clause} ORDER BY o.received_at"
    else:
        columns = [
            "observation_id", "session_id", "event_type", "target", "enemy_id", "timestamp",
            "chest_type", "gold", "item_name", "rarity", "category", "category_group",
            "name_confidence", "confidence_tier",
        ]
        query = f"""
            SELECT o.observation_id, o.session_id, o.event_type, o.target, o.enemy_id, o.timestamp,
                   o.chest_type, o.gold, i.item_name, i.rarity, i.category, i.category_group,
                   i.name_confidence, i.confidence_tier
            FROM items i JOIN observations o ON o.observation_id = i.observation_id
            {where_clause}
            ORDER BY o.received_at
        """

    def _generate():
        conn = sqlite3.connect(DB_PATH)
        try:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(columns)
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

            cur = conn.execute(query, params)
            for row in cur:
                writer.writerow(row)
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
        finally:
            conn.close()

    filename = f"kraken_ledger_{scope}" + (f"_{enemy_id}" if enemy_id else "") + ".csv"
    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
