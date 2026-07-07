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
        return dict(config.DEFAULT_SETTINGS)
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
