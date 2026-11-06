"""SQLite storage: themes, topics, sources, raw items, cards and feedback events."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    weight REAL DEFAULT 1.0,
    paused INTEGER DEFAULT 0,
    created REAL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    theme_id INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    keywords TEXT DEFAULT '[]',      -- JSON list, used to search sources
    kind TEXT DEFAULT 'news',        -- news: fed by sources, time-sensitive | series: one-off set of written cards
    weight REAL DEFAULT 1.0,
    paused INTEGER DEFAULT 0,
    created REAL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- rss | hn | github | collection
    url TEXT NOT NULL UNIQUE,
    name TEXT DEFAULT '',
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    enabled INTEGER DEFAULT 1,
    last_fetched REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    norm_title TEXT,
    summary TEXT DEFAULT '',
    text TEXT DEFAULT '',
    published REAL,
    fetched REAL,
    source_id INTEGER,
    origin TEXT DEFAULT 'feed',      -- feed | collection | backfill
    status TEXT DEFAULT 'new',       -- new | ready | carded | rejected | failed
    score REAL,
    reason TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    topic_hint INTEGER               -- topic a backfill search found it for; its cards go there
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    points TEXT NOT NULL,            -- JSON list of sentences
    takeaway TEXT DEFAULT '',
    score REAL DEFAULT 5,
    evergreen INTEGER DEFAULT 0,
    created REAL,
    last_shown REAL DEFAULT 0,
    shown_count INTEGER DEFAULT 0,
    reaction INTEGER DEFAULT 0,      -- 1 liked, -1 disliked
    image TEXT DEFAULT ''            -- optional picture, a /static/... path (road signs)
);
CREATE INDEX IF NOT EXISTS cards_shown ON cards(shown_count, created);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    card_id INTEGER,
    type TEXT NOT NULL,              -- view | like | dislike
    dwell_ms INTEGER DEFAULT 0,
    ts REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_local = threading.local()
_write_lock = threading.Lock()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


@contextmanager
def tx():
    """Serialise writes across threads; commit on success."""
    with _write_lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        if "topic_hint" not in cols:  # databases created before 2026-11-03 14:30
            c.execute("ALTER TABLE items ADD COLUMN topic_hint INTEGER")
        if "image" not in {r["name"] for r in c.execute("PRAGMA table_info(cards)")}:  # added 2026-11-04
            c.execute("ALTER TABLE cards ADD COLUMN image TEXT DEFAULT ''")
        if "kind" not in {r["name"] for r in c.execute("PRAGMA table_info(topics)")}:  # added 2026-11-06
            c.execute("ALTER TABLE topics ADD COLUMN kind TEXT DEFAULT 'news'")
            # Topics whose cards were all written in a session, with no feed of their own, are series.
            c.execute("UPDATE topics SET kind='series' WHERE id NOT IN (SELECT topic_id FROM sources WHERE topic_id IS NOT NULL)"
                      " AND (SELECT COUNT(*) FROM cards k WHERE k.topic_id=topics.id AND k.item_id IS NULL)"
                      " > 10 * (SELECT COUNT(*) FROM cards k WHERE k.topic_id=topics.id AND k.item_id IS NOT NULL)"
                      " AND (SELECT COUNT(*) FROM cards k WHERE k.topic_id=topics.id AND k.evergreen=1) > 0")


def rows(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def get_meta(key: str, default=None):
    r = one("SELECT value FROM meta WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_meta(key: str, value) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))


def now() -> float:
    return time.time()
