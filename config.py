"""Central place for all the constants that used to be scattered/hardcoded."""
import os

# --- Version codename ---
# Each release gets a codename, one deceased rock & roll musician per
# version, in the order they were used -- purely cosmetic (shown on the
# Version info page as "Current build: <version> <codename>"), same
# spirit as Ubuntu's animal names or Android's dessert codenames. This
# list only covers versions from when this feature was introduced
# onward -- earlier releases (pre-v3.49) were never retroactively named.
# To cut a new named release: bump APP_VERSION and append the next name
# here (VERSION_CODENAMES[-1] is always the current build's codename).
APP_VERSION = "3.62"
VERSION_CODENAMES = [
    "Elvis",            # v3.49 -- Elvis Presley (1935-1977)
    "Bowie",            # v3.50 -- David Bowie (1947-2016)
    "Lennon",           # v3.51 -- John Lennon (1940-1980)
    "Cobain",           # v3.52 -- Kurt Cobain (1967-1994)
    "Petty",            # v3.53 -- Tom Petty (1950-2017)
    "Hendrix",          # v3.54 -- Jimi Hendrix (1942-1970)
    "Moon",             # v3.55 -- Keith Moon (1946-1978)
    "Harrison",         # v3.56 -- George Harrison (1943-2001)
    "Mercury",          # v3.57 -- Freddie Mercury (1946-1991)
    "Cornell",          # v3.58 -- Chris Cornell (1964-2017)
    "Reed",             # v3.59 -- Lou Reed (1942-2013)
    "Winehouse",        # v3.60 -- Amy Winehouse (1983-2011)
    "Bonham",           # v3.61 -- John Bonham (1948-1980)
    "Joplin",           # v3.62 -- Janis Joplin (1943-1970)
]
APP_CODENAME = VERSION_CODENAMES[-1]

# --- Storage ---
CONFIG_DIR   = os.environ.get("CONFIG_DIR", "/app/data")
CONFIG_FILE  = os.path.join(CONFIG_DIR, "hotspots.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
FAVORITES_FILE = os.path.join(CONFIG_DIR, "favorites.json")
ASL_FAVORITES_FILE = os.path.join(CONFIG_DIR, "asl_favorites.json")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.json")
QSOS_FILE    = os.path.join(CONFIG_DIR, "qsos.json")

# --- Server ---
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))
# waitress's default (4) is sized for typical request/response traffic --
# too low here, since each open camera MJPEG stream (camera_stream.py) holds
# a thread for its entire viewing duration on top of normal dashboard
# polling from any number of browser tabs.
WAITRESS_THREADS = int(os.environ.get("WAITRESS_THREADS", 16))

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

# Shared by both node types -- generic Linux temp/uptime/CPU, nothing
# WPSD- or ASL3-specific about it.
_LINUX_HOST_STATS_CMD = (
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
)

SSH_STATUS_CMD = (
    _LINUX_HOST_STATS_CMD +
    "L=$(ls -1tr /var/log/pi-star/MMDVM-*.log /var/log/wpsd/MMDVM-*.log 2>/dev/null | tail -1); "
    f'tail -n {LOG_TAIL_LINES} "$L" 2>/dev/null'
)


def build_asl_status_cmd(node: str) -> str:
    """SSH command for an ASL3 (AllStarLink) hotspot: same generic Linux
    temp/uptime/CPU as WPSD, plus `rpt xnode` -- which dumps app_rpt's
    dialplan variables, including RPT_ALINKS (per-linked-node keyed state,
    confirmed against a real ASL3 node -- see monitor.py's ASL3 parsing).

    Needs `sudo` -- the Asterisk control socket (asterisk.ctl) is normally
    root/asterisk-group only, and a permission error there goes to stderr,
    which this app doesn't capture, so it silently looks like "no output"
    rather than an obvious permission error. Confirmed against a real ASL3
    node, which needed `sudo asterisk -rx ...` for the same command run
    manually to produce any output at all. Assumes passwordless sudo for
    the SSH user, same assumption this app already makes for WPSD (no
    sudo password is ever supplied anywhere in this codebase).

    `node` is interpolated into a shell string executed on the remote host,
    so the caller MUST validate it's digits-only first (see app.py's /setup
    handler) -- this re-validates defensively since /setup has no auth.
    """
    if not node.isdigit():
        raise ValueError(f"invalid ASL node number: {node!r}")
    return _LINUX_HOST_STATS_CMD + f'sudo asterisk -rx "rpt xnode {node}"'


# DigiPi (KM6LYW's Raspberry Pi ham radio data hotspot) -- Direwolf isn't a
# systemd service on a real device, it's a plain background process logging
# to /run/direwolf.log; confirmed against a real DigiPi before writing this
# (see digipi.py's module docstring). 100 lines is generous enough to
# reliably catch several packets between polls at the default 5s interval.
DIGIPI_LOG_TAIL_LINES = 100


def build_digipi_status_cmd() -> str:
    return _LINUX_HOST_STATS_CMD + f"tail -n {DIGIPI_LOG_TAIL_LINES} /run/direwolf.log 2>/dev/null"


# openSPOT 4 (SharkRF) -- no SSH/shell involved at all (a closed embedded
# device controlled over HTTP + WebSocket), so unlike WPSD/ASL3/DigiPi
# above there's no shell command to build here. Just the numeric knobs;
# protocol details (endpoint paths, regexes, mode-prefix map) live local
# to openspot.py, same as digipi.py keeps its own PACKET_RE local rather
# than centralizing it here.
OPENSPOT4_HTTP_TIMEOUT      = int(os.environ.get("OPENSPOT4_HTTP_TIMEOUT", 5))
# Originally 10 -- confirmed live (real device log + the drop cadence
# matching exactly RECV_TIMEOUT + RECONNECT_BACKOFF) that 10s was too
# tight: the device doesn't reliably push *something* at least once every
# 10s even when the connection is perfectly healthy, so this was
# self-inflicted 20-second drop/reconnect cycling, not a device-side
# kick. Bumped to 60s -- still catches a genuinely dead connection
# reasonably promptly, just stops mistaking a normal quiet stretch for one.
OPENSPOT4_RECV_TIMEOUT      = int(os.environ.get("OPENSPOT4_RECV_TIMEOUT", 60))
OPENSPOT4_RECONNECT_BACKOFF = int(os.environ.get("OPENSPOT4_RECONNECT_BACKOFF", 10))


# ASL3 ilink function codes for `rpt cmd <node> ilink <code> <remotenode>` --
# NOT the DTMF-simulated `rpt fun <node> *3<remotenode>` form, which requires
# replicating app_rpt's DTMF digit-collection state machine and proved
# unreliable in practice. `rpt cmd` takes the function code and remote node
# as plain separate arguments -- confirmed both from AllScan
# (https://github.com/davidgsd/AllScan, astapi/connect.php, a widely-used
# production tool) and against a real node (W1ZLA, node 59929).
ASL_ILINK_CONNECT    = 3   # connect specified link, transceive (temporary)
ASL_ILINK_DISCONNECT = 11  # disconnect specified link


def build_asl_ilink_cmd(local_node: str, ilink_code: int, remote_node: str) -> str:
    """SSH command to connect/disconnect a link on an ASL3 hotspot.
    Both node numbers are interpolated into a shell string executed on the
    remote host, so callers MUST validate both are digits-only first (see
    app.py's /api/asl_connect handler) -- this re-validates defensively
    since /setup has no auth."""
    if not local_node.isdigit():
        raise ValueError(f"invalid ASL node number: {local_node!r}")
    if not remote_node.isdigit():
        raise ValueError(f"invalid ASL node number: {remote_node!r}")
    if ilink_code not in (ASL_ILINK_CONNECT, ASL_ILINK_DISCONNECT):
        raise ValueError(f"invalid ilink code: {ilink_code!r}")
    return f'sudo asterisk -rx "rpt cmd {local_node} ilink {ilink_code} {remote_node}"'

# --- Log line parsing ---
BER_PATTERN        = r"BER: (\d+\.?\d*)%"
# P25 reports RSSI as "RSSI: -115/-47/-54 dBm" (min/avg/max).
# Capture just the first (minimum) value; DMR reports a single value.
RSSI_PATTERN       = r"RSSI: (-?\d+)"
MODE_PATTERN       = r"DMR|D-Star|YSF|P25|NXDN"
COLOR_CODE_PATTERN = r"Colo(?:u)?r Code:\s*(\d+)"   # matches "Color Code: 15" and "Colour Code: 15"
SLOT_PATTERN       = r"DMR Slot (\d+)"               # matches "DMR Slot 2"

# ASL3 (AllStarLink): `rpt xnode <node>` prints this dialplan-variable line,
# e.g. "RPT_ALINKS=3,1603TU,622630CU,600671TU" -- count, then one
# <node><mode T/R/L/C><K keyed/U unkeyed> entry per linked node. Confirmed
# against a real ASL3 node (source: apps/app_rpt/rpt_link.c's __mklinklist()).
ASL_ALINKS_LINE_PATTERN = r"^RPT_ALINKS=(.*)$"
ASL_ALINK_ENTRY_PATTERN = r"^(\d+)([TRLC])([KU])$"

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

# --- AllStarLink stats.allstarlink.org lookup (free, no auth) ---
# Resolves an ASL3 node's currently-linked nodes to callsigns in one call --
# querying a node's own stats returns its linkedNodes array pre-resolved,
# rather than needing one lookup per linked node.
ASLSTATS_AGENT     = os.environ.get("ASLSTATS_AGENT", "hotspot-dashboard/1.0")
ASLSTATS_TIMEOUT   = int(os.environ.get("ASLSTATS_TIMEOUT", 5))
ASLSTATS_CACHE_TTL = int(os.environ.get("ASLSTATS_CACHE_TTL", 120))  # link topology changes -- short TTL

# --- Brandmeister repeater profile lookup ---
BRANDMEISTER_AGENT     = os.environ.get("BRANDMEISTER_AGENT", "hotspot-dashboard/1.0 (+https://github.com/)")
BRANDMEISTER_TIMEOUT   = int(os.environ.get("BRANDMEISTER_TIMEOUT", 5))
BRANDMEISTER_CACHE_TTL = int(os.environ.get("BRANDMEISTER_CACHE_TTL", 120))

# --- Camera cards (RTSP via ffmpeg, Bambu Labs A1 via bambulabs_api) ---
# Both camera types are bridged to a plain MJPEG multipart stream server-side
# so the frontend only ever needs a plain <img> tag -- it doesn't know or
# care which camera type it's looking at.
CAMERA_RTSP_FPS          = int(os.environ.get("CAMERA_RTSP_FPS", 5))
CAMERA_BAMBU_POLL_SEC    = float(os.environ.get("CAMERA_BAMBU_POLL_SEC", 1.5))
CAMERA_FFMPEG_TIMEOUT    = int(os.environ.get("CAMERA_FFMPEG_TIMEOUT", 10))   # seconds of silence before treating ffmpeg as dead
CAMERA_RECONNECT_BACKOFF = int(os.environ.get("CAMERA_RECONNECT_BACKOFF", 5))  # seconds between reconnect attempts
CAMERA_IDLE_STOP_SEC     = int(os.environ.get("CAMERA_IDLE_STOP_SEC", 20))   # stop the worker this long after the last viewer disconnects
CAMERA_TEST_TIMEOUT      = int(os.environ.get("CAMERA_TEST_TIMEOUT", 12))    # Settings "Test connection" button

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

# WPSD's own live MMDVMHost config -- confirmed against a real WPSD install
# (2026-07) to be /etc/mmdvmhost (no ".ini", no hyphen -- WPSD generates this
# itself from the admin panel's hardware/frequency settings; it does NOT
# match upstream MMDVMHost's own shipped template, which has no RXFrequency/
# TXFrequency at all as of the current g4klx/MMDVMHost master). Reads a
# handful of static, rarely-changing fields for the hotspot card: Freq
# (RX/TXFrequency), Duplex, and identity info (Callsign/Id/Location) from
# the [General]/[Info] sections. Scoped with an awk state machine keyed on
# the CURRENT section header, not a bare `grep -E '^(Callsign|Id|Duplex)='`
# -- confirmed against a real full config dump that "Callsign"/"Id" are
# plausible/common key names other sections (DMR/D-Star/NXDN network
# blocks, etc.) could reasonably reuse for their own unrelated purposes,
# so anchoring by key name alone risks silently grabbing the wrong line if
# either ever appears again further down the file. RXFrequency/TXFrequency
# specifically also needs this scoping for the same reason as before:
# CallsignFrequency (CW ID interval), AckFrequency (ack tone Hz),
# CTCSSFrequency (FM sub-tone Hz), and a bare Frequency= key all exist
# elsewhere in this same file and are unrelated to the radio's actual
# RX/TX frequency.
HOTSPOT_INFO_CHECK_CMD = (
    "awk -F= '/^\\[/{s=$0} "
    "s==\"[General]\" && /^(Callsign|Id|Duplex)=/ {print} "
    "s==\"[Info]\" && /^(RXFrequency|TXFrequency|Location)=/ {print}' "
    "/etc/mmdvmhost 2>/dev/null"
)

# --- Dashboard defaults (overridden by settings.json) ---
DEFAULT_SETTINGS = {
    "dashboard_name": "W1ZLA Hotspot Dashboard",
    "dark_mode": True,
    # Unused since the "instrument panel" reskin was promoted to be the
    # only dashboard (v3.53) -- there's no separate classic/beta choice
    # left to make. Kept only for backward compat with old settings.json
    # files that already have it saved; nothing reads it anymore.
    "use_beta_dashboard": False,
    # A short synthesized courtesy-tone beep (Web Audio, no audio file)
    # when any hotspot's active call starts. Key kept "beta_"-prefixed
    # from before the reskin was promoted -- renaming would just be churn
    # for no functional gain.
    "beta_courtesy_tone": False,
    # Dim/desaturate idle hotspot cards while any one is active, so the
    # live card visually pops. On by default (existing behavior); a
    # toggle since some users find the dimming distracting. Same
    # "beta_"-prefix-kept-for-no-reason-to-rename note as above.
    "beta_spotlight_dimming": True,
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
    # ASL favorites & control card -- off by default, same reasoning as
    # fleet activity (adds a new persisted list + a write-capable feature,
    # opt-in rather than on by default).
    "show_asl_favorites": False,
    # Where the ASL favorites card sits among the hotspot cards -- same
    # scheme as fleet_activity_position (0 = first, N = after the Nth
    # hotspot card), set from the Card order drag list.
    "asl_favorites_position": 0,
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
    # Self-update check (Version tab) -- compares the locally deployed
    # build's commit (BUILD_COMMIT, written by install.sh/update.sh/
    # docker-update.sh) against the latest commit on this branch via the
    # git host's REST API (Forgejo/Gitea-compatible). Defaults point at
    # this project's own repo; override if you run a fork.
    "update_check_enabled": True,
    "update_check_repo": "https://git.trytheitguy.com/rodney_berry/W1ZLAHotspot_Dashboard",
    "update_check_branch": "main",
    # Camera cards -- off by default, since it's a new external process
    # (ffmpeg) per RTSP camera and pulls in a new pip dependency
    # (bambulabs_api) for Bambu Labs cameras. Individual cameras (and their
    # position among the hotspot cards) live in cameras.json, same
    # dynamic-list treatment as hotspots.json -- this is just the master
    # on/off switch for the whole feature.
    "show_cameras": False,
    # APRS Messages card -- off by default. Receives APRS-IS messages
    # addressed to aprs_msg_callsign (the same "your callsign" setting the
    # outbound favorite-alert feature already uses -- one identity, two
    # independent on/off features). Position among the hotspot cards uses
    # the same single-sentinel scheme as Fleet Activity/ASL Favorites.
    "aprs_inbox_enabled": False,
    "aprs_inbox_position": 0,
    # DigiPi card -- off by default. SSH connection details for a single
    # DigiPi (digipi.py polls Direwolf's /run/direwolf.log for APRS
    # activity + the same generic Linux temp/CPU/uptime stats every
    # hotspot already shows). Single-device config, same precedent as
    # station_grid/aprs_msg_callsign, not a hotspots.json-style list --
    # this project only has one to support right now. Position uses the
    # same single-sentinel scheme as Fleet Activity/ASL Favorites/etc.
    "digipi_enabled": False,
    "digipi_ip": "",
    "digipi_user": "",
    "digipi_pass": "",
    "digipi_position": 0,
    # HF Conditions card -- off by default. Solar/band propagation data
    # from N0NBH's free public feed (hf_conditions.py), no API key.
    # Position among the hotspot cards uses the same single-sentinel
    # scheme as Fleet Activity/ASL Favorites/APRS Messages.
    "show_hf_conditions": False,
    "hf_conditions_position": 0,
    # Band Plan card -- off by default. Purely static FCC Part 97.301
    # reference data baked into dashboard.html, no backend client at all
    # (the only card in the app with no Python module of its own).
    "show_band_plan": False,
    "band_plan_position": 0,
    # License Quiz card -- off by default. One random question at a time
    # from the bundled Extra (Element 4) pool (license_quiz.py), rotating
    # every ~10 min. Per-section accuracy stats live in the browser's
    # localStorage, not here -- this app has no user accounts, so "your"
    # progress can only mean "this browser's" progress.
    "show_license_quiz": False,
    "license_quiz_position": 0,
    # Band Activity card -- off by default. Live WSPR beacon-spot counts
    # (wspr_activity.py, wspr.live) within RADIUS_METERS of station_grid,
    # a fundamentally different kind of data than HF Conditions' N0NBH
    # prediction (real observed activity, not a solar-index forecast).
    # Blank station_grid disables it even if the toggle is on, since
    # there's no location to filter by.
    "show_wspr_activity": False,
    "wspr_activity_position": 0,
    "station_grid": "",
    # Big Ass Clock card -- off by default. Purely client-side (Date/
    # Intl.DateTimeFormat in dashboard.html), no backend module at all --
    # same "no Python client" precedent as the Band Plan card. Style
    # (digital/analog/tix), 12/24hr format, and second-clock/timezone are
    # all per-browser localStorage preferences (nothing to sync across
    # devices, same split as map style/grey-line default), so this only
    # needs a show/position pair like every other optional card.
    "show_big_clock": False,
    "big_clock_position": 0,
    # Satellites card + Live map ground-track overlay -- off by default.
    # tracked_satellites is a user-editable list (norad_id/name/mode/
    # downlink_mhz/uplink_mhz per entry), defaulting to a small set of
    # live-verified-active ham satellites (see satellites.py's own
    # docstring for exactly how/when each was verified -- don't extend
    # this default list from memory, satellite operational status
    # changes over time). Observer position reuses station_grid, same
    # as wspr_activity.py/psk_reporter.py -- no separate setting needed.
    "show_satellites": False,
    "satellites_position": 0,
    "tracked_satellites": [
        {"norad_id": 25544, "name": "ISS",    "mode": "APRS", "downlink_mhz": 145.825, "uplink_mhz": 145.825},
        {"norad_id": 27607, "name": "SO-50",  "mode": "FM",   "downlink_mhz": 436.795, "uplink_mhz": 145.850},
        {"norad_id": 43017, "name": "AO-91",  "mode": "FM",   "downlink_mhz": 145.960, "uplink_mhz": 435.250},
        {"norad_id": 43678, "name": "PO-101", "mode": "FM",   "downlink_mhz": 145.900, "uplink_mhz": 437.500},
    ],
    # Recent Contacts card -- off by default. Reads the same qsos.json
    # both the ADIF importer and wsjtx.py's live logger write to, newest
    # first -- no separate data source, just a different view onto QSOs
    # already on the Live map.
    "show_recent_contacts": False,
    "recent_contacts_position": 0,
    # QSO Stats card -- off by default. Pure client-side aggregation over
    # the same qsos.json Recent Contacts/the map already read -- no new
    # data source, no new backend computation (see /api/qsos).
    "show_qso_stats": False,
    "qso_stats_position": 0,
    # Top 5 activity card -- off by default. Ranks the fleet's own most
    # active CALLSIGNS (who transmitted, not which talkgroup/node) over a
    # trailing window (default 24h) -- reuses storage_activity.py's
    # existing activity_log (same opt-in show_fleet_activity gate for
    # whether rows get logged at all), just a different aggregate query.
    # Deliberately own-fleet only, not a network-wide "what's busy right
    # now" feed -- see CLAUDE.md for the Brandmeister/TGIF/AllStarLink/
    # YSF research that ruled that out as a clean REST-pollable option.
    "show_top_activity": False,
    "top_activity_position": 0,
    # Live WSJT-X QSO logging -- off by default. Listens for WSJT-X's own
    # UDP telemetry protocol (the same feed GridTracker/JTAlert use) and
    # appends each logged QSO to the same qsos.json the ADIF importer
    # writes to, so it shows up on the Live map with zero frontend
    # changes. No position setting -- this doesn't add a card, it just
    # feeds the existing QSO map layer. See wsjtx.py.
    "wsjtx_enabled": False,
    "wsjtx_port": 2237,
    # PSK Reporter overlay for the Live map -- off (no callsign) by
    # default. Unlike POTA (a free public feed needing no per-user
    # config, just a per-browser localStorage toggle like grey-line/
    # aurora), this needs to know "my callsign" to query
    # retrieve.pskreporter.info for -- blank disables the map-legend
    # checkbox's data, same "blank disables it" convention as
    # station_grid/digipi_ip. See psk_reporter.py.
    "psk_reporter_callsign": "",
    # One-time "here's what's available" tour panel on the Dashboard tab,
    # shown once a hotspot exists. Default here is True (already-seen) --
    # this value is what an EXISTING install's settings.json merges
    # against when this key is absent from its saved file (an upgrade,
    # not a fresh install), so upgrading never makes the tour pop up
    # unexpectedly. A genuinely fresh install (settings.json doesn't
    # exist yet at all) gets False instead, via a special case in
    # storage.py's load_settings() -- NOT from this default. Don't
    # "simplify" by changing this default without re-reading that
    # special case, or existing installs will start seeing the tour.
    "onboarding_tour_seen": True,
    # Explicit relative-order tiebreak for the "extra card" drag list
    # (Settings -> Cards). Each *_position field only ever records "how
    # many hotspot rows precede this card" -- a real, reported bug: two+
    # cards dragged to sit on the SAME side of every hotspot (extremely
    # common, e.g. "all after the last hotspot") end up computing the
    # IDENTICAL saved position no matter their relative order, since
    # that's genuinely all a single "count of preceding hotspots" number
    # can represent. This list (of "__key__"-style sentinel/camera ids,
    # in the user's last-dragged order) is the secondary sort key used
    # to break that tie -- empty by default, which preserves today's
    # exact fallback behavior (declared order in app.py's _SENTINEL_DEFS)
    # for any card never yet explicitly reordered relative to a sibling,
    # so this is purely additive and never surprises an existing install.
    "card_order_tiebreak": [],
    # HamAlert notification card -- off by default. Persistent Telnet
    # connection to hamalert.org:7300 (a personal DXCC-needed/callsign/
    # band alert service), needs its own account credentials since this
    # isn't shared with any other integration here. Position uses the
    # same single-sentinel scheme as Fleet Activity/APRS Messages/etc.
    # See hamalert.py.
    "hamalert_enabled": False,
    "hamalert_username": "",
    "hamalert_password": "",
    "hamalert_position": 0,
    # Notifications card (merged APRS Messages + HamAlert display, v3.51,
    # later joined by fleet/solar alerts) -- each source setting below
    # stays independent (gates its own data source); notifications_position
    # only controls where the ONE merged card sits.
    # aprs_inbox_position/hamalert_position are now unused dead
    # settings, kept only for backward compat with old settings.json files.
    "notifications_position": 0,
    # Fleet online/offline transition alerts in the Notifications card --
    # off by default. No new connection/poll -- monitor.py already tracks
    # offline_since for the card badges; this just surfaces the moment it
    # flips as a notification event (see FleetMonitor.fleet_events()).
    "fleet_alerts_enabled": False,
    # Geomagnetic-storm alerts in the Notifications card -- off by default.
    # No new connection/poll -- hf_conditions.py already fetches K-index
    # hourly for the HF Conditions card; this just surfaces a Kp>=5
    # threshold crossing as a notification event (see
    # HfConditionsClient.alert_events()).
    "solar_alerts_enabled": False,
    # Brandmeister network-wide favorite-activity alerts in the
    # Notifications card -- off by default. Persistent WebSocket to
    # Brandmeister's own public "Last Heard" Socket.IO feed (no account/
    # credentials needed, unlike HamAlert) -- see brandmeister_lastheard.py
    # for how the live endpoint was found and verified. Notifies when a
    # favorite callsign (favorites.json) keys up ANYWHERE on the
    # Brandmeister network, not just through this fleet's own hotspots.
    "brandmeister_alerts_enabled": False,
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
