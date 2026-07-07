# W1ZLA WPSD Hotspot Dashboard

Live status dashboard for a fleet of Pi-Star / WPSD hotspots, polled over SSH.

## Project layout

```
app.py          Flask routes only
config.py       all the tunables (timeouts, thresholds, regex patterns)
models.py       HotspotStatus dataclass — the shape of each dashboard card
storage.py      load/save hotspots.json and settings.json
storage_activity.py  SQLite log for the optional Fleet activity card
monitor.py      FleetMonitor: SSH polling + MMDVM log parsing
qrz.py          optional QRZ.com lookup for the active caller's name/city/state/photo/coords
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

## Dashboard customization

Everything below is configured from the **Settings** page (`/setup`) —
no environment variables or code changes needed, and changes are saved
immediately to `settings.json` in the appdata folder. Settings are split
across tabs: **General** (name, theme, toolbar links), **Weather**,
**Integrations** (QRZ.com, RadioID.net, APRS.fi), **Hotspots**, **Favorites**,
and **Version info**.

- **Dashboard name** — shown in the title bar and browser tab.
- **Light/dark mode** — a toggle switch; the whole UI (dashboard, setup
  page, map) reflows to the new theme.
- **Toolbar links** — a row of buttons under the title (e.g. links to
  Brandmeister, QRZ, your reflector dashboard, APRS.fi). Add, remove, and
  drag to reorder from the Settings page. A "Show toolbar links" toggle
  (General tab) hides the whole row if you'd rather not show it; the ⚙
  Settings link itself always stays visible either way.
- **Card order** — drag hotspot cards into whatever order you want them
  to appear on the dashboard, from the same drag-and-drop list used for
  toolbar links.
- **Weather** — its own tab on the Settings page: a location field
  (city name, zip, or "City, ST" — blank hides the weather card) and a
  °F/°C toggle for the displayed temperature unit.
- **Fleet activity card** — off by default (General tab toggle). Adds a
  small SQLite-backed log of completed transmissions; see "Fleet activity"
  below.
- **Per-hotspot latitude/longitude** — optional, set on the Hotspots tab's
  add/edit form. Plots that hotspot on the Live map (see "Live map" below).
- **Per-hotspot Brandmeister ID** — optional, also on the Hotspots tab.
  Your hotspot's Brandmeister/CCS7 ID — the same ID shown under Brandmeister
  SelfCare → My hotspots. Enables the Brandmeister repeater profile lookup
  (see "Brandmeister repeater profile" below).

## QRZ caller lookup

When `QRZ_USERNAME` / `QRZ_PASSWORD` are set, the dashboard looks up the
active caller's name, city/state, profile photo, and coordinates (if
available) on QRZ.com and shows them next to their callsign. A few things
worth knowing:

- **Requires an XML subscription.** Any QRZ login can authenticate, but
  looking up someone *else's* callsign (not your own) only returns full
  name/address/photo/coordinate data if your account has an active **QRZ
  XML Logbook Data** subscription (a separate paid add-on from a regular
  qrz.com login — currently ~$35.95/year, see qrz.com's subscription page
  for current pricing). Without one, lookups return a "subscription
  required" message and the dashboard just shows the callsign with no
  extra info — it won't error out, and the Live Map tab simply won't have
  a pin for that caller.
- **Only the active caller is looked up**, once per new callsign (not on
  every 5-second poll), and results are cached for an hour
  (`QRZ_CACHE_TTL`) to stay well under QRZ's lookup-rate expectations.
- The photo, if present, shows as a 64×64 thumbnail on the right side of
  the card while that caller is active — click it to open a full-size
  popup with their name and a link to their QRZ profile. If the image URL
  ever 404s, it just disappears instead of showing a broken-image icon.
- Credentials are passed as plain environment variables, consistent with
  how hotspot SSH passwords are handled in this project right now (see
  "Known tradeoffs" below).
- If QRZ has no subscription or doesn't have the callsign, the dashboard
  automatically falls back to **RadioID.net** for name/city-state (see
  below) — no configuration needed beyond the on/off toggle in Settings →
  Integrations.
- If the caller has a live **APRS.fi** beacon, their real-time position is
  used on the map instead of QRZ's static home-station coordinates (see
  "APRS.fi live position" below).

## RadioID.net lookup

A free, no-account fallback for caller name and city/state, used only when
QRZ doesn't return anything (no subscription, or the callsign isn't in
QRZ's database). It has no lat/lon data, so it never contributes a map pin
on its own — it only fills in the name/location text on the card. On by
default; toggle it off in Settings → Integrations if you'd rather see a
bare callsign than an unverified fallback name.

## APRS.fi live position

Optional — requires a free API key from
[aprs.fi/page/api](https://aprs.fi/page/api), entered in Settings →
Integrations. When the active caller has a recent APRS beacon, their live
position from that beacon is used on the Live map instead of QRZ's static
home-station coordinates — much more useful for mobile or portable
operators. Positions are cached only 2 minutes (`APRS_CACHE_TTL`) since
they can move. Leave the API key blank to disable; the map falls back to
QRZ/RadioID's static location or no pin at all if neither has data.

## Brandmeister repeater profile

Optional — set your hotspot's Brandmeister/CCS7 ID (Settings → Hotspots,
per hotspot) to show that hotspot's Brandmeister status and static
talkgroup subscriptions on its card. Uses Brandmeister's current `v2`
("Halligan") API — `GET /v2/device/{id}` and `GET /v2/device/{id}/talkgroup`
— checked on the same slow background cadence as the WPSD/Pi-Star update
check (`VERSION_CHECK_INTERVAL`, default 30 min), not on every 5-second
poll. No API key is needed for these read-only lookups.

The status shown on the card is Brandmeister's own `statusText` field
(e.g. `"DMO"` for a simplex hotspot) shown as-is, rather than translated
into a guessed "online"/"offline" label — Brandmeister's numeric status
codes aren't documented anywhere we could find, so displaying the literal
text is more honest than a mistranslated boolean.

**Scope note:** Brandmeister's *live, cross-network* Last Heard feed
(seeing who else is active on a talkgroup network-wide, not just your own
hotspots) is only available over a persistent Socket.IO connection, not a
simple polled REST call — a materially different architecture (persistent
background connection, extra dependency, reconnect handling) from the rest
of this app's SSH-poll model. That's not implemented here; this feature
covers the device/talkgroup REST endpoints only.

## WPSD/Pi-Star update check

If a hotspot's dashboard is a git-based checkout (true for WPSD and most
Pi-Star-derived dashboards), the dashboard periodically runs `git
rev-parse` and `git ls-remote` over the existing SSH connection to compare
the locally installed commit against its upstream remote — for **all
three** of WPSD's separate repos (WPSD-WebCode at `/var/www/dashboard`,
WPSD-Scripts at `/usr/local/sbin`, WPSD-Binaries at `/usr/local/bin`), not
just the dashboard web code. If any of them is behind its remote, an
"⬆ update" badge appears on that hotspot's card, linking directly to that
hotspot's WPSD Admin → Update page (`/admin/update.php`, opens in a new
tab) — hover it first for which component(s) are outdated, plus the
installed WebCode commit and date.
This check requires the hotspot itself to have outbound internet access
(the same access it already needs for the DMR/D-Star/etc. network), and
runs on the slow `VERSION_CHECK_INTERVAL` cadence, not every poll. If the
dashboard isn't a git checkout (or `git` isn't installed), the check
silently degrades — no badge, no error.

Each git command runs with a per-invocation `-c safe.directory='*'`
override, since WPSD's dashboard `.git` is typically owned by `www-data`
while SSH logs in as a different user — modern git otherwise refuses to
run against a repo it doesn't recognize as owned by the current user, and
fails silently under this check's error suppression. This flag doesn't
modify the hotspot's own git config; it only applies to this one command.

## Home Assistant (MQTT)

Optional — set a broker host in Settings → Integrations to publish every
hotspot to Home Assistant via
[MQTT auto-discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).
Point it at the same broker your HA instance already uses; no manual HA
YAML configuration is needed. Each hotspot appears as its own HA device
with:

- **Binary sensors:** Online (device_class `connectivity`), Active,
  Favorite Active, Dashboard Update Available
- **Sensors:** Mode, Active Call, Talkgroup, Temperature (°C,
  device_class `temperature`), CPU Load (%), RSSI (dBm, device_class
  `signal_strength`), and Brandmeister Status (only if a Brandmeister ID
  is configured for that hotspot)

State is published on the same cadence as the main poll loop
(`MQTT_PUBLISH_INTERVAL`, defaults to `POLL_INTERVAL`). Discovery config
messages are retained, so entities reappear automatically after an HA or
broker restart — no need to re-save settings. An availability topic
(`hotspot_dashboard/status`) tracks whether the dashboard itself is
connected to the broker, using MQTT's Last Will mechanism, so HA marks all
of this dashboard's entities "unavailable" if the app stops or loses its
broker connection, rather than showing stale last-known values forever.

This is one of two integrations in this project with an actual new pip
dependency (`paho-mqtt` here, `aprslib` for APRS favorite alerts below)
rather than using only the standard library — if you're on the standalone
Pi/Linux install, run `sudo bash update.sh` after pulling an update that
touches `requirements.txt` (it diffs the file and reinstalls
automatically); on Docker/Unraid, rebuild the image
(`docker compose up -d --build` or the Unraid "force update" equivalent)
rather than just restarting the existing container.

## APRS favorite alerts

Optional — set your callsign in Settings → Integrations to get an APRS
text message sent to yourself whenever a favorite becomes active. This
uses [APRS-IS](http://www.aprs-is.net/) (the internet backbone of the
APRS network) via the `aprslib` package — the same underlying mechanism
as ordinary ham-to-ham APRS messaging, not a proprietary notification
service. No signup or API key: your "passcode" is a deterministic
checksum computed automatically from your callsign
(`aprslib.passcode()`), the same mechanism every APRS client uses to let
you inject your own traffic.

**You'll need something watching for APRS messages addressed to your
callsign to actually see the alert** — e.g. the
[APRSdroid](https://aprsdroid.org/) Android app (supports push
notifications for incoming APRS messages), aprs.fi's message inbox, or a
radio/TNC with message duplex support. This is APRS's own delivery model
— best-effort over a shared network, not a guaranteed-delivery push API,
so treat it as a bonus channel rather than your only alerting method.

Settings:
- **Your callsign** — used to log into APRS-IS and as the message's "from"
- **Send to** (optional) — defaults to your own callsign if blank; set
  this to a different callsign-SSID (e.g. `W1ZLA-9`) if you want the
  alert routed to a specific device rather than your base callsign
- **Minimum minutes between alerts** — a per-favorite cooldown so a
  favorite keying up repeatedly doesn't spam APRS-IS; the alert only
  fires again once the favorite has gone idle and become active again
  *and* the cooldown has elapsed

The message itself is short (APRS messages cap around 67 characters) —
e.g. `"W1ZLA active on Garage Hotspot"` — and there's no delivery
confirmation; sending is fire-and-forget, matching how simple APRS bots
generally behave. It uses the caller's actual callsign rather than their
optional friendly name/label, to read as a proper APRS message.

## Transmission timer

While a node is active, the card shows a live "⏱ m:ss" counter next to
the caller's name, counting up from the moment that specific transmission
started. It resets to 0:00 the instant a *new* callsign keys up (it does
not accumulate across multiple callers or multiple transmissions from the
same person).

## Live map

The **Live map** tab shows an interactive map (Leaflet.js + OpenStreetMap,
with an optional satellite tile view, no API key required for either) with:

- A **purple square marker for each of your own hotspots** that has a
  latitude/longitude set (Settings → Hotspots). This is opt-in per hotspot
  since there's no reliable way to read a WPSD/Pi-Star node's physical
  location automatically over SSH.
- A **pin for every caller** currently active or in any node's last-5
  history, using QRZ/RadioID/APRS coordinates (in that priority order —
  see the integration sections above). Green pins are active; blue pins
  are recent. Caller pins cluster at low zoom levels to avoid overlapping
  when there's a lot of activity.
- A **dashed distance line** from the receiving hotspot to the caller, for
  active calls only, labeled with the distance in miles or km (matches the
  Weather tab's °F/°C unit setting).
- Click a caller pin for their callsign, name, location, the node that
  heard them, and (for a live APRS position) a note saying so — plus a
  "Go to card →" link that switches to the Dashboard tab and highlights
  that hotspot's card. Each dashboard card also has a small "📍 map" link
  (when the hotspot has lat/lon set) that jumps back to the map and centers
  on that hotspot's marker.
- The map **auto-fits to all markers the first time data loads**, then
  leaves your pan/zoom alone on subsequent 3-second refreshes.
- A chip list below the map mirrors the caller data for a quick scan
  without needing to click around.

Only callers with known coordinates (from QRZ, or a live APRS beacon)
appear as pins — the QRZ subscription caveat above applies here too.

## Fleet activity

Optional — off by default (Settings → General → "Show fleet activity
card"), since it adds a small SQLite log (`activity.db` in the appdata
folder alongside `hotspots.json`) and a write on every completed
transmission. Once enabled, a "Fleet activity" card appears on the
dashboard showing:

- **Online** — hotspots currently online vs. total configured.
- **Uptime** — how long the dashboard process itself has been running.
- **Last activity** — how long ago and which hotspot last had a call,
  live-updating the same way the per-card "last heard" timers do.
- **Mode breakdown** — a stacked bar + legend showing the share of
  transmissions by mode (DMR/D-Star/YSF/P25/NXDN) over the last 12 hours.
- **Activity chart** — a line chart of transmission counts over the last
  12 hours in 15-minute buckets, toggleable between **Per-hotspot** (one
  line per hotspot that had any activity in the window) and **Aggregate**
  (summed across the fleet).

One row is logged per completed transmission (not per poll), and rows
older than 13 hours are pruned automatically on each write — the table
doesn't grow unbounded. Turning the toggle off stops new writes but
doesn't delete the existing `activity.db` file.

## Talkgroup / reflector display

Each card shows a "Linked:" line with the last talkgroup or reflector seen
in that hotspot's MMDVM log. It's extracted from the same
`... from CALL to TG 31665`-style line used to detect the active call, the
same way mode/RSSI/BER are. A couple of behavior notes:

- It's **sticky** — once a destination is parsed, it stays shown (as "last
  known link") even after the call ends or while idle, rather than
  flickering back to blank. It only updates when a new line parses cleanly.
- It's tuned against DMR-style phrasing, since that's the format
  confirmed working in this setup. Other modes (D-Star reflectors, YSF
  rooms, P25/NXDN talkgroups) may phrase the destination differently and
  might not populate at first. If that's the case for one of your nodes,
  grab a sample log line while it's mid-transmission (same approach as
  diagnosing a missing active-call indicator) and the parser in
  `monitor.py`'s `_extract_destination` can be tuned to match.

## Known tradeoffs (intentionally left as-is for now)

- No authentication on `/setup` — anyone who can reach the dashboard can
  add/edit/delete hotspots.
- Hotspot passwords are stored in plaintext in `hotspots.json` and are
  re-sent to the browser to pre-fill the Edit form (so editing a hotspot
  doesn't force you to retype the password). Both are fine for a private
  LAN; worth revisiting before exposing this anywhere public.
- The QRZ password and APRS.fi API key are likewise stored in plaintext in
  `settings.json` and pre-filled into their Settings fields the same way.
  The MQTT broker password (if set) is stored the same way too.
- SSH host keys are auto-accepted (`AutoAddPolicy`) — convenient for a small
  fleet of devices you control, but it skips host key verification.
