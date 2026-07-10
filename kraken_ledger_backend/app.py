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

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

DB_PATH = Path(__file__).parent / "kraken_ledger.db"

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
                chest_correlation_id TEXT,
                received_at REAL NOT NULL
            )
        """)
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
                     associated_kill_number, linked_kill_observation_id, chest_correlation_id,
                     received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.observation_id, batch.session_id, event.event_type, event.timestamp,
                    event.target, event.enemy_id, event.enemy_color, event.location,
                    event.kill_number, event.capture_quality, event.chest_type, event.gold,
                    event.associated_kill_number, event.linked_kill_observation_id,
                    event.session_id, now,
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
