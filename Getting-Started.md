# Getting Started

## Project layout

```
app.py          Flask routes only
config.py       all the tunables (timeouts, thresholds, regex patterns)
models.py       HotspotStatus dataclass — the shape of each dashboard card
storage.py      load/save hotspots.json and settings.json
storage_activity.py  SQLite log for the optional Fleet activity card
monitor.py      FleetMonitor: SSH polling + MMDVM log parsing (WPSD) / rpt xnode parsing (ASL3)
openspot.py     openSPOT4 (SharkRF): persistent WebSocket client, one per configured device
qrz.py          optional QRZ.com lookup for the active caller's name/city/state/photo/coords
aslstats.py     optional AllStarLink node-to-callsign lookup for ASL3 nodes
templates/
    dashboard.html
    setup.html
Dockerfile
docker-compose.yml      for the Compose Manager plugin / non-Unraid Docker hosts
unraid-template.xml     for Unraid's Docker GUI (see "Running on Unraid" below)
```

## Running on Raspberry Pi / Linux (standalone)

For a standalone install on a Raspberry Pi or any Debian/Ubuntu-based Linux system
— no Docker required.

### Requirements

- Raspberry Pi OS (Bookworm or Bullseye), Debian 11/12, or Ubuntu 22/24
- Python 3.10 or newer (`python3 --version`)
- Network access to your hotspots over SSH

### One-command install

Copy the project folder to your Pi (via SCP, USB, or git clone), then:

```bash
sudo bash install.sh
```

**The Settings → Version tab's update-check feature needs a real `.git`
checkout** to compare your deployed commit against the git remote — if
you copy files via SCP or a downloaded zip instead of `git clone`,
`install.sh` automatically sets one up for you at
`/opt/hotspot-dashboard-src` (network access to the git host required)
and installs from that instead. If it can't reach the git host, it
falls back to installing from your copy as-is and update checks will
show "unknown" until you retry with network access, or run `install.sh`
again from an actual `git clone`. `update.sh` does the same thing for
the self-update button specifically (separate from whatever files it
installs from `update.sh`'s own location) if it's ever missing.

The installer will:
1. Install Python dependencies into an isolated virtual environment
2. Create a dedicated `hotspot` system user to run the service
3. Set up `/etc/hotspot-dashboard/` as the data directory (hotspots.json, settings.json, etc.)
4. Install and start a systemd service that survives reboots
5. Print the URL to open the dashboard

### After install

Open `http://your-pi-hostname.local:5000` or `http://192.168.x.x:5000` in a browser,
then go to **Settings → Hotspots** to add your nodes.

### Useful commands

```bash
# Check service status
sudo systemctl status hotspot-dashboard

# View live logs
sudo journalctl -u hotspot-dashboard -f

# Restart after a config change
sudo systemctl restart hotspot-dashboard

# Update to a new version
# (copy new files over /opt/hotspot-dashboard/, then restart)
sudo cp *.py /opt/hotspot-dashboard/
sudo cp templates/*.html /opt/hotspot-dashboard/templates/
sudo systemctl restart hotspot-dashboard
```

### Change the port

The default port is 5000. To use a different port, edit the service file:

```bash
sudo systemctl edit hotspot-dashboard
```

Add:
```ini
[Service]
Environment=PORT=8080
```

Then `sudo systemctl restart hotspot-dashboard`.

### Updating to a new version

When a new zip is available, copy the files to your Pi and run:

```bash
sudo bash update.sh
```

The updater will:
1. Show exactly which files changed before touching anything
2. Stop the service
3. Back up the current app files to a timestamped folder (e.g. `/opt/hotspot-dashboard-backup-20260701-143022/`)
4. Copy in the new files and update dependencies only if `requirements.txt` changed
5. Restart the service and confirm it came back healthy
6. Print rollback instructions in case anything goes wrong

Your config and data in `/etc/hotspot-dashboard/` is never touched.

### Uninstall

```bash
sudo bash uninstall.sh
```

This removes the service and app files. Your data in `/etc/hotspot-dashboard/`
is preserved — delete it manually if you no longer need it.

---

## Running on Unraid

Unraid's Docker GUI doesn't build images from a Dockerfile directly — you
build the image once on the box, then point a container at it. Two ways to
get it running:

### Option A — Build + add via the GUI template (recommended)

1. Copy this whole folder to the array, e.g. `/mnt/user/appdata/hotspot-dashboard-src/`
   (via the **Unraid Shares** SMB share, or `scp`).
2. SSH into Unraid and build the image:
   ```bash
   cd /mnt/user/appdata/hotspot-dashboard-src
   docker build -t hotspot-dashboard:latest .
   ```
3. Drop `unraid-template.xml` into `/boot/config/plugins/dockerMan/templates-user/`
   (SSH or via the Unraid `flash` share over SMB).
4. In the Unraid GUI: **Docker tab → Add Container → Template: hotspot-dashboard**.
   It'll pre-fill the port (5000), the appdata path
   (`/mnt/user/appdata/hotspot-dashboard`), and the polling settings as
   editable fields. Click **Apply**.
5. Open the dashboard from its **WebUI** link on the Docker tab, or
   `http://<unraid-ip>:5000`.

If you ever change the code, rerun the `docker build` command — but note
that just **restarting** the existing container does *not* pick up the
new image; Docker containers are bound to the specific image they were
created from, not the tag name. You have to remove and recreate the
container (Docker tab → stop → remove → re-add from the template again;
your appdata volume is untouched either way). `docker-update.sh` (included
in this folder) automates the whole build → stop → remove → recreate cycle
in one command if you'd rather not go through the GUI each time — open it
first to confirm the port/volume/env values match your setup, since it's
built from one specific container's configuration as an example.

`docker-update.sh` also sets the `net.unraid.docker.managed=dockerman`
label and (re)installs `unraid-template.xml` into
`/boot/config/plugins/dockerMan/templates-user/` on every run, so the
Docker tab's **Edit** option keeps working even though the container
itself is recreated via a plain `docker run` rather than through the
GUI. Both are needed — Unraid only looks for a matching template file
at all when that label is present; without it, **Edit silently doesn't
appear at all** (not a broken form, just missing from the menu), no
matter how correct the template file is. The template step is skipped
harmlessly if you're not running on Unraid.

### Option B — Compose Manager plugin

If you have the **Docker Compose Manager** plugin (from Community Apps)
installed, copy this folder under
`/boot/config/plugins/compose.manager/projects/hotspot-dashboard/` and use
*Compose Manager → Add New Stack → Compose Up*. The included
`docker-compose.yml` already bind-mounts
`/mnt/user/appdata/hotspot-dashboard` for you. `docker compose up -d --build`
handles the rebuild-and-recreate cycle in one command automatically —
compose (unlike a bare `docker restart`) always recreates the container
when the underlying image changes.

### Option C — Plain `docker run`

```bash
docker build -t hotspot-dashboard:latest .
docker run -d \
  --name hotspot-dashboard \
  -p 5000:5000 \
  -v /mnt/user/appdata/hotspot-dashboard:/app/data \
  --restart unless-stopped \
  hotspot-dashboard:latest
```

Same caveat as Option A: updating later means removing and recreating the
container, not just restarting it.

---

Once it's running, open the dashboard and hit **Settings** to add a hotspot
(name, IP, SSH user/password). It polls every 5 seconds by default.
Hotspot config lives in `hotspots.json` under the appdata folder above, so
it survives container rebuilds and Unraid reboots.

## Config via environment variables

All in `config.py`, overridable without touching code:

| Variable | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL` | 5 | seconds between fleet sweeps |
| `SSH_TIMEOUT` | 5 | seconds per SSH connection attempt |
| `FAILURE_THRESHOLD` | 2 | consecutive failures before marking "Offline" |
| `MAX_WORKERS` | 5 | parallel SSH checks per sweep |
| `PORT` | 5000 | Flask port |
| `CONFIG_DIR` | /app/data | where hotspots.json lives |
| `QRZ_USERNAME` | (blank) | QRZ.com username — see "QRZ caller lookup" below |
| `QRZ_PASSWORD` | (blank) | QRZ.com password |
| `QRZ_CACHE_TTL` | 3600 | seconds to cache a callsign lookup before refetching |
| `RADIOID_CACHE_TTL` | 3600 | seconds to cache a RadioID.net lookup |
| `APRS_CACHE_TTL` | 120 | seconds to cache an APRS.fi position (short — positions move) |
| `BRANDMEISTER_CACHE_TTL` | 120 | seconds to cache a Brandmeister repeater profile |
| `VERSION_CHECK_INTERVAL` | 1800 | seconds between WPSD/Pi-Star update checks and Brandmeister profile checks (separate, slower loop from the main poll) |
| `MQTT_PUBLISH_INTERVAL` | (matches `POLL_INTERVAL`) | seconds between MQTT state publishes to Home Assistant |

Example: `docker compose run -e POLL_INTERVAL=10 ...`

## First run

A fresh install (no hotspots configured yet) shows a plain "Let's get
your fleet on the map" card in place of the usual card grid, linking
straight to Settings → Hotspots — everything else in Settings stays
reachable, nothing is blocked. Once your first hotspot is added, that
card goes away for good, and a one-time "here's what's available" panel
appears once, listing every optional integration/card/overlay grouped by
category (ham radio integrations, extra cards, map overlays, cameras).
Dismiss it and it's gone — reopen it anytime from Settings → General
("Show feature tour again").

**Upgrading an existing install never triggers either of these.** Both
are gated on `settings.json` having genuinely never existed on disk at
all (not just missing a couple of newer keys) — an existing install's
`settings.json` already exists from before this feature shipped, so it's
treated as "already seen," full stop, regardless of how long ago you set
it up.
