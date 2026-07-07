#!/usr/bin/env bash
# =============================================================================
# W1ZLA Hotspot Dashboard — uninstaller
# Usage: sudo bash uninstall.sh
# =============================================================================
set -euo pipefail

APP_NAME="hotspot-dashboard"
INSTALL_DIR="/opt/${APP_NAME}"
DATA_DIR="/etc/${APP_NAME}"
SERVICE_USER="hotspot"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "\033[0;34m[•]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash uninstall.sh"

echo -e "\n${BOLD}W1ZLA Hotspot Dashboard — uninstall${NC}\n"
warn "This will remove the application and service."
warn "Your config/data in ${DATA_DIR} will be preserved."
echo
read -rp "Continue? [y/N] " confirm
[[ "${confirm,,}" != "y" ]] && { echo "Aborted."; exit 0; }

# Stop and disable service
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    systemctl stop "$APP_NAME"
    success "Service stopped"
fi
if systemctl is-enabled --quiet "$APP_NAME" 2>/dev/null; then
    systemctl disable --quiet "$APP_NAME"
    success "Service disabled"
fi
rm -f "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload
success "Service removed"

# Remove app files
rm -rf "$INSTALL_DIR"
success "Application files removed (${INSTALL_DIR})"

# Remove service user
if id "$SERVICE_USER" &>/dev/null; then
    userdel "$SERVICE_USER" 2>/dev/null || true
    success "Service user removed"
fi

echo
echo -e "${GREEN}Uninstall complete.${NC}"
echo -e "Your data is still in ${YELLOW}${DATA_DIR}${NC} — remove it manually if you don't need it:"
echo -e "  ${BOLD}sudo rm -rf ${DATA_DIR}${NC}"
echo
