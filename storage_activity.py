"""SQLite-backed fleet activity log for the optional 'Fleet activity' metrics
card. One row is inserted per completed transmission (see monitor.py, where
a hotspot transitions from active back to last-heard).

Kept separate from storage.py's JSON files since this is a growing time
series, not a small config blob -- SQLite (stdlib, no new dependency) is a
better fit than rewriting a whole JSON file on every transmission.
"""
import os
import sqlite3
import threading
import time

import config

DB_PATH = os.path.join(config.CONFIG_DIR, "activity.db")

# Sized off the longest selectable span (Settings -> General -> Fleet
# activity time span), plus a 1h buffer -- not just the default 12h window.
# If this only covered the default, switching to a longer span in Settings
# would silently show nothing for the extra hours, since the rows behind
# them would already have been pruned.
RETENTION_SECONDS = (max(config.FLEET_ACTIVITY_HOUR_OPTIONS) + 1) * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    hotspot_ip TEXT NOT NULL,
    hotspot_name TEXT NOT NULL,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);
"""

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    # CREATE TABLE IF NOT EXISTS is a no-op against an already-existing
    # table on any real deployed install -- a genuine ALTER TABLE migration
    # is needed for the target/target_type columns (Top 5 activity card),
    # or existing users' history never gains them until the file is
    # deleted. Guarded on "does the column already exist" rather than
    # catching the duplicate-column error, so this is safe to run on
    # every single connection open, not just once at startup.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(activity_log)")}
    if "target" not in cols:
        conn.execute("ALTER TABLE activity_log ADD COLUMN target TEXT")
    if "target_type" not in cols:
        conn.execute("ALTER TABLE activity_log ADD COLUMN target_type TEXT")
    if "via" not in cols:
        conn.execute("ALTER TABLE activity_log ADD COLUMN via TEXT")
    return conn


def log_activity(hotspot_ip: str, hotspot_name: str, mode: str | None,
                  target: str | None = None, target_type: str | None = None,
                  via: str | None = None) -> None:
    """Insert one row for a just-completed transmission and prune anything
    older than RETENTION_SECONDS. Prune happens inline here rather than a
    separate thread -- inserts are infrequent (once per completed
    transmission), so there's no meaningful cost to doing it every time.

    target/target_type (currently always a callsign) are optional and
    default to None -- a hotspot type with no equivalent concept just
    never contributes to top_targets(). via (the WPSD talkgroup this
    specific transmission was heard on -- ASL3 has no separate concept,
    since the linked node IS effectively the "channel") is likewise
    optional, shown alongside the callsign for context, not used for
    ranking."""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO activity_log (ts, hotspot_ip, hotspot_name, mode, target, target_type, via) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, hotspot_ip, hotspot_name, mode, target, target_type, via),
            )
            conn.execute("DELETE FROM activity_log WHERE ts < ?", (now - RETENTION_SECONDS,))
            conn.commit()
        finally:
            conn.close()


def top_targets(hours: int = 24, limit: int = 5) -> list:
    """Most active callsigns (who transmitted, not which talkgroup/node
    they went through) across the fleet in the trailing `hours` window,
    ranked by transmission count -- own-fleet activity only, not a
    network-wide feed (see CLAUDE.md for why a network-wide version
    isn't built: no clean REST-pollable option was found for any of
    Brandmeister/TGIF/AllStarLink/YSF).

    Each row also carries `via` -- the talkgroup/node this callsign was
    MOST RECENTLY heard on within the window (a correlated subquery
    picking the `via` from that callsign's own latest row), not an
    aggregate across every transmission -- simpler and more useful at a
    glance than "most common," and cheap at this table's bounded size."""
    cutoff = time.time() - hours * 3600
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                """
                SELECT a1.target, a1.target_type, COUNT(*) AS cnt,
                       (SELECT a2.via FROM activity_log a2
                        WHERE a2.target = a1.target AND a2.target_type = a1.target_type
                          AND a2.ts >= ? AND a2.via IS NOT NULL
                        ORDER BY a2.ts DESC LIMIT 1) AS last_via
                FROM activity_log a1
                WHERE a1.target IS NOT NULL AND a1.ts >= ?
                GROUP BY a1.target, a1.target_type
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (cutoff, cutoff, limit),
            ).fetchall()
        finally:
            conn.close()
    return [{"target": t, "target_type": tt, "count": cnt, "via": via} for t, tt, cnt, via in rows]


def dvswitch_sparkline(hotspot_ip: str, minutes: int = 180, buckets: int = 14) -> list:
    """Compact recent-activity bucket counts for one ASL3 hotspot's
    DVSwitch traffic -- backs the DVSwitch card's footer sparkline, a
    small glanceable trend rather than the detailed Fleet Activity chart.
    Reuses the exact same activity_log rows _check_dvswitch_tx() already
    writes with mode="DVSwitch" -- no new data source, no new column,
    just a shorter/differently-bucketed read of what's already there."""
    now      = time.time()
    interval = max(1, (minutes * 60) // buckets)
    start    = now - buckets * interval
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT ts FROM activity_log WHERE hotspot_ip = ? AND mode = 'DVSwitch' AND ts >= ?",
                (hotspot_ip, start),
            ).fetchall()
        finally:
            conn.close()
    counts = [0] * buckets
    for (ts,) in rows:
        idx = int((ts - start) // interval)
        if 0 <= idx < buckets:
            counts[idx] += 1
    return counts


def query_activity(hours: int = 12, interval_minutes: int = 15) -> dict:
    """Bucketed activity counts over the trailing `hours` window, plus a
    mode breakdown and the most recent activity row. Always returns a full
    set of buckets (zero-filled) even if activity_log has no rows yet."""
    now              = time.time()
    interval_seconds = max(1, interval_minutes) * 60
    num_buckets      = max(1, (hours * 3600) // interval_seconds)
    # Anchor the LAST bucket to `now` (floored to an interval boundary, e.g.
    # :00/:15/:30/:45) and count backwards -- anchoring to the window start
    # instead would floor-align there and could push the last bucket's end
    # before `now`, silently dropping the most recent activity out of range.
    now_bucket_start    = (int(now) // interval_seconds) * interval_seconds
    first_bucket_start  = now_bucket_start - (num_buckets - 1) * interval_seconds

    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                """
                SELECT
                    CAST((ts - ?) / ? AS INTEGER) AS bucket_idx,
                    hotspot_name,
                    COUNT(*) AS cnt
                FROM activity_log
                WHERE ts >= ?
                GROUP BY bucket_idx, hotspot_name
                """,
                (first_bucket_start, interval_seconds, first_bucket_start),
            ).fetchall()

            mode_rows = conn.execute(
                """
                SELECT mode, COUNT(*) FROM activity_log
                WHERE ts >= ? AND mode IS NOT NULL
                GROUP BY mode
                """,
                (first_bucket_start,),
            ).fetchall()

            last_row = conn.execute(
                "SELECT hotspot_name, ts FROM activity_log ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

    # Only include hotspots with at least one count > 0 anywhere in the
    # window -- omit flat-zero series entirely rather than padding every
    # bucket with hotspots that never transmitted.
    active_hotspots = sorted({name for _, name, cnt in rows if cnt > 0})

    buckets = []
    for i in range(num_buckets):
        bucket_ts = first_bucket_start + i * interval_seconds
        buckets.append({
            "t": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(bucket_ts)),
            "total": 0,
            "by_hotspot": {name: 0 for name in active_hotspots},
        })

    for bucket_idx, hotspot_name, cnt in rows:
        if 0 <= bucket_idx < num_buckets:
            b = buckets[bucket_idx]
            b["total"] += cnt
            if hotspot_name in active_hotspots:
                b["by_hotspot"][hotspot_name] = b["by_hotspot"].get(hotspot_name, 0) + cnt

    total_activity = sum(b["total"] for b in buckets)
    mode_breakdown = [
        {
            "mode":  mode,
            "count": cnt,
            "pct":   round(cnt / total_activity * 100, 1) if total_activity else 0.0,
        }
        for mode, cnt in mode_rows if cnt > 0
    ]
    mode_breakdown.sort(key=lambda m: m["count"], reverse=True)

    last_activity = None
    if last_row:
        last_activity = {"hotspot": last_row[0], "ts": last_row[1]}

    return {
        "buckets":        buckets,
        "mode_breakdown": mode_breakdown,
        "last_activity":  last_activity,
    }
