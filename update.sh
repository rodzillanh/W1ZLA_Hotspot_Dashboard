#!/usr/bin/env bash
# =============================================================================
# W1ZLA Hotspot Dashboard — updater
# Usage: sudo bash update.sh
# =============================================================================

set -euo pipefail

APP_NAME="hotspot-dashboard"
INSTALL_DIR="/opt/${APP_NAME}"
DATA_DIR="/etc/${APP_NAME}"
BACKUP_DIR="/opt/${APP_NAME}-backup-$(date +%Y%m%d-%H%M%S)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[•]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
header()  { echo -e "\n${BOLD}$*${NC}"; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash update.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

header "W1ZLA Hotspot Dashboard — update"
echo -e "  Source:  ${SCRIPT_DIR}"
echo -e "  Install: ${INSTALL_DIR}"
echo -e "  Data:    ${DATA_DIR} ${GREEN}(never touched)${NC}"
echo

# --- verify install exists ---
[[ -d "$INSTALL_DIR" ]] || error "Dashboard not installed at ${INSTALL_DIR}. Run install.sh first."
[[ -f "${INSTALL_DIR}/venv/bin/python" ]] || error "Virtualenv missing. Run install.sh to reinstall."

# --- show what's changing ---
header "Checking for changes"
CHANGED=()
for f in *.py; do
    [[ -f "${INSTALL_DIR}/${f}" ]] || { CHANGED+=("${f} (new)"); continue; }
    if ! diff -q "${SCRIPT_DIR}/${f}" "${INSTALL_DIR}/${f}" &>/dev/null; then
        CHANGED+=("${f}")
    fi
done
for f in templates/*.html; do
    dest="${INSTALL_DIR}/${f}"
    [[ -f "$dest" ]] || { CHANGED+=("${f} (new)"); continue; }
    if ! diff -q "${SCRIPT_DIR}/${f}" "$dest" &>/dev/null; then
        CHANGED+=("${f}")
    fi
done

# Check if requirements changed
REQS_CHANGED=false
if ! diff -q "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt" &>/dev/null; then
    REQS_CHANGED=true
    CHANGED+=("requirements.txt")
fi

if [[ ${#CHANGED[@]} -eq 0 ]]; then
    success "All files are already up to date — nothing to do."
    exit 0
fi

echo -e "  Files to update:"
for f in "${CHANGED[@]}"; do
    echo -e "    ${YELLOW}${f}${NC}"
done
echo

# --- stop service ---
header "Stopping service"
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    systemctl stop "$APP_NAME"
    success "Service stopped"
else
    warn "Service was not running"
fi

# --- back up current app files ---
header "Backing up current install"
mkdir -p "$BACKUP_DIR"
cp -r "${INSTALL_DIR}"/*.py          "$BACKUP_DIR/" 2>/dev/null || true
cp -r "${INSTALL_DIR}/templates"     "$BACKUP_DIR/" 2>/dev/null || true
cp    "${INSTALL_DIR}/requirements.txt" "$BACKUP_DIR/" 2>/dev/null || true
success "Backup saved to ${BACKUP_DIR}"
info  "Your data in ${DATA_DIR} is untouched"

# --- copy new files ---
header "Installing updates"
cp "${SCRIPT_DIR}"/*.py                    "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt"        "${INSTALL_DIR}/"
mkdir -p "${INSTALL_DIR}/templates"
cp "${SCRIPT_DIR}/templates/"*.html        "${INSTALL_DIR}/templates/"
chown -R hotspot:hotspot "${INSTALL_DIR}"
success "Files updated"

# --- update dependencies if needed ---
if [[ "$REQS_CHANGED" == "true" ]]; then
    header "Updating dependencies"
    "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
    "${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
    success "Dependencies updated"
else
    info "Dependencies unchanged — skipping pip install"
fi

# --- restart and verify ---
header "Restarting service"
systemctl start "$APP_NAME"

# Wait up to 10 seconds for the service to come up healthy
for i in {1..10}; do
    if systemctl is-active --quiet "$APP_NAME"; then
        success "Service is running"
        break
    fi
    sleep 1
    if [[ $i -eq 10 ]]; then
        error "Service failed to start. Check logs: sudo journalctl -u ${APP_NAME} -n 50"
    fi
done

# --- done ---
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-pi-ip")
HOSTNAME=$(hostname).local

header "Update complete"
echo
echo -e "  Dashboard: ${GREEN}http://${HOSTNAME}:5000${NC}  or  ${GREEN}http://${LOCAL_IP}:5000${NC}"
echo
echo -e "  If anything looks wrong, restore the backup:"
echo -e "    ${BOLD}sudo systemctl stop ${APP_NAME}${NC}"
echo -e "    ${BOLD}sudo cp ${BACKUP_DIR}/*.py ${INSTALL_DIR}/${NC}"
echo -e "    ${BOLD}sudo cp -r ${BACKUP_DIR}/templates ${INSTALL_DIR}/${NC}"
echo -e "    ${BOLD}sudo systemctl start ${APP_NAME}${NC}"
echo
