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

# The Activity patterns heatmap (Hotspot Activity card's drawer) needs
# weeks of history, far more than the raw log above keeps. So it reads a
# separate compact rollup, activity_hourly: one row per hotspot per UTC
# hour per talkgroup (`via`), kept this long. Worst case is a few thousand
# rows per hotspot -- small enough to send the whole window to the browser,
# which does the local-time bucketing itself.
HOURLY_RETENTION_DAYS = 90

_HOURLY_SCHEMA = """
CREATE TABLE activity_hourly (
    hotspot_id TEXT NOT NULL,
    hotspot_name TEXT NOT NULL,
    hour_ts INTEGER NOT NULL,
    via TEXT NOT NULL DEFAULT '',
    tx INTEGER NOT NULL DEFAULT 0,
    secs REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (hotspot_id, hour_ts, via)
);
CREATE INDEX idx_hourly_ts ON activity_hourly(hour_ts);
"""

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
    if "duration" not in cols:
        conn.execute("ALTER TABLE activity_log ADD COLUMN duration REAL")
    if "hotspot_id" not in cols:
        # Nullable, same self-healing-not-retroactive precedent as
        # duration above -- old rows just have NULL here. hotspot_ip
        # stopped being a unique identity once two hotspots could share
        # one ip (two ASL3 radios behind one SSH login); hotspot_id
        # (storage.load_hotspots()'s generated id) is the real identity
        # now, used by dvswitch_sparkline()'s per-hotspot filter below.
        conn.execute("ALTER TABLE activity_log ADD COLUMN hotspot_id TEXT")
    _ensure_hourly(conn)
    return conn


def _ensure_hourly(conn: sqlite3.Connection) -> None:
    """Create activity_hourly on first use and seed it from whatever the raw
    log still holds (about two days), so the heatmap isn't blank right
    after upgrading. Only runs when the table doesn't exist yet."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_hourly'").fetchone()
    if exists:
        return
    conn.executescript(_HOURLY_SCHEMA)
    conn.execute(
        "INSERT INTO activity_hourly (hotspot_id, hotspot_name, hour_ts, via, tx, secs) "
        "SELECT COALESCE(hotspot_id, hotspot_ip), MAX(hotspot_name), CAST(ts / 3600 AS INTEGER) * 3600, "
        "COALESCE(via, ''), COUNT(*), COALESCE(SUM(duration), 0) "
        "FROM activity_log WHERE hotspot_id IS NOT NULL GROUP BY 1, 3, 4")
    conn.commit()


def log_activity(hotspot_ip: str, hotspot_name: str, mode: str | None,
                  target: str | None = None, target_type: str | None = None,
                  via: str | None = None, duration: float | None = None,
                  hotspot_id: str | None = None) -> None:
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
    ranking. duration (seconds, monitor.py's _log_activity computes it
    from status.tx_start before that field gets cleared) is optional too
    -- rows logged before this column existed, or from a caller that
    never had a tx_start to read, are simply excluded from a
    duration-ranked top_targets() query rather than sorting as zero."""
    now = time.time()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO activity_log (ts, hotspot_ip, hotspot_name, mode, target, target_type, via, duration, hotspot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, hotspot_ip, hotspot_name, mode, target, target_type, via, duration, hotspot_id),
            )
            conn.execute("DELETE FROM activity_log WHERE ts < ?", (now - RETENTION_SECONDS,))
            conn.execute(
                "INSERT INTO activity_hourly (hotspot_id, hotspot_name, hour_ts, via, tx, secs) "
                "VALUES (?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(hotspot_id, hour_ts, via) DO UPDATE SET "
                "tx = tx + 1, secs = secs + excluded.secs, hotspot_name = excluded.hotspot_name",
                (hotspot_id or hotspot_ip, hotspot_name, int(now // 3600) * 3600,
                 via or "", float(duration or 0)),
            )
            conn.execute("DELETE FROM activity_hourly WHERE hour_ts < ?",
                         (now - HOURLY_RETENTION_DAYS * 86400,))
            conn.commit()
        finally:
            conn.close()


def top_targets(hours: int = 24, limit: int = 5, rank_by: str = "count") -> list:
    """Most active callsigns (who transmitted, not which talkgroup/node
    they went through) across the fleet in the trailing `hours` window --
    own-fleet activity only, not a network-wide feed (see CLAUDE.md for
    why a network-wide version isn't built: no clean REST-pollable option
    was found for any of Brandmeister/TGIF/AllStarLink/YSF).

    rank_by picks which metric determines the top 5, not just the
    ordering of a fixed set -- "count" (raw transmission count) and
    "duration" (summed talk-time) can genuinely disagree on WHICH five
    targets make the cut, not just their order, so this has to be a
    real query-time choice rather than a client-side re-sort of one
    fetched list. Any value other than "duration" falls back to "count"
    -- this is only ever driven by a fixed two-button toggle, not
    free-text input, but validating defensively costs nothing here since
    the column name is chosen in Python, never interpolated from the
    argument itself.

    Each row also carries `via` -- the talkgroup/node this callsign was
    MOST RECENTLY heard on within the window (a correlated subquery
    picking the `via` from that callsign's own latest row), not an
    aggregate across every transmission -- simpler and more useful at a
    glance than "most common," and cheap at this table's bounded size."""
    order_col = "total_duration" if rank_by == "duration" else "cnt"
    cutoff = time.time() - hours * 3600
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                f"""
                SELECT a1.target, a1.target_type, COUNT(*) AS cnt,
                       SUM(COALESCE(a1.duration, 0)) AS total_duration,
                       (SELECT a2.via FROM activity_log a2
                        WHERE a2.target = a1.target AND a2.target_type = a1.target_type
                          AND a2.ts >= ? AND a2.via IS NOT NULL
                        ORDER BY a2.ts DESC LIMIT 1) AS last_via
                FROM activity_log a1
                WHERE a1.target IS NOT NULL AND a1.ts >= ?
                GROUP BY a1.target, a1.target_type
                ORDER BY {order_col} DESC
                LIMIT ?
                """,
                (cutoff, cutoff, limit),
            ).fetchall()
        finally:
            conn.close()
    return [
        {
            "target": t, "target_type": tt, "count": cnt,
            "duration_seconds": round(dur) if dur else 0, "via": via,
        }
        for t, tt, cnt, dur, via in rows
    ]


def dvswitch_sparkline(hotspot_id: str, minutes: int = 180, buckets: int = 14) -> list:
    """Compact recent-activity bucket counts for one ASL3 hotspot's
    DVSwitch traffic -- backs the DVSwitch card's footer sparkline, a
    small glanceable trend rather than the detailed Fleet Activity chart.
    Reuses the exact same activity_log rows _check_dvswitch_tx() already
    writes with mode="DVSwitch" -- no new data source, just a shorter/
    differently-bucketed read of what's already there. Filters on
    hotspot_id (not hotspot_ip) since two hotspots can share one ip --
    rows logged before hotspot_id existed have it as NULL and simply
    never match here (a one-time, cosmetic gap in old history, not a
    crash)."""
    now      = time.time()
    interval = max(1, (minutes * 60) // buckets)
    start    = now - buckets * interval
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT ts FROM activity_log WHERE hotspot_id = ? AND mode = 'DVSwitch' AND ts >= ?",
                (hotspot_id, start),
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


def hourly_patterns(days: int = HOURLY_RETENTION_DAYS) -> dict:
    """Everything the Activity patterns heatmap needs, raw: one row per
    hotspot/UTC-hour/talkgroup over the trailing `days`. The browser
    converts hours to its own time zone, averages per week and looks for
    regular nets -- doing it there keeps DST and time zones out of SQL.
    `since` is the oldest hour ever recorded, so the drawer can say how
    long it has been collecting."""
    now = time.time()
    days = max(1, min(int(days), HOURLY_RETENTION_DAYS))
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT hour_ts, hotspot_id, via, tx, secs FROM activity_hourly "
                "WHERE hour_ts >= ? ORDER BY hour_ts", (now - days * 86400,)).fetchall()
            names = dict(conn.execute(
                "SELECT hotspot_id, MAX(hotspot_name) FROM activity_hourly GROUP BY hotspot_id").fetchall())
            since = conn.execute("SELECT MIN(hour_ts) FROM activity_hourly").fetchone()[0]
        finally:
            conn.close()
    return {
        "rows": [[r[0], r[1], r[2], r[3], round(r[4], 1)] for r in rows],
        "hotspots": names,
        "since": since,
        "now": now,
        "retention_days": HOURLY_RETENTION_DAYS,
    }
