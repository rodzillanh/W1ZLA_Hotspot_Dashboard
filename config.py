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
APP_VERSION = "4.12"
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
    "Cocker",           # v3.63 -- Joe Cocker (1944-2014)
    "Berry",            # v3.64 -- Chuck Berry (1926-2017)
    "Bennington",       # v3.65 -- Chester Bennington (1976-2017)
    "Staley",           # v3.66 -- Layne Staley (1967-2002)
    "Ramone",           # v3.67 -- Joey Ramone (1951-2001)
    "Vicious",          # v3.68 -- Sid Vicious (1957-1979)
    "Holly",            # v3.69 -- Buddy Holly (1936-1959)
    "Orbison",          # v3.70 -- Roy Orbison (1936-1988)
    "Cash",             # v3.71 -- Johnny Cash (1932-2003)
    "Prince",           # v3.72 -- Prince (1958-2016)
    "Morrison",         # v3.73 -- Jim Morrison (1943-1971) -- corrected from a
                        # duplicate "Cobain" (already used at v3.52) caught
                        # while cutting v3.74
    "Vaughan",          # v3.74 -- Stevie Ray Vaughan (1954-1990)
    "Scott",            # v3.75 -- Bon Scott, AC/DC (1946-1980)
    "Bolan",            # v3.76 -- Marc Bolan, T. Rex (1947-1977)
    "Danko",            # v3.77 -- Rick Danko, The Band (1943-1999)
    "Garcia",           # v3.78 -- Jerry Garcia, Grateful Dead (1942-1995)
    "Zappa",            # v3.79 -- Frank Zappa (1940-1993)
    "Diddley",          # v3.80 -- Bo Diddley (1928-2008)
    "Kilmister",        # v3.81 -- Lemmy Kilmister, Motorhead (1945-2015)
    "Hutchence",        # v3.82 -- Michael Hutchence, INXS (1960-1997)
    "Buckley",          # v3.83 -- Jeff Buckley (1966-1997)
    "Marley",           # v3.84 -- Bob Marley (1945-1981)
    "Ronson",           # v3.85 -- Mick Ronson, guitarist, David Bowie's
                        # Spiders from Mars (1946-1993)
    "Curtis",           # v3.86 -- Ian Curtis, Joy Division (1956-1980)
    "Bolin",            # v3.87 -- Tommy Bolin, guitarist, Deep Purple/
                        # James Gang (1951-1976)
    "Entwistle",        # v3.88 -- John Entwistle, The Who (1944-2002)
    "Rossington",       # v3.89 -- Gary Rossington, Lynyrd Skynyrd (1951-2023)
    "Wilson",           # v3.90 -- Dennis Wilson, The Beach Boys (1944-1983)
    "Burton",           # v3.91 -- Cliff Burton, Metallica bassist (1962-1986)
    "Watts",            # v3.92 -- Charlie Watts, The Rolling Stones (1941-2021)
    "Moore",            # v3.93 -- Gary Moore, guitarist, Thin Lizzy/solo (1952-2011)
    "Gaines",           # v3.94 -- Steve Gaines, Lynyrd Skynyrd (1949-1977)
    "Kath",             # v3.95 -- Terry Kath, Chicago (1946-1978)
    "Whitten",          # v3.96 -- Danny Whitten, Crazy Horse (1943-1972)
    "Bloomfield",       # v3.97 -- Mike Bloomfield, guitarist, Paul
                        # Butterfield Blues Band (1943-1981)
    "Jones",            # v3.98 -- Davy Jones, The Monkees (1945-2012)
    "Frehley",          # v3.99 -- Ace Frehley, KISS guitarist (1951-2025)
    "Allman",           # v4.0 -- Duane Allman, Allman Brothers Band (1946-1971)
    "Lynott",           # v4.1 -- Phil Lynott, Thin Lizzy (1949-1986)
    "Bruce",            # v4.2 -- Jack Bruce, bassist, Cream (1943-2014)
    "Clemons",          # v4.3 -- Clarence Clemons, saxophonist, Bruce
                        # Springsteen's E Street Band (1942-2011)
    "Kirwan",           # v4.4 -- Danny Kirwan, guitarist, Fleetwood Mac
                        # (1950-2018)
    "Winter",           # v4.5 -- Johnny Winter, blues-rock guitarist
                        # (1944-2014)
    "Balin",            # v4.6 -- Marty Balin, singer, Jefferson Airplane
                        # (1942-2018)
    "Hopkins",          # v4.7 -- Nicky Hopkins, session pianist for the
                        # Rolling Stones, the Kinks, the Beatles (1944-1994)
    "Stewart",          # v4.8 -- Ian Stewart, pianist and founding member
                        # of the Rolling Stones (1938-1985)
    "Nilsson",          # v4.9 -- Harry Nilsson, singer-songwriter
                        # (1941-1994)
    "Lee",              # v4.10 -- Alvin Lee, guitarist, Ten Years After
                        # (1944-2013)
    "Grech",            # v4.11 -- Ric Grech, bassist, Blind Faith/Family
                        # (1946-1990)
    "Federici",         # v4.12 -- Danny Federici, keyboardist, Bruce
                        # Springsteen's E Street Band (1950-2008)
]
APP_CODENAME = VERSION_CODENAMES[-1]

# --- Storage ---
CONFIG_DIR   = os.environ.get("CONFIG_DIR", "/app/data")
CONFIG_FILE  = os.path.join(CONFIG_DIR, "hotspots.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
FAVORITES_FILE = os.path.join(CONFIG_DIR, "favorites.json")
ASL_FAVORITES_FILE = os.path.join(CONFIG_DIR, "asl_favorites.json")
# Max ASL favorites shown as tiles on the compact ASL Favorites card at
# once (v4.0) -- the rest still exist as real favorites, managed/pinnable
# from the ASL Control drawer, they just don't take up card space. Kept
# in sync manually with dashboard.html's own ASL_FAV_CARD_CAP JS constant,
# same "no shared source of truth across Python/JS" tradeoff as every
# other client-side-mirrored constant in this project.
ASL_FAV_CARD_CAP = 5
BM_TG_FAVORITES_FILE = os.path.join(CONFIG_DIR, "bm_tg_favorites.json")
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


# DVSwitch (Analog_Bridge) bridge traffic, as an opt-in 6th mode alongside
# DMR/D-Star/YSF/P25/NXDN on the Fleet Activity card -- confirmed real via
# Analog_Bridge's own shipped systemd unit + logrotate config (not guessed):
# `Environment=AnalogBridgeLogDir=/var/log/dvswitch` in analog_bridge.service,
# and the logrotate config's own path confirms the resulting filename.
# `Analog_Bridge.ini` itself has NO `[Log]` section despite the name --
# only a bare `logLevel` under `[GENERAL]`; the sibling MMDVM_Bridge.ini
# does have a real `[Log]` section, a different component. 20 lines is
# generous for the default 5s poll interval, same reasoning as
# DIGIPI_LOG_TAIL_LINES above.
DVSWITCH_LOG_PATH        = "/var/log/dvswitch/Analog_Bridge.log"
DVSWITCH_LOG_TAIL_LINES  = 20
# The "Begin TX:" line format, CONFIRMED against a real live device (a
# user's own SSH session, not just an old GitHub issue quote) --
# "Begin TX: src=3100486 rpt=310048611 dst=603 slot=2 cc=0 call=W1ZLA".
# The trailing field varies: a 2020 GitHub issue quote showed
# "metadata=<n>" instead of "call=<callsign>" in that position -- both are
# real, just different Analog_Bridge versions/configs; monitor.py checks
# for call= first and falls back to the bare src= DMR ID, never assumes
# metadata= means anything. There is still NO confirmed end-of-transmission
# line in THIS log (Analog_Bridge.log) -- monitor.py logs one Fleet
# Activity row per NEWLY SEEN start line instead of per completed
# transmission, a real disclosed asymmetry, not an oversight. See
# CLAUDE.md for the full research trail before touching this.
DVSWITCH_BEGIN_TX_PATTERN = r"Begin TX:"
DVSWITCH_TX_SRC_PATTERN   = r"\bsrc=(\d+)"
DVSWITCH_TX_DST_PATTERN   = r"\bdst=(\S+)"
DVSWITCH_TX_CALL_PATTERN  = r"\bcall=(\S+)"
# Both directions of this are now confirmed live against the same real
# device, same day: "DV3000 not found at 127.0.0.1:2460 (Reset failed)"
# -> "Using software MBE decoder version 1.2.3" (before a config fix), and
# after fixing Analog_Bridge.ini's [DV3000] section, "Connecting to DV3000
# hardware......" -> "Begin DV3000 decode" -> "Using hardware AMBE
# vocoder" (a real, confirmed SUCCESS message -- an earlier version of
# this file said no such message was known to exist; it does).
# monitor.py's _parse_dvswitch_vocoder() now asserts "hardware" only when
# this success text is actually seen, not just "no fallback seen" --
# a real, meaningfully stronger claim now that the positive text is known.
DVSWITCH_SOFTWARE_FALLBACK_PATTERN = "Using software"
DVSWITCH_HARDWARE_VOCODER_PATTERN  = "Using hardware AMBE vocoder"
# grep's -m1 stops at the FIRST match in the file -- wrong here, since a
# single (not-yet-rotated) day's log can genuinely contain BOTH an older
# software-fallback line and a newer hardware-success line, e.g. right
# after fixing a DV3000 config issue and restarting Analog_Bridge (a real
# sequence observed live, not hypothetical). Matching both patterns and
# taking the LAST line via `tail -1` in build_asl_status_cmd (not `-m1`)
# reflects current state, not whichever happened first that day.
DVSWITCH_VOCODER_GREP_PATTERN = f"{DVSWITCH_HARDWARE_VOCODER_PATTERN}|{DVSWITCH_SOFTWARE_FALLBACK_PATTERN}"

# MMDVM_Bridge.log -- a DIFFERENT DVSwitch component's log than
# Analog_Bridge.log above (confirmed real, same live device/session as
# every other DVSwitch fact in this file): unlike Analog_Bridge.log, this
# one has real, confirmed end-of-transmission lines with actual duration/
# loss/BER, plus explicit link-status lines -- the two things
# Analog_Bridge.log genuinely doesn't have. Scoped to DMR + D-Star only
# (by user choice) -- YSF/P25/NXDN each write to their OWN separate log
# file (YSFGateway-*.log/P25Gateway-*.log/NXDNGateway-*.log, confirmed via
# a real `ls /var/log/mmdvm/`), not covered here.
#
# Path is DATE-STAMPED (MMDVM_Bridge-YYYY-MM-DD.log, confirmed via a real
# `ls -la /var/log/mmdvm/` -- a same-named but empty `MMDVM_Bridge.log`
# also exists there and is NOT the active file, don't tail that one) --
# resolved the same dynamic "newest matching file" way WPSD's own
# SSH_STATUS_CMD already does for its differently-pathed MMDVM-*.log.
DVSWITCH_MMDVM_LOG_GLOB       = "/var/log/mmdvm/MMDVM_Bridge-*.log"
DVSWITCH_MMDVM_TAIL_LINES     = 40
# Real confirmed line shapes (quoted verbatim from a live capture):
#   "DMR Slot 2, received network voice header from W1ZLA to TG 603"
#   "DMR Slot 2, received network late entry from W1ZLA to TG 603"
#   "DMR Slot 2, received network end of voice transmission, 2.6 seconds, 0% packet loss, BER: 0.0%"
#   "DMR Slot 2, network watchdog has expired, 0.1 seconds, 0% packet loss, BER: 0.0%"
#   "D-Star, received network header from W1ZLA   /INFO to CQCQCQ  "
#   "D-Star, received network end of transmission, 2.5 seconds, 0% packet loss, BER: 0.0%"
#   "DMR, Logged into the master successfully: tgif.network:62031"
#   "DMR, Connection to the master has timed out, retrying connection"
#   "DMR, Closing DMR Network" / "DMR, Opening DMR Network"
#   'D-Star link status set to "Not linked          "'
# "network watchdog has expired" is a REAL, confirmed-live edge case: it
# fired mid-transmission (a brief network hiccup), immediately followed by
# a "late entry" resuming the SAME transmission a few milliseconds later.
# It shares the end-of-transmission line's own trailing shape (duration/
# loss/BER), so it's matched as an end-like event too -- this means a live
# RX indicator can flicker idle-then-active-again within milliseconds for
# this case, invisible at this app's 5s poll cadence, so no special-casing
# beyond treating both as "this slot went idle" was needed.
DVSWITCH_MMDVM_DMR_START_PATTERN   = r"DMR Slot (\d+), received network (?:voice header|late entry) from (\S+) to (.+)"
DVSWITCH_MMDVM_DMR_END_PATTERN     = r"DMR Slot (\d+), (?:received network end of voice transmission|network watchdog has expired), ([\d.]+) seconds"
DVSWITCH_MMDVM_DSTAR_START_PATTERN = r"D-Star, received network header from (\S+)\s*/\S*\s+to\s+(.+)"
DVSWITCH_MMDVM_DSTAR_END_PATTERN   = r"D-Star, received network end of transmission, ([\d.]+) seconds"
DVSWITCH_MMDVM_DMR_LINKED_PATTERN   = r"DMR, Logged into the master successfully"
DVSWITCH_MMDVM_DMR_UNLINKED_PATTERN = r"DMR, (?:Closing DMR Network|Connection to the master has timed out)"
DVSWITCH_MMDVM_DSTAR_LINK_PATTERN   = r'D-Star link status set to "(.+?)"'

# Section markers this app's own SSH command echoes between the DVSwitch
# sub-commands below, so monitor.py can reliably split one combined
# command's output back into named sections (tail / vocoder grep / one
# ABInfo.json per configured port / MMDVM_Bridge.log tail) rather than
# guessing by line position -- the ASL/host-stats/DVSwitch output all
# lands in one shell string, per one `_ssh_exec()` connect/exec/close
# cycle (see build_asl_status_cmd).
DVSWITCH_TAIL_MARKER    = "===DVSWITCH_TAIL==="
DVSWITCH_VOCODER_MARKER = "===DVSWITCH_VOCODER==="
DVSWITCH_ABINFO_MARKER  = "===DVSWITCH_ABINFO==="  # this app appends the port number right after, e.g. "===DVSWITCH_ABINFO===31000"
DVSWITCH_MMDVM_MARKER   = "===DVSWITCH_MMDVM==="


def build_asl_status_cmd(
    node: str, dvswitch_enabled: bool = False, dvswitch_ports: list[str] | None = None
) -> str:
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

    `dvswitch_enabled` appends a tail of DVSwitch's (Analog_Bridge) own log
    onto this SAME one-shot SSH command/connection -- `_ssh_exec()` opens a
    fresh connection, runs one command, and closes it every poll tick, with
    no persistent session to attach a second command to, so this has to be
    one more `;`-chained clause, not a separate call. Log path/directory
    confirmed from Analog_Bridge's own real systemd unit and logrotate
    config (`AnalogBridgeLogDir=/var/log/dvswitch`, no `[Log]` section in
    Analog_Bridge.ini itself despite the name suggesting one) -- see
    CLAUDE.md for the full research trail and its real, disclosed gaps
    (no confirmed end-of-transmission line, unlike every other mode here).
    Also tails MMDVM_Bridge.log (a different DVSwitch component's log,
    scoped to DMR + D-Star only) for live RX/TX state and link status --
    see DVSWITCH_MMDVM_* constants' own comments for why that log needed
    separate handling (date-stamped filename, real end-of-transmission
    lines this one has that Analog_Bridge.log doesn't).

    `dvswitch_ports` (DVSwitch card only, independent of the Fleet Activity
    mode above) is a list of Analog_Bridge instance ports configured for
    this hotspot -- confirmed real that multiple instances can run on one
    node, each addressed by its own port via /tmp/ABInfo_<port>.json (see
    Analog_Bridge.ini's own [USRP] section comment and dvswitch.sh's
    getABInfoFileName()). Each port gets its own `cat` appended, wrapped in
    an echo'd marker so monitor.py can split the combined output back into
    per-port sections -- same one-shot-connection reasoning as the log
    tail above, not a separate SSH call per port. Non-digit ports are
    silently skipped (defensive re-validation; app.py's /setup handler is
    the primary gate, same pattern as `node` above).
    """
    if not node.isdigit():
        raise ValueError(f"invalid ASL node number: {node!r}")
    cmd = _LINUX_HOST_STATS_CMD + f'sudo asterisk -rx "rpt xnode {node}"'
    if dvswitch_enabled:
        cmd += (
            f"; echo {DVSWITCH_TAIL_MARKER}"
            f"; tail -n {DVSWITCH_LOG_TAIL_LINES} {DVSWITCH_LOG_PATH} 2>/dev/null"
            f"; echo {DVSWITCH_VOCODER_MARKER}"
            # Matches BOTH the hardware-success and software-fallback lines,
            # then `tail -1` picks whichever happened MOST RECENTLY -- not
            # `grep -m1` (first match), which would keep reporting a stale
            # earlier-that-day result once the file has both (see this
            # constant's own comment above).
            f'; grep -E "{DVSWITCH_VOCODER_GREP_PATTERN}" {DVSWITCH_LOG_PATH} 2>/dev/null | tail -1'
            f"; echo {DVSWITCH_MMDVM_MARKER}"
            # MMDVM_Bridge.log's own path is date-stamped (unlike
            # Analog_Bridge.log's fixed name) -- same "newest matching
            # file" resolution _LINUX_HOST_STATS_CMD's sibling
            # SSH_STATUS_CMD already uses for WPSD's differently-pathed
            # MMDVM-*.log, not a new pattern.
            f'; M=$(ls -1tr {DVSWITCH_MMDVM_LOG_GLOB} 2>/dev/null | tail -1)'
            f'; tail -n {DVSWITCH_MMDVM_TAIL_LINES} "$M" 2>/dev/null'
        )
        for port in (dvswitch_ports or []):
            if not port.isdigit():
                continue
            cmd += f"; echo {DVSWITCH_ABINFO_MARKER}{port}; cat /tmp/ABInfo_{port}.json 2>/dev/null"
    return cmd


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
# Monitor = receive-only link (you hear the remote node, your own audio
# doesn't go out to it). Confirmed directly from AllScan's connect.php
# (fetched 2026-08, same file the two codes above were already verified
# against) rather than recalled from memory -- that file's switch
# statement maps button=='monitor' -> ilink 2 (non-permanent) / 12
# (permanent). This app has no "permanent" connection concept anywhere
# (every ASL3 link here is a session link), so only the non-permanent
# code is used.
ASL_ILINK_MONITOR = 2
# Local Monitor -- also receive-only, but does NOT relay what it hears
# onward to your other connected links the way plain Monitor above does.
# Re-confirmed against a fresh fetch of AllScan's connect.php (2026-08):
# button=='localmonitor' -> ilink 8 (non-permanent) / 18 (permanent) --
# only the non-permanent code is used, same reasoning as Monitor.
ASL_ILINK_LOCAL_MONITOR = 8
# Disconnect ALL links on the node -- NOT the same code as
# ASL_ILINK_DISCONNECT above (which targets one specific node). AllScan's
# connect.php sends this exact code (6) whenever its 'disconnect' button
# is invoked with remotenode=='0' -- confirmed from the same fetch as
# ASL_ILINK_LOCAL_MONITOR. Deliberately never exposed with a
# caller-supplied remote node; see api_asl_connect() in app.py, which
# hardcodes "0" server-side rather than trusting whatever the client sent.
ASL_ILINK_DISCONNECT_ALL = 6


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
    if ilink_code not in (ASL_ILINK_CONNECT, ASL_ILINK_MONITOR, ASL_ILINK_DISCONNECT,
                           ASL_ILINK_LOCAL_MONITOR, ASL_ILINK_DISCONNECT_ALL):
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
# <node><mode T/R/C><K keyed/U unkeyed> entry per linked node. Confirmed
# against a real ASL3 node (source: apps/app_rpt/rpt_link.c's __mklinklist()).
# NOTE: `L` was originally believed to be a real mode letter seen here too
# (same source), but live verification (2026-08, see CLAUDE.md) found a
# confirmed Local Monitor link (ilink 8) NEVER appears in RPT_ALINKS at
# all, under any letter -- see ASL_CONNTABLE_LINE_PATTERN below for how
# this app actually detects Local Monitor links instead. The regex still
# accepts L defensively in case some app_rpt version/config does emit it.
ASL_ALINKS_LINE_PATTERN = r"^RPT_ALINKS=(.*)$"
ASL_ALINK_ENTRY_PATTERN = r"^(\d+)([TRLC])([KU])$"

# The raw IAX2-level connection table at the very TOP of `rpt xnode`'s own
# output (before RPT_ALINKS), e.g.:
#   43136     208.113.166.28      0           OUT        01:02:06            ESTABLISHED
# Columns: node, IP/host, an unidentified numeric field (unused here),
# direction (IN/OUT), duration, state. Confirmed live (2026-08, a real
# W1ZLA node, reported directly by the user) that a Local Monitor link
# (ilink 8) shows up HERE but gets NO entry in RPT_ALINKS at all --
# presumably because "doesn't relay to your other links" is implemented
# by app_rpt as "never added to the link table in the first place," not
# as a distinct mode letter the way the ASL_ALINK_ENTRY_PATTERN comment
# above previously assumed (that assumption was sourced from reading
# rpt_link.c, not live-tested against a real Local Monitor connection --
# this is the correction). monitor.py treats any node ESTABLISHED here
# but absent from RPT_ALINKS as an inferred Local Monitor link -- a
# reasonable, evidence-based inference, not a directly-reported fact.
ASL_CONNTABLE_LINE_PATTERN = r"^(\d+)\s+\S+\s+\S+\s+(IN|OUT)\s+[\d:]+\s+(\S+)\s*$"

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
# ASL Control's per-favorite Rx%/LCnt/status is far slower-moving than link
# topology (a busy-ness % over the node's whole uptime, not "who's linked
# right now") -- a longer, separate TTL than ASLSTATS_CACHE_TTL cuts how
# often a full-favorites-list refetch burst has to happen at all.
ASLSTATS_FAVORITE_CACHE_TTL = int(os.environ.get("ASLSTATS_FAVORITE_CACHE_TTL", 300))
# stats.allstarlink.org's own documented cap is 30 req/min for the whole
# source IP (confirmed live 2026-08: X-RateLimit-Limit: 30) -- shared across
# every favorite fetched AND monitor.py's own separate link-topology polling.
# A cold cache (e.g. right after a restart, or every ASLSTATS_FAVORITE_CACHE_TTL
# once several favorites' entries expire in lockstep, since they were all
# populated in the same original burst) used to fire one request per favorite
# back-to-back with zero spacing -- confirmed live to trip a 429 for every
# node in that burst at once ("Stats unavailable" on every single favorite
# simultaneously, even ones known to be real/registered). This throttles
# AslStatsClient's own outbound requests to at most one every this many
# seconds, so a 15-favorite cold-cache burst spreads over ~9s instead of
# landing in under 1s.
ASLSTATS_MIN_LIVE_INTERVAL_SEC = float(os.environ.get("ASLSTATS_MIN_LIVE_INTERVAL_SEC", 0.6))

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
    "update_check_repo": "https://git.trytheitguy.com/rodney_berry/W1ZLA_Hotspot_Dashboard",
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
    # DVSwitch card position (v3.99) -- one consolidated card covering
    # every DVSwitch-enabled ASL3 hotspot via its own "Show:" node picker,
    # not one card per hotspot. Unlike every other *_position setting
    # here there's no matching "show_*" toggle -- enablement is derived
    # from hotspots.json (any hotspot with dvswitch_enabled set), the
    # same way show_cameras' absence is handled for cameras.
    "dvswitch_position": 0,
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
    # Personal Brandmeister v2 API key (settings.json-only, no env var
    # fallback -- same convention as every integration added after QRZ).
    # Generated from the user's own Brandmeister dashboard (Profile
    # Settings -> API Keys), used as an "Authorization: Bearer <key>"
    # header against the confirmed-live v2 write endpoints
    # (POST/DELETE .../device/{id}/talkgroup) -- see brandmeister.py.
    # Read-only lookups (brandmeister.py's existing BrandmeisterClient.lookup())
    # need no key at all; this only gates the hotspot card drawer's
    # link/unlink talkgroup controls.
    "brandmeister_api_key": "",
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
