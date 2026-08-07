# Dashboard and Live Map

## Dashboard look

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
  to appear on the dashboard (Cards tab). If the Fleet activity, ASL
  Favorites & Control, HF Conditions, Band Plan, License Quiz, Band
  Activity, Big Ass Clock, Notifications (APRS Messages + HamAlert), or
  Satellites card is enabled, it appears in the same drag list and can be moved to
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

## Transmission timer

While a node is active, the card shows a live "⏱ m:ss" counter next to
the caller's name, counting up from the moment that specific transmission
started. It resets to 0:00 the instant a *new* callsign keys up (it does
not accumulate across multiple callers or multiple transmissions from the
same person).

## Live map

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
  - **Docker/Unraid**: this needs a UDP port actually published from the
    container, not just the app-level toggle — `docker-compose.yml` and
    the Unraid template both publish `2237/udp` already. If you change
    the port in Settings away from 2237, update the container's port
    mapping to match (Unraid: Docker tab → Edit → add/adjust the "WSJT-X
    UDP Port" field), or WSJT-X's packets will never reach the container
    at all. Standalone Pi/systemd installs don't have this extra step —
    whatever port you pick in Settings just works immediately.
- **POTA spots** — a "POTA spots" checkbox in the map legend plots
  current Parks on the Air activator spots worldwide, color-coded
  separately from every hotspot/QSO color (shown in the Map key). Free,
  no-auth public feed (`api.pota.app`), no configuration needed — just
  turn it on. Off by default, only polls while the checkbox is checked.
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

## Fleet activity

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
