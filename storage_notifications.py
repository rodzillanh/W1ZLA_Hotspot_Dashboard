"""SQLite-backed persistence for the Notifications card's five independent
sources (APRS Messages, HamAlert, Fleet online/offline, geomagnetic-storm
alerts, Brandmeister favorite activity). Each source already keeps its own
bounded in-memory deque(maxlen=N) -- this module exists purely so that
history survives an app restart, not to change what's shown or how many;
each source keeps its own existing cap (aprs_inbox.py/hamalert.py/
hf_conditions.py/brandmeister_lastheard.py all cap at 50, monitor.py's
fleet events at 100) rather than one shared pooled budget, so a chatty
source can't crowd out a quiet one.

Same "own db file, connection-per-call" shape as storage_activity.py, kept
in a separate file/table since this is a different concern (external
notification events, not fleet talker activity used for the Fleet
Activity/Top 5 cards).

The payload for each event is stored as an opaque JSON blob -- the five
sources' entry dicts have genuinely different shapes (aprs: from/to/
message_text/..., fleet: ip/name/kind/..., solar: kind/k_index/..., etc.),
so a single source/at/payload table avoids either five separate tables or
one wide table full of columns that are NULL for four out of five sources.
Each source's own module is still the only thing that interprets its own
payload shape -- this module never looks inside it.
"""
import json
import os
import sqlite3
import threading

import config

DB_PATH = os.path.join(config.CONFIG_DIR, "notifications.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_source_at ON notifications(source, at);
"""

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def log_notification(source: str, at: float, payload: dict, limit: int) -> None:
    """Insert one event and prune that SAME source back down to `limit`
    rows, in the same call -- same "insert then inline-prune, no separate
    cleanup job" shape as storage_activity.py's log_activity(). Never
    raises: a failed write here must never take down the listener thread
    calling it (aprs_inbox/hamalert/brandmeister_lastheard all run their
    own background thread with no supervisor to catch this), matching
    every other integration's degrade-gracefully contract in this project."""
    try:
        with _lock:
            conn = _get_conn()
            try:
                conn.execute(
                    "INSERT INTO notifications (source, at, payload) VALUES (?, ?, ?)",
                    (source, at, json.dumps(payload)),
                )
                conn.execute(
                    "DELETE FROM notifications WHERE source = ? AND id NOT IN "
                    "(SELECT id FROM notifications WHERE source = ? ORDER BY at DESC LIMIT ?)",
                    (source, source, limit),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def recent(source: str, limit: int) -> list:
    """Up to `limit` most recent persisted events for one source, OLDEST
    first. Callers use this at startup to reseed their own in-memory deque
    by replaying each payload through the exact same append/appendleft
    call a live event would use -- oldest-first replay order reproduces
    the same final deque state live traffic would have built up, whether
    that source appends newest-at-the-end (fleet/solar/brandmeister) or
    newest-at-the-front (aprs/hamalert, via appendleft). Returns [] on any
    failure rather than raising, same graceful-degradation contract as
    every other integration client in this project -- a notification
    history is a convenience, never something worth crashing startup over."""
    try:
        with _lock:
            conn = _get_conn()
            try:
                rows = conn.execute(
                    "SELECT payload FROM notifications WHERE source = ? ORDER BY at DESC LIMIT ?",
                    (source, limit),
                ).fetchall()
            finally:
                conn.close()
        return [json.loads(row[0]) for row in reversed(rows)]
    except Exception:
        return []
