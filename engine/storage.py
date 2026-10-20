"""SQLite: найденные материалы, очередь постов, состояние каналов."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    body TEXT,
    image TEXT,
    source TEXT,
    weight REAL DEFAULT 1,
    use_images INTEGER DEFAULT 0,
    fetch_full_text INTEGER DEFAULT 0,
    published_ts REAL,
    fetched_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',   -- new | rejected | used | duplicate
    reason TEXT,
    UNIQUE(channel, key)
);
CREATE INDEX IF NOT EXISTS items_ch_status ON items(channel, status);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    item_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'news',    -- news | promo
    text TEXT NOT NULL,
    image TEXT,
    status TEXT NOT NULL,                 -- queued | pending_review | rejected | published | failed
    created_ts REAL NOT NULL,
    published_ts REAL,
    message_id INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS posts_ch_status ON posts(channel, status);
CREATE TABLE IF NOT EXISTS state (
    channel TEXT NOT NULL,
    k TEXT NOT NULL,
    v TEXT,
    PRIMARY KEY(channel, k)
);
CREATE TABLE IF NOT EXISTS llm_calls (ts REAL NOT NULL);
"""


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        if "body" not in cols:
            self.db.execute("ALTER TABLE items ADD COLUMN body TEXT")

    # --- items ---
    def add_item(self, channel: str, it: dict) -> bool:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO items(channel,key,title,title_norm,url,summary,body,image,source,
               weight,use_images,fetch_full_text,published_ts,fetched_ts,status,reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (channel, it["key"], it["title"], it["title_norm"], it.get("url"), it.get("summary"), it.get("body"),
             it.get("image"), it.get("source"), it.get("weight", 1.0), int(it.get("use_images", False)),
             int(it.get("fetch_full_text", False)), it.get("published_ts"), time.time(),
             it.get("status", "new"), it.get("reason")),
        )
        self.db.commit()
        return cur.rowcount > 0

    def recent_titles(self, channel: str, hours: float = 72) -> list[str]:
        rows = self.db.execute(
            "SELECT title_norm FROM items WHERE channel=? AND fetched_ts>? AND status!='duplicate'",
            (channel, time.time() - hours * 3600)).fetchall()
        return [r[0] for r in rows]

    def new_in_order(self, channel: str, limit: int = 50) -> list[sqlite3.Row]:
        """Для relay: новые материалы в порядке выхода."""
        return self.db.execute(
            "SELECT * FROM items WHERE channel=? AND status='new' "
            "ORDER BY COALESCE(published_ts, fetched_ts), id LIMIT ?", (channel, limit)).fetchall()

    def candidates(self, channel: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM items WHERE channel=? AND status='new' "
            "ORDER BY weight * (1.0 / (1 + (? - COALESCE(published_ts, fetched_ts)) / 3600.0)) DESC LIMIT ?",
            (channel, time.time(), limit)).fetchall()

    def set_item_status(self, item_id: int, status: str, reason: str | None = None):
        self.db.execute("UPDATE items SET status=?, reason=? WHERE id=?", (status, reason, item_id))
        self.db.commit()

    def expire_old(self, channel: str, max_age_hours: float):
        self.db.execute(
            "UPDATE items SET status='rejected', reason='too old' WHERE channel=? AND status='new' "
            "AND COALESCE(published_ts, fetched_ts) < ?", (channel, time.time() - max_age_hours * 3600))
        self.db.commit()

    # --- posts ---
    def add_post(self, channel: str, text: str, status: str, item_id: int | None = None,
                 image: str | None = None, kind: str = "news") -> int:
        cur = self.db.execute(
            "INSERT INTO posts(channel,item_id,kind,text,image,status,created_ts) VALUES(?,?,?,?,?,?,?)",
            (channel, item_id, kind, text, image, status, time.time()))
        self.db.commit()
        return cur.lastrowid

    def count_posts(self, channel: str, status: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM posts WHERE channel=? AND status=?",
                               (channel, status)).fetchone()[0]

    def next_queued(self, channel: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM posts WHERE channel=? AND status='queued' ORDER BY created_ts LIMIT 1",
            (channel,)).fetchone()

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def set_post_status(self, post_id: int, status: str, message_id: int | None = None,
                        error: str | None = None):
        ts = time.time() if status == "published" else None
        self.db.execute(
            "UPDATE posts SET status=?, message_id=COALESCE(?,message_id), error=?, "
            "published_ts=COALESCE(?,published_ts) WHERE id=?",
            (status, message_id, error, ts, post_id))
        self.db.commit()

    def published_since(self, channel: str, since_ts: float, kind: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM posts WHERE channel=? AND status='published' AND published_ts>=?"
        args: list = [channel, since_ts]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return self.db.execute(q, args).fetchone()[0]

    def last_published_ts(self, channel: str, kind: str | None = None) -> float | None:
        q = "SELECT MAX(published_ts) FROM posts WHERE channel=? AND status='published'"
        args: list = [channel]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return self.db.execute(q, args).fetchone()[0]

    def expire_queue(self, channel: str, max_age_hours: float):
        """Новости протухают: не публикуем пост, который пролежал в очереди слишком долго."""
        self.db.execute(
            "UPDATE posts SET status='rejected', error='stale' WHERE channel=? AND kind='news' "
            "AND status IN ('queued','pending_review') AND created_ts<?",
            (channel, time.time() - max_age_hours * 3600))
        self.db.commit()

    # --- state ---
    def get_state(self, channel: str, k: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT v FROM state WHERE channel=? AND k=?", (channel, k)).fetchone()
        return r[0] if r else default

    def set_state(self, channel: str, k: str, v: str | None):
        self.db.execute("INSERT INTO state(channel,k,v) VALUES(?,?,?) "
                        "ON CONFLICT(channel,k) DO UPDATE SET v=excluded.v", (channel, k, v))
        self.db.commit()

    # --- бюджет LLM ---
    def llm_calls_last_day(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM llm_calls WHERE ts>?",
                               (time.time() - 86400,)).fetchone()[0]

    def log_llm_call(self):
        self.db.execute("INSERT INTO llm_calls(ts) VALUES(?)", (time.time(),))
        self.db.execute("DELETE FROM llm_calls WHERE ts<?", (time.time() - 3 * 86400,))
        self.db.commit()

    def stats(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT channel, status, COUNT(*) n FROM posts GROUP BY channel, status ORDER BY channel").fetchall()
