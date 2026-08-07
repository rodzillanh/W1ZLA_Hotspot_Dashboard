# Integrations

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
e.g. `"W1ZLA active on Garage Hotspot"`, or
`"N1IWP active on Garage Hotspot - TGIF 603"` when a talkgroup/reflector is
linked — and there's no delivery confirmation; sending is fire-and-forget,
matching how simple APRS bots generally behave. It uses the caller's
actual callsign rather than their optional friendly name/label, to read as
a proper APRS message.

## Notifications card

Optional dashboard card (Settings → Cards) that merges up to five
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
(50 entries for APRS/HamAlert/geomagnetic alerts/Brandmeister favorites,
100 for fleet online/offline events) in a small local database, not just
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

All enabled sources share one connection-status readout in the card
header (Connected / Connecting / Reconnecting per source, where the
source has a real connection to be in) and one combined, capped/
scrollable list — same pattern as the ASL Favorites card's row list —
rather than growing the card forever. Position it anywhere in the same
drag-and-drop **Card order** list (Settings → Hotspots), same as Fleet
Activity/APRS Messages/etc.

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
