#!/usr/bin/env bash
# =============================================================================
# W1ZLA Hotspot Dashboard — standalone installer
# Supports: Raspberry Pi OS (Bookworm/Bullseye), Debian 11/12, Ubuntu 22/24
# Usage:  sudo bash install.sh
# =============================================================================

set -euo pipefail

# --- config ---
APP_NAME="hotspot-dashboard"
INSTALL_DIR="/opt/${APP_NAME}"
DATA_DIR="/etc/${APP_NAME}"
SERVICE_USER="hotspot"
PORT="${PORT:-5000}"
PYTHON="python3"

# --- colours ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[•]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
header()  { echo -e "\n${BOLD}$*${NC}"; }

# --- checks ---
header "W1ZLA Hotspot Dashboard installer"

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash install.sh"

# Detect OS
if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    info "Detected OS: ${PRETTY_NAME:-unknown}"
else
    warn "Could not detect OS — proceeding anyway"
fi

# Check Python 3.10+
PY_VER=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ $PY_MAJOR -lt 3 || ($PY_MAJOR -eq 3 && $PY_MINOR -lt 10) ]]; then
    error "Python 3.10 or newer required (found ${PY_VER}). Run: sudo apt install python3"
fi
success "Python ${PY_VER} found"

# --- install system packages ---
header "Installing system dependencies"
apt-get update -qq
# ffmpeg bridges RTSP camera feeds to MJPEG for the optional camera cards
# (off by default, but installed unconditionally -- same treatment as the
# paho-mqtt/aprslib pip deps for MQTT/APRS, both also off by default).
apt-get install -y -qq python3-venv python3-pip ffmpeg
success "System packages installed"

# --- create service user ---
header "Setting up service user"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    success "Created user: ${SERVICE_USER}"
else
    success "User ${SERVICE_USER} already exists"
fi

# --- grant reboot/power-off permission (Settings -> General "Host power
# control" buttons) via polkit, since the service runs with
# NoNewPrivileges=yes -- sudo/setuid is blocked entirely regardless of
# sudoers config, but a polkit rule lets systemctl reboot/poweroff work via
# D-Bus instead. A headless service account has no active session, so
# default polkit policy would otherwise deny these actions. ---
header "Configuring reboot/power-off permission"
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/49-hotspot-dashboard-power.rules << POLKIT
// Allows the ${SERVICE_USER} service user to reboot/power off this device
// via Settings -> General -- see README.md "Host power control".
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.login1.reboot" ||
         action.id == "org.freedesktop.login1.reboot-multiple-sessions" ||
         action.id == "org.freedesktop.login1.power-off" ||
         action.id == "org.freedesktop.login1.power-off-multiple-sessions") &&
        subject.user == "${SERVICE_USER}") {
        return polkit.Result.YES;
    }
});
POLKIT
systemctl try-restart polkit 2>/dev/null || true
success "Reboot/power-off permission configured"

# --- stop existing service if running ---
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    info "Stopping existing service..."
    systemctl stop "$APP_NAME"
fi

# --- install application files ---
header "Installing application"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$INSTALL_DIR"
# Copy all Python files and templates
cp "${SCRIPT_DIR}"/*.py         "$INSTALL_DIR/"
cp "${SCRIPT_DIR}/requirements.txt" "$INSTALL_DIR/"
mkdir -p "${INSTALL_DIR}/templates"
cp "${SCRIPT_DIR}/templates/"*.html "${INSTALL_DIR}/templates/"
success "Application files copied to ${INSTALL_DIR}"

# --- record the deployed commit (Version tab's update check reads this;
# the running app has no other way to know what it is, since these are
# plain file copies, not a git clone) ---
git -C "$SCRIPT_DIR" rev-parse HEAD > "${INSTALL_DIR}/BUILD_COMMIT" 2>/dev/null \
    || echo "unknown" > "${INSTALL_DIR}/BUILD_COMMIT"

# --- create data directory ---
mkdir -p "$DATA_DIR"
chown "${SERVICE_USER}:${SERVICE_USER}" "$DATA_DIR"
success "Data directory: ${DATA_DIR}"

# --- create virtual environment ---
header "Creating Python virtual environment"
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    $PYTHON -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
success "Virtual environment ready"

# --- set ownership ---
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"

# --- write systemd unit ---
header "Installing systemd service"
cat > "/etc/systemd/system/${APP_NAME}.service" << UNIT
[Unit]
Description=W1ZLA Hotspot Dashboard
Documentation=https://github.com/w1zla/hotspot-dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}

# Environment
Environment=CONFIG_DIR=${DATA_DIR}
Environment=PORT=${PORT}

# Hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --quiet "$APP_NAME"
systemctl start "$APP_NAME"
success "Service installed and started"

# --- self-update: web UI "Install update" button (Version tab) ---
# The main service runs unprivileged with NoNewPrivileges=yes and
# ProtectSystem=strict (read-only outside DATA_DIR), so it can't pull new
# code or restart itself directly. Instead: it writes a trigger file into
# DATA_DIR (the one path it can write), and a separate, more-privileged
# systemd path unit watches for that file and runs the actual git pull +
# update.sh as root -- same privilege-separation idea as the reboot/
# poweroff polkit rule above, scaled up for a bigger action. This only
# triggers on an explicit button click in the web UI; nothing here polls
# or auto-installs on its own.
header "Configuring self-update trigger"
cat > "${INSTALL_DIR}/run_update.sh" << RUNUPDATE
#!/usr/bin/env bash
# Auto-generated by install.sh -- runs as root via the ${APP_NAME}-updater
# systemd path unit whenever the dashboard's "Install update" button
# writes ${DATA_DIR}/update_requested. Safe to re-run by hand too.
set -uo pipefail
SOURCE_DIR="${SCRIPT_DIR}"
TRIGGER_FILE="${DATA_DIR}/update_requested"
LOG_FILE="/var/log/${APP_NAME}-update.log"

{
    echo "=== Update run: \$(date) ==="
    if [[ -d "\${SOURCE_DIR}/.git" ]]; then
        git -C "\$SOURCE_DIR" pull --ff-only
    else
        echo "Source dir \${SOURCE_DIR} is not a git clone -- skipping pull, re-running update.sh with files as-is"
    fi
    bash "\${SOURCE_DIR}/update.sh"
} >> "\$LOG_FILE" 2>&1

rm -f "\$TRIGGER_FILE"
RUNUPDATE
chmod +x "${INSTALL_DIR}/run_update.sh"

cat > "/etc/systemd/system/${APP_NAME}-updater.service" << UPDATERSERVICE
[Unit]
Description=W1ZLA Hotspot Dashboard self-update (triggered by the Version tab)

[Service]
Type=oneshot
ExecStart=${INSTALL_DIR}/run_update.sh
UPDATERSERVICE

cat > "/etc/systemd/system/${APP_NAME}-updater.path" << UPDATERPATH
[Unit]
Description=Watch for a Hotspot Dashboard update request

[Path]
PathExists=${DATA_DIR}/update_requested
Unit=${APP_NAME}-updater.service

[Install]
WantedBy=multi-user.target
UPDATERPATH

systemctl daemon-reload
systemctl enable --quiet --now "${APP_NAME}-updater.path"
success "Self-update trigger configured"

# --- detect IP for access URL ---
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-pi-ip")
HOSTNAME=$(hostname).local

# --- done ---
header "Installation complete"
echo
echo -e "  Dashboard URL:  ${GREEN}http://${HOSTNAME}:${PORT}${NC}"
echo -e "  Also try:       ${GREEN}http://${LOCAL_IP}:${PORT}${NC}"
echo
echo -e "  Service commands:"
echo -e "    ${BOLD}sudo systemctl status ${APP_NAME}${NC}   — check status"
echo -e "    ${BOLD}sudo systemctl restart ${APP_NAME}${NC}  — restart"
echo -e "    ${BOLD}sudo journalctl -u ${APP_NAME} -f${NC}   — view logs"
echo
echo -e "  Config & data:  ${DATA_DIR}"
echo -e "  App files:      ${INSTALL_DIR}"
echo
