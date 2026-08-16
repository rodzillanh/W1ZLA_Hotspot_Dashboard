#!/usr/bin/env bash
# =============================================================================
# W1ZLA Hotspot Dashboard -- ASL3 live audio-level VU meter provisioning
#
# Run THIS SCRIPT WITH SUDO, ON THE ASL3 NODE ITSELF (not on the dashboard
# host), once per node you want a real audio-level meter for:
#
#   sudo bash provision-audio-meter.sh <node-number>
#
# It never touches your existing extensions.conf/modules.conf CONTENT --
# only appends an #include line (if not already present) and a small
# per-node dialplan file of its own. Safe to re-run for the same node
# (idempotent: the per-node dialplan file and #include line are only ever
# written once, module loading/noload-uncommenting is a no-op if already
# done).
#
# What this does NOT need: no new credential, no new port opened on the
# dashboard host's firewall/Docker config. The dashboard reuses the SSH
# login already stored for this hotspot to trigger a spy call and tunnel
# its audio back over that same connection (an SSH reverse port forward)
# -- see config.py's ASL_AUDIO_* constants and asl_audio.py's module
# docstring for the full design rationale, and CLAUDE.md for the live
# diagnostic session (a real W1ZLA/node 59929 SSH capture, 2026-08) this
# was built from. After running this, just tick "Live audio-level VU
# meter" for this hotspot in the dashboard's Settings -> Hotspots (or its
# card's own gear-icon drawer) -- nothing else to configure.
# =============================================================================

set -euo pipefail

# --- colours (same convention as install.sh) ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[.]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[X]${NC} $*"; exit 1; }
header()  { echo -e "\n${BOLD}$*${NC}"; }

header "ASL3 live audio meter provisioning"

# --- args / preconditions ---
NODE="${1:-}"
if [ -z "$NODE" ] || ! [[ "$NODE" =~ ^[0-9]+$ ]]; then
    error "Usage: sudo bash provision-audio-meter.sh <node-number>  (e.g. 59929)"
fi
if [ "$(id -u)" -ne 0 ]; then
    error "Must be run as root -- re-run with sudo."
fi
command -v asterisk >/dev/null 2>&1 || error "asterisk CLI not found on this host."

# --- these MUST match config.py's ASL_AUDIO_* constants exactly, kept in
#     sync manually -- same "no shared code between bash and Python here"
#     tradeoff already accepted for e.g. install.sh/update.sh's file-copy
#     lists vs. Dockerfile's COPY list ---
TUNNEL_PORT=8288
CONTEXT="dashboard-audiospy-${NODE}"
SPY_EXTEN="spy"
AUDIOSOCKET_EXTEN="audiosocket"
# res_audiosocket.so is app_audiosocket.so's own real, source-confirmed
# dependency (apps/app_audiosocket.c's AST_MODULE_INFO .requires) --
# chan_audiosocket.so (an unrelated inbound-call channel driver, NOT
# needed by the AudioSocket() dialplan app) was in an earlier version of
# this list and failed to load on a real node for exactly that reason;
# removed rather than debugged further since it was never actually used.
# res_clioriginate.so is what actually provides the `channel originate`
# CLI command this script's trigger relies on -- missing it doesn't fail
# loudly (no module-load error), it just makes the CLI report "No such
# command 'channel originate ...'", which reads like a syntax mistake
# rather than a missing module. Confirmed live the hard way (2026-08).
MODULES=(res_audiosocket.so app_audiosocket.so app_chanspy.so res_clioriginate.so)
DIALPLAN_FILE="/etc/asterisk/dashboard-audiospy-${NODE}.conf"
EXTENSIONS_CONF="/etc/asterisk/extensions.conf"
MODULES_CONF="/etc/asterisk/modules.conf"
INCLUDE_LINE="#include \"dashboard-audiospy-${NODE}.conf\""

[ -f "$EXTENSIONS_CONF" ] || error "$EXTENSIONS_CONF not found."
[ -f "$MODULES_CONF" ]    || error "$MODULES_CONF not found."

# --- 1. find this node's own rxchannel from rpt.conf -- scoped to THIS
#        node's own [<node>] stanza specifically (an awk state machine
#        keyed on the current section header, not a bare grep -- rpt.conf
#        can have multiple node stanzas on one box, confirmed live, and a
#        bare grep would just grab the first rxchannel= line in the whole
#        file regardless of which node it belongs to). Same discipline
#        config.py's HOTSPOT_INFO_CHECK_CMD already uses for WPSD's
#        /etc/mmdvmhost, for the same reason. ---
header "1. Finding node ${NODE}'s rxchannel"
RPT_CONF=$(find /etc/asterisk -maxdepth 1 -iname "rpt.conf" 2>/dev/null | head -1)
[ -n "$RPT_CONF" ] || error "rpt.conf not found under /etc/asterisk."

RXCHANNEL=$(awk -v node="${NODE}" '
    # Static /.../ regex literals only -- no backslashes inside a dynamic
    # double-quoted string, which some awk variants (confirmed live:
    # mawk/busybox awk in a non-gawk environment) silently mis-parse a
    # "\\[" string escape as plain "[", dropping the backslash entirely
    # and breaking the intended \[node\] regex without erroring loudly.
    # Plain string equality on the stripped bracket contents sidesteps
    # that whole class of portability problem.
    /^\[/ {
        insec = 0
        line = $0
        sub(/^\[/, "", line)
        sub(/\].*/, "", line)
        if (line == node) insec = 1
        next
    }
    insec && /^[[:space:]]*rxchannel[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, "");
        sub(/[[:space:]]*;.*$/, "");
        print;
        exit
    }
' "$RPT_CONF")

if [ -z "$RXCHANNEL" ]; then
    warn "No [${NODE}] stanza with an rxchannel= line found in $RPT_CONF."
    echo "Section headers actually present in that file:"
    grep -n '^\[' "$RPT_CONF" || echo "  (none found)"
    error "Fix the node number above, or open $RPT_CONF and check its rxchannel= line manually."
fi
success "rxchannel for node ${NODE}: ${RXCHANNEL}"

# --- 2. ensure the 3 required modules are loaded (they may exist on disk
#        but not be loaded/autoloaded -- confirmed a real, common state on
#        a stock ASL3 build, see CLAUDE.md) ---
header "2. Loading required Asterisk modules"
if ! grep -q "^\[modules\]" "$MODULES_CONF"; then
    error "$MODULES_CONF has no [modules] section -- unexpected, not touching it."
fi
# Prune any line THIS script previously added (tagged with the comment
# below) for a module no longer in $MODULES -- specifically cleans up a
# stray "load => chan_audiosocket.so" left behind by an earlier version
# of this script that included an unneeded module, without touching
# anything a human might have added by hand.
for existing_mod in $(grep -oE '^load[[:space:]]*=>[[:space:]]*[A-Za-z0-9_]+\.so[[:space:]]*;[[:space:]]*added by provision-audio-meter\.sh' "$MODULES_CONF" | grep -oE '[A-Za-z0-9_]+\.so'); do
    if ! printf '%s\n' "${MODULES[@]}" | grep -qxF "$existing_mod"; then
        sed -i "/^load[[:space:]]*=>[[:space:]]*${existing_mod}[[:space:]]*;[[:space:]]*added by provision-audio-meter\.sh/d" "$MODULES_CONF"
        info "Removed stale 'load => ${existing_mod}' (no longer needed) from $MODULES_CONF"
    fi
done
if [ ! -f "${MODULES_CONF}.bak-audiometer" ]; then
    cp "$MODULES_CONF" "${MODULES_CONF}.bak-audiometer"
    info "Backed up $MODULES_CONF -> ${MODULES_CONF}.bak-audiometer"
fi
for mod in "${MODULES[@]}"; do
    # Uncomment an explicit noload for this module, if present.
    if grep -qE "^[[:space:]]*noload[[:space:]]*=>[[:space:]]*${mod}[[:space:]]*$" "$MODULES_CONF"; then
        sed -i -E "s/^[[:space:]]*noload[[:space:]]*=>[[:space:]]*${mod}[[:space:]]*\$/; (dashboard) was noload'd, now loaded explicitly below/" "$MODULES_CONF"
        info "Removed 'noload => ${mod}' from $MODULES_CONF"
    fi
    # Ensure EXACTLY ONE "load => <mod>" line -- matching on the prefix
    # only (not anchored to end-of-line) since the line THIS script
    # writes always carries a trailing "; added by ..." comment; an
    # earlier version anchored the check with a bare `$` right after the
    # module name, which never matched its own previously-written line
    # and silently appended a fresh duplicate on every re-run (a real,
    # observed bug, not hypothetical -- see CLAUDE.md). Collapsing any
    # existing duplicates (from that earlier bug) down to one, rather
    # than just fixing the check going forward, so a re-run actually
    # cleans up a modules.conf that already accumulated them.
    match_pattern="^[[:space:]]*load[[:space:]]*=>[[:space:]]*${mod}([[:space:]]|\$|;)"
    existing_count=$(grep -cE "$match_pattern" "$MODULES_CONF" || true)
    if [ "${existing_count:-0}" -gt 1 ]; then
        sed -i -E "/${match_pattern}/d" "$MODULES_CONF"
        info "Collapsed ${existing_count} duplicate 'load => ${mod}' lines in $MODULES_CONF down to one"
        existing_count=0
    fi
    if [ "${existing_count:-0}" -eq 0 ]; then
        sed -i "/^\[modules\]/a load => ${mod}  ; added by provision-audio-meter.sh" "$MODULES_CONF"
        info "Added 'load => ${mod}' to $MODULES_CONF"
    fi
    # Take effect right now too, without a full Asterisk restart (which
    # would drop any active call) -- a no-op if already loaded.
    asterisk -rx "module load ${mod}" >/dev/null 2>&1 || true
done
for mod in "${MODULES[@]}"; do
    if asterisk -rx "module show like ${mod}" | grep -q "1 modules loaded"; then
        success "${mod} loaded"
    else
        error "${mod} failed to load -- check 'asterisk -rx \"module load ${mod}\"' output manually."
    fi
done

# --- 3. write this node's own dialplan file (whole-file overwrite each
#        run -- deterministic content, safe to regenerate) ---
header "3. Writing dialplan for node ${NODE}"
# Deterministic per-node UUID -- doesn't need to be globally unique or
# random, this app never correlates it against anything, AudioSocket just
# requires SOME UUID-shaped string. Same value every re-run keeps this
# file byte-identical (no diff churn) -- MUST match asl_audio.py's own
# expectation, which doesn't check it at all, so this only matters for
# not needlessly rewriting the file.
UUID=$(printf '00000000-0000-0000-0000-%012x' "$NODE")

# ChanSpy just spies in plain quiet mode -- no barge/whisper option.
# The bridge between this extension and ${AUDIOSOCKET_EXTEN} below
# happens automatically because the app.py-side trigger command
# originates a Local channel pair where THIS extension is the channel's
# own embedded destination (so it runs on the ";2" half) while
# ${AUDIOSOCKET_EXTEN} is the origination's separate `extension`
# argument (so it runs on the ";1" half) -- the two halves of a Local
# channel are inherently bridged to each other, confirmed live via a
# throwaway test dialplan (see CLAUDE.md). An earlier version of this
# file used a fabricated `qB(context^exten^priority)` ChanSpy option
# that doesn't exist -- see config.build_asl_audio_originate_cmd()'s own
# docstring for the corrected mechanism.
cat > "$DIALPLAN_FILE" <<EOF
; Generated by provision-audio-meter.sh for node ${NODE} -- safe to
; regenerate by re-running that script; do not hand-edit, it will be
; overwritten on the next run.
[${CONTEXT}]
exten => ${SPY_EXTEN},1,NoOp(Dashboard live audio meter spy for node ${NODE})
 same => n,Answer()
 same => n,ChanSpy(${RXCHANNEL},q)
 same => n,Hangup()

exten => ${AUDIOSOCKET_EXTEN},1,NoOp(Dashboard AudioSocket leg for node ${NODE})
 same => n,Answer()
 same => n,AudioSocket(${UUID},127.0.0.1:${TUNNEL_PORT})
 same => n,Hangup()
EOF
success "Wrote $DIALPLAN_FILE"

if ! grep -qF "$INCLUDE_LINE" "$EXTENSIONS_CONF"; then
    if [ ! -f "${EXTENSIONS_CONF}.bak-audiometer" ]; then
        cp "$EXTENSIONS_CONF" "${EXTENSIONS_CONF}.bak-audiometer"
        info "Backed up $EXTENSIONS_CONF -> ${EXTENSIONS_CONF}.bak-audiometer"
    fi
    echo "$INCLUDE_LINE" >> "$EXTENSIONS_CONF"
    info "Appended '${INCLUDE_LINE}' to $EXTENSIONS_CONF"
else
    info "$EXTENSIONS_CONF already includes this node's dialplan file"
fi

# --- 4. reload just the dialplan -- doesn't touch live calls, unlike a
#        full Asterisk restart ---
header "4. Reloading dialplan"
asterisk -rx "dialplan reload" >/dev/null
if asterisk -rx "dialplan show ${CONTEXT}" | grep -q "${SPY_EXTEN}"; then
    success "Dialplan for node ${NODE} loaded"
else
    error "Dialplan reload didn't pick up context '${CONTEXT}' -- check 'asterisk -rx \"dialplan show ${CONTEXT}\"' manually."
fi

header "Done"
echo "Node ${NODE} is provisioned. In the dashboard, open Settings -> Hotspots"
echo "(or this hotspot's own card -> gear icon) and enable"
echo "\"Live audio-level VU meter\" -- no credentials or ports to configure,"
echo "it reuses this hotspot's existing SSH login."
