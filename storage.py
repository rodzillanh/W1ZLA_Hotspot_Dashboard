"""Reading and writing the hotspots.json and settings.json config files."""
import contextlib
import json
import os
import threading
import time
import uuid

import config

# RLock, not a plain Lock -- settings_transaction() below needs to hold
# this lock across a whole load-modify-save sequence while STILL calling
# load_settings()/save_settings(), which each also acquire it internally
# for their own individual read/write. A plain Lock would deadlock a
# thread against itself the moment settings_transaction() called either
# of them; RLock lets the same thread re-enter without blocking on
# itself, while still serializing against every OTHER thread exactly as
# before.
_file_lock = threading.RLock()


# --- hotspots ---

def load_hotspots() -> list:
    """Return the hotspot list, backfilling a stable `id` for any entry
    that predates this field -- same self-healing-on-load pattern as
    load_asl_favorites()'s pinned/pinned_at backfill below. `id` (not
    `ip`) is every hotspot's real identity key everywhere in the app now
    -- `ip` is purely a connection target, so two entries CAN legitimately
    share one (e.g. two ASL3 radios/node numbers behind the same SSH
    login). Self-healing: any write path that doesn't know about `id`
    (an old backup import, hand-edited JSON) just omits it, and it gets
    backfilled the same way the next time this loads."""
    if not os.path.exists(config.CONFIG_FILE):
        return []
    try:
        with _file_lock, open(config.CONFIG_FILE, "r") as f:
            hotspots = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    migrated = False
    for h in hotspots:
        if not h.get("id"):
            h["id"] = f"hs-{uuid.uuid4().hex[:10]}"
            migrated = True
    if migrated:
        save_hotspots(hotspots)
    return hotspots


def save_hotspots(hotspots: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.CONFIG_FILE, "w") as f:
        json.dump(hotspots, f, indent=4)


# --- settings ---

def load_settings() -> dict:
    if not os.path.exists(config.SETTINGS_FILE):
        # A genuinely fresh install -- settings.json has never been saved
        # at all, not even once, so this is the one reliable one-time
        # signal to distinguish "just installed" from "upgrading an
        # existing install" (which already has this file on disk, just
        # missing newer keys -- handled in the merge branch below via
        # DEFAULT_SETTINGS' own onboarding_tour_seen=True). Queues the
        # one-time onboarding tour; don't remove this override without
        # re-reading config.DEFAULT_SETTINGS' comment on the same key.
        fresh = dict(config.DEFAULT_SETTINGS)
        fresh["onboarding_tour_seen"] = False
        return fresh
    try:
        with _file_lock, open(config.SETTINGS_FILE, "r") as f:
            saved = json.load(f)
        # Merge with defaults so new keys added in later versions get their defaults
        merged = dict(config.DEFAULT_SETTINGS)
        merged.update(saved)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(config.DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)


@contextlib.contextmanager
def settings_transaction():
    """Serializes an entire load-modify-save sequence for settings.json
    against every OTHER concurrent settings_transaction() (or plain
    load_settings()/save_settings() call) -- confirmed live as a real,
    reported bug without this: setup.html's saveCardOrder() fires several
    /api/settings POSTs in parallel (one per changed field), and without
    a lock spanning the WHOLE read-modify-write sequence, two concurrent
    requests can each read the same stale snapshot before either has
    written, then each write back their own full settings dict --
    whichever writes last wins and silently discards the other's change
    in its entirety. A live test firing 3 concurrent /api/settings POSTs
    lost 2 of the 3 updates this way (both reverted to their defaults).
    Callers should do the usual `settings = load_settings(); settings[k]
    = v; save_settings(settings)` sequence INSIDE this context manager's
    `with` block, e.g.:

        with settings_transaction():
            settings = load_settings()
            settings["foo"] = "bar"
            save_settings(settings)

    Safe to call load_settings()/save_settings() from inside this block
    because _file_lock is an RLock -- the same thread re-entering it
    (which those two functions each do internally) doesn't deadlock,
    while a DIFFERENT thread's transaction still blocks until this one
    exits, serializing the whole sequence exactly as intended."""
    with _file_lock:
        yield


# --- favorites ---

def load_favorites() -> list:
    """Return list of dicts: [{call, label}, ...]"""
    if not os.path.exists(config.FAVORITES_FILE):
        return []
    try:
        with _file_lock, open(config.FAVORITES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_favorites(favorites: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.FAVORITES_FILE, "w") as f:
        json.dump(favorites, f, indent=4)


def favorites_set() -> set:
    """Return a set of uppercased callsigns for fast O(1) lookup."""
    return {f["call"].upper() for f in load_favorites()}


# --- ASL favorites (node numbers, distinct from the callsign favorites above) ---

def load_asl_favorites() -> list:
    """Return list of dicts: [{node, label, pinned, pinned_at}, ...].

    `pinned` (shown as a tile on the compact ASL Favorites card, capped at
    config.ASL_FAV_CARD_CAP -- the rest are still real favorites, just
    pin-managed from the ASL Control drawer instead) is migrated in place
    the first time an entry is seen without the key: the first CAP such
    entries (in existing list order) default to pinned, the rest don't --
    preserves "roughly the same favorites show up" for an existing install
    upgrading, rather than surprising it with an empty tile grid or an
    arbitrary cutoff. `pinned_at` (a Unix timestamp, used to pick which
    pinned favorite gets bumped when a new one is pinned past the cap)
    defaults to 0 for migrated entries -- deliberately the oldest possible
    value, so a real future pin action always outranks a migrated default
    for eviction purposes. Self-healing: any write path that doesn't know
    about these fields (e.g. a bulk backup import) just omits them, and
    they get backfilled the same way the next time this loads."""
    if not os.path.exists(config.ASL_FAVORITES_FILE):
        return []
    try:
        with _file_lock, open(config.ASL_FAVORITES_FILE, "r") as f:
            favorites = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    migrated = False
    pinned_count = sum(1 for f in favorites if f.get("pinned") is True)
    for f in favorites:
        if "pinned" not in f:
            f["pinned"] = pinned_count < config.ASL_FAV_CARD_CAP
            if f["pinned"]:
                pinned_count += 1
            f["pinned_at"] = 0
            migrated = True
    if migrated:
        save_asl_favorites(favorites)
    return favorites


def save_asl_favorites(favorites: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.ASL_FAVORITES_FILE, "w") as f:
        json.dump(favorites, f, indent=4)


# --- Brandmeister talkgroup favorites (quick-link chips, distinct from
# both callsign and ASL node favorites above) ---

def load_bm_tg_favorites() -> list:
    """Return list of dicts: [{tg, slot, label}, ...]"""
    if not os.path.exists(config.BM_TG_FAVORITES_FILE):
        return []
    try:
        with _file_lock, open(config.BM_TG_FAVORITES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_bm_tg_favorites(favorites: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.BM_TG_FAVORITES_FILE, "w") as f:
        json.dump(favorites, f, indent=4)


# --- cameras (RTSP / Bambu Labs A1) ---

def load_cameras() -> list:
    if not os.path.exists(config.CAMERAS_FILE):
        return []
    try:
        with _file_lock, open(config.CAMERAS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_cameras(cameras: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.CAMERAS_FILE, "w") as f:
        json.dump(cameras, f, indent=4)


# --- QSOs (imported from an ADIF log) ---

def load_qsos() -> list:
    if not os.path.exists(config.QSOS_FILE):
        return []
    try:
        with _file_lock, open(config.QSOS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_qsos(qsos: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.QSOS_FILE, "w") as f:
        json.dump(qsos, f, indent=4)


# --- push subscriptions ---

def load_push_subscriptions() -> list:
    """Browser Web Push subscription objects (Pocket Dash, PR 3), one per
    entry, unique by `endpoint`. Same flat-JSON-in-CONFIG_DIR convention
    as favorites.json -- absent file just means nobody's subscribed."""
    if not os.path.exists(config.PUSH_SUBSCRIPTIONS_FILE):
        return []
    try:
        with _file_lock, open(config.PUSH_SUBSCRIPTIONS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_push_subscriptions(subs: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.PUSH_SUBSCRIPTIONS_FILE, "w") as f:
        json.dump(subs, f, indent=4)


def add_push_subscription(sub: dict) -> None:
    """Upsert by endpoint -- a browser re-subscribing (permission
    re-granted, key rotated) replaces its old entry rather than stacking
    a duplicate. Read-modify-write under _file_lock, same as append_qso."""
    endpoint = sub.get("endpoint")
    if not endpoint:
        return
    with _file_lock:
        subs = [s for s in load_push_subscriptions() if s.get("endpoint") != endpoint]
        subs.append(sub)
        save_push_subscriptions(subs)


def remove_push_subscription(endpoint: str) -> None:
    if not endpoint:
        return
    with _file_lock:
        subs = [s for s in load_push_subscriptions() if s.get("endpoint") != endpoint]
        save_push_subscriptions(subs)


# --- per-hotspot notification preferences (Pocket Dash, PR 5) ---

_NOTIFICATION_PREF_DEFAULTS = {"offline": True, "call_start": False, "muted_until": None}


def load_notification_prefs() -> dict:
    """Raw stored prefs: {hotspot_id: {offline, call_start, muted_until}}.
    A hotspot missing from this dict just means "use the defaults" --
    see notification_pref_for()."""
    if not os.path.exists(config.NOTIFICATION_PREFS_FILE):
        return {}
    try:
        with _file_lock, open(config.NOTIFICATION_PREFS_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_notification_prefs(prefs: dict) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.NOTIFICATION_PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=4)


def notification_pref_for(hotspot_id: str) -> dict:
    """Effective prefs for one hotspot: stored values merged over the
    defaults (offline on, call-start off, not muted). Same `.get()`-
    defaulted convention as `enabled` elsewhere -- an unconfigured
    hotspot still behaves sensibly."""
    stored = load_notification_prefs().get(hotspot_id) or {}
    merged = dict(_NOTIFICATION_PREF_DEFAULTS)
    for k in merged:
        if k in stored:
            merged[k] = stored[k]
    return merged


def set_notification_pref(hotspot_id: str, patch: dict) -> dict:
    """Read-modify-write one hotspot's prefs under the shared lock.
    `patch` may carry any of offline/call_start/muted_until. Returns the
    hotspot's full stored entry afterwards."""
    with _file_lock:
        prefs = load_notification_prefs()
        entry = dict(prefs.get(hotspot_id) or {})
        for k in ("offline", "call_start", "muted_until"):
            if k in patch:
                entry[k] = patch[k]
        prefs[hotspot_id] = entry
        save_notification_prefs(prefs)
        return entry


def append_qso(qso: dict) -> None:
    """Adds one QSO to the existing list -- for live WSJT-X logging
    (wsjtx.py), which arrives one QSO at a time, unlike a bulk ADIF
    import which replaces the whole list via save_qsos() above. Read-
    modify-write under the same _file_lock as every other read/write here,
    so a live QSO landing mid-request from /api/import_adif or
    /api/clear_qsos can't interleave with either of those."""
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock:
        try:
            with open(config.QSOS_FILE, "r") as f:
                qsos = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            qsos = []
        qsos.append(qso)
        with open(config.QSOS_FILE, "w") as f:
            json.dump(qsos, f, indent=4)


# Mode names that mean the same "voice on SSB" QSO regardless of which
# sideband the two logs happened to record -- everything else is compared
# as-is (CW==CW, FT8==FT8, etc.), deliberately NOT grouped into a fuzzy
# "digital" bucket, since FT8 vs FT4 vs RTTY really are different contacts.
_PHONE_MODES = {"SSB", "USB", "LSB", "DSB", "FM", "AM", "PHONE"}


def _mode_key(mode: str) -> str:
    m = (mode or "").strip().upper()
    return "PHONE" if m in _PHONE_MODES else m


def _qso_matches(a: dict, b: dict, window_sec: int) -> bool:
    """True if `a` and `b` are almost certainly the same contact logged
    from two sources (QRZ Logbook vs. WSJT-X live / ADIF import): same
    callsign + band + mode-group, and their timestamps within
    `window_sec`. Falls back to same-date equality only when a usable
    timestamp is missing on either side."""
    if (a.get("call") or "").upper() != (b.get("call") or "").upper():
        return False
    if (a.get("band") or "").lower() != (b.get("band") or "").lower():
        return False
    if _mode_key(a.get("mode")) != _mode_key(b.get("mode")):
        return False
    ta, tb = a.get("logged_at"), b.get("logged_at")
    if ta is None or tb is None:
        return bool(a.get("date")) and (a.get("date") or "") == (b.get("date") or "")
    return abs(ta - tb) <= window_sec


def merge_qrz_qsos(add_records: list, confirm_map: dict,
                   window_sec: int = None) -> dict:
    """Fold a QRZ Logbook sync into qsos.json under _file_lock (so it
    can't interleave with append_qso()/save_qsos()):

      * `add_records` -- QSO dicts freshly pulled from QRZ (each carrying
        `qrz_logid` and source "qrz"). One whose `qrz_logid` is already
        stored is skipped. One that fuzzily matches an existing entry
        (WSJT-X- or ADIF-logged, see _qso_matches) enriches that entry in
        place -- attaches the `qrz_logid`, fills only blank fields, never
        appends a duplicate row. Otherwise it's appended.
      * `confirm_map` -- {qrz_logid: confirmed_at_epoch} for QSOs QRZ now
        reports confirmed. Sets `qrz_confirmed`/`qrz_confirmed_at` on the
        matching stored entry (by logid) if not already set.

    Returns {"added", "matched", "confirmed", "confirmed_logids"} -- the
    caller (qrz_logbook.py) uses `confirmed_logids` to post one
    Notifications event per QSO that ACTUALLY flipped to confirmed here
    (a real transition), never for one that arrived already-confirmed.
    Never raises."""
    if window_sec is None:
        window_sec = config.QRZ_LOGBOOK_MATCH_WINDOW_SEC
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    confirmed_logids = []
    with _file_lock:
        try:
            with open(config.QSOS_FILE, "r") as f:
                qsos = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            qsos = []

        known_logids = {q.get("qrz_logid") for q in qsos if q.get("qrz_logid") is not None}
        added = matched = confirmed = 0

        for rec in add_records or []:
            lid = rec.get("qrz_logid")
            if lid is not None and lid in known_logids:
                continue
            # Fuzzy-match only against rows from a DIFFERENT source (a
            # WSJT-X / ADIF entry) -- two QRZ-sourced rows are already
            # deduped by qrz_logid above, and the fuzzy window would
            # otherwise wrongly merge two genuinely distinct QSOs with
            # the same station a few minutes apart that both came from
            # the QRZ logbook.
            hit = next((q for q in qsos
                        if q.get("qrz_logid") is None
                        and _qso_matches(rec, q, window_sec)), None)
            if hit is not None:
                if lid is not None and hit.get("qrz_logid") is None:
                    hit["qrz_logid"] = lid
                    known_logids.add(lid)
                if rec.get("qrz_confirmed") and not hit.get("qrz_confirmed"):
                    hit["qrz_confirmed"] = True
                    hit["qrz_confirmed_at"] = rec.get("qrz_confirmed_at") or time.time()
                for k in ("name", "city", "state", "country", "grid",
                          "rst_sent", "rst_rcvd", "frequency_hz"):
                    if not hit.get(k) and rec.get(k):
                        hit[k] = rec[k]
                matched += 1
            else:
                qsos.append(rec)
                if lid is not None:
                    known_logids.add(lid)
                added += 1

        if confirm_map:
            for q in qsos:
                lid = q.get("qrz_logid")
                if lid is not None and lid in confirm_map and not q.get("qrz_confirmed"):
                    q["qrz_confirmed"] = True
                    q["qrz_confirmed_at"] = confirm_map[lid]
                    confirmed_logids.append(lid)
                    confirmed += 1

        with open(config.QSOS_FILE, "w") as f:
            json.dump(qsos, f, indent=4)

    return {"added": added, "matched": matched, "confirmed": confirmed,
            "confirmed_logids": confirmed_logids}
