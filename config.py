"""Central place for all the constants that used to be scattered/hardcoded."""
import os

# --- Storage ---
CONFIG_DIR   = os.environ.get("CONFIG_DIR", "/app/data")
CONFIG_FILE  = os.path.join(CONFIG_DIR, "hotspots.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
FAVORITES_FILE = os.path.join(CONFIG_DIR, "favorites.json")

# --- Server ---
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))

# --- Polling ---
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", 5))
SSH_TIMEOUT        = int(os.environ.get("SSH_TIMEOUT", 5))
FAILURE_THRESHOLD  = int(os.environ.get("FAILURE_THRESHOLD", 2))
MAX_WORKERS        = int(os.environ.get("MAX_WORKERS", 5))
LOG_TAIL_LINES     = 50
MAX_HISTORY        = 5

# How long after the last log line before a node is forced back to idle.
# Set to 0 to disable.
ACTIVE_TIMEOUT = int(os.environ.get("ACTIVE_TIMEOUT", 30))

# How long to keep the last caller's info visible after a transmission ends.
# After this period the card returns to true idle ("Listening...").
# Set to 0 to disable — cards go straight to idle on end of transmission.
LAST_HEARD_TTL = int(os.environ.get("LAST_HEARD_TTL", 300))  # 5 minutes

SSH_STATUS_CMD = (
    'cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo "N/A"; '
    "uptime -p; "
    # CPU: pipe two /proc/stat samples 200ms apart into awk as separate lines.
    # Using NR==1/NR==2 avoids field-numbering bugs when both lines are
    # concatenated, and handles kernels with different numbers of cpu fields.
    "{ grep '^cpu ' /proc/stat; sleep 0.2; grep '^cpu ' /proc/stat; } | "
    "awk 'NR==1{for(i=2;i<=NF;i++)t1+=$i; idle1=$5} "
    "NR==2{for(i=2;i<=NF;i++)t2+=$i; idle2=$5; "
    "if(t2-t1>0) printf \"%.1f\\n\",(1-(idle2-idle1)/(t2-t1))*100; "
    "else print \"N/A\"}'; "
    "L=$(ls -1tr /var/log/pi-star/MMDVM-*.log /var/log/wpsd/MMDVM-*.log 2>/dev/null | tail -1); "
    f'tail -n {LOG_TAIL_LINES} "$L" 2>/dev/null'
)

# --- Log line parsing ---
BER_PATTERN        = r"BER: (\d+\.?\d*)%"
# P25 reports RSSI as "RSSI: -115/-47/-54 dBm" (min/avg/max).
# Capture just the first (minimum) value; DMR reports a single value.
RSSI_PATTERN       = r"RSSI: (-?\d+)"
MODE_PATTERN       = r"DMR|D-Star|YSF|P25|NXDN"
COLOR_CODE_PATTERN = r"Colo(?:u)?r Code:\s*(\d+)"   # matches "Color Code: 15" and "Colour Code: 15"
SLOT_PATTERN       = r"DMR Slot (\d+)"               # matches "DMR Slot 2"

END_OF_TRANSMISSION_MARKERS = (
    "end of voice transmission",   # DMR network
    "end of rf transmission",      # P25 / some RF modes
    "end of transmission",         # generic / some firmware variants
    "end of qso",                  # D-Star
    "transmission lost",
    "network problem",
)

# Note: "voice" and "transmission" deliberately removed from start markers --
# "received network end of voice transmission" contains both, causing the end-of-
# call line to be misdetected as a new transmission start.
# "late entry" covers NXDN's "received RF late entry from CALL" start format.
TRANSMISSION_START_MARKERS = ("header", "late entry", "stream", "data")

# --- QRZ lookup ---
QRZ_USERNAME  = os.environ.get("QRZ_USERNAME", "")
QRZ_PASSWORD  = os.environ.get("QRZ_PASSWORD", "")
QRZ_AGENT     = os.environ.get("QRZ_AGENT", "hotspot-dashboard/1.0")
QRZ_TIMEOUT   = int(os.environ.get("QRZ_TIMEOUT", 5))
QRZ_CACHE_TTL = int(os.environ.get("QRZ_CACHE_TTL", 3600))

# --- RadioID.net lookup (free fallback for caller name/location) ---
RADIOID_AGENT     = os.environ.get("RADIOID_AGENT", "hotspot-dashboard/1.0")
RADIOID_TIMEOUT   = int(os.environ.get("RADIOID_TIMEOUT", 5))
RADIOID_CACHE_TTL = int(os.environ.get("RADIOID_CACHE_TTL", 3600))

# --- APRS.fi lookup (live position, overrides QRZ's static coordinates) ---
APRS_TIMEOUT   = int(os.environ.get("APRS_TIMEOUT", 5))
APRS_CACHE_TTL = int(os.environ.get("APRS_CACHE_TTL", 120))  # positions can move -- short TTL

# --- Brandmeister repeater profile lookup ---
BRANDMEISTER_AGENT     = os.environ.get("BRANDMEISTER_AGENT", "hotspot-dashboard/1.0 (+https://github.com/)")
BRANDMEISTER_TIMEOUT   = int(os.environ.get("BRANDMEISTER_TIMEOUT", 5))
BRANDMEISTER_CACHE_TTL = int(os.environ.get("BRANDMEISTER_CACHE_TTL", 120))

# --- Home Assistant MQTT auto-discovery ---
MQTT_PUBLISH_INTERVAL = int(os.environ.get("MQTT_PUBLISH_INTERVAL", POLL_INTERVAL))
# Runs on a much slower cadence than the main poll loop since it does a
# `git ls-remote` (a real network round-trip from the hotspot itself).
VERSION_CHECK_INTERVAL = int(os.environ.get("VERSION_CHECK_INTERVAL", 1800))  # 30 min
VERSION_CHECK_CMD = (
    # Checks all three WPSD repos, not just the first one found -- an
    # earlier version stopped after /var/www/dashboard (WebCode), which
    # meant a pending update in WPSD-Scripts or WPSD-Binaries specifically
    # would never be detected even though WPSD's own Admin->Update page
    # would show it. Repo paths/names match WPSD's own official
    # .wpsd-check4updates script.
    #
    # -c safe.directory='*' is required because these repos' .git are
    # typically owned by www-data/root while SSH logs in as a different
    # user -- modern git (2.35.2+) refuses to run ANY command against a
    # repo it doesn't recognize as owned by the current user ("detected
    # dubious ownership"), and fails *silently* under 2>/dev/null.
    # Confirmed against a real WPSD install (2026-07). This only affects
    # this one-off command invocation; it does not write to the hotspot's
    # own git config.
    #
    # Uses a plain shell function (not associative arrays) since the
    # non-interactive SSH shell on some systems is dash, not bash, and
    # dash doesn't support bash-only array syntax.
    "check_repo() { "
    "  p=\"$1\"; name=\"$2\"; "
    "  if [ -d \"$p/.git\" ]; then "
    "    ( cd \"$p\" 2>/dev/null || exit 0; "
    "      LOCAL=$(git -c safe.directory='*' rev-parse --short HEAD 2>/dev/null); "
    "      REMOTE=$(git -c safe.directory='*' ls-remote origin HEAD 2>/dev/null | cut -c1-7); "
    "      DATE=$(git -c safe.directory='*' log -1 --format=%cd --date=short 2>/dev/null); "
    "      echo \"$name|$LOCAL|$REMOTE|$DATE\" ); "
    "  fi; "
    "}; "
    "check_repo /var/www/dashboard WPSD-WebCode; "
    "check_repo /usr/local/sbin WPSD-Scripts; "
    "check_repo /usr/local/bin WPSD-Binaries"
)

# --- Dashboard defaults (overridden by settings.json) ---
DEFAULT_SETTINGS = {
    "dashboard_name": "W1ZLA Hotspot Dashboard",
    "dark_mode": True,
    "links": [
        {"label": "Brandmeister", "url": "https://brandmeister.network"},
        {"label": "QRZ.com",      "url": "https://www.qrz.com"},
        {"label": "DMR-MARC",     "url": "https://www.dmr-marc.net"},
        {"label": "APRS.fi",      "url": "https://aprs.fi"},
    ],
    "weather_location": "",   # city name, zip, or "City, ST" — blank = disabled
    "weather_unit": "F",      # "F" or "C"
    "show_host_stats": True,  # show host CPU + memory bar on dashboard
    "show_toolbar": True,     # show the row of custom toolbar links on the dashboard
    # Fleet activity metrics card — off by default since it adds a new SQLite
    # table and a background write on every completed transmission.
    "show_fleet_activity": False,
    # Where the fleet activity card sits among the hotspot cards -- 0 = first,
    # N = after the Nth hotspot card. Set from the Card order drag list
    # (Settings -> Hotspots) alongside the hotspot order itself.
    "fleet_activity_position": 0,
    # How far back the fleet activity chart/mode-breakdown look -- one of
    # FLEET_ACTIVITY_HOUR_OPTIONS. storage_activity.py's retention window is
    # sized off the largest option here, so switching to a longer span never
    # comes up empty because old rows were already pruned.
    "fleet_activity_hours": 12,
    # QRZ credentials — stored here so the Settings page works on all platforms.
    # Env vars QRZ_USERNAME / QRZ_PASSWORD are still read as a fallback so
    # existing Unraid installs with env vars keep working without reconfiguring.
    "qrz_username": "",
    "qrz_password": "",
    # RadioID.net — free, no auth. Used as a fallback when QRZ has no
    # subscription or doesn't have the callsign. On by default.
    "radioid_enabled": True,
    # APRS.fi — requires a free API key from https://aprs.fi/page/api.
    # Blank disables it.
    "aprs_api_key": "",
    # Home Assistant MQTT auto-discovery — blank host disables it.
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    # APRS messaging — sends an APRS-IS text message to yourself when a
    # favorite becomes active. Blank callsign disables it. to_callsign
    # defaults to my_callsign if left blank (message to yourself).
    "aprs_msg_callsign": "",
    "aprs_msg_to_callsign": "",
    "aprs_msg_cooldown_min": 10,
}

# --- Fleet activity ---
# Bucket width (minutes) per selectable time span, chosen so every option
# renders roughly the same number of chart points (~36-48) regardless of
# how far back it looks.
FLEET_ACTIVITY_HOUR_OPTIONS = {
    3:  5,
    6:  10,
    12: 15,
    24: 30,
    48: 60,
}

# --- Weather ---
WEATHER_CACHE_TTL  = int(os.environ.get("WEATHER_CACHE_TTL", 600))   # seconds (10 min)
GEOCODE_URL        = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_URL     = "https://api.open-meteo.com/v1/forecast"
