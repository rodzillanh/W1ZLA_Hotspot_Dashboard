#!/bin/bash
# One-command update script for the hotspot-dashboard container.
#
# Rebuilding the image alone does NOT update an already-running container --
# Docker has to stop, remove, and recreate it from the new image. This
# script does all of that in one shot, using the exact same port/volume/env
# configuration your container currently has (pulled via `docker inspect`
# on 2026-07-06), so it's a drop-in replacement for the manual
# GUI stop -> remove -> re-add cycle.
#
# --network host (changed from -p 5000:5000 -p 2237:2237/udp) -- a
# deliberate tradeoff, not a default. openSPOT4 hotspots can be reached
# by their mDNS ".local" hostname (confirmed live -- the device runs its
# own mDNS responder, and this app has no static-IP requirement for that
# field), but mDNS needs multicast UDP to reach the real LAN, which
# Docker's default bridge network doesn't reliably pass through -- host
# networking shares the box's own network stack directly instead, so a
# ".local" name in the hotspot IP field resolves the same way it would
# for any other program running right on the host. This also means the
# WSJT-X UDP port (2237) and the dashboard's own port (5000) are reachable
# automatically -- host mode has no concept of "publish a port," every
# port the app binds is already exposed, so the old -p flags are gone
# rather than kept-but-ignored. Real cost: this container no longer has
# its own isolated port space -- whatever it binds must not collide with
# anything else already running on the host. If that tradeoff isn't
# worth it, reverting to bridge + these two -p flags + a DHCP reservation
# on the openSPOT4 (a static IP, managed by the router instead of this
# app) is the equivalent fix with none of this cost. If you ever change
# the "wsjtx_port" Settings field away from 2237, this script has nothing
# to update anymore (no port mapping to keep in sync) -- just make sure
# nothing else on the host is already using whatever port you pick.
#
# Includes Unraid's net.unraid.docker.webui label so the container keeps
# its "WebUI" shortcut in the Docker tab dropdown -- creating a container
# via plain `docker run` (as this script does) doesn't set that
# automatically the way Unraid's own "Add Container" GUI does, which is
# why that link disappears otherwise.
#
# For the same reason, a container created this way is invisible to
# Unraid's "Edit" logic. Confirmed directly from Unraid's own source
# (dynamix.docker.manager/include/DockerClient.php): it only attempts to
# match a container to a template file at all when the container's
# net.unraid.docker.managed label equals exactly "dockerman" --
# otherwise the template lookup is skipped entirely regardless of
# whether a matching template file exists in templates-user/. This
# script sets that label AND (re)installs unraid-template.xml into
# Unraid's templates-user directory every run, so Edit keeps working on
# a fresh install and every subsequent update alike.
#
# Your appdata volume (hotspots.json, settings.json, etc.) is untouched --
# only the container itself is replaced.
#
# Usage: run this from your source directory, e.g.:
#   cd /mnt/user/appdata/hotspot-dashboard-src
#   bash docker-update.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="hotspot-dashboard:latest"
CONTAINER_NAME="hotspot-dashboard"

echo "==> Recording build commit (read by the Version tab's update check)..."
git rev-parse HEAD > BUILD_COMMIT 2>/dev/null || echo "unknown" > BUILD_COMMIT

echo "==> Building image..."
docker build -t "$IMAGE_NAME" .

echo "==> Stopping existing container (if running)..."
docker stop "$CONTAINER_NAME" 2>/dev/null || true

echo "==> Removing existing container..."
docker rm "$CONTAINER_NAME" 2>/dev/null || true

echo "==> Starting new container..."
docker run -d \
  --name="$CONTAINER_NAME" \
  --restart=unless-stopped \
  --network host \
  -v /mnt/user/appdata/hotspot-dashboard:/app/data \
  -e TZ=America/New_York \
  -e POLL_INTERVAL=5 \
  -e SSH_TIMEOUT=5 \
  -e FAILURE_THRESHOLD=2 \
  -e MAX_WORKERS=5 \
  -e QRZ_USERNAME="${QRZ_USERNAME:-}" \
  -e QRZ_PASSWORD="${QRZ_PASSWORD:-}" \
  -l net.unraid.docker.webui='http://[IP]:5000/' \
  -l net.unraid.docker.managed='dockerman' \
  "$IMAGE_NAME"

# --- register the Unraid template so "Edit" works in the Docker tab ---
# Idempotent -- (re)written every run, same reasoning as the standalone
# install's polkit rule and self-update systemd units: whatever silently
# went missing gets restored just by running this script again. Skipped
# entirely (not an error) on a non-Unraid Docker host, where this
# directory doesn't exist.
UNRAID_TEMPLATES_DIR="/boot/config/plugins/dockerMan/templates-user"
if [ -d "/boot/config/plugins/dockerMan" ]; then
    echo "==> Registering Unraid template (enables the 'Edit' button)..."
    mkdir -p "$UNRAID_TEMPLATES_DIR"
    cp "${SCRIPT_DIR}/unraid-template.xml" "${UNRAID_TEMPLATES_DIR}/my-${CONTAINER_NAME}.xml"
else
    echo "==> Skipping Unraid template registration (not running on Unraid)"
fi

echo "==> Done. Tailing logs (Ctrl+C to stop watching, container keeps running):"
docker logs -f "$CONTAINER_NAME"
