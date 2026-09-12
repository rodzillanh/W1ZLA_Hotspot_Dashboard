# W1ZLA Hotspot Dashboard

<!-- wiki-image: dashboard-full.png -->

Live status dashboard for a fleet of Pi-Star / WPSD hotspots, AllStarLink
(ASL3) nodes (polled over SSH), and openSPOT 4 (SharkRF) nodes (monitored
over HTTP/WebSocket).

<!-- wiki-group: Getting Started -->
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


<!-- wiki-group: Getting Started -->
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

<!-- wiki-group: Getting Started -->
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
  --network host \
  -v /mnt/user/appdata/hotspot-dashboard:/app/data \
  --restart unless-stopped \
  hotspot-dashboard:latest
```

`--network host` (matching Options A/B) is what makes an openSPOT4's
`.local` hostname resolvable from inside the container — see the
openSPOT4 section further down for why. If you don't need that and would
rather keep the container's own isolated network, swap it for
`-p 5000:5000` (and `-p 2237:2237/udp` too, if you use the live WSJT-X
QSO logging feature).

Same caveat as Option A: updating later means removing and recreating the
container, not just restarting it.

---

Once it's running, open the dashboard and hit **Settings** to add a hotspot
(name, IP, SSH user/password). It polls every 5 seconds by default.
Hotspot config lives in `hotspots.json` under the appdata folder above, so
it survives container rebuilds and Unraid reboots.

<!-- wiki-group: Getting Started -->
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

<!-- wiki-group: Getting Started -->
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

<!-- wiki-group: Dashboard and Live Map -->
## Dashboard look
<!-- wiki-image: hotspot-card-wpsd-active.png -->

The dashboard uses an "instrument panel" visual identity — graphite panel
surfaces, a warm amber accent, a separate cyan color meaning "on-air right
now", monospace treatment for callsigns/frequencies/timers. A few extras
beyond the base card/map UI:

- **Fleet status pill** in the toolbar — "X/Y online · Z active now,"
  turning live/pulsing the instant anything's on the air, without needing
  to scroll through cards to check.
- **Spotlight dimming** — when any hotspot is active, the idle ones dim
  slightly so the live one stands out (an actively-transmitting favorite
  never dims). Toggle in Settings → General → Appearance ("Dim inactive
  hotspot cards when one is active") if you'd rather every card stay at
  full brightness — on by default.
- **A live VU meter** on an active hotspot's card, replacing the RSSI/BER
  line while a call is in progress — anchored to that hotspot's real
  signal reading, not just decorative animation.
- **An optional courtesy tone** (Settings → General → Appearance → "Play
  a courtesy tone on new activity") — a short synthesized beep, same idea
  as a real repeater's courtesy tone, when any hotspot's active call
  starts. Off by default.
- **A gear icon (⚙) on every hotspot and camera card opens its detail
  drawer** — the card body itself isn't a click target; the existing
  links inside a card (its name, 📍 map, the update badge, any callsign)
  keep doing exactly what they did before. The drawer combines everything
  that's tracked but doesn't fit on the compact card — full recent-talker
  history, Brandmeister status/static talkgroups, and WPSD update status
  (AllStarLink cards get the complete linked-node list instead, plus a
  quick Connect/Disconnect action per node and a "connect to node #"
  field) — with an editable **Settings** section for that specific card:
  name, IP, SSH/admin credentials, lat/lon, and the type-specific fields
  (Brandmeister ID for WPSD, ASL node + DVSwitch bridge/ports for
  AllStarLink, admin password + additional profile passwords for
  openSPOT4; camera cards get name/type/stream URL or Bambu
  IP+access-code+serial). Every field saves the instant you leave it —
  no separate Save button — and the same Test connection/Test ASL node/
  Test openSPOT4/Test Brandmeister ID/Test stream buttons the full
  Settings page has are right there too. Deleting a hotspot or camera is
  intentionally NOT available from the drawer — an "Open in full
  Settings →" link covers that and anything else not shown here.
- **Quick Settings** — the toolbar's "⚙ Settings" link opens this first
  (one settings entry point, not a separate gear icon sitting next to
  it): theme, spotlight dimming, and courtesy tone, plus — once the Live
  map tab has been opened at least once this session — map style, every
  map overlay toggle (grey line, aurora, POTA, SOTA, PSK Reporter,
  Satellites), and the ADIF QSO log tools (import, stats, "show on map",
  clear). Everything saves instantly, same as the full Settings page; the
  overlay toggles proxy directly to the map legend's own checkboxes
  rather than tracking a second copy of that state, so the two never
  disagree — while you're actually on the Live map tab, that section
  shows a short note pointing at the legend instead of duplicating it
  there. A "More settings →" link at the bottom goes to the full
  `/setup` page for everything not covered here.

<!-- wiki-group: Dashboard and Live Map -->
## Dashboard customization
<!-- wiki-image: settings.png -->

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
  to appear on the dashboard (Cards tab). If the Fleet activity, ASL
  Favorites & Control, HF Conditions, Band Plan, License Quiz, Band
  Activity, Big Ass Clock, Notifications (APRS Messages + HamAlert),
  Satellites, or Flights Overhead card is enabled, it appears in the same drag list and can be moved to
  any position among the hotspot cards, not just first. Every enabled
  camera card (Settings → Cameras) gets its own row in the same list
  too, one per camera.
  Adding/editing/deleting a hotspot itself is a separate list on the
  Hotspots tab — Card order is just for arranging what's already there.
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
- **Node type** — WPSD/Pi-Star (default) or AllStarLink (ASL3), set per
  hotspot on the Hotspots tab's add/edit form. Switches which fields show
  (ASL node number instead of Brandmeister ID) and how that hotspot is
  polled — see "AllStarLink (ASL3) nodes" below.
- **Per-hotspot Enabled toggle** — Hotspots tab, both in each row (a quick
  Enable/Disable button, no need to open the edit form) and in the add/edit
  form itself. A disabled hotspot keeps its saved IP/credentials/settings —
  it's just skipped from polling and hidden from the dashboard/live map
  entirely until re-enabled, rather than having to delete and re-add it
  later.
- **Host power control** — General tab, only shown on a standalone install
  running directly on real Raspberry Pi hardware; see "Host power control"
  below.
- **ASL favorites & control card** — off by default (General tab toggle);
  see "ASL Favorites & Control" below.

<!-- wiki-group: Integrations -->
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

<!-- wiki-group: Integrations -->
## RadioID.net lookup

A free, no-account fallback for caller name and city/state, used only when
QRZ doesn't return anything (no subscription, or the callsign isn't in
QRZ's database). It has no lat/lon data, so it never contributes a map pin
on its own — it only fills in the name/location text on the card. On by
default; toggle it off in Settings → Integrations if you'd rather see a
bare callsign than an unverified fallback name.

<!-- wiki-group: Integrations -->
## QRZ Logbook sync

Optional, **read-only** — pulls the contacts already in your
[QRZ Logbook](https://logbook.qrz.com) into the Recent Contacts card and,
when a contact gets confirmed, posts a "confirmed contact" event to the
Notifications card and shows a green **✓ QRZ** badge on that row.

This uses a **QRZ Logbook API key**, a different credential from the
QRZ.com login above — find it under QRZ Logbook → Settings → "This
logbook is enabled for the QRZ API". The paid XML subscription is **not**
required. Enter the key in Settings → Integrations → "QRZ Logbook sync",
then turn the sync on under Settings → Cards → Notifications card sources
→ "QRZ logbook confirmations in Notifications" (that one switch gates the
whole feature — sync, the badge, and the notifications). A **Sync now**
button next to the key runs a sync immediately and reports what it pulled
(added / matched / newly confirmed), so you don't have to wait for the
next background cycle.

- **Deduped** against QSOs already logged by WSJT-X or an ADIF import — a
  contact in both places is matched on callsign + band + mode + time and
  shown once, with the logbook's details filling any blank fields, rather
  than added a second time.
- **Confirmations are a state change on an existing QSO**, so the sync
  periodically re-checks your still-unconfirmed contacts (up to a year
  old) and flags the moment one flips to confirmed — exactly one
  notification per confirmation, never a repeat, and never one for a
  contact that was already confirmed the first time it synced.
- **One-way.** It only reads your logbook; it never uploads this
  dashboard's WSJT-X / imported QSOs to QRZ.
- New QSOs are pulled about every 30 minutes, confirmations re-checked a
  few times a day — a logbook confirmation depends on the other operator
  logging the contact too, so it's never instant regardless.

<!-- wiki-group: Integrations -->
## APRS.fi live position

Optional — requires a free API key from
[aprs.fi/page/api](https://aprs.fi/page/api), entered in Settings →
Integrations. When the active caller has a recent APRS beacon, their live
position from that beacon is used on the Live map instead of QRZ's static
home-station coordinates — much more useful for mobile or portable
operators. Positions are cached only 2 minutes (`APRS_CACHE_TTL`) since
they can move. Leave the API key blank to disable; the map falls back to
QRZ/RadioID's static location or no pin at all if neither has data.

<!-- wiki-group: Integrations -->
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

<!-- wiki-group: Integrations -->
## Rig control (rigctld)

Optional — Settings → Integrations → **Rig control (rigctld)**, off by
default. Point it at a [Hamlib](https://hamlib.github.io/) `rigctld`
server on your LAN and every activator-spot frequency in the [POTA
card](#pota-card) becomes a **tap-to-tune button**: click it and your
radio jumps to that frequency, and — when "send mode" is on — its mode
too (`SSB` maps to USB/LSB by band, `CW` to CW, `FT8`/`FT4`/`DATA` to
PKTUSB).

Any `rigctld` source works. [WFView](https://wfview.org/) has one built
in — Settings → enable **RigCtld**, and make sure it's set to listen on
the LAN interface, not just localhost. A standalone `rigctld` (Hamlib),
or anything else that speaks the rigctl protocol (SparkSDR, Thetis, …),
works the same way. Default port is **4532**; a bare `host:port` in the
host field works too.

Under the hood this is a short-lived TCP connection per click — connect,
send `F <hz>` (and `M <mode> 0`), read `RPRT 0`, disconnect. `rigctld`
serializes CAT access, so it doesn't collide with WSJT-X or a logger
holding their own connections. A small pill on the POTA card shows
whether the rig server is reachable (and the rig model, if it reports
one); when it isn't reachable the frequency is just plain text. **No
login** — `rigctld` has no auth, so this assumes a trusted LAN, same as
the links to hotspot web UIs. Use the **Test connection** button in
Settings to check reachability.

<!-- wiki-group: Hotspot Types -->
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

<!-- wiki-group: Hotspot Types -->
## WPSD radio frequency / duplex / identity display

A WPSD hotspot's card shows a few extra pieces of static info, all read
from `/etc/mmdvmhost` (WPSD's own live MMDVMHost config — confirmed
against a real install; it's not the same as upstream MMDVMHost's own
shipped config template, which has no such fields at all):

- **Frequency**, right next to Mode (e.g. "433.750 MHz (Simplex)") —
  shown as one combined number plus Simplex/Duplex for the overwhelming
  majority of hotspots; shown as "RX/TX MHz" only if RX and TX genuinely
  differ.
- **Registered callsign/DMR ID and configured location**, as a small line
  under the card's name (e.g. "W1ZLA (3100486) · Barrington, NH") — the
  hotspot's own identity as configured on the device, distinct from
  whatever name you've given the card in Settings (which might be a
  nickname) and distinct from whoever's currently keying up through it.

All of this is read on the same slow background cadence as the update
check above, not every poll — it rarely changes. Not available for ASL3
(AllStarLink has no equivalent concept) or openSPOT 4 (no SSH access at
all), so these lines simply don't appear on those card types; a field
missing from your `/etc/mmdvmhost` (e.g. no `Location` set) just omits
that piece rather than showing a placeholder.

<!-- wiki-group: Hotspot Types -->
## D-STAR reflector link/unlink (ircDDBGateway)

If a WPSD hotspot runs **ircDDBGateway** (WPSD's D-STAR reflector
daemon) with its remote-control interface enabled, its card drawer gets
a "D-STAR (ircDDBGateway)" section: your current reflector link, any
secondary DExtra/DPlus/DCS/CCS clients, and buttons to link a new
reflector or unlink an existing one — right from the dashboard, no SSH
involved.

This talks to ircDDBGateway's own remote-control protocol directly over
plain UDP (the same port/password ircDDBGateway itself uses for its
Windows/Android remote-control apps) — not SSH, and not a new port you
need to open anywhere, since it's a request this app makes outward to
the hotspot.

To enable it for a hotspot:

1. On the hotspot, confirm ircDDBGateway's remote control is on and note
   its port/password:
   ```bash
   sudo grep -A2 -i remote /etc/ircddbgateway
   ```
   This should show `remoteEnabled=1`, a `remotePort` (WPSD's own
   default is `54321`), and a `remotePassword`.
2. In this dashboard's Settings → Hotspots, edit that WPSD hotspot and
   check **"This node runs ircDDBGateway (D-Star)"**. Enter the port,
   password, and the repeater callsign ircDDBGateway is configured with
   (its own `repeaterCall1` — often, but not always, your callsign plus
   a trailing module letter, e.g. `W1ZLA D`).
3. Click **Test connection** to confirm the dashboard can log in.

Only available for WPSD hotspots — ASL3 has no D-STAR concept, and
openSPOT 4 has its own separate remote control (no SSH access at all).

<!-- wiki-group: Hotspot Types -->
## AllStarLink (ASL3) nodes
<!-- wiki-image: hotspot-card-asl3.png -->

A second node type alongside WPSD/Pi-Star, added in v3.0 — Settings →
Hotspots → **Node type** → "AllStarLink (ASL3)" instead of the default
WPSD/Pi-Star. Same SSH credentials as any other hotspot; add the node's
own **ASL node number** in the field that appears.

**Requires passwordless `sudo` for the SSH user.** The Asterisk control
socket (`asterisk.ctl`) is normally root/`asterisk`-group only — this
dashboard runs `sudo asterisk -rx "rpt xnode <node>"`, the same way you'd
run it by hand at a shell prompt. If that SSH user would be prompted for
a sudo password interactively, the poll (and the Settings "Test ASL node"
button) will fail silently rather than hang, since there's no way to
supply a password non-interactively — it'll look like `"Connected, but
node <N> didn't return link status"` even though the node number is
correct, since running the same command by hand at an interactive
terminal lets you type the password and see it succeed, masking the
problem. If you see that message, confirm it's this rather than a wrong
node number by running the exact command by hand over SSH — if it
prompts for a password, set up a passwordless sudo rule for that one
command (adjust the `asterisk` path if `which asterisk` differs):

```bash
echo 'youruser ALL=(ALL) NOPASSWD: /usr/sbin/asterisk' | sudo tee /etc/sudoers.d/hotspot-dashboard-asterisk
sudo chmod 0440 /etc/sudoers.d/hotspot-dashboard-asterisk
```

Rather than tailing an MMDVM log, an ASL3 hotspot is polled by running
`asterisk -rx "rpt xnode <node>"` over the same SSH connection, which
dumps `app_rpt`'s dialplan variables — including `RPT_ALINKS`, which
gives real per-linked-node keyed state (`<node><mode><K or U>` per link),
confirmed against a real node rather than assumed. This is what lets the
card show which specific linked node is currently transmitting, not just
"something is active."

Linked node numbers are resolved to callsigns via AllStarLink's free,
public stats API (`stats.allstarlink.org`) — not every linked node has a
callsign on file (private/unregistered nodes just show their bare
number). Once resolved, a callsign gets the same QRZ/RadioID enrichment
and favorites/APRS-alert treatment as a DMR caller does.

The card uses a side-by-side layout: node number/temperature/CPU (same
generic Linux commands as WPSD) sit in a narrow left column, and a
**Linked:** list on the right shows every currently connected node —
callsign (or bare node number if it has none on file), plus its
frequency/description and location where AllStarLink has that data,
with a pulsing dot next to whichever one is currently keyed. Putting
the linked-node list on the right rather than stacking it below keeps
the card from growing taller as more nodes link in. The active/
last-heard state above it uses the same live timers as every other
card. There's no WPSD-equivalent concept for
mode/RSSI/BER/color-code/timeslot/Brandmeister, so those don't appear on
an ASL3 card — and the WPSD-specific git update check is skipped for
this node type entirely, rather than running a check that could never
apply.

**SA818 radio frequency (and CTCSS/DCS tone, if set).** Many simplex
ASL3 builds (e.g. a SHARI-style Pi hotspot) use an SA818 RF module,
configured via the `sa818-menu` tool. If `/etc/sa818.conf` is present,
its last-programmed frequency shows as a **Freq** stat right in the
node/temp/CPU column, with a **Tone** stat underneath it whenever a
CTCSS or DCS tone is actually configured (e.g. "CTCSS 110.9" or "DCS
023") — omitted entirely if no tone is set. The hotspot's own detail
drawer (gear icon) has a fuller "Radio (SA818)" section, which also
explains *why* there's no frequency shown when there isn't one: either
`sa818-menu` was run but nothing was ever programmed (still its
`000.0000` placeholder), or there's no SA818 config file on that host
at all. Since the module can't be read back over the air, this is only
ever a record of what that specific host last wrote — if the radio was
reprogrammed from a different machine, this won't reflect it.

**Brief keyups can be missed between polls.** Unlike the WPSD path (which
tails a log with a short rolling history buffer), each ASL3 poll only
sees the *instant* it connects — there's no history. A keyup shorter than
the poll interval (`POLL_INTERVAL`, default 5s) can start and end
entirely between two polls and never get captured. Confirmed against a
real node: a brief test keyup was missed, but a sustained one (5-10+
seconds) showed up correctly. This is an inherent tradeoff of polling
point-in-time state rather than a log, not a bug to chase.

**"ACTIVE: &lt;callsign&gt;" identifies the linked node, not the individual
operator talking through it.** `RPT_ALINKS` only reports which *node* is
currently keyed — AllStarLink/`app_rpt` has no per-transmission caller ID
equivalent to DMR's talker alias. If the keyed node is a shared hub/
reflector, its registered callsign can stay pinned on the card (and
`tx_start` won't reset) across several different people keying up one
after another through that same node, since the dashboard has no way to
tell them apart — it only sees "this node is keyed," same as any other
ASL3 monitoring tool (AllScan included). Not something pollable/fixable
via SSH; a protocol ceiling, not a bug.

<!-- wiki-group: Hotspot Types -->
## DVSwitch card
<!-- wiki-image: dvswitch-card.png -->

One optional card, shared by every ASL3 node that also runs a DVSwitch
(Analog_Bridge) audio bridge — distinct from the DVSwitch mode on the
**Fleet Activity** card above; that's just an activity count, this is a
full status card. A **"Show:" picker** in the header switches between
nodes if you have more than one — enabling DVSwitch on a second node adds
it to that list, not a second card. Turn it on per node the same place as
the Fleet Activity mode (Settings → Hotspots → edit the ASL3 node →
"This node also runs DVSwitch (Analog_Bridge)"), then list one or more
**bridge ports** (comma or newline separated — Analog_Bridge supports
running multiple instances on one node, each identified by its own port).
All of this reuses the same SSH connection/credentials already configured
for that node — no separate login or remote-access setup needed.

When there's genuinely nothing to report — no bridge tuned, no call in
progress, nothing heard yet this session — the card collapses to a single
quiet status line instead of always showing the full layout below.
Otherwise it shows:

- **Header badge** — lights up only while a transmission is actually in
  progress, alongside how many configured bridges are currently tuned to
  something vs. idle. A bridge sitting tuned to a static talkgroup with
  nobody talking through it doesn't light the badge on its own — that's a
  configuration fact, not activity.
- **Bridge chips** — one per configured port that's currently tuned, with
  the talkgroup/reflector it's tuned to. "Tuned" reflects whether
  Analog_Bridge's own live status file has a value set, not a fully
  verified connect/link state — see the caveat below. Untuned ports are
  summarized in a single muted line rather than a full chip each.
- **Last heard** — the two most recent callers heard through any of this
  node's bridges, with elapsed time. Callsign links go to QRZ if
  configured, RadioID.net otherwise, same as every other clickable
  callsign in this app.
- **Vocoder pill** — whether Analog_Bridge is using its hardware AMBE chip
  or has fallen back to a software vocoder. Read from each bridge's own
  live status file every poll (so it can't go stale), falling back to a
  one-time log line from Analog_Bridge's own startup only if that live
  data isn't available.
- **Sparkline** — a small recent-activity trend, reusing the same logged
  data the Fleet Activity card's DVSwitch mode already writes.
- **Live RX/TX row** — a pulsing "W1ZLA → TG 603" style indicator for a
  transmission that's actually in progress right now, plus DMR master /
  D-Star link status. Unlike everything else on this card, this is
  sourced from a *second* DVSwitch log file (`MMDVM_Bridge.log`, not
  `Analog_Bridge.log`) that has real start-and-end transmission markers —
  scoped to DMR and D-Star only; YSF/P25/NXDN each log to their own
  separate file and aren't covered.

**Three honest caveats, not hidden anywhere else in this app's docs:**
Analog_Bridge's own log has no confirmed "transmission ended" line, so
"Last heard" shows time since a transmission started, not how long it
lasted. And the exact value Analog_Bridge's status file uses to mean
"nothing currently tuned" was never confirmed against a real device beyond
one live test — if a bridge that's actually idle ever shows as tuned (or
vice versa), that's the mostly likely reason; the underlying status data
itself is otherwise real, not simulated. And the live RX/TX row is
re-checked fresh on every poll with no memory of the previous one — if an
unusually long transmission's own start line scrolls out of the recent
log window before the call ends, the indicator can under-report idle
rather than guess.

Its position is part of the same drag-and-drop **Card order** list as
everything else (Settings → Hotspots) — one position for the whole card,
same as Fleet Activity/ASL Favorites, not one per node.

<!-- wiki-group: Hotspot Types -->
## ASL3 live audio-level VU meter
<!-- wiki-image: hotspot-card-asl3.png -->

An optional, real (not synthetic) VU meter for ASL3 hotspot cards. WPSD/
openSPOT4 cards already have one driven by RSSI/BER/dejitter telemetry —
ASL3 has none of that (it's IAX2/Asterisk, not a radio modem), so this
taps the node's own repeater audio directly instead.

**One-time setup per node**, run *on the node itself* over SSH:

```
sudo bash provision-audio-meter.sh <your-node-number>
```

This loads four Asterisk modules (already present on a stock ASL3 build,
just not loaded by default) and installs a small, isolated dialplan file
of its own — it never edits your existing `extensions.conf`/`modules.conf`
content, only appends an `#include` line and a `load =>` line if they're
not already there. Safe to re-run. Then, back in the dashboard, tick
**"Live audio-level VU meter"** for that hotspot (Settings → Hotspots →
edit the node, or the card's own gear-icon drawer).

No SSH credentials or ports to configure beyond what's already there —
the dashboard triggers a `ChanSpy`/`AudioSocket` dialplan call over the
same SSH login already stored for that hotspot, and tunnels the audio
back to itself over that same connection (an SSH reverse port forward),
so nothing new needs opening on your network or firewall.

The meter updates several times a second, not just once per poll cycle,
so it visibly tracks real signal dynamics rather than stepping between
infrequent samples.

Confirmed working live against real hardware (see CLAUDE.md for the full
diagnostic story) — as a one-time sanity check after provisioning a new
node, this should show at most one matched pair while the meter is
active, and none while idle:

```
sudo asterisk -rx "core show channels concise" | grep dashboard-audiospy
```

<!-- wiki-group: Hotspot Types -->
## openSPOT 4 (SharkRF) nodes

A third node type alongside WPSD/Pi-Star and ASL3, added in v3.38 —
Settings → Hotspots → **Node type** → "openSPOT 4 (SharkRF)". Unlike the
other two types, an openSPOT4 is a closed embedded device with no SSH
access at all — there's no user field to fill in, just the device's own
admin password (the same field WPSD/ASL3 use for the SSH password).

Monitored over the device's own HTTP + WebSocket API rather than SSH:
logging in gets a JWT, then a persistent WebSocket connection
(`ws://<ip>/<jwt>`) streams live status and call events, so the card
updates the moment a call starts or ends rather than waiting on a poll
cycle. This whole API was verified live against a real openSPOT 4 Pro
(browser dev-tools network capture) — SharkRF's own published API docs
turned out to describe an older, incompatible openSPOT generation with
different endpoint names.

The device resolves DMR-ID-to-callsign itself and reports it directly;
that callsign still gets the same QRZ/RadioID enrichment (name,
location, photo) and favorites/APRS-alert treatment as any other card.
Mode/RSSI/BER and the linked talkgroup show the same way a WPSD card
does — there's no Linux host access, so board temperature/CPU don't
appear the way they do for WPSD/ASL3 (the openSPOT4's own board
temperature shows in its Battery section instead, on a battery-powered
unit). Uptime, battery status, WiFi signal, network round-trip latency,
the active config profile, and its own built-in APRS messaging all show
on the card/drawer too — none of this is in openSPOT4's documented HTTP
API, all of it comes over the same WebSocket feed.

**The IP address field can be a hostname, not just a numeric IP.**
openSPOT4 runs its own mDNS responder and answers to a `.local` name
derived from its configured hostname (e.g. `openspot4.local`) — if that
resolves for you (test with `ping openspot4.local` from the same
network), you can put that hostname straight into the Settings IP field
instead of a raw IP, so a DHCP-assigned address change no longer breaks
the connection. Confirmed working from a bare Windows/macOS/Linux
machine on the LAN. **Docker/Unraid needs one extra step for this to
work**: mDNS depends on multicast traffic reaching the real network,
which Docker's default bridge network doesn't reliably pass through —
`docker-compose.yml`/`docker-update.sh`/the Unraid template all default
to `--network host` for exactly this reason (see the tradeoff explained
in their own comments). If you'd rather keep the container's isolated
bridge network, a plain numeric IP (optionally with a DHCP reservation
on your router, so it stops changing) works exactly as before with no
extra step needed at all.

**DMR and C4FM(YSF) verified so far.** Call start/end tracking has been
confirmed against real calls on both DMR (Homebrew/BrandMeister-style
connectors) and C4FM/YSF (YSF Reflector connectors, e.g. TGIF) — these
two turned out to behave differently enough under the hood (see
`CLAUDE.md`) that getting DMR working didn't automatically mean YSF
would work too, and it initially didn't. D-STAR and NXDN/P25 are still
unverified — active-call info may not populate correctly in those modes
until confirmed against a real call the same way.

**Each openSPOT4 config profile can have its own separate password.**
Switching the device to a different profile (which reboots it) can mean
the password saved in Settings no longer works, even though the IP
hasn't changed. Rather than having to update the password every time you
switch profiles, add each profile's password under **Additional profile
passwords** (one per line) in the openSPOT4 hotspot's Settings form — the
dashboard tries the primary password first, then each additional one in
turn, until one works, since only one profile is ever active on the
device at a time. If the card still shows Offline and the Settings
"Test" button reports a 401 after all of that, none of the stored
passwords matched the currently active profile.

<!-- wiki-group: Admin and Maintenance -->
## Host power control

Optional "🔄 Reboot Pi" / "⏻ Power off Pi" buttons in Settings → General —
controls the device *running the dashboard itself*, not a monitored
hotspot. Runs `systemctl reboot` / `systemctl poweroff` locally (no SSH
involved, since it's the same machine) rather than `sudo` — the systemd
service runs with `NoNewPrivileges=yes`, which blocks `sudo`/setuid
entirely no matter how sudoers is configured, so `systemctl` (talking to
systemd over D-Bus) is used instead, keeping that hardening intact.

This needs one small polkit rule granting the service user permission for
the `org.freedesktop.login1.reboot`/`power-off` actions — a headless
systemd service has no active login session, so polkit denies these by
default otherwise. `install.sh` writes this rule automatically on a fresh
install, and `update.sh` (re-)writes it on every update, so **existing
installs pick it up just by running `sudo bash update.sh` again** — no
separate manual step needed once you're on this version.

**Only shown on a standalone install running directly on real Raspberry
Pi hardware**, detected automatically at startup by checking for
`/proc/device-tree/model` (genuine Pi hardware) and the absence of
`/.dockerenv` (not inside a container). This is deliberate: inside a
Docker/Unraid container, "reboot the host" can't actually reboot the
underlying box from in here, so the buttons are hidden entirely rather
than doing something misleading. There's no environment variable or
Settings toggle to force this on — it's autodetected only.

**Power off is one-way** — the Pi stays off until someone physically
restores power; there's no remote way to turn it back on. Both actions
require confirming a browser dialog first.

**Security note:** like every other write in this app, these routes have
no authentication (see "Known tradeoffs" below) — anyone who can reach
the dashboard on your network can trigger them. That's a materially
bigger consequence than editing a hotspot's config, so if this dashboard
is reachable beyond a trusted LAN, put it behind a reverse proxy with
auth, or don't enable this feature's host access, before relying on it.

<!-- wiki-group: Admin and Maintenance -->
## Self-update

Settings → Version info checks this project's git repository for new
commits and, on a standalone Pi/Linux install, can install them with
one click. A flashing ⚡ next to the ⚙ Settings link on the dashboard
itself shows up whenever an update is available and links straight
there.

- **Check** — compares the commit this deployment was built from
  (`BUILD_COMMIT`, written automatically by `install.sh`/`update.sh`/
  `docker-update.sh`) against the latest commit on the configured
  branch, via the git host's REST API (Forgejo/Gitea-compatible:
  `GET /api/v1/repos/{owner}/{repo}/branches/{branch}`) — no git
  installed inside the container/venv needed at runtime, just one HTTPS
  GET. Cached for ~5 minutes; the Version info tab checks once on load,
  and a "Check for updates" button forces a fresh check. Repo URL/branch and
  an on/off toggle live in Settings → General ("Software updates") —
  defaults to this project's own repo.
- **Install (standalone Pi/Linux only)** — an "Install update" button
  appears when an update is available. The dashboard's own process runs
  unprivileged (`NoNewPrivileges=yes`, `ProtectSystem=strict` — it can
  only write inside its data directory), so it can't pull new code or
  restart itself directly. Clicking the button just writes a trigger
  file into that data directory; a separate systemd `.path` unit
  (installed by `install.sh`, re-provisioned idempotently by
  `update.sh` — existing installs pick it up just by re-running
  `update.sh`) notices the file and runs `git pull` + `update.sh` as
  root, the same privilege-separation idea as Host power control's
  polkit rule above, scaled up for a bigger action. Logs to
  `/var/log/hotspot-dashboard-update.log`.
- **Docker/Unraid** — install is not automatic. A container can't
  safely rebuild and replace itself from the inside without mounting
  the Docker socket in, which is close to giving it root on the host —
  not done here. Instead, the Version info tab shows the exact command
  to run yourself: `cd /mnt/user/appdata/hotspot-dashboard-src && git
  pull && bash docker-update.sh`.
- If this deployment has no `BUILD_COMMIT` yet (a manual/dev checkout,
  or an install from before this feature existed), the check just shows
  "unknown" and no update banner — re-run `update.sh`/`docker-update.sh`
  once to start tracking it.

**Security note:** same as Host power control — no authentication by
default, and "install update" is a bigger consequence than most actions
in this app (it runs `git pull` and restarts the service as root).
Put this behind a reverse proxy with auth, or turn off "Check for
updates" in Settings, if the dashboard is reachable beyond a trusted
LAN.

<!-- wiki-group: Hotspot Types -->
## ASL Favorites & Control
<!-- wiki-image: asl-favorites-card.png -->

Optional dashboard card (Settings → Cards → "Show ASL favorites &
control card", off by default), inspired by
[AllScan](https://github.com/davidgsd/AllScan)'s favorites/scan/connect
model. Lets you keep a list of ASL node numbers you care about (a
different list from the DMR callsign Favorites tab) and connect or
disconnect them from one of your ASL3 hotspots with one tap.

The card itself is deliberately minimal — a glance-and-tap surface, not a
management screen:

- **Tile grid** — 5 pinned favorites, each one a tile you tap to
  connect or disconnect. A tile's color/dot shows its live state (🔴
  keyed, 🟢 connected, amber for "recently active elsewhere on the
  network but not through your hub right now") straight from the
  selected hotspot's already-polled link table — no extra polling or
  external API calls.
- **A 6th, reserved tile** — always set aside for whatever you're
  connected to *outside* your 5 pinned favorites, never a 6th favorite.
  A quiet placeholder when nothing extra is connected; fills in with its
  own status and ☆ Pin / Disconnect buttons the moment something is.
- **Quick connect** — a field next to the "ASL Control" button, right on
  the card. Type any node # and hit Go — no need to save it as a
  favorite first, and no need to open the drawer. Hover it for a
  reminder of what it does and where the connection shows up.
- **Multi-connect** — a checkbox right on the card (see below).
- **Control from** — a dropdown picks which of your configured ASL3
  hotspots originates the connect/disconnect command, if you have more
  than one.
- A small status pill next to the card's name shows keyed/connected
  counts across every pinned favorite — nothing shown at all when
  everything's idle.

Everything else — adding/removing favorites, choosing *which* favorites
are pinned to the card, and Monitor mode — lives one tap away in the
**ASL Control drawer** (the card's own "ASL Control" button, or, per
below, any ASL3 hotspot's own card):

- **Pin/unpin** — a ★/☆ next to each favorite in the drawer's list
  chooses whether it takes one of the card's 5 tile slots. Pin a 6th
  while already full and the longest-pinned favorite steps aside
  automatically to make room — the card always shows exactly who you
  chose, never an arbitrary "most recent" or "most active" cutoff.
- **Add / remove a favorite** — node number + optional label. A resolved
  callsign (via the same `aslstats.py` lookup ASL3 cards already use) is
  shown automatically if the label is left blank. A newly added favorite
  is pinned by default (same "step the oldest one aside if full" rule).
- **Monitor mode** — receive-only, no transmit capability, for any node
  number whether or not it's a saved favorite (Quick Connect itself lives
  on the card now — see above).
- **Local Monitor** — a third Quick Connect option next to Connect and
  Monitor. Also receive-only, but unlike Monitor it does **not** relay
  what it hears onward to your other linked nodes — useful for quietly
  listening in on a node without exposing it (or your own traffic) to
  whatever else you're connected to. A Local-Monitor'd link shows up as
  its own cyan "🎧 Local Mon" state, and plain Monitor gets a matching
  amber "🎧 Monitor" state — on the hotspot's own card, the ASL Favorites
  card, the ASL Control drawer, and the hotspot's detail drawer — so
  neither is ever indistinguishable from a normal connection, and
  Disconnect already works on both everywhere a link can show up. A node
  still mid-handshake shows a "Connecting…" tag too. Where there's room
  (the hotspot's own detail drawer, and as a hover tooltip elsewhere),
  each link also shows how long it's been connected.
- **Disconnect all links on this node** — a separate, deliberately
  de-emphasized action near the bottom of the drawer, behind a confirm
  dialog that names the actual node. Unlike every other action here,
  this affects the node's **entire** link table at once, not just
  favorites this app tracks — including any permanent/backbone
  connections — so it gets real friction instead of sitting next to
  Connect/Monitor as a peer button.
- **Local node status** — the *controlling* hotspot's own live state
  (idle/keyed, uptime, temperature), which the compact card itself never
  shows since it's only ever displaying favorites' state.
- **Import a `favorites.ini`** — pulls in AllScan's own favorites file
  format directly, for anyone migrating from an existing AllScan setup.
- **AllScan link** — jumps straight to that node's own AllScan control
  panel, if it's running one, at `http://<node ip>/allscan`.

**Connect / Disconnect** runs `asterisk -rx "rpt cmd <node> ilink <code>
<remotenode>"` over the same SSH connection already used for polling.
This is **not** the DTMF-simulated `rpt fun <node> *3<node>` form (which
requires replicating `app_rpt`'s digit-collection state machine and
proved unreliable in testing) — `rpt cmd` takes the function code and
node as plain separate arguments, confirmed both against a real node and
by checking how AllScan itself — a mature, widely-used tool — does the
same thing. Connects are temporary (transceive), not permanent — there's
no "connect permanently" option here; use WPSD/AllStarLink's own admin
tools for that.

By default, connecting to a favorite first disconnects any *other*
favorite currently connected/keyed on that same hotspot, so you don't end
up stacking links by accident. **Multi-connect** (a checkbox on the card
itself, and again next to the favorites list in the drawer — both toggle
the same shared setting, so checking one instantly checks the other)
skips that and just connects. This only ever touches favorites tracked in
this app — it won't disconnect a link made some other way (e.g. a
permanent link configured directly on the node).

**ASL Control stays reachable even with this card turned off entirely** —
every ASL3 hotspot's own card has an "Open in ASL Control" button in its
own detail drawer (the ⚙ on that card), landing you on that node already
selected. Turning off the compact Favorites card only removes the
glanceable multi-node tile view; Quick Connect, Monitor mode, and
favorite management are still one tap away from any ASL3 node.

By default the card appears first, before any hotspot cards. Its position
is part of the same drag-and-drop **Card order** list as the hotspot cards
(Settings → Hotspots) — drag it anywhere in that list to move it.

<!-- wiki-group: Integrations -->
## Camera cards

Optional (Settings → Cameras → "Enable camera cards", off by default).
Each camera you add gets its own card on the dashboard — a live MJPEG
video feed, not a static snapshot — positioned in the same **Card
order** drag list as hotspot cards (Settings → Hotspots), exactly like a
hotspot card rather than a single fixed-position card the way Fleet
Activity/ASL Favorites work.

Three camera types, all configured in Settings → Cameras:

- **Generic RTSP** — any camera exposing an RTSP stream URL
  (`rtsp://user:pass@ip:554/...`). Bridged to the browser via `ffmpeg`
  (transcoded to MJPEG server-side), since browsers can't play RTSP
  directly. **Requires `ffmpeg`** — installed automatically by
  `install.sh`/`update.sh` (standalone) and the Dockerfile; if a camera
  shows "ffmpeg not found," your install predates this and needs
  `update.sh`/a rebuild re-run once.
- **Wyze (RTSP firmware)** — a cosmetic alias for Generic RTSP, for
  Wyze cameras with Wyze's own **official RTSP firmware** flashed
  (Cam v2/v3, Pan v2/v3, and newer models per Wyze's firmware notes —
  not a third-party hack). Once flashed, a Wyze cam speaks plain RTSP
  at `rtsp://user:pass@<camera-ip>:554/stream0/`, so this uses the
  exact same `ffmpeg` bridge as Generic RTSP — the separate dropdown
  entry exists purely so it's discoverable, since most people don't
  know a Wyze cam can do this at all.
- **Bambu Labs A1** — needs the printer's **IP**, **serial number**, and
  **access code** (all in the printer's own Settings → WLAN screen).
  Bambu's camera doesn't use RTSP at all — it's a proprietary local
  protocol — handled via the
  [`bambulabs_api`](https://pypi.org/project/bambulabs-api/) package
  rather than anything hand-rolled here.

All three camera types are bridged to the same plain MJPEG stream
server-side, so from the browser's point of view a camera card is just
an `<img>` tag either way — an offline/reconnecting camera shows a
placeholder instead of a broken image, and a "⛶ Fullscreen" button on
each card is the same regardless of camera type.

**Resource use**: a camera's `ffmpeg` process (or Bambu connection)
only starts once its card is actually being viewed, and stops itself
~20 seconds after the last viewer navigates away — an enabled-but-
unwatched camera doesn't run in the background indefinitely. Click
"Test connection" in Settings before saving a camera to confirm it's
reachable without needing to fully add it first.

<!-- wiki-group: Integrations -->
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

<!-- wiki-group: Integrations -->
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
e.g. `"W1ZLA active on Garage Hotspot"`, or
`"N1IWP active on Garage Hotspot - TGIF 603"` when a talkgroup/reflector is
linked — and there's no delivery confirmation; sending is fire-and-forget,
matching how simple APRS bots generally behave. It uses the caller's
actual callsign rather than their optional friendly name/label, to read as
a proper APRS message.

<!-- wiki-group: Integrations -->
## Notifications card

Optional dashboard card (Settings → Cards) that merges up to six
independent message/alert sources into one scrollable, time-sorted list —
appears as soon as *any* is enabled, and each remains its own separate
on/off toggle underneath. Position it anywhere in the same drag-and-drop
**Card order** list (Settings → Hotspots), same as Fleet Activity/ASL
Favorites/Camera cards. Each enabled source shows as one pill in the card
header (with a connection dot for sources that have a real connection to
show) — click a pill to isolate that source's rows, click it again to go
back to showing everything. Each row is also tagged with a small icon so
the source stays visible even when showing all of them together.

**History survives a restart** — each source keeps its own recent history
(50 entries for APRS/HamAlert/geomagnetic alerts/Brandmeister favorites/
QRZ logbook confirmations, 100 for fleet online/offline events) in a
small local database, not just
in memory, so restarting the dashboard (an update, a reboot, a container
recreate) doesn't wipe out what you'd already seen. Each source's history
is capped independently — a busy source can't crowd out a quiet one.

**APRS Messages** (Settings → Cards → "Show APRS Messages card", off by
default) — the receiving half of APRS favorite alerts above: a persistent
APRS-IS connection, logged in as the same callsign already set for
outbound alerts (Settings → Integrations), that shows real incoming APRS
text messages addressed to you.

- **Auto-acknowledgment** — messages that request an ack (most APRS
  clients do) get one sent back automatically, same as any real APRS
  client. Skipping this would leave the sender's app thinking the message
  never arrived, retrying it several times. Shown per-message as a small
  "✓ ack sent" tag.
- **Duplicate-safe** — APRS senders retry an unacked message a few times
  over the network; retries of a message already seen (same sender +
  message number) are recognized and not shown or re-acked twice.
- **Catches messages to any SSID of your callsign** — same as how apps
  like APRS.fi aggregate messages regardless of which SSID they were
  sent to (`W1ZLA`, `W1ZLA-9`, `W1ZLA-1`, etc.), not just the exact
  callsign string typed into Settings. Each message shows which specific
  SSID it was addressed to, and the ack is sent from that same SSID.
- This is a separate, independent on/off toggle from APRS favorite
  alerts — you can run either one alone, or both, sharing the one
  callsign setting.

**HamAlert** (Settings → Cards → "Show HamAlert card", off by default) —
shows your own personal [HamAlert](https://hamalert.org) alert matches
(DXCC needed, specific callsigns, band/mode triggers, etc.) live as they
happen. HamAlert is a separate free service you configure yourself — set
up your alert "triggers" on hamalert.org first, then enter your HamAlert
username and **Telnet password** (Settings → Integrations → HamAlert).
The Telnet password is a dedicated credential HamAlert lets you set on
its own **Destinations** page — separate from your regular website/app
login, specifically for this kind of integration — not the password you
use to log into hamalert.org itself. A "Test connection" button confirms
the login before you enable the card. This dashboard connects outbound to
HamAlert's Telnet-based streaming interface (no inbound port needed on
your end) and shows each matching spot as it arrives: callsign (linked
to QRZ), source (DX cluster / Reverse Beacon Network / POTA / SOTA /
WWFF / PSK Reporter), frequency/mode, DXCC entity, and — when present —
the specific trigger comment that matched (e.g. "New DXCC needed").

**Fleet alerts** (Settings → Cards → "Fleet online/offline alerts in
Notifications", off by default) — no new connection or credentials at
all: this dashboard already tracks when a hotspot goes offline (for the
red "OFFLINE" badge on its card) and comes back; this setting just also
surfaces that exact moment as a notification.

**Solar alerts** (Settings → Cards → "Geomagnetic storm alerts in
Notifications", off by default) — also no new connection: the HF
Conditions card already fetches K-index hourly. This setting adds a
notification whenever K-index crosses the NOAA G1 "minor storm"
threshold (Kp≥5, the conventional level hams watch for aurora
propagation / HF disturbance), in either direction — once when it
crosses up, once when it drops back down — not a repeated alert every
hour it stays elevated.

**Brandmeister favorite alerts** (Settings → Cards → "Brandmeister
favorite alerts in Notifications", off by default) — a persistent
connection to Brandmeister's own public real-time "Last Heard" feed
(no account needed, unlike HamAlert) that notifies when a favorite
callsign (Settings → Favorites) keys up **anywhere on the Brandmeister
network**, not just through this fleet's own hotspots — the network-wide
counterpart to the fleet-scoped APRS favorite alerts above.

**QRZ logbook confirmations** (Settings → Cards → "QRZ logbook
confirmations in Notifications", off by default) — adds an event when a
contact in your logbook gets confirmed on QRZ. Needs a QRZ Logbook API
key (Settings → Integrations → "QRZ Logbook sync"); the same setting also
syncs your logged contacts into the Recent Contacts card and marks the
confirmed ones there with a ✓ badge. See "QRZ Logbook sync" above.

All enabled sources share one connection-status readout in the card
header (Connected / Connecting / Reconnecting per source, where the
source has a real connection to be in) and one combined, capped/
scrollable list — same pattern as the ASL Favorites card's row list —
rather than growing the card forever. Position it anywhere in the same
drag-and-drop **Card order** list (Settings → Hotspots), same as Fleet
Activity/APRS Messages/etc.

<!-- wiki-group: Optional Cards -->
## HF Conditions card
<!-- wiki-image: hf-conditions-card.png -->

Optional dashboard card (Settings → Cards → "Show HF Conditions
card", off by default) — solar and band propagation data from
[N0NBH's free public feed](https://www.hamqsl.com/solar.html), no API
key or signup needed. Same treatment as every other extra card: one
card, position it anywhere in the same drag-and-drop **Card order**
list.

- **Band conditions table** (80–40m, 30–20m, 17–15m, 12–10m, Day and
  Night) is the headline — N0NBH's own calculated Good/Fair/Poor
  ratings, not something computed here, shown as color-coded pills.
  This is the actually actionable part; the raw indices below it mean
  little to most people at a glance.
- **Key indices** — Solar Flux Index, A-index, K-index, and sunspot
  count in a small stats row.
- **Secondary stats** — X-ray flare class, aurora index, geomagnetic
  field state, and signal/noise floor, in a quiet footer row rather
  than competing with the band table for attention.
- **Polled roughly every 15 minutes**, cached server-side for an hour
  — the feed itself only updates on a similar cadence, so anything
  faster is just extra load on someone else's free service for no
  fresher data.

<!-- wiki-group: Optional Cards -->
## Band Plan card
<!-- wiki-image: band-plan-card.png -->

Optional dashboard card (Settings → Cards → "Show Band Plan card", off
by default) — a static FCC Part 97.301 frequency/mode reference for the
HF bands, General and Extra class privileges shown together on one card.
No network client at all — the only card in the app with no Python
module of its own, just data baked into the template.

- **General privileges** are the primary numbers shown per band; on the
  4 bands where Extra gets more (80m/40m/20m/15m), a small purple
  "↳ Extra: ..." note underneath marks where the wider Extra segment
  starts. The other 6 bands are identical for both classes.
- **Reference only** — a caveat line links to ARRL's own band chart.
  This is regulatory data baked into the app rather than pulled live,
  so it can go stale if the FCC amends Part 97.301 — check the linked
  chart before operating, don't rely on this card alone.
- Same drag-and-drop **Card order** placement as every other extra card.

<!-- wiki-group: Optional Cards -->
## License Quiz card
<!-- wiki-image: license-quiz-card.png -->

Optional dashboard card (Settings → Cards → "Show License Quiz card",
off by default) — one random practice question at a time from any of the
three US amateur license classes (Technician, General, Extra — picked
via the dropdown in the card's header), auto-rotating to a new one every
~10 minutes, or on demand via "New Question". Pool data is bundled
server-side (`license_quiz.py`) and never sent to the browser in bulk —
each poll fetches exactly one question.

- **All three current pools**, each sourced from a machine-readable
  export of NCVEC's official public-domain release and cross-checked
  question-for-question against NCVEC's own current (most-recent-errata)
  PDF before bundling — see `license_quiz.py`'s docstring for the exact
  sources, licenses, and per-pool verification notes:
  - **Technician** ([2026-2030](https://ncvec.org/index.php/2026-2030-technician-question-pool), effective July 1, 2026): 397 questions
    (12 diagram-based questions excluded since the referenced figures
    aren't bundled).
  - **General** ([2023-2027](https://ncvec.org/index.php/2023-2027-general-question-pool-release), effective through June 30, 2027): 418
    questions (5 diagram-based excluded).
  - **Extra** ([2024-2028](https://ncvec.org/index.php/2024-2028-extra-class-question-pool-release), effective through June 30, 2028): 571
    questions (28 diagram-based excluded).
- **Click an answer to reveal** correct (green) / wrong (red); the other
  choices dim. Two-column layout keeps the card's height in line with
  the others regardless of answer length.
- **Per-section accuracy tracker** — a 10-pip row (subelement 0 through
  9 for whichever class is selected) that colors green/yellow/red per
  subelement group once you've answered at least one question from it,
  with rollup "Answered"/"Accuracy" stats and a reset button. This is
  stored in the browser's **localStorage**, not on the server — this app
  has no user accounts, so "your" progress (and which class you're
  currently studying) can only mean "this browser's". Different devices
  viewing the same dashboard get independent stats and can each study a
  different class. Subelement codes are unique per class (T-/G-/E-
  prefixed), so switching classes never overwrites another class's saved
  stats.
- Same drag-and-drop **Card order** placement as every other extra card.

<!-- wiki-group: Optional Cards -->
## Satellites card
<!-- wiki-image: satellites-card.png -->

Optional dashboard card (Settings → Cards → "Show Satellites card", off
by default) — upcoming pass predictions for a small list of amateur radio
satellites, plus a live ground-track overlay on the **Live map**.

- **Pass predictions** — AOS time ("in 12 min"), max elevation,
  mode/downlink/uplink frequency, and which direction to look (e.g.
  "rises NW, sets SE") for each tracked satellite's next passes over
  your station, sorted soonest-first. A pass currently in progress is
  highlighted. Needs a **station grid square** set (Settings → Cards,
  same field the Band Activity card uses) — without one, the card shows
  a prompt instead of pass times.
- **Live map overlay** — each tracked satellite's current position (🛰️
  icon), visibility footprint (the area it's above the horizon for), and
  ground track (roughly one orbit, centered on now) drawn directly on
  the Live map. Works independently of the station grid square — this
  part doesn't need an observer location, only the pass-time list does.
  Has its own "Satellites" checkbox in the map legend (off by default),
  separate from the card itself — clicking a satellite's name in the
  pass list switches to the map, turns this overlay on if it was off,
  and pans to that satellite.
- **Tracked satellites** are editable in Settings → Cards → "Tracked
  satellites" (NORAD catalog ID + display name/mode/frequency per
  entry) — default list is ISS, SO-50, AO-91, and PO-101. Orbital data
  itself comes live from [CelesTrak](https://celestrak.org) (free, no
  key); mode/frequency for satellites you add yourself can be looked up
  at [SatNOGS DB](https://db.satnogs.org).
- Real orbital mechanics (SGP4 propagation), not an approximation —
  verified against a live ground-truth satellite position before
  shipping. Satellite operational status changes over time (satellites
  go silent, decay, or get replaced), so the default list is
  intentionally small and editable rather than exhaustive.
- **Polar elevation scope** — a small sky-dome next to the pass list.
  Center is straight overhead (zenith), the rim is your horizon, and
  azimuth runs around the edge like a compass — deliberately a
  different shape than the Flights Overhead card's radar (which plots
  bearing + ground distance): a satellite pass is about how high in
  the sky it is, not how far away on the ground. Any tracked satellite
  currently above your horizon shows as a live pulsing dot at its real
  elevation/azimuth; a dashed arc previews the next upcoming pass's
  rise-to-set track across the sky even before it's risen.
- **Naked-eye visibility prediction** — each pass is checked for
  whether it'd actually be visible to the eye, not just above your
  horizon: the satellite has to be sunlit (not in Earth's shadow) *and*
  your own sky has to be dark enough (Sun below -6°, nautical
  twilight). A pass meeting both gets a "👁 visible" tag next to the
  existing "● overhead" one. Verified against a real published sunset
  time before shipping.
- **Possible Starlink train sightings** (Settings → Cards → "Show
  possible Starlink train sightings", off by default) — when a
  Starlink launch batch is currently tracked by
  [CelesTrak](https://celestrak.org)'s supplemental-elements feed, its
  predicted visible-only passes show in a labeled sub-section of this
  card. Explicitly marked **best-effort** — unlike every other
  integration in this app, finding "the current launch batch" means
  scraping a CelesTrak page (there's no documented API for that
  specific lookup), and the tracked satellite count can be incomplete
  in the hours right after a launch, before the full ~20-28 satellites
  get their own individually catalogued orbits. If nothing's currently
  tracked, or nothing's predicted visible, the section either stays
  hidden or says so plainly rather than showing an error.

<!-- wiki-group: Optional Cards -->
## Flights Overhead card
<!-- wiki-image: flights-card.png -->

Optional dashboard card (Settings → Cards → "Show Flights Overhead
card", off by default) — live nearby aircraft on a small radar scope +
list, from [OpenSky Network](https://opensky-network.org)'s free,
anonymous, no-signup feed. Needs a **station grid square** set (Settings
→ Cards, same field Band Activity/Satellites use) — without one, the
card shows a prompt instead of aircraft.

- **Radar scope + list** — aircraft plot on the scope by real bearing
  and distance from your station, heading-oriented plane glyphs when a
  heading is known. The list beside it ranks nearest-first: callsign,
  distance/bearing/speed, altitude (feet below 18,000ft, flight level
  above), and a climb/level/descend indicator. Fixed ~25 mile radius,
  not currently configurable.
- **Click a callsign** to open a detail drawer with the full live
  picture — position, vertical rate, squawk, aircraft type/registration/
  manufacturer/owner, and (when known) the callsign's usual route with
  the airline name, plus a link out to FlightAware for that flight's
  full track and history. OpenSky's own live feed doesn't carry
  aircraft type or route — those two fields come from a second,
  on-demand lookup against [adsbdb.com](https://www.adsbdb.com) (a
  free, keyless API), fetched only when you actually open a flight's
  drawer, not for the whole list on every poll. The route shown is
  "this callsign's usual route," not a live flight-plan lookup for
  this exact flight — labeled as such rather than overclaiming
  precision. GA/private aircraft with no commercial route on file say
  so plainly instead of showing nothing.
- Anonymous OpenSky access has a modest daily rate limit, so this card
  refreshes every 5 minutes server-side, shared across every browser
  tab viewing the dashboard.

<!-- wiki-group: Optional Cards -->
## Recent Contacts card
<!-- wiki-image: recent-contacts-card.png -->

Optional dashboard card (Settings → Cards → "Show Recent Contacts card",
off by default) — a newest-first list of your logged QSOs, drawing on
the same `qsos.json` the Live map's QSO layer already plots (ADIF bulk
import and/or live WSJT-X logging — see the Live map section below).
No separate data source or configuration; this is just a different view
onto QSOs already being tracked.

Each row shows: a country flag (when known), callsign, the other
operator's name (when known), city/state, band and mode, frequency,
signal report (RST sent/received), distance worked, grid square, and how
long ago it was logged. Name/RST/distance and flag/city/state/country
all degrade gracefully when the source data isn't there — an older
logging app or a plain FT8 exchange with no free-text name won't have
all of these, and the row just omits whatever's missing rather than
showing a placeholder. The flag comes from QRZ's own `country` field (if
a QRZ subscription is configured) or the ADIF log's own `COUNTRY` field
— **not** a guessed callsign-prefix DXCC lookup, so a contact with
neither source just shows no flag rather than a potentially wrong one.
Distance uses the same miles/km preference as the Weather card
(Settings → Weather → "Temperature in °F").

Click a row (not the callsign, which still opens QRZ/RadioID directly)
to slide out the full detail: a small live map centered on the contact
(with a line back to your station's QTH when known), band/mode/
frequency/RST/grid/distance/location, and a "View on full map" button
for exploring further on the actual Live map.

If **QRZ Logbook sync** is configured (see Integrations), contacts also
in your QRZ logbook are folded into this list, and a green **✓ QRZ**
badge marks the ones QRZ has confirmed — the detail drawer shows the
confirmation date.

<!-- wiki-group: Optional Cards -->
## QSO Stats card
<!-- wiki-image: qso-stats-card.png -->

Optional dashboard card (Settings → Cards → "Show QSO Stats card", off
by default) — a quick summary over the same logged QSOs Recent Contacts
shows: total contacts, unique countries worked, unique grid squares
worked (at 4-character Maidenhead precision — e.g. "FN42" — the
standard grid-square-award granularity, not the finer 6-character
precision some logs store), unique bands worked, and a small breakdown
of your most-used bands. Pure client-side aggregation, no new data
source or backend query.

<!-- wiki-group: Optional Cards -->
## Top 5 Activity card
<!-- wiki-image: top-activity-card.png -->

Optional dashboard card (Settings → Cards → "Show Top 5 Activity card",
off by default) — ranks **your own fleet's** most active callsigns (who
transmitted) over the last 24 hours, as a simple bar-ranked list, each
linking to QRZ (or RadioID.net if QRZ isn't configured) same as the
Recent Contacts card. WPSD entries also show the talkgroup that callsign
was most recently heard on (e.g. "TG 3172"); AllStarLink has no separate
concept here, since the linked node is already baked into the callsign
itself. Needs "Show fleet activity card" enabled too — it reads the same
activity log that card's chart is built from, just a different aggregate
query (ranked totals instead of a time series).

This is deliberately scoped to your own fleet, not a network-wide "what's
busy right now" feed — Brandmeister/TGIF/AllStarLink/YSF were each
investigated for a clean, free, REST-pollable "busiest talkgroups
network-wide" endpoint and none had one (Brandmeister and TGIF's
real-time activity both live behind a persistent Socket.IO connection
rather than a periodic poll — see the Notifications card's Brandmeister
favorite alerts above for the one place this dashboard *does* use a
Brandmeister live connection, for a different, narrower purpose).

<!-- wiki-group: Optional Cards -->
## POTA card
<!-- wiki-image: pota-card.png -->

Optional dashboard card (Settings → Cards → "Show POTA card", off by
default) for Parks on the Air hunters. It's a **fixed-size card** — a
one-line stat glance, then a ranked activator-spot list at a fixed
height that scrolls — so it sits level with your other cards no matter
what.

The spot list is the same live `api.pota.app/spot/activator` feed the
Live map's "POTA spots" overlay uses, but **ranked** rather than
newest-first: "Smart rank" (the default) surfaces parks that are new to
you, freshly spotted, close to you, and early in the activation (a
low QSO count means you'll get through and the activator still needs
contacts). Other views: **New parks only**, **Nearest**, **Freshest**,
or by mode (CW / SSB / FT8·FT4). Each row shows the activator (linked to
QRZ), park reference and name, frequency/band/mode, **distance and
bearing from your grid**, location, spot comment, age, and QSO count.

A **🚩 NEW** tag marks parks you haven't worked — checked against the
POTA references in your logged QSOs (ADIF imports now keep `POTA_REF` /
`SIG_INFO`) plus your recent POTA hunts.

The **⚙ Stats** button (or the glance line) opens a side drawer with
your POTA **hunter stats** — parks, QSOs, awards, endorsements — and
your recent hunts. That needs your callsign under Settings →
Integrations → Parks on the Air. It's a plain callsign, **not a login**:
every `api.pota.app` endpoint used here is public, no account or key.
Without a callsign the card still works — it just shows the spot list
without stats.

**Tap-to-tune:** with [Rig control](#rig-control-rigctld) configured,
each spot's frequency becomes a button that tunes your radio to it (and
sets the mode, optionally). A pill in the card header shows whether the
rig server is reachable; when it isn't, the frequency is plain text.

<!-- wiki-group: Optional Cards -->
## HF Favorites card
<!-- wiki-image: hf-favorites-card.png -->

Optional dashboard card (Settings → Cards → "Show HF Favorites card",
off by default) — a **tap-to-tune memory bank** of common HF activity
frequencies, 80 m through 6 m, grouped by band. It's the same
fixed-size scrolling shape as the POTA card, and it uses the same
[Rig control](#rig-control-rigctld) path: click a frequency and your
radio jumps to it, sending the stored **mode** (USB / LSB / CW / data /
AM / FM) along with it.

It ships with a **curated ~20-entry default set** — one FT8 and one
phone frequency per band, plus FM simplex on 10 m / 6 m. On first run
that set is copied into `hf_favorites.json` as ordinary rows: **add your
own**, **edit any row in place** (frequency, label, mode), or delete
what you don't use. There's no "default vs custom" distinction after the
first run — a **Restore default set** button in the edit drawer just
re-adds any of the curated entries you've since deleted. SSTV, FT4, JS8,
PSK31, beacons, MSK144 and AM windows aren't in the defaults on purpose;
add the ones you want.

The card header shows the **live rig dial frequency** (from the same
reachability probe the pill uses), and whichever favorite you're
currently tuned to is highlighted **ON AIR**. A mode filter (all / FT8 &
data / phone / CW) sits in the card and remembers its last setting per
browser.

Needs "Rig control" configured under Settings → Integrations. Without a
reachable rig server the card still lists everything — the frequencies
are just plain text instead of buttons.

<!-- wiki-group: Optional Cards -->
## Rig Panel card
<!-- wiki-image: rig-panel-card.png -->

Optional dashboard card (Settings → Cards → "Show Rig Panel card", off
by default) — a **read-only software front panel** for your radio, over
the same [Rig control](#rig-control-rigctld) connection.

- **Frequency, mode, filter, VFO, split** (with the TX frequency when
  split's on)
- **S-meter** on receive
- On **transmit**: SWR, ALC, power (watts if the rig reports them,
  otherwise the power setting), and speech compression, as bars
- **PA (finals) temperature** with a short sparkline. Radios like the
  IC-7300 run hot at 100% duty (FT8, RTTY, JS8) — this answers "am I
  cooking the finals?" at a glance. `rigctld` reports the PA meter as a
  raw 0–100% of full scale; enter a 2-point °C calibration under
  Settings → Integrations if you know your rig's curve, otherwise it
  shows a percentage
- **Status pills** for ATU, noise blanker, noise reduction, auto-notch,
  compression, antenna — only the ones your rig actually reports
- **TX/RX** indicator with a keyed timer

Every field degrades on its own: a reading the rig or Hamlib build
doesn't expose just shows a dash. **How much you get, and how fast,
depends on the rigctld.** WFView's built-in RigCtld works but is a
lean re-implementation: its TX meters (SWR, forward power) are cached on
WFView's own slow cadence, so they lag a few seconds behind a key-up and
sit at a resting value until then; PA temperature it doesn't expose at
all (comes back as a flat 0, shown as "not reported"). A stock Hamlib
`rigctld` talking to the radio's CI-V port maps the full set and updates
promptly. The **⚙ drawer's "Raw readings"** section shows exactly what
your rigctld is answering for each field, so it's easy to see what's
real vs. defaulted vs. still settling.

Polling is split so it stays light on the CAT link (shared with your
logger and WSJT-X): the fast readings — frequency, mode, S-meter, PTT,
and the TX meters — refresh about once a second, while the slower ones —
PA temperature, ATU / NB / NR / notch / antenna — refresh every few
seconds. Raise `RIG_PANEL_POLL_SEC` if the radio ever feels busier with
the card on.

The **⚙ drawer** holds the **Operating Timeline**: every band and mode
the rig has sat on since the dashboard started ("20 m FT8 14:02–14:37 ·
40 m SSB 14:37–now"), built by watching the frequency change. It's
in-memory only — it starts fresh after a restart.

**PA-temperature alerts:** turn on "Rig PA-temperature alerts in
Notifications" (Settings → Cards) and the [Notifications card](#notifications-card)
gains a 7th source — one alert when the finals meter sits above your
threshold for a sustained spell, and one when it drops back. The
threshold (% of full scale) is under Settings → Integrations → Rig
control. This works whether or not the Rig Panel card itself is shown.

<!-- wiki-group: Optional Cards -->
## Big Ass Clock card
<!-- wiki-image: big-clock-card.png -->

Optional dashboard card (Settings → Cards → "Show Big Ass Clock card",
off by default) — a large clock, purely client-side (no backend module,
no network call — same as the static Band Plan card). Three selectable
styles via a dropdown in the card header:

- **Digital** — large tabular time + date. A 12/24-hour toggle in the
  card's own controls (UTC/Zulu is always forced 24-hour — the real
  ham radio/aviation convention).
- **Analog** — a proper clock face: 60 minute ticks with bolder hour
  marks, numerals, tapered hands with a counterweight tail, and a
  subtle gradient face/bezel. Two extra toggles appear alongside it:
  - **Square face** — swaps the round dial for a squared-off
    "instrument panel" case: ticks and the 12/3/6/9 numerals are traced
    directly on the case's own edges/corners (not a circle sitting
    inside a rounded square), with corner rivets and a recessed inner
    bezel line. When it's the only clock shown, the case stretches into
    a true rectangle to fill the card's actual width instead of staying
    a small fixed square — a second clock still shows two plain squares
    side by side.
  - **Show date** — a small recessed date window at 3 o'clock, watch-style,
    showing today's month + day. Works on both the round and square
    faces; the 3 o'clock numeral steps aside while it's on, the same way
    a real watch's index does next to its own date window.
- **TIX** — a dot-matrix "TIX clock" style display: each digit shown as
  a grid of lit dots, count = digit value. Tens digits get a smaller
  grid sized to what they actually need (hour tens only ever needs 0-1,
  minute/second tens only ever needs 0-5) rather than the full 9-dot
  grid units digits use. Always 12-hour, no second clock — kept
  deliberately simple/glanceable.
- **Second clock** — Digital and Analog can both show a second
  clock alongside the first, in any of several common timezones or
  UTC/Zulu. Digital's pair stacks vertically; Analog's sits side by
  side (two faces — round or square, matching whichever face is
  selected — read naturally next to each other).
- Style, format, face shape, date, and second-clock/timezone choices are
  all saved in the browser's **localStorage**, not settings.json —
  nothing here needs to sync across every device viewing the same
  dashboard.
- Same drag-and-drop **Card order** placement as every other extra card.

<!-- wiki-group: Optional Cards -->
## Band Activity card
<!-- wiki-image: band-activity-card.png -->

Optional dashboard card (Settings → Cards → "Show Band Activity card",
off by default, needs a **station grid square** set below the toggle) —
live WSPR beacon-spot activity within 500km of your station, for six
band groups (160m, 80-40m, 30-20m, 17-15m, 12-10m, 6m — the same
groupings HF Conditions uses). This is a fundamentally different kind of
data than HF Conditions: real observed spot counts from
[WSPR Live](https://wspr.live/), not a solar-index prediction.

- **24h sparkline per band group**, not a single snapshot number — a
  spot-count trend is far more informative than "N spots right now,"
  which swings with whatever happened to transmit in the last few
  minutes. Hover any sparkline for its peak value/hour.
- **Localized to your station** — set a Maidenhead grid square (e.g.
  `FN42`) in Settings → Cards; the dashboard converts it to lat/lon
  server-side and filters WSPR Live's data to receivers within 500km.
  Without a grid square set, the card shows an "unavailable" message
  instead of guessing a location.
- **Expect sparser data than HF Conditions** — localizing trades a
  smooth global curve for a spikier, more personally-relevant one. On a
  quiet band (160m and 6m especially) some hours may show a genuine
  zero rather than a small nonzero number — that's real data, not a
  rendering bug.
- **Higher spot counts mean more activity, not necessarily better
  conditions** — 20m/30m are consistently the busiest bands simply
  because they have the most WSPR stations running, independent of
  actual propagation quality.
- Cached ~30 minutes server-side (`wspr_activity.py`) — an hourly-
  bucketed chart doesn't need fresher data than that, and WSPR Live's
  free API is rate-limited (20 requests/minute, non-commercial use).
- Same drag-and-drop **Card order** placement as every other extra card.

<!-- wiki-group: Dashboard and Live Map -->
## Transmission timer

While a node is active, the card shows a live "⏱ m:ss" counter next to
the caller's name, counting up from the moment that specific transmission
started. It resets to 0:00 the instant a *new* callsign keys up (it does
not accumulate across multiple callers or multiple transmissions from the
same person).

<!-- wiki-group: Dashboard and Live Map -->
## Live map
<!-- wiki-image: live-map.png -->

The **Live map** tab shows an interactive map (Leaflet.js + OpenStreetMap,
with an optional satellite tile view, no API key required for either) with:

- A **square marker for each of your own hotspots** that has a
  latitude/longitude set (Settings → Hotspots). This is opt-in per hotspot
  since there's no reliable way to read a WPSD/Pi-Star node's physical
  location automatically over SSH. Each hotspot gets its own assigned
  color (cycled from a fixed palette, in Settings → Hotspots order) — a
  **"Map key"** in the bottom-left corner of the map ties every color back
  to its hotspot's name, plus one more entry for FT8/FT4 QSOs (see below).
- A **pin for every caller** currently active or in any node's last-5
  history, using QRZ/RadioID/APRS coordinates (in that priority order —
  see the integration sections above), colored to match the hotspot that
  heard them. An active call **pings** (an expanding ring, in that
  hotspot's own color); a recent one sits static and dimmed — color now
  tells you *which* hotspot, motion tells you *active vs. recent*. Caller
  pins cluster at low zoom levels to avoid overlapping when there's a lot
  of activity.
- A **dashed distance line** from the receiving hotspot to the caller, for
  active calls only, labeled with the distance in miles or km (matches the
  Weather tab's °F/°C unit setting) — in that hotspot's own color too.
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

- **Resizable** — drag the small handle just below the map to make it
  taller or shorter. Saved per-browser via `localStorage`, not a shared
  Settings value, since the right height on a phone and a desktop
  monitor are very different and there's no per-user login here to key
  a shared preference off of.
- **Grey line overlay** — a "Grey line" checkbox in the map legend
  toggles a day/night terminator overlay, useful for spotting greyline
  propagation windows. It's computed from real solar-position math
  (subsolar point + the sun's zenith angle at each point on the map),
  not a static image, and recomputes once a minute. Also a per-browser
  preference (`localStorage`), same reasoning as the map height.
- **Aurora oval overlay** — an "Aurora oval" checkbox plots NOAA SWPC's
  live OVATION auroral activity model as a soft, intensity-graded glow
  near the poles. Also off by default, also stops polling the moment
  you turn it off. A genuinely different signal than the single
  "Aurora: N" number on the HF Conditions card — this is the actual
  geographic model, not a single index value.
- **ADIF QSO import** — an "Import ADIF log" control in Quick Settings'
  "Live map" section (⚙ Settings → once the Live map tab has been opened
  once this session) lets you upload a `.adi`/`.adif` log file exported
  from your logging software; each worked station gets plotted as a pin
  in one fixed color
  (shown in the Map key as "FT8/FT4 (WSJT-X)"), distinct from every
  hotspot's own color — band/mode are still shown in the pin's tooltip,
  just not encoded as a separate color scheme. Unlike the layer
  checkboxes above, this is persistent — it's saved
  server-side and stays on the map across visits until you clear it,
  the same as everything else in this app. A "Show on map" checkbox
  lets you hide the pins temporarily without deleting the imported log.
  Position comes from the QSO's own `GRIDSQUARE` field when the log
  includes one, falling back to the same QRZ/RadioID lookup every
  other card already uses when it doesn't; a QSO with neither is
  skipped rather than plotted incorrectly. Each pin also gets a thin
  line back to the QTH it was worked from — that QSO's own
  `MY_GRIDSQUARE` field if the log recorded one (handles a portable/
  rover log where your operating location changes between QSOs),
  falling back to your station grid square set in Settings → General
  otherwise. No line is drawn if neither is known. Importing a new log
  replaces the previous one — there's no merge/dedupe across imports.
- **Live WSJT-X QSO logging** (Settings → Integrations, off by default) —
  listens for WSJT-X's own UDP telemetry (the same feed GridTracker/
  JTAlert use, no SSH, no polling) and plots each QSO on the map the
  moment you log it in WSJT-X, using the same pin/line rendering as an
  ADIF import above. In WSJT-X, set File → Settings → Reporting → "UDP
  Server" to this dashboard's IP and the port configured here (2237 by
  default, WSJT-X's own default too). A "Check status" button shows
  whether packets are actually being received. Unlike a bulk ADIF import
  (which replaces the whole map), live-logged QSOs simply add to
  whatever's already there.
  - **Docker/Unraid**: `docker-compose.yml`/`docker-update.sh`/the Unraid
    template all run the container on `--network host` (see the openSPOT4
    section above for why), which means any port the app binds — 2237
    included — is reachable automatically, same as a standalone Pi/systemd
    install. If you've instead kept the container on Docker's default
    bridge network, this needs a UDP port actually published, or WSJT-X's
    packets never reach the container at all: publish `2237/udp` (or
    whichever port you set here) explicitly, and keep it in sync if you
    ever change the port in Settings.
- **POTA spots** — a "POTA spots" checkbox in the map legend plots
  current Parks on the Air activator spots worldwide. Pins are styled by
  what you still need: a park you haven't hunted shows as a bright pin, a
  park you've worked is dimmed, spots early in an activation (under 10
  QSOs) are drawn a little larger, and just-spotted ones pulse. Click a
  pin to draw a line back to your station (the tooltip shows distance and
  bearing). Free, no-auth public feed (`api.pota.app`) — turn it on with
  no configuration; add a callsign under Settings → Integrations → Parks
  on the Air and it also knows which parks are new to you. Off by
  default, only polls while the checkbox is checked. See also the **POTA
  card** for the ranked spot list + hunter stats.
- **SOTA spots** — same idea for Summits on the Air: a "SOTA spots"
  checkbox plots current activator spots worldwide (`api2.sota.org.uk`,
  free, no key). Each spot's summit position is resolved server-side
  (SOTA's own spot feed doesn't include coordinates, only an
  association+summit code) and cached indefinitely per summit, since a
  mountain doesn't move. Off by default, only polls while checked.
- **PSK Reporter** — a "PSK Reporter" checkbox plots where **your own**
  signal was actually heard, using your callsign (Settings →
  Integrations). Deliberately scoped to one callsign, not a worldwide
  feed — there's no clutter risk the way the old WSPR spots overlay had.
  Each report gets a line back to your station grid square. Off by
  default; PSK Reporter's own server actively rate-limits frequent
  polling, so this is cached 10 minutes server-side regardless of how
  long the checkbox has been on.
<!-- wiki-group: Dashboard and Live Map -->
## Fleet activity
<!-- wiki-image: fleet-activity-card.png -->

Optional — off by default (Settings → Cards → "Show fleet activity
card"), since it adds a small SQLite log (`activity.db` in the appdata
folder alongside `hotspots.json`) and a write on every completed
transmission. Once enabled, a "Fleet activity" card appears on the
dashboard showing:

- **Online** — hotspots currently online vs. total configured.
- **Uptime** — how long the dashboard process itself has been running.
- **Last activity** — how long ago and which hotspot last had a call,
  live-updating the same way the per-card "last heard" timers do.
- **Mode breakdown** — a stacked bar + legend showing the share of
  transmissions by mode (DMR/D-Star/YSF/P25/NXDN, plus DVSwitch if
  enabled — see below) over the selected time span.
- **Activity chart** — a line chart of transmission counts over the selected
  time span, toggleable between **Per-hotspot** (one line per hotspot that
  had any activity in the window) and **Aggregate** (summed across the fleet).

By default the card appears first, before any hotspot cards. Its position
is part of the same drag-and-drop **Card order** list as the hotspot cards
(Settings → Hotspots) — drag it anywhere in that list to move it.

**Time span** — Settings → Cards → "Fleet activity time span" — 3, 6, 12
(default), 24, or 48 hours. The chart's bucket width scales with the
selected span (5 to 60 minutes) so it always renders roughly the same
number of points rather than getting denser or sparser as the span changes.

One row is logged per completed transmission (not per poll), and rows
older than 49 hours (one hour past the longest selectable span) are pruned
automatically on each write — the table doesn't grow unbounded, and
switching to a longer span never comes up empty because older rows were
already pruned for the previous, shorter default. Turning the toggle off
stops new writes but doesn't delete the existing `activity.db` file.

**DVSwitch (Analog_Bridge)** — an ASL3 (AllStarLink) hotspot that also
runs a DVSwitch bridge can opt in per-node (Settings → Hotspots → edit
that node → "This node also runs DVSwitch (Analog_Bridge)") to add
DVSwitch as a 6th mode in the breakdown above, sourced by tailing
Analog_Bridge's own log (`/var/log/dvswitch/Analog_Bridge.log`) over the
same SSH connection already used for that node's AllStarLink status —
no separate connection or credentials needed. One caveat worth knowing:
every other mode counts a *completed* transmission, but DVSwitch counts
a *newly started* one instead — Analog_Bridge's log doesn't have a
confirmed end-of-transmission line to detect completion from, so this is
a deliberate, disclosed difference in what's being counted for this mode
specifically, not a bug.

<!-- wiki-group: Dashboard and Live Map -->
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

<!-- wiki-group: Admin and Maintenance -->
## Backup & Restore

Settings → **Backup** tab lets you export/import a single combined JSON file
covering whichever of these you check: Hotspots, Favorites, ASL Favorites,
Cameras, and Settings.

- **Export** downloads `dashboard-backup-YYYY-MM-DD.json`. Hotspots,
  Cameras, and Settings each carry plain-text credentials (SSH passwords,
  camera RTSP URLs/access codes, QRZ/RadioID/APRS.fi/MQTT keys) — the
  Backup tab flags these with a "secrets" badge. Treat the exported file
  like any credentials file.
- **Import** accepts that same file back (on this dashboard or a different
  one), with a preview of what it contains before anything is applied, and
  a choice of mode:
  - **Add & update** (default) — for Hotspots/Favorites/ASL
    Favorites/Cameras, keeps what you already have and adds/updates
    entries from the file, matched by that list's natural key (hotspot
    `ip`, favorite `call`, ASL favorite `node`, camera `id`). For
    Settings, only the keys present in the file are overwritten —
    everything else stays as you had it.
  - **Replace** — for each checked category, only what's in the file
    remains; anything else currently configured in that category is
    removed.

This is the same tool for moving your setup to a new install as it is for
just keeping a backup around.

<!-- wiki-group: Dashboard and Live Map -->
## Offline detection

A hotspot card turns red with an "⚠ OFFLINE" badge once its SSH poll fails
`FAILURE_THRESHOLD` times in a row (default 2, ~10 seconds at the default
5-second poll interval — see the env vars table above). The card shows how
long it's been down with a live-updating timer, same idea as the active-call
timer elsewhere on the card. Everything else on the card (mode, RSSI, temp,
etc.) stays visible but dimmed — it's the last known-good data, not live.

This reuses the existing SSH-based poll loop rather than adding a separate
ping — a failed SSH connection is already a stronger "is this actually
working" signal than ICMP (it proves the SSH service itself responds, not
just that the network stack does), and it comes for free every poll cycle
with no extra network traffic or permissions.

<!-- wiki-group: Dashboard and Live Map -->
## Card uptime

Each card shows a small "⏱ 3d 2h" badge in the top-right corner, next to the
hotspot name — how long the underlying Linux host has been up (`uptime -p`
over SSH, already fetched every poll cycle for the temp/CPU stats, just not
previously displayed). It's compacted to the two most significant units
(days+hours, or hours+minutes) rather than shown as the full sentence
`uptime -p` prints; hover it for the exact value. Shares the same corner as
the "⬆ update" badge and "📍 map" link when those are present — all three
are grouped into one cluster that stays pinned to the right regardless of
how many are shown at once, rather than spreading across the card.

<!-- wiki-group: Integrations -->
## DigiPi card

Shows recent APRS/Direwolf activity and a live screen mirror from a
[DigiPi](https://digipi.org/) (KM6LYW's Raspberry Pi ham radio data
hotspot) — off by default. Enable it in Settings → Cards, and set the
DigiPi's IP/SSH credentials in Settings → Integrations.

- **Recent packets** — polls `/run/direwolf.log` over SSH the same way
  WPSD's own MMDVM log gets tailed, showing each packet's direction (heard
  directly over RF, gated from RF out to APRS-IS, or received from the
  APRS-IS feed), callsign, a simple position/message/status type label,
  and the raw payload text. Deliberately doesn't decode APRS position
  payloads into coordinates (especially the compressed format some
  beacons use) — that's real position/map data, and getting the decoding
  wrong would be a worse failure than not showing a position at all.
- **Screen mirror** — a small thumbnail (same size/style as a QRZ caller
  photo elsewhere on this dashboard, click to enlarge) showing a live
  mirror of the DigiPi's own physical screen, pulled directly from its
  `direwatch.png` — the same image the DigiPi's own `direwatch.php` web
  page shows, just embedded here on a ~2s refresh instead of its own 1s.
  No credentials needed for this part; it's a plain unauthenticated image
  on the DigiPi's own web server, fetched directly by your browser (same
  "link straight to the device's own web UI" pattern this dashboard
  already uses for a WPSD hotspot's admin page).
- This is a **dedicated card**, not a hotspot — a digipeater doesn't have
  one "active call" the way a repeater does, so it doesn't get its own
  entry in the main hotspot grid. Currently supports one DigiPi.
- Only APRS/Direwolf is covered right now. FLDigi and Winlink RMS (both
  also bundled with DigiPi) aren't included yet — this needs a real
  device actually running them to build against, the same discipline
  used for everything else in this app.

<!-- wiki-group: Mobile -->
## Mobile dashboard (Pocket Dash)
<!-- wiki-image: mobile-status.png -->

A separate, lightweight phone view lives at **`/mobile`** ("Pocket Dash").
It polls the same `/api/data` and `/api/qsos` the desktop dashboard uses and
keeps none of its own state — the main dashboard at `/` is completely
untouched. It's a single-column layout with a bottom tab bar:

- **Status** — one card per hotspot, same online / idle / last-heard /
  offline / away states as the desktop, reflowed for one column. WPSD cards
  show talkgroup / mode / RSSI / BER; ASL3 cards show the linked-node list
  (the keyed node pulsing); openSPOT 4 cards show the active profile and, on
  battery, the charge level. Callsigns are tappable and open a QRZ lookup.
- **Map** — a Leaflet map (loaded only when you first open the tab) with a
  pin per hotspot that has map coordinates set, colored by state (cyan and
  pulsing = a call in progress, grey = idle, red = offline). Tap a pin for a
  popup, then "Open details" to jump to that hotspot on the Status tab.
- **Activity** — recent logged contacts (the same `qsos.json` the Live map
  and Recent Contacts card read), newest first.
- **More** — links back to the full dashboard, Settings, changelog and
  README, plus the notification controls below.

<!-- wiki-image: mobile-map.png -->

### Installing it as an app

Open `/mobile` in the phone's browser and use **Add to Home Screen**. It
installs as a standalone app called *Pocket Dash*, launches without browser
chrome, and a service worker caches the shell so it still opens on a weak
signal. On iPhone this step is required for notifications — iOS only
delivers web push to an installed PWA, not a Safari tab.

Installing and push both need the dashboard reachable over **HTTPS** with a
valid certificate (a plain `http://` LAN address works for *viewing* but not
for installing or notifications). The simplest way to get that without
opening any ports is [Tailscale](https://tailscale.com/) with
`tailscale serve` in front of the dashboard.

### Push notifications
<!-- wiki-image: mobile-notifications.png -->

Under **More → Notifications**, tap **Enable notifications** and allow the
browser prompt. From then on the phone gets a push when:

- a hotspot **stops responding** (and again when it **recovers**), or
- a **call starts** on any hotspot.

A per-hotspot cooldown keeps a flapping node or a busy repeater from
spamming. A tapped notification opens a glanceable full-screen view of just
the hotspot it's about — the triggering callsign large, the talkgroup, and a
live timer — rather than the whole list.

<!-- wiki-image: mobile-focus.png -->

The VAPID keypair the server needs is generated automatically on first run
and stored in `settings.json`. There's one contact field
(`push_vapid_contact`, a `mailto:` the push services can reach you at about
delivery problems) which ships as a placeholder — set your own in
Settings → Integrations if you like, but it isn't required for delivery.
Subscriptions live in `push_subscriptions.json`; dead ones are pruned
automatically.

### Per-hotspot alerts and muting
<!-- wiki-image: mobile-alert-prefs.png -->

**More → Notifications → Customize per hotspot** opens a screen with one row
per hotspot: an **Offline & recovery** toggle, a **Call starts** toggle, and
a **Mute** control (1 hour / 4 hours / 24 hours). New hotspots default to
offline alerts **on** and call-start alerts **off**. A muted hotspot shows
an amber border and a countdown until it un-mutes.

You can also **long-press a hotspot card** on the Status tab for a quick
mute sheet without opening the settings screen.

Preferences are stored in `notification_prefs.json` and are shared across
every subscribed device — muting a hotspot on your phone mutes it
everywhere.

<!-- wiki-group: Admin and Maintenance -->
## Known tradeoffs (intentionally left as-is for now)

- No authentication on `/setup` — anyone who can reach the dashboard can
  add/edit/delete hotspots. This also covers the "Host power control"
  buttons (reboot/power off the dashboard's own device), the "ASL
  Favorites & Control" card's connect/disconnect buttons, camera cards'
  live video feeds, and the Notifications card's APRS inbox (real
  messages addressed to your callsign, from the public APRS network)
  when those features are active — a materially bigger consequence than
  editing a config, worth weighing before exposing this dashboard beyond a
  trusted LAN.
- Hotspot passwords are stored in plaintext in `hotspots.json` and are
  re-sent to the browser to pre-fill the Edit form (so editing a hotspot
  doesn't force you to retype the password). Both are fine for a private
  LAN; worth revisiting before exposing this anywhere public.
- The QRZ password and APRS.fi API key are likewise stored in plaintext in
  `settings.json` and pre-filled into their Settings fields the same way.
  The MQTT broker password (if set) is stored the same way too. Camera
  RTSP URLs (which often embed a username/password) and Bambu printer
  access codes are stored the same way in `cameras.json`.
- SSH host keys are auto-accepted (`AutoAddPolicy`) — convenient for a small
  fleet of devices you control, but it skips host key verification.
