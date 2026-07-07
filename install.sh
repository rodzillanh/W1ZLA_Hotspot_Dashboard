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
apt-get install -y -qq python3-venv python3-pip
success "System packages installed"

# --- create service user ---
header "Setting up service user"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    success "Created user: ${SERVICE_USER}"
else
    success "User ${SERVICE_USER} already exists"
fi

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
