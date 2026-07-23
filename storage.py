"""Reading and writing the hotspots.json and settings.json config files."""
import json
import os
import threading

import config

_file_lock = threading.Lock()


# --- hotspots ---

def load_hotspots() -> list:
    if not os.path.exists(config.CONFIG_FILE):
        return []
    try:
        with _file_lock, open(config.CONFIG_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


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
    """Return list of dicts: [{node, label}, ...]"""
    if not os.path.exists(config.ASL_FAVORITES_FILE):
        return []
    try:
        with _file_lock, open(config.ASL_FAVORITES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_asl_favorites(favorites: list) -> None:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with _file_lock, open(config.ASL_FAVORITES_FILE, "w") as f:
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
