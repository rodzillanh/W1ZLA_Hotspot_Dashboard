# CLAUDE.md

Context for Claude Code working in this repo. This is a self-hosted Flask
dashboard for monitoring a fleet of WPSD/Pi-Star amateur radio hotspots,
AllStarLink (ASL3) nodes (both polled over SSH), and openSPOT 4 (SharkRF)
nodes (monitored over HTTP/WebSocket, no SSH access at all) — live status,
active-call info, and a map, with optional QRZ/RadioID/APRS/Brandmeister/
AllStarLink-stats/Home Assistant/APRS-messaging integrations. Runs as
Docker (Unraid) or standalone via systemd on a Pi/Linux box.

For end-user-facing docs (install, features, config), see `README.md` —
this file is oriented at making changes to the code, not using the app.

## Architecture

```
app.py            Flask routes, wires everything together, owns the
                   global monitor/mqtt_pub/aprs_msg instances and the
                   background thread loops
monitor.py         FleetMonitor class: SSH polling (paramiko), MMDVM log
                   parsing (WPSD) / `rpt xnode` parsing (ASL3), caller
                   lookup composition, map data assembly. `check_one()`
                   dispatches on `hotspot.get("type", "wpsd")` to
                   `_check_one_wpsd()` / `_check_one_asl3()` /
                   `_check_one_openspot4()` -- this is the pattern for
                   any future node type. The openspot4 branch is a
                   near-no-op (just the shared LAST_HEARD_TTL aging
                   check) since that type is push-based, not polled --
                   see openspot.py. `apply_external_update()` /
                   `mark_external_offline()` / `lookup_caller_info()` /
                   `apply_favorite_match()` / `log_activity()` are the
                   public hooks a push-based integration calls into
                   FleetMonitor instead of going through check_one()
models.py          HotspotStatus dataclass -- the shape returned by
                   /api/data. Adding a field here + setting it in
                   monitor.py is how new per-hotspot data reaches the UI
config.py          All tunables (env vars with defaults) + DEFAULT_SETTINGS
                   dict (settings.json schema) + the WPSD version-check
                   shell command
storage.py          hotspots.json / settings.json / favorites.json --
                   flat JSON files in CONFIG_DIR (the mounted volume)

qrz.py, radioid.py, aprs.py, brandmeister.py, aslstats.py,
mqtt_publisher.py, aprs_messaging.py, update_check.py, aprs_inbox.py,
hf_conditions.py
                   One self-contained client class per integration.
                   Each: caches results, NEVER raises out of its public
                   methods (returns None/False on any failure), and is
                   independently hot-swappable via a `set_*_client()` /
                   rebuild-on-settings-save pattern in app.py.
                   update_check.py compares this deployment's BUILD_COMMIT
                   file against the git host's REST API -- see the
                   self-update gotcha below before touching it.
                   aprs_inbox.py is the one exception to "stateless client,
                   rebuilt fresh from settings" -- it owns a persistent
                   background socket (AprsInbox.configure() reconfigures
                   it in place via a generation counter rather than the
                   app dropping/recreating the object), since a listening
                   connection can't be discarded and remade per-call the
                   way a QRZ/Brandmeister lookup client can

license_quiz.py    The one module in this list that ISN'T a network
                   client -- LicenseQuizPool loads all three bundled
                   pools (technician_2026_2030.json, general_2023_2027
                   .json, extra_2024_2028.json -- Technician/General/
                   Extra, one JSON file each) once at startup and serves
                   random questions from memory, no cache/TTL/fetch
                   involved. `random_question(license_class)`/
                   `count(license_class)` take which pool to draw from
                   ("technician"/"general"/"extra", default "extra" for
                   backward compat). See its docstring for each pool's
                   source/license/errata-verification history before
                   ever touching a data file.

wspr_activity.py  WsprActivityClient: live WSPR beacon-spot counts from
                   wspr.live, localized to within RADIUS_METERS of
                   settings' station_grid (a Maidenhead locator,
                   converted to lat/lon by this module's own
                   grid_to_latlon()). Backs the Band Activity card --
                   deliberately a DIFFERENT data source/kind of signal
                   than hf_conditions.py (real observed spot activity,
                   not a solar-index prediction), even though both
                   cards group bands the same way. Cached per-grid-
                   square, same pattern as hf_conditions.py otherwise.

openspot.py        _OpenSpot4Worker + OpenSpot4Manager: openSPOT4 (SharkRF)
                   has NO SSH access -- monitored over its own HTTP+
                   WebSocket API instead. One persistent WebSocket
                   connection per configured device (JWT login, then
                   `ws://<ip>/<jwt>`, subprotocol `openspot4`), same
                   "N independent persistent connections" shape as
                   camera_stream.py's CameraStreamManager, not
                   aprs_inbox.py's single-global-connection pattern.
                   Reverse-verified live against a real openSPOT 4 Pro
                   (browser dev-tools capture) -- SharkRF's own published
                   API docs (github.com/sharkrf/osp-http-api) describe an
                   OLDER, incompatible openSPOT generation with different
                   endpoint names. See the Hard-won gotchas section
                   before touching the message parser -- only DMR call
                   start/end log lines are verified against a real call.

adif.py            parse_adif(): a pure ADIF (ham radio log interchange
                   format) tokenizer for the Live map's "Import ADIF
                   log" feature -- no ham-radio-lookup dependencies here
                   by design, position resolution (GRIDSQUARE vs. QRZ/
                   RadioID callsign fallback via monitor.lookup_caller_
                   info()) lives in app.py's /api/import_adif route
                   instead. Imported QSOs persist in qsos.json
                   (storage.py's load_qsos()/save_qsos(), same flat-file
                   convention as hotspots.json/cameras.json) -- replaced
                   wholesale on each import, no merge/dedupe.

wsjtx.py           WsjtxListener: listens for WSJT-X's own UDP telemetry
                   protocol (the same feed GridTracker/JTAlert use) and
                   appends each logged QSO to the SAME qsos.json adif.py's
                   importer writes to (storage.py's new append_qso(), not
                   save_qsos() -- appends one at a time rather than
                   replacing the whole list, since a live QSO arrives
                   incrementally). One persistent UDP socket, reconfigured
                   in place via a generation counter -- same shape as
                   aprs_inbox.py's AprsInbox, but simpler: UDP has no
                   login/reconnect-with-backoff to manage, closing the
                   socket unblocks a stale thread's recvfrom() immediately.
                   Only handles the QSOLogged message type, not the much
                   chattier Decode message (every single decode attempt) --
                   deliberately avoids reproducing the "worldwide spotter
                   clutter" the WSPR spots map overlay was removed for.
                   Position resolution reuses the exact same grid-square-
                   first/monitor.lookup_caller_info()-fallback logic as
                   adif.py's importer, including the QTH-line behavior
                   (WSJT-X's own configured grid square, not a settings
                   fallback, in the very common case it's actually set).

pota.py            PotaClient: live Parks on the Air activator spots
                   (api.pota.app, free/no-auth/CORS-open, confirmed live)
                   for the Live map's "POTA spots" overlay. No per-user
                   config needed -- unlike psk_reporter.py below, POTA's
                   feed already carries lat/lon directly, no grid-square
                   conversion needed. Reuses wsjtx.py's freq_to_band()
                   (after converting POTA's kHz-string frequency to Hz)
                   rather than a third duplicate band-edge table.

sota.py            SotaClient: live Summits on the Air activator spots
                   (api2.sota.org.uk, free/no-auth, confirmed live) for
                   the Live map's "SOTA spots" overlay. Unlike pota.py
                   above, SOTA's own spot feed carries only an
                   association+summit code, no coordinates -- a SECOND
                   lookup per distinct summit against
                   api-db2.sota.org.uk/api/summits/{assoc}/{code} (also
                   confirmed live) resolves a position, cached
                   indefinitely per summit code since a mountain doesn't
                   move, unlike the short-TTL spot list cache itself.

brandmeister_lastheard.py
                   BrandmeisterLastHeardListener: persistent WebSocket to
                   Brandmeister's own public real-time "Last Heard"
                   Socket.IO feed, for the Notifications card's
                   "Brandmeister favorite alerts" -- notifies when a
                   favorite callsign keys up ANYWHERE on the network, not
                   just through this fleet's own hotspots (that narrower
                   case is monitor.py's own favorite matching). The
                   host/path/event-name/join-room/payload shape here were
                   ALL found via live reverse-engineering of
                   brandmeister.network's own bundled frontend JS plus
                   direct protocol probing -- none of it is documented
                   anywhere by Brandmeister. See the module's own
                   docstring and the Hard-won gotchas section below for
                   the full story before touching this if it ever stops
                   working.

psk_reporter.py    PskReporterClient: live PSK Reporter reception reports
                   (retrieve.pskreporter.info) for the Live map's "PSK
                   Reporter" overlay -- shows where YOUR OWN signal was
                   heard, scoped to settings.psk_reporter_callsign, NOT a
                   worldwide feed like the removed WSPR spots overlay.
                   XML response (confirmed live, NOT JSON despite some
                   secondhand summaries), fields as plain XML attributes,
                   not child tags like qrz.py's shape. Cached 10 min --
                   PSK Reporter's server actively rate-limited this
                   dev environment after a handful of test queries a
                   couple minutes apart, so this is deliberately far more
                   conservative than every other integration's caching
                   here. See the Hard-won gotchas section for the
                   flowStartSeconds request-vs-response meaning trap.

satellites.py      SatelliteTracker: current position + upcoming pass
                   predictions for the Satellites card and the Live
                   map's ground-track overlay. Orbital elements (TLEs)
                   from celestrak.org (free, no key -- a bare `curl`
                   with no User-Agent gets a 503 from their bot
                   protection, a normal browser-style UA gets a clean
                   200); real SGP4 propagation (the `sgp4` PyPI
                   package, not hand-rolled orbital mechanics) plus a
                   manual TEME->ECEF->geodetic conversion for position
                   and ECEF->topocentric-ENU for observer-relative
                   elevation/azimuth -- verified against a live
                   ground-truth ISS position (api.wheretheiss.at)
                   before trusting any of it; see the module's own
                   docstring and the Hard-won gotchas section for the
                   full verification story, including a real
                   SatNOGS DB API gotcha (`satellite__norad_cat_id`,
                   not the more obvious `norad_cat_id`, which silently
                   ignores the filter and returns the whole unfiltered
                   transmitter table instead of erroring).

digipi.py          DigipiMonitor: SSH-polls a DigiPi's Direwolf log
                   (/run/direwolf.log, NOT a systemd service -- a plain
                   background process) for APRS activity. Deliberately a
                   DEDICATED card, not a hotspot type -- a digipeater has
                   no single "active call" the way WPSD/ASL3 do, so it
                   doesn't reuse HotspotStatus/monitor.py at all. Own
                   settings.json keys (digipi_*), own /api/digipi route,
                   own background thread in app.py's main(). See the
                   Hard-won gotchas section below before touching the
                   packet-line parser -- it's built against two real
                   captured log samples, not DigiPi's own docs (which
                   don't document the log format at all).

hamalert.py        HamAlertListener: persistent Telnet connection to
                   hamalert.org:7300 (a user's own personal DXCC/
                   callsign/band alert triggers, configured on their
                   HamAlert account, not this app) for the optional
                   "HamAlert" notification card. Same generation-counter
                   reconfigure-in-place shape as aprs_inbox.py's
                   AprsInbox/wsjtx.py's WsjtxListener, adapted for plain
                   Telnet instead of APRS-IS/WSJT-X UDP. Login is a bare
                   username\r\n then password\r\n (no prompt-matching),
                   set/json\r\n switches the session to one JSON object
                   per matched spot -- both confirmed against a real
                   third-party integration and HamAlert's own support
                   forum, NOT live-tested against a real account (none
                   available in this dev environment) -- see the
                   Hard-won gotchas section before touching the login/
                   parsing logic if this ever stops working. Deliberately
                   uses the Telnet destination over HamAlert's webhook
                   alternative, since a webhook would require this
                   dashboard to accept a public inbound connection --
                   the one thing every other integration here avoids.

host_stats.py, weather.py
                   Small standalone pollers (host CPU/mem, Open-Meteo).
                   host_stats.py also has is_pi_standalone() (checks
                   /proc/device-tree/model + absence of /.dockerenv),
                   computed once at app.py startup as HOST_CAN_POWER_CONTROL
                   -- gates the Settings "reboot/power off this Pi" buttons.
                   is_docker() alone (no Pi-hardware check) backs the
                   broader HOST_IS_STANDALONE, gating self-update install

camera_stream.py   CameraStreamManager: bridges RTSP (ffmpeg subprocess)
                   and Bambu Labs A1 (bambulabs_api) camera feeds to plain
                   MJPEG, one background worker per actively-viewed camera,
                   shared across any number of browser tabs. Cameras
                   themselves live in cameras.json (storage.py), a plain
                   dict list like hotspots.json -- not part of the
                   HotspotStatus/monitor.py world at all, since a camera
                   isn't a hotspot and has no SSH-polled status

templates/dashboard.html   Main UI: cards + live map (Leaflet). One big
                           inline <script> block, no build step, no
                           frontend framework. This IS the "instrument
                           panel" reskin -- graphite panel surfaces, a warm
                           amber brand accent used ONLY for identity, a
                           separate cyan `--live` token meaning "on-air/
                           connected right now", monospace for every data
                           value. See the "dashboard_beta.html promoted"
                           gotcha below for how this file got here and
                           what to know before touching its color tokens.
                           Extras beyond the base card/map UI: a toolbar
                           fleet-status pill (X/Y online, Z active now,
                           live-pulsing once anything is), "spotlight"
                           dimming of idle hotspot cards while any one is
                           active (scoped to `.hotspot-card` specifically
                           so it doesn't dim unrelated sentinel cards, and
                           to `:not(.favorite)` too so an actively-
                           transmitting friend doesn't get dimmed along
                           with genuinely idle cards -- toggleable via
                           `beta_spotlight_dimming`, on by default), a thin
                           animated LED-bargraph VU meter that replaces the
                           static RSSI/BER line on an active card (anchored
                           to that hotspot's real `rssiBarPct`, not fully
                           decorative -- driven by a persistent fast tick
                           loop that looks elements up by id each tick,
                           same pattern as the existing tx-time/last-heard
                           timer tick, since `renderCards()` fully replaces
                           the card markup every 3s poll and a per-render
                           `setInterval` would get torn down with it), and
                           an optional synthesized two-tone courtesy beep
                           (`beta_courtesy_tone` setting, Web Audio, no
                           audio file) on a new active call -- silently
                           no-ops if the browser hasn't seen a user gesture
                           yet (AudioContext autoplay restriction), same
                           degrade-gracefully contract as every other
                           best-effort feature in this app.
templates/setup.html       Settings UI: General / Weather / Integrations /
                           Hotspots / Cameras / Favorites tabs. Shares
                           dashboard.html's "instrument panel" color tokens
                           (own independent file/`:root` block, not a
                           shared partial -- see the setup.html reskin
                           gotcha before touching either theme block)
templates/version.html     Changelog + feature list + module hash/version
                           info (shown at /version)
templates/readme.html      Renders README.md client-side via marked.js
                           CDN (shown at /readme)
```

### Data flow

1. `monitor.run_forever()` — background thread, every `POLL_INTERVAL`
   (default 5s): SSH into each hotspot, tail MMDVM log, parse active
   call/mode/talkgroup, update `self._data[ip]` (a `HotspotStatus`).
2. `monitor.run_slow_checks_forever()` — separate background thread,
   every `VERSION_CHECK_INTERVAL` (default 30min): Brandmeister profile +
   WPSD git-based update check. Deliberately slow because both involve a
   real network round-trip from the hotspot itself.
3. `app._mqtt_publish_loop()` / `app._aprs_alert_loop()` — separate
   background threads in `app.py` (not `monitor.py`), decoupled by just
   reading `monitor.snapshot()` on the same cadence. This is the pattern
   to follow for any new "do something with the current fleet state"
   feature — don't thread it through `monitor.py` itself.
4. Browser polls `/api/data` (cards) and `/api/map_data` (map) every 3s.

### Settings vs env vars

QRZ credentials check `settings.json` first, env var as fallback
(`_qrz_credentials()` in `app.py`) — this exists for backward compat with
older Unraid templates that set env vars. Every integration added *after*
QRZ (RadioID, APRS, Brandmeister, MQTT, APRS messaging) is
settings.json-only, no env var fallback. Don't add new env-var-based
config for per-integration credentials; put it in
`config.DEFAULT_SETTINGS` and a Settings UI field instead.

## Hard-won gotchas (read before touching these areas)

- **`str_replace` with `old_str` = just a bare `@app.route(...)`
  decorator line is dangerous.** If `new_str` doesn't re-include that
  exact decorator, the *next* function's decorator silently disappears
  (str_replace matches and replaces only what you gave it — the
  decorator immediately below your replacement was never touched, but if
  your old_str was `@app.route("/api/foo")` alone and you're inserting a
  new route+function that itself ends without a trailing decorator, you
  can end up consuming the wrong one). This has actually happened twice
  in this project (`test_mqtt` and `test_brandmeister` both silently lost
  their decorators this way and 404'd for a while before being caught).
  **After any edit touching multiple `@app.route` functions, verify with:**
  ```python
  import app
  print(len(list(app.app.url_map.iter_rules())))  # sanity-check the count
  ```
  or the AST-based decorator-presence check (see git history / prior
  session for the exact script) rather than assuming a green py_compile
  means routes are intact.

- **WPSD's `.git` is typically owned by `www-data`/`root`, not the SSH
  login user.** Modern git (2.35.2+) refuses to run *any* command
  against a repo it doesn't recognize as owned by the current user
  ("detected dubious ownership") and fails silently under
  `2>/dev/null`. Every git invocation in `config.VERSION_CHECK_CMD` uses
  `-c safe.directory='*'` to work around this — a per-invocation
  override, not a persisted config change on the hotspot. Confirmed
  against a real WPSD install, not assumed.

- **WPSD tracks three separate git repos**, not one: WPSD-WebCode
  (`/var/www/dashboard`), WPSD-Scripts (`/usr/local/sbin`),
  WPSD-Binaries (`/usr/local/bin`). An earlier version of the update
  check stopped after the first repo found (`break`), which meant a
  pending update in Scripts/Binaries specifically was invisible even
  though WPSD's own Admin→Update page would show it. Check all three.

- **`leaflet.markercluster` requires the map object itself to have
  `maxZoom`** (`L.map('map', { maxZoom: 19 })`), not just the tile
  layers. Without it, `MarkerClusterGroup` throws `"Map has no maxZoom
  specified"` on every single refresh cycle forever — this was the root
  cause of a real "map freezes the dashboard" bug.

- **`refreshMap()` must check the map tab is actually visible** before
  doing its marker/line rebuild. It's still invoked from the main
  3-second `refresh()` poll loop unconditionally; the function itself
  gates on `#panel-map` having `.active`. If you touch this, keep that
  gate — it used to run forever in the background even after navigating
  away from the map tab.

- **Docker: restarting an existing container does NOT pick up a newly
  built image at the same tag.** Containers are bound to the specific
  image ID they were created from. Any change here requires
  stop → remove → recreate (`docker-update.sh` does this in one step).
  `docker-compose up -d --build` handles this automatically; a bare
  `docker restart` does not.

- **Unraid's "WebUI" dropdown link is driven by the
  `net.unraid.docker.webui` container label**, which Unraid's "Add
  Container" GUI sets automatically but a plain `docker run` does not.
  `docker-update.sh` sets it explicitly for this reason — don't drop it
  if you touch that script.

- **Unraid's Docker tab "Edit" option requires the
  `net.unraid.docker.managed=dockerman` container label — an XML
  template file in `templates-user/` alone is NOT sufficient.**
  Confirmed directly from Unraid's own source, not guessed:
  `dynamix.docker.manager/include/DockerClient.php` sets
  `$c['Manager'] = $info['Config']['Labels']['net.unraid.docker.managed'] ?? false`,
  and `DockerContainers.php`'s template lookup (`getUserTemplate()`,
  which matches `templates-user/*.xml` by `<Name>`) only even runs when
  `$ct['Manager'] === 'dockerman'` — otherwise `$tmp['template']` is
  forced to `null` and the container name isn't rendered as a clickable
  Edit link, full stop. A first attempt at fixing this (v3.7.1) only
  copied `unraid-template.xml` into
  `/boot/config/plugins/dockerMan/templates-user/` without setting this
  label, and empirically did NOT restore Edit after a `docker-update.sh`
  recreate, even though the template file was confirmed present with a
  matching `<Name>` — proof that the file-matching path never even runs
  without the label. Fixed for real in v3.7.2 by adding
  `-l net.unraid.docker.managed=dockerman` to `docker-update.sh`'s
  `docker run` (alongside the template-file copy, which is still needed
  so `getUserTemplate()` has something to find once the label makes it
  look). If you ever touch this again, verify against Unraid's actual
  source (`grep` the relevant plugin directory, path found via
  `find /usr/local/emhttp/plugins -maxdepth 1 -iname "*docker*"` — the
  plugin is `dynamix.docker.manager`, not `dockerMan`; that name only
  applies to the templates-user directory path) rather than guessing
  from container-list behavior alone — one wrong guess already shipped
  here before the label was found.
  If `unraid-template.xml`'s fields ever drift from what
  `docker-update.sh`'s `docker run` actually sets (ports/volumes/env
  vars), fix both together — they're two independent descriptions of
  the same container config, nothing keeps them in sync automatically.

- **This sandbox/dev environment has a restricted network egress
  allowlist.** Test failures against real external APIs (Brandmeister,
  APRS-IS, MQTT brokers) from a dev/CI sandbox may just be the sandbox's
  own firewall (`x-deny-reason: host_not_allowed` style errors), not the
  real service. Verify against a real device/network before concluding
  an integration is broken — this exact mistake was made once already
  with the Brandmeister API (misread a sandbox 403 as a live API
  response) and corrected.

- **Every integration client degrades silently by design.** QRZ,
  RadioID, APRS, Brandmeister, MQTT, and APRS-messaging all swallow their
  own exceptions and return `None`/do nothing on failure, specifically so
  a flaky external API/broker never crashes the main poll loop or the
  background threads. Keep new integrations consistent with this —
  don't let a new client raise out of `monitor.py` or the background
  loops in `app.py`.

- **ASL3's per-linked-node keyed state is NOT in `rpt lstats`/`rpt stats`
  output** — both only expose connection state (`ESTABLISHED`/
  `CONNECTING`) per link, and a local-node-aggregate keyed flag ("Signal
  on input"). The real signal is `asterisk -rx "rpt xnode <node>"`'s
  dialplan-variable dump, specifically the `RPT_ALINKS=<count>,<node><mode
  T/R/L/C><K keyed/U unkeyed>,...` line — confirmed against a real node
  (W1ZLA, node 59929), not assumed from `app_rpt` source alone. If you're
  touching `config.ASL_ALINKS_LINE_PATTERN`/`ASL_ALINK_ENTRY_PATTERN` or
  `monitor._parse_asl_output`, re-verify against real output rather than
  reasoning from the CLI docs, which don't mention this field at all.
  Node→callsign resolution has the same trap: `/var/lib/asterisk/
  rpt_extnodes` looks like it should have callsigns but is purely IAX2
  connection routing (`node=radio@host:port/node,host`) — use
  `aslstats.py`'s `stats.allstarlink.org` lookup instead.

- **`RPT_ALINKS`'s keyed state is per-*node*, not per-operator — there is
  no ASL3 equivalent of DMR's talker alias/per-transmission caller ID.**
  `monitor._parse_asl_output`'s `active_call` is the registered callsign
  of whichever *linked node* is currently keyed (via `aslstats.py`), not
  necessarily the specific human transmitting. If that node is a shared
  hub/reflector, its callsign (and `tx_start`) can stay pinned across
  several different operators keying up back-to-back through the same
  node, since nothing in `rpt xnode`'s output distinguishes them — this
  was reported as "the active call never changes even though multiple
  people are clearly talking" and confirmed live (node 600672/W2ECR,
  an East Coast hub) to be this ceiling, not a lookup bug. Don't try to
  fix this by polling harder or adding more SSH commands — `app_rpt`
  genuinely doesn't expose per-speaker identity for a linked node, only
  per-node keyed state (same limitation AllScan has).

- **The grey line (day/night terminator) overlay uses a general
  closed-form zenith-latitude solve, not the textbook `tan(lat) =
  -cos(H)/tan(dec)` terminator formula directly** (`dashboard.html`'s
  `latForZenith()`). That classic formula only holds at exactly Z=90°
  (the true terminator); the twilight bands drawn on either side of it
  (Z=82°/98°) need the general form `A*sin(lat)+B*cos(lat)=C` solved via
  `R*sin(lat+phi)=C`. Verified numerically before shipping (not trusted
  from memory): at Z=90 it reproduces the classic formula bit-for-bit,
  and plugging any solved `lat` back into the zenith formula reproduces
  the requested `Z`. Also checked that the solve has no "no crossing"
  (polar day/night) case anywhere in the +-12° twilight band used here,
  even at max declination (23.44°, solstice) — that only becomes
  possible past roughly Z<66.5° or Z>113.5°, well outside this range, so
  `latForZenith()`'s `null` return is reachable in principle but never
  hit in practice by the three bands actually drawn. If the twilight
  band width ever changes, re-verify this the same way (see prior
  session's Python reference-point checks) rather than assuming it still
  holds.

- **`NoNewPrivileges=yes` in the systemd unit blocks `sudo`/setuid
  entirely, regardless of sudoers config.** The Host power control
  feature (v3.1) originally shipped calling `sudo reboot`/`sudo shutdown
  -h now` via `subprocess.Popen` — this failed silently in production
  (Popen never checks the exit code, so the API claimed success
  regardless) because the standalone install's systemd unit has
  `NoNewPrivileges=yes` (`install.sh`), which prevents the process from
  ever gaining privileges via a setuid binary like `sudo`, independent of
  whether the service user is even in sudoers. Fixed in v3.2 by switching
  to `systemctl reboot`/`systemctl poweroff` (talks to systemd over
  D-Bus, not setuid) plus a polkit rule
  (`/etc/polkit-1/rules.d/49-hotspot-dashboard-power.rules`, written by
  both `install.sh` and `update.sh`) granting the service user the
  `org.freedesktop.login1.reboot`/`power-off` actions — a headless
  service account has no active session, so default polkit policy would
  otherwise deny it too. If you add another feature needing elevated
  privileges from within the app, use this same D-Bus/polkit pattern
  rather than `sudo`, and actually check the subprocess result (see
  `app._run_power_command`) rather than assuming `Popen` succeeded.

- **ASL3 connect/disconnect uses `rpt cmd <node> ilink <code>
  <remotenode>`, NOT `rpt fun <node> *3<remotenode>`.** The DTMF-simulated
  `rpt fun` form (`macro_append()` in `rpt_utils.c`, feeding the same
  digit-collection state machine real DTMF uses) looked plausible from
  `app_rpt`'s docs and even its own comment header, but two different
  live-tested variants (concatenated `*11603`, comma-paced `*1,1603`)
  both silently no-op'd against a real node with no error output at all.
  `rpt cmd` (used by `config.build_asl_ilink_cmd`) bypasses that state
  machine entirely, takes the function code and node as plain separate
  arguments, and is confirmed working both against a real node and by
  reading how [AllScan](https://github.com/davidgsd/AllScan) (a mature,
  widely-deployed tool) does the identical thing in its `astapi/
  connect.php`. If you ever need another `ilink`-family action, check
  AllScan's `connect.php` for the exact `ilink` code before guessing from
  `app_rpt` source/docs alone — the DTMF-string form is a trap that looks
  authoritative but doesn't behave as documented in practice.

- **Self-update (v3.7) reuses the Host Power Control privilege-separation
  pattern, scaled up, rather than weakening `ProtectSystem=strict`/
  `NoNewPrivileges=yes` for the main service.** The main app process can
  only write inside `CONFIG_DIR`; it can't `git pull` its own install dir
  or restart itself. So `/api/install_update` just writes a trigger file
  (`${CONFIG_DIR}/update_requested`), and a *separate* systemd unit pair
  installed by `install.sh` (idempotently re-provisioned by `update.sh`,
  same as the polkit rule) does the actual work as root:
  `hotspot-dashboard-updater.path` (`PathExists=` the trigger file) fires
  `hotspot-dashboard-updater.service` (oneshot, runs `run_update.sh`,
  which does `git pull --ff-only` against the original install-time
  `SCRIPT_DIR` then re-runs `update.sh`). If you touch this, keep the
  privilege boundary intact — don't grant the main service broader
  filesystem/D-Bus access just to make this simpler.
- **There is no `.git` and no `git` binary at runtime for either
  deployment type**, so `update_check.py` can't just run `git rev-parse
  HEAD` against the running app's own directory — confirmed by reading
  `install.sh`/`update.sh` (plain `cp`, not a git clone) and the
  Dockerfile (`python:3.12-slim` doesn't ship `git`). Instead
  `install.sh`/`update.sh`/`docker-update.sh` each capture
  `git rev-parse HEAD` on the *source* checkout at install/build time
  into a plain-text `BUILD_COMMIT` file the running app just reads —
  don't reintroduce a runtime `git` dependency for this.
- **The update-check API call is a Forgejo/Gitea REST endpoint
  (`/api/v1/repos/{owner}/{repo}/branches/{branch}`), confirmed against
  the real `git.trytheitguy.com` instance** (not GitHub's differently-
  shaped API) before writing `update_check.py` — if the configured repo
  ever needs to support GitHub too, that's a different endpoint shape
  entirely, not a drop-in.
- **A standalone Pi install with no `.git` checkout (SCP/zip-copied
  source instead of `git clone`) is a real, reported scenario, not a
  hypothetical.** This is `git -C "$SCRIPT_DIR" rev-parse HEAD` failing
  (no `.git` directory at all — a different failure than the WPSD
  "dubious ownership" gotcha above, which is about *ownership* of an
  existing `.git`, not its absence). Fixed in two stages:
  1. First pass: the *message* shown for this case in `setup.html`'s
     `renderUpdateStatus()` used to say "re-run update.sh (or
     docker-update.sh) once to enable update checks" — actively
     misleading, since re-running update.sh against the same non-git
     `$SCRIPT_DIR` hits the identical `git rev-parse` failure forever.
     Fixed the message, and had `install.sh`/`update.sh` `warn()` loudly
     at deploy time instead of failing silently.
  2. Second pass (the real fix): `install.sh` now actively sets up a
     **persistent git checkout** (`/opt/hotspot-dashboard-src`, cloned
     from `DEFAULT_REPO_URL`) when `$SCRIPT_DIR` has no `.git`, and
     installs from *that* instead of the non-git copy — so update
     checks and the self-update button work even if the user never
     touches git themselves. `update.sh` does the equivalent for the
     self-update button specifically (`UPDATER_SOURCE_DIR`, resolved
     separately from `$SCRIPT_DIR` — see next point) since it can't
     retroactively fix `BUILD_COMMIT`/the just-installed files without
     silently overriding whatever the user intentionally placed at
     `$SCRIPT_DIR` for *this* run, which would be a correctness
     regression for someone deliberately testing local changes via SCP.
     Both scripts fall back to today's warn-and-continue behavior if
     cloning fails (no network, git host unreachable) — never a hard
     install failure over this.
- **`install.sh`/`update.sh` distinguish "what got copied into
  INSTALL_DIR" from "what the self-update button pulls from" — two
  different variables, deliberately not unified.** `install.sh`
  resolves one `SOURCE_DIR` and uses it for both (copying files AND
  seeding `run_update.sh`), since at install time there's no
  already-installed state to preserve. `update.sh` keeps `SCRIPT_DIR`
  (wherever *this* update.sh run's files came from — respected as-is,
  copied verbatim, whatever the user intended) separate from
  `UPDATER_SOURCE_DIR` (what `run_update.sh` gets regenerated to pull
  from next time the button is clicked — falls back to the persistent
  `/opt/hotspot-dashboard-src` clone if `$SCRIPT_DIR` itself isn't git,
  auto-cloning one if neither exists yet). Don't collapse these back
  into one variable in `update.sh` — that reintroduces exactly the
  "silently overrides local files with upstream" regression stage 2
  above was written to avoid, and also reintroduces the older latent
  bug where every manual `update.sh` run regenerated `run_update.sh`
  with `SOURCE_DIR="$SCRIPT_DIR"` unconditionally, quietly breaking the
  self-update button again if that particular run happened to be
  invoked from a transient non-git location.

- **Camera cards (v3.8) — Bambu Labs A1's camera is NOT RTSP.** It's a
  proprietary TLS/port-6000 framed-JPEG protocol. A first instinct to
  hand-roll that framing from memory (the way `config.build_asl_ilink_cmd`
  hand-rolls a *known, verified* AllStarLink command) was deliberately
  rejected here — an incorrect guess at undocumented binary framing is a
  much worse failure mode than an incorrect guess at a documented CLI
  command, and would require live iterative debugging against real
  hardware to even notice it's wrong. Used the `bambulabs_api` pip
  package instead (confirmed on PyPI + its docs' "Get a Camera Frame"
  example, `printer.get_camera_image()`, before committing to it) — same
  reasoning as `paho-mqtt`/`aprslib` elsewhere in this project: a
  focused, tested dependency beats a fragile reimplementation of a
  reverse-engineered protocol.
- **All three camera types converge on one MJPEG broadcaster
  (`camera_stream.py`)** so the frontend never needs to know which type
  it's looking at — just `<img src="/api/camera_feed/<id>">`. Workers
  are lazy (start on first viewer) and self-stopping
  (`CAMERA_IDLE_STOP_SEC` after the last viewer leaves) specifically so
  an *enabled* camera that nobody is currently looking at the dashboard
  doesn't hold an ffmpeg process or a printer connection open forever.
  One real bug caught by testing before shipping, not by inspection: a
  `threading.Condition.wait()`-based frame waiter that only checked
  `frame_seq` (not `state`) missed the notify from an immediately-offline
  misconfigured camera (the notify fires before the waiter starts
  waiting — a Condition doesn't queue missed notifications), so it
  stalled for the full timeout instead of failing fast. Fixed by also
  checking `state` before deciding whether to wait at all
  (`camera_stream.py`'s `wait_for_frame`) — if you touch that method,
  re-verify with a misconfigured (blank URL / blank credentials) camera,
  not just a working one, since the bug only showed up in the failure
  path.
- **Camera position reuses the Fleet Activity/ASL Favorites sentinel
  scheme (`position` = index among hotspot cards), not hotspots.json's
  own plain-list-order scheme** — even though cameras are a dynamic,
  addable/removable/editable list like hotspots.json, they don't get
  their own ordered list the way hotspots do. Each camera is one more
  entry in `computeCardOrders()`'s `sentinels` array (`dashboard.html`)
  and gets its own `data-ip="__camera__<id>"` row interleaved into the
  same `#card-order-list` hotspots use (`setup.html`), saved via
  `/api/reorder_cameras` (`{camera_id: position}`) rather than
  `/api/reorder_hotspots`'s ordered-list-of-ids shape. This was a
  deliberate choice, not an oversight — it reuses an already-tested
  algorithm instead of merging hotspots.json and cameras.json into one
  combined list, which would have been a much bigger refactor for the
  same visible result (free interleaving in the drag list).
- **Camera `type: "wyze"` (v3.16) is a cosmetic alias for `"rtsp"`, not a
  real third camera implementation.** Wyze cameras with Wyze's own
  official RTSP firmware (confirmed real, not a third-party hack —
  Wyze publishes it themselves for Cam v2/v3, Pan v2/v3, and newer
  models) just speak plain RTSP, so `camera_stream.py`'s dispatch
  (`_BambuWorker if camera.get("type") == "bambu_a1" else _RtspWorker`)
  already routes it correctly with zero changes there. The only places
  that know about `"wyze"` at all are `app.py`'s `/api/cameras` type
  validation/field-selection (`cam_type in ("rtsp", "wyze")`) and the
  Settings dropdown/dashboard badge label. If you ever add a genuinely
  different Wyze integration (e.g. via Wyze's cloud API for models
  without RTSP firmware, like the unofficial `docker-wyze-bridge`
  project does), that would need real branching in `camera_stream.py`
  the way Bambu does — don't assume "wyze" already means that.

- **A precipitation radar overlay (RainViewer, animated) was built,
  fixed for a flicker bug, then removed entirely in v3.11** — even the
  flicker-free crossfade version still felt "choppy" (13 sparse
  snapshots at 700ms apart just doesn't read as smooth motion, no matter
  how cleanly each swap is handled), and the user preferred dropping the
  feature over further tuning. If this gets revisited, the crossfade
  technique (new transparent tile layer per frame, swap in only after
  its Leaflet `'load'` event fires, never `.setUrl()` on a live layer)
  is still the right way to avoid the flicker — but the "choppy" feel is
  a separate, harder problem (real motion-interpolation between sparse
  radar snapshots, which most weather apps don't attempt either) that
  wasn't solved and shouldn't be assumed away.

- **The APRS Messages card (`aprs_inbox.py`) was verified against
  `aprslib`'s actual source before being written, the same discipline as
  `aprs_messaging.py`'s send side** — a synthetic message packet parsed
  with the real library confirms the exact field shape (`from`,
  `addresse` — note aprslib's own spelling, not "addressee" — `format`,
  `message_text`, `msgNo`). Critically, **an incoming ack/reject of a
  message *we* sent also comes back with `format == "message"`**; the
  only reliable way to tell "a real message to show and ack" apart from
  "someone acking something I sent" is whether `message_text` is present
  at all, not the `format` field. Get this wrong and the card would
  either show ack/reject packets as if they were real messages, or
  (worse) auto-ack a packet that was never a message to begin with.
- **The `b/CALLSIGN*` "buddy" APRS-IS filter wildcards across every SSID
  of the configured callsign — it did NOT originally, and that was a
  real, reported gap, not a hypothetical.** The javAPRSFilter spec's own
  wording for `b/` is "Pass all traffic from **exact** call" — fetched
  and quoted directly from aprs-is.net before touching this, not assumed
  — meaning a bare `b/W1ZLA` only ever matched literal "W1ZLA", silently
  missing "W1ZLA-9", "W1ZLA-1", etc., unlike how apps such as APRS.fi
  aggregate messages across all SSIDs of a callsign. Fixed by wildcarding
  the filter on the base callsign (`_base_call()`, strips a trailing
  `-N`) and — critically — widening `_on_packet`'s match check to the
  same base comparison. Both sides have to move together: widening only
  the filter without widening the match would just mean the new SSID
  variants arrive and get silently discarded right after; widening only
  the match without the filter would never receive them from APRS-IS in
  the first place. Verified against synthetic packets addressed to three
  different SSIDs of the same base call (all three now show up) plus a
  deliberately similar-but-different callsign ("W1ZLAB" vs "W1ZLA" —
  `_base_call()`'s regex only strips an actual `-digits` suffix, so this
  correctly does NOT match). The login identity sent to APRS-IS still
  uses the exact configured callsign (SSID included, if any) — only the
  filter and match widen, not the login itself; `aprslib.passcode()`
  computes the same value regardless of SSID either way, confirmed
  directly against the installed library, not assumed. Acks are now sent
  from the specific SSID a message was actually addressed to
  (`packet["addresse"]`), not the generic base callsign — the sender's
  client is tracking the conversation with that specific addressee.
- **APRS-IS disconnects a connection the instant a SECOND connection logs
  in VERIFIED as the same callsign — not just when the login callsign
  string matches.** This was a real, reported bug ("test message doesn't
  show up in the inbox"), diagnosed and fixed by testing directly against
  the real network, not guessed: `aprs_inbox.py`'s persistent listener
  used to log in with the real computed passcode (verified), same as
  `aprs_messaging.py`'s short-lived send connections — so every real
  outbound alert (and the "Send test message" button) silently kicked the
  listener off, which then took `RECONNECT_BACKOFF` (~15s) to recover,
  long enough to miss the very message that caused the kick. Confirmed
  two things live before fixing: (1) reproducing the exact scenario
  (both connections verified as the real callsign) does trigger the kick
  — the server literally closes the socket right after `logresp W1ZLA
  verified, ...`; (2) a connection logged in **unverified** (`passwd=
  "-1"`) as the same callsign survives a second verified connection
  logging in alongside it, and still receives filtered traffic
  identically — verified status only gates transmit permission, not what
  you can receive. Fixed by changing the listener's login to `passwd=
  "-1"` (it never needed to transmit — acks already use their own
  separate short-lived verified connection, same pattern
  `aprs_messaging.py` uses, and that's unaffected since there's only ever
  one verified connection active at a time now). A bigger fix was
  seriously considered first — routing all sends through the listener's
  own persistent connection instead of opening a second one — but reading
  `aprslib/IS.py` directly turned up a real, unguarded thread-safety
  hazard for that approach: `sendall()` and the consumer loop's
  `_socket_readlines()` both mutate the same socket's blocking mode with
  no locking between them, so calling `sendall()` from another thread
  while `consumer()` blocks in the listener's own thread is a genuine
  race, not just a style concern. Don't reach for a shared-connection
  redesign for this class of bug — the verified-vs-verified collision was
  the actual constraint, not the connection count. (This also means the
  SSID-wildcard gotcha above needs a small correction: it says "the login
  identity sent to APRS-IS still uses the exact configured callsign" —
  still true, but as of this fix it's an *unverified* login, not
  verified, for the listener specifically.)
- **`AprsInbox.configure()` reconfigures the same object in place via a
  generation counter, not a rebuild-a-fresh-client pattern** — a
  listening socket needs its own thread lifecycle, unlike the other
  stateless integration clients. When the callsign or enabled state
  changes, the generation counter bumps and a new thread starts; the old
  thread's callback checks the counter and raises `StopIteration`
  (caught internally by `aprslib`'s own `consumer()` loop) the next time
  it sees a packet, so it exits cleanly instead of running forever
  logged in under stale settings. One known, accepted gap: a stale
  thread blocked waiting on a quiet connection (no traffic, only
  server heartbeats) won't notice the generation change until *something*
  arrives to trigger the check — a lingering-connection risk on
  reconfiguration, not a correctness bug, since the stale callback still
  refuses to process/store/ack anything once it does check.

- **`hf_conditions.py`'s `<band>` XML elements are re-sorted against a
  hardcoded `BAND_ORDER` list rather than trusted to arrive in display
  order.** Confirmed live against the real feed before writing the
  parser (not just its docs) that each `<band>` carries `name`/`time`
  attributes (e.g. `name="80m-40m" time="day"`) rather than being
  nested under a per-band element, so day/night pairs for the same band
  have to be collected into a dict first, then re-ordered for display —
  don't assume the feed's own element order is what you want to render.

- **The License Quiz card's `extra_2024_2028.json` was sourced from
  a real, verifiable machine-readable export, not typed out from
  memory.** Getting FCC-exam content wrong is a worse failure mode than
  most other data-accuracy misses in this app (same caution level as the
  Band Plan card's frequency tables, one notch more so since this is
  literally exam prep). Source: NCVEC's official public-domain
  2024-2028 Extra (Element 4) release, transcribed by
  https://github.com/russolsen/ham_radio_question_pool (Apache-2.0) into
  JSON/YAML/CSV. That export has 599 questions; NCVEC's own release
  notes originally cited 603 for this cycle — flagged for a long time in
  this file as a small, unreconciled discrepancy between snapshots,
  rather than silently accepted. **Resolved** (see the "Technician and
  General pools added" entry below): NCVEC's own 4th errata (Feb 4,
  2026) confirms 4 questions were withdrawn after the original 603-count
  release (E2A13, E4D05, E6D07, E9E10), leaving exactly the 599 the
  export already reflected — both numbers were correct for different
  points in the errata history, not a real conflict. 27 of the 599
  reference a circuit diagram figure and are excluded (the images aren't
  bundled), leaving the 572 in `extra_2024_2028.json`. If this ever
  needs updating for the next pool cycle (2028), re-fetch from the
  export AND cross-check the active-question-ID set against NCVEC's own
  current errata PDF the same way — don't just trust the export
  snapshot or hand-edit/add questions from training-data recall.
- **`E0` is a real graded subelement in the current Extra pool** ("Safety"
  — RF exposure, tower/climbing safety, grounding), not a bonus/appendix
  section invented by the data export. Confirmed via a second, independent
  web source before trusting the E0 entries in the bundled data — don't
  assume subelement codes match older, more commonly-cited "E1-E9 only"
  descriptions of the Extra pool structure.
- **Technician and General pools added (v3.52) using the same
  export-plus-official-errata-PDF verification discipline as Extra, not
  a repeat of trusting the export alone.** For each class, the full
  active-question-ID set (all IDs in the export's JSON minus any marked
  "Question Deleted" in NCVEC's own current PDF) was diffed against the
  export -- a real, live diff, not a spot-check -- before bundling:
  - **Technician (technician-2026-2030, effective 7/1/2026)**: the OLD
    2022-2026 cycle is already in the export's own `outdated/` folder,
    confirming the cycle had genuinely rolled over, not an assumption
    from the calendar alone. 409 questions in the export; NCVEC issued a
    Feb 19, 2026 wording revision to 4 questions (T1C01, T5A05, T7A09,
    T0A10) -- fetched NCVEC's own revised PDF and confirmed word-for-
    word, including correct-answer letters, that the export already has
    the corrected text, not the original Dec 2025 wording (the export's
    one GitHub commit postdates the revision). 12 of 409 reference an
    unbundled figure, leaving 397.
  - **General (general-2023-2027, effective through 6/30/2027)**: 432
    questions at original release; NCVEC's own 6 rounds of errata
    (most recent Feb 4, 2026) withdrew 9 (G1A04, G1C08, G1C09, G1C10,
    G1E09, G6B09, G8C01, G9C06, G9D13) -- the full active-ID diff against
    NCVEC's 6th-errata PDF matched the export's 423 exactly, zero missing
    and zero extra. 5 of 423 reference an unbundled figure, leaving 418.
  Both pools were trimmed to the same `{id, question, answers, correct}`
  shape as `extra_2024_2028.json` (dropping the export's extra `refs`/
  `correct_letter` fields, unused elsewhere in this app) before bundling
  as `technician_2026_2030.json`/`general_2023_2027.json` at the repo
  root -- same "not in `data/`" placement as Extra, for the same
  `CONFIG_DIR` collision reason. `license_quiz.py`'s `SECTION_NAMES`
  became a dict-of-dicts keyed by class; each class's own subelement
  codes are globally unique (T-/G-/E-prefixed), which is what let the
  License Quiz card's client-side `lqStats` in `localStorage` stay one
  flat object across all three classes with zero risk of one class's
  saved accuracy stats colliding with or overwriting another's --
  deliberately not namespaced per-class. The card's new class `<select>`
  (`#lq-class-select`) persists the user's last-picked class in its own
  `localStorage` key (`lqSelectedClass`), same per-browser scoping as the
  stats themselves, defaulting to `"extra"` so an existing install with
  saved Extra-pool progress sees no behavior change until the user
  actively switches. `/api/quiz_question` takes `?class=` (default
  `extra` server-side too, via `license_quiz.DEFAULT_CLASS`) -- an
  unrecognized value just yields `license_quiz.random_question()`
  returning `None` (same 503 as a genuinely missing/corrupt pool file),
  no separate validation needed. If either pool is ever revisited for
  its next cycle, re-verify the exact same way: fetch the export, fetch
  NCVEC's own current-errata PDF, and diff the full active-ID sets --
  don't assume a single commit timestamp being "recent enough" is proof
  the export is current, confirm it against the actual official text.
- **License Quiz per-section accuracy stats live in browser
  `localStorage`, not `settings.json` or any other server-side store.**
  This app has no user accounts/login (already a documented tradeoff),
  so "your" progress can only mean "this browser's" progress — deliberately
  scoped narrower than every other piece of app state, which all lives
  server-side and is shared across every device viewing the dashboard.
  Don't move this into settings.json if asked to "sync" it — that would
  make one person's practice history overwrite another's on a shared
  dashboard, which is worse than not syncing at all.
- **`extra_2024_2028.json` lives at the repo root, deliberately NOT in a
  `data/` subdirectory — a `data/` directory there would collide with
  `CONFIG_DIR`'s default of `/app/data` and get silently shadowed.** This
  was a real bug, not a hypothetical: it originally shipped in `data/`,
  which `Dockerfile`'s `COPY . .` puts at `/app/data/extra_2024_2028.json`
  in the image — but `docker-compose.yml`/`docker-update.sh` bind-mount a
  host directory over that exact path (`-v .../hotspot-dashboard:/app/data`,
  the persistent hotspots.json/settings.json volume), which completely
  replaces the image's `/app/data` contents at container start rather than
  merging with them. The bundled question pool was invisible on every
  Docker/Unraid deployment as a result (`/api/quiz_question` returning
  503, surfaced as "Question pool unavailable right now." on the card) —
  Docker's `COPY . .` made it *look* covered, but the volume mount silently
  shadowed it at runtime. Confirmed and fixed by moving the file to the
  repo root. If you ever add another bundled static data file, keep it
  out of any path that collides with `config.CONFIG_DIR` (currently just
  `/app/data`) for the same reason.
- **`install.sh`/`update.sh` copy application files via an explicit list
  (`*.py`, `templates/*.html`), not a full-directory copy — a bundled
  data file needs its own explicit `cp` line in both scripts or it's
  silently never copied to `$INSTALL_DIR` on a standalone Pi/Linux
  install**, independent of the Docker `data/` collision above (this bug
  applies even with the file at the repo root, since standalone installs
  don't use `Dockerfile`'s `COPY . .` at all). `extra_2024_2028.json` has
  its own `cp` line in both scripts now (install.sh's main copy block;
  update.sh's backup block, copy block, and manual-rollback echo lines) —
  if you add another bundled non-`.py`/non-template file, add it in all
  of those same four places, not just one. `README.md` had exactly this
  bug too (confirmed on a real Pi: `/readme` returned "README.md not
  found," and its error message wrongly assumed a Docker deployment
  specifically -- "check your image build" -- since that's the only
  place this gap had been considered before). Fixed the same four-place
  way, plus a fifth: `update.sh`'s "Checking for changes" diff loop only
  ever compared `*.py`/templates/`requirements.txt` -- a commit that
  *only* touched `README.md` or `extra_2024_2028.json` (the latter had
  this exact same latent gap, never separately noticed) would make the
  `${#CHANGED[@]} -eq 0` check report "up to date, nothing to do" and
  exit *before ever reaching the copy commands* that would have updated
  them. Any future non-`.py`/non-template bundled file needs an explicit
  `diff -q` check added there too, or it can go stale forever on a
  standalone install as long as no `.py`/template file happens to change
  in the same commit.
- **`wspr_activity.py`'s WSPR band codes are NOT "the MHz digit of the
  frequency" below 10 MHz — confirmed via a real query, not assumed
  from the pattern that happens to work for 6m/10m/20m/etc.** 160m is
  `band=1`, not `1.8`; other low bands are non-sequential too (2200m is
  `-1`, 630m is `0`). `wspr.live`'s own docs describe the "first digit
  of frequency" rule, which is actually true for the bands from 40m
  (band=7) up through 6m (band=50), but breaks down below that — this
  was checked against a real live query (`SELECT band, min(frequency),
  max(frequency) ... GROUP BY band`) before committing to the codes in
  `BAND_GROUPS`, the same discipline as `config.build_asl_status_cmd`'s
  `rpt xnode` gotcha elsewhere in this file. If WSPR ever adds/changes a
  band code, re-verify the same way rather than trusting the "first
  digit" shorthand.
- **`grid_to_latlon()`'s Maidenhead conversion was verified against a
  real `wspr.live` row, not just the textbook formula.** A live query
  returned a row with `tx_loc="JN58th"` and `tx_lat=48.312`,
  `tx_lon=11.625`; running that same grid square through
  `grid_to_latlon()` produces `(48.3125, 11.625)` -- an exact match
  (to rounding). If this function is ever touched, re-verify the same
  way (pull a real row, compare its own lat/lon to what the function
  computes from its own grid square) rather than trusting the formula
  alone.
- **The Band Activity card (`wspr_activity.py`) is a deliberately
  different kind of data than the HF Conditions card, even though they
  share the same band groupings (160m, 80m-40m, 30m-20m, 17m-15m,
  12m-10m, 6m).** HF Conditions is N0NBH's solar-index-based Good/Fair/
  Poor *prediction*; Band Activity is a real WSPR spot-count *observation*
  within `RADIUS_METERS` (500km) of `settings.station_grid`. Don't merge
  these into one card or one data model -- higher WSPR spot counts mean
  "more stations are actively watching this band," not strictly "better
  propagation" (20m/30m are consistently busiest simply because they have
  the most WSPR stations running, independent of conditions), and at a
  500km radius the weaker bands (160m, 6m) can show a genuine zero for
  several hours -- that's real sparse data, not a bug, and shouldn't be
  "smoothed" or backfilled with an interpolated value.
- **`wspr.live` is rate-limited to 20 requests/minute and restricted to
  non-commercial use per its own terms** -- fine for this free
  self-hosted dashboard (cached 30 min server-side, one shared client
  instance), but don't lower `CACHE_TTL` significantly or add a second
  caller of `WsprActivityClient._fetch()` without accounting for the
  shared rate limit across however many dashboards happen to be running
  this code.

- **The Live map's "WSPR spots" overlay (`WsprSpotsClient` in
  `wspr_activity.py`) was removed in v3.44** -- worldwide spotter↔
  transmitter lines just didn't read well at a glance even after a prior
  round of visual tuning (spot-count limit, etc.), and the user preferred
  dropping the layer over further tuning. `WsprActivityClient` (the Band
  Activity card's per-station-grid spot *counts*) is unaffected and still
  live -- only the separate global per-spot-pair client/route/map layer
  is gone. Two schema facts learned while it existed are worth keeping in
  mind if a similar per-spot `wspr.rx` query is ever added again:
  - **`wspr.rx`'s callsign columns are named `tx_sign`/`rx_sign`, NOT
    `tx_call`/`rx_call`.** The removed client's first attempt guessed
    `tx_call`/`rx_call` as the natural ham-radio-terminology names and got
    a real, immediate `404`/`Code: 47 UNKNOWN_IDENTIFIER` error back from
    ClickHouse (with a helpful `Maybe you meant: ['tx_lat']` hint) rather
    than silently returning wrong data -- confirmed the real names via
    `DESCRIBE TABLE wspr.rx FORMAT JSON` before writing the fixed query.
  - **WSPR spot inserts into `wspr.rx` arrive in bursts, not a smooth
    continuous stream -- a tight time window can return zero rows even
    when the feed is working correctly.** Confirmed live: querying the
    last 120 seconds returned 7 rows one moment and 0 rows moments later,
    while a 5-minute window reliably returned ~6350 rows globally and a
    10-minute window ~22855 -- query a wider window (the removed client
    used 5 min) and rely on `ORDER BY time DESC LIMIT n` to bound the
    result size, not a tight window, if resurrecting this.
- **NOAA SWPC's OVATION aurora feed (`aurora.py`) was verified with a
  real, complete download before trusting its shape** -- an initial
  WebFetch-based check against the same URL gave inconsistent/partial
  numbers (it appears to summarize large responses rather than reading
  them completely), so the real numbers came from a direct `curl` +
  full `json.load()`: 65,160 coordinate entries (exactly 360x181, confirming
  a full 1x1 degree global grid), `[longitude, latitude, aurora_value]`
  per entry, longitude 0-359 (needs `-360` for values >180 to get
  standard -180..180 for Leaflet), value range 0-18 at verification time
  with only ~1000 points above 10. `MIN_INTENSITY=10` is a real,
  data-backed threshold, not a guess. If this feed's shape is ever
  suspected to have changed, re-verify with an actual download the same
  way -- don't trust a WebFetch summary of it alone.

- **Never interpolate a Jinja value directly into a JS string literal
  inside an `onclick` attribute (`onclick="fn('{{ value }}')"`) — Jinja's
  default HTML auto-escaping does NOT protect this, even though it looks
  like it should.** A single quote in `value` gets escaped to `&#39;` in
  the HTML source, but the browser's HTML parser decodes that back to a
  literal `'` *before* handing the attribute's text to the JS engine as
  inline event-handler code — so the quote still terminates the JS string
  early and breaks the handler's syntax, silently making the button do
  nothing. This was a real, reported bug (not hypothetical): a user
  couldn't Edit one specific hotspot, traced to an apostrophe in its
  name/SSH username/SSH password. Fixed at all four sites that had this
  pattern (`editHotspot`/hotspot delete-confirm/`editCamera`/
  `deleteCamera` in `setup.html`) by moving the values into `data-*`
  attributes instead (genuinely safe here, since HTML-attribute-escaping
  is the *only* parsing that happens to them — nothing re-decodes and
  re-parses them as JS afterward) and reading them back via
  `this.dataset.*` inside the handler, rather than baking raw values into
  the onclick string at all. If you add a new Edit/Delete-style button
  for a list item with user-editable text fields (hotspot/camera name,
  any free-text setting), use the same `data-*` pattern from the start —
  don't reintroduce the string-interpolation version even for a field
  that "probably" won't contain a quote (IPs and UUIDs are fine; names,
  usernames, and passwords are exactly the fields most likely to have
  one eventually).
- **Editing a hotspot's IP address is a rename of its identity key, not
  just a field update — `/setup` POST needs the pre-edit IP separately
  from the submitted one.** `hotspots.json` entries are matched/replaced
  by `ip` (`app.py`'s `hotspots = [h for h in hotspots if h["ip"] !=
  ...]` upsert pattern). The original version matched on the *submitted*
  ip, which works fine for adding a new hotspot or editing one without
  touching its IP, but silently broke changing the IP itself: filtering
  for the new ip matches nothing (no existing entry has it yet), so the
  old entry never gets removed — you'd end up with the untouched old
  entry plus a new orphaned duplicate, which reads as "my IP change
  didn't save" since the UI's hotspot list still shows the old IP too.
  Fixed by adding a separate `orig_ip` hidden field (`setup.html`),
  populated by `editHotspot()` from the ip the record had *before* this
  edit (not touched by the visible IP input the user might change), and
  matching on `orig_ip` when present (`app.py`, falls back to the
  submitted ip for the add-new-hotspot case, where there's no prior
  entry to match). Verified with a live `app.test_client()` sequence:
  add → edit-with-IP-change → confirm exactly one hotspot remains at the
  new IP (not two) → edit-without-IP-change → confirm that path still
  behaves as before → add a second hotspot → confirm it doesn't clobber
  the first. If another field is ever promoted to be part of a record's
  matching identity, it needs this same `orig_<field>` treatment, not
  just a value swap.
- **`setup.html`'s `/setup#version` deep-link check must run at the very
  end of the script, after every `let`/`const` it touches (indirectly,
  via `loadVersion()`) has already been declared — putting it anywhere
  earlier reintroduces a real, previously-shipped bug.** It used to sit
  right after `switchTab()`'s definition, near the top of the script,
  and called `switchTab('version', btn)` → `loadVersion()` →
  `versionLoaded`/`vStartTime`, both `let`-declared much further down in
  the same script (in the "--- version tab ---" section). `let`
  declarations are hoisted but stay in the temporal dead zone until
  their declaration line actually executes, so referencing them earlier
  throws `Uncaught ReferenceError: Cannot access 'versionLoaded' before
  initialization` — silently breaking the Version tab (and everything
  that runs after the crash point in that top-level script block) on
  every page load that lands on `/setup#version` specifically. That URL
  isn't a rare edge case — it's what the dashboard's lightning-bolt
  update indicator links to, what the "Version info tab" link (General
  tab, added v3.19) navigates to, and what `waitForRestart()`'s
  `location.reload()` (added v3.20) lands back on after every successful
  self-update — so this bug fired on nearly every real update-checking
  interaction, reported as "update checking doesn't work" symptoms that
  were actually this crash, not a server-side/network problem. If you
  add another deep-link-style init block, put it after all `let`/`const`
  declarations in the script (the very end is simplest), not near the
  top next to related function definitions — function declarations
  hoist safely, `let`/`const` don't.

- **Backup/restore (v3.30) reuses each list's existing natural identity
  field as the merge key, rather than inventing a new one** — hotspot
  `ip`, favorite `call`, ASL favorite `node`, camera `id` — same fields
  `storage.py`'s own save functions and the dashboard's own lookups
  already treat as unique. "Add & update" mode builds a dict keyed on
  that field seeded from the current on-disk list, then overwrites with
  entries from the imported file, so an imported entry matching an
  existing one updates in place instead of duplicating. Settings has no
  such natural key — its "add & update" mode instead means `dict.update()`
  (only the keys present in the imported file get overwritten; anything
  the dashboard already has that isn't in the file is left alone), which
  is a different merge semantic from the four list-shaped categories and
  is called out explicitly in both the Settings UI copy and the README
  so it doesn't read as a bug.
- **Imported hotspots go through the same `asl_node.isdigit()` guard as
  the Settings-UI add/edit path** (`api_import_backup` in `app.py`),
  not just a schema/type check — `asl_node` gets shell-interpolated into
  an SSH command (`config.build_asl_status_cmd`), so a backup file with
  a hostile `asl_node` value (e.g. containing `; rm -rf /`) needs to be
  neutralized on import the same way it already is on manual entry, not
  just trusted because it came from a JSON file rather than a form
  field. Verified with a live `app.test_client()` import containing
  `"asl_node": "59929; rm -rf /"` — the field comes back `None` (stripped
  by `.pop()`), not the injected string.

- **The app is served by `waitress`, not Flask's own dev server (`app.run()`)
  — and it must stay single-process.** `app.py`'s `main()` owns a single
  global `FleetMonitor` (its own background SSH-polling threads),
  `camera_stream.py`'s per-camera ffmpeg workers, and `aprs_inbox.py`'s
  persistent APRS-IS socket, all as in-process singletons. A typical
  production WSGI setup (e.g. `gunicorn --workers N`) forks N separate
  processes, each of which would spin up its *own* independent copy of all
  of that — N× redundant SSH polling of every hotspot, N× redundant
  APRS-IS logins (real risk of getting rate-limited/kicked), N× redundant
  ffmpeg processes per viewed camera. `waitress.serve(app, ...)` has no
  multi-process/worker concept at all (only `threads=`, currently
  `config.WAITRESS_THREADS`), which was the deciding factor over gunicorn
  — there's no flag to accidentally misconfigure into duplicating this
  state. If a future change ever needs true multi-process scaling, the
  background-thread ownership model in `app.py`/`monitor.py`/
  `camera_stream.py`/`aprs_inbox.py` would need to move out of the web
  process entirely (a separate poller process publishing to shared
  storage) — don't just add `--workers` to the server invocation.
  `WAITRESS_THREADS` defaults to 16, well above waitress's own default of
  4, because each open camera MJPEG stream
  (`camera_stream.py`'s `stream()` generator) holds a thread for its
  entire viewing duration on top of normal dashboard polling from any
  number of browser tabs — confirmed by tracing `api_camera_feed()`'s
  `Response(camera_manager.stream(camera), mimetype="multipart/x-mixed-
  replace...")` before making the switch, not assumed. waitress has no
  built-in TLS/HTTPS support at all (confirmed by inspecting its actual
  `Adjustments` class, not from memory/docs) — if HTTPS is ever added,
  it's via a reverse proxy in front (Traefik/nginx/Caddy) terminating TLS
  and forwarding plain HTTP, using waitress's `trusted_proxy`/
  `trusted_proxy_headers` options so `request.remote_addr` reflects the
  real client IP rather than the proxy's.

- **Offline detection (card turns red + "OFFLINE" badge) deliberately reuses
  the existing SSH-failure tracking in `monitor.py` rather than adding a
  ping.** `_record_failure()` already set `status = "Offline"` after
  `config.FAILURE_THRESHOLD` consecutive SSH failures well before the UI did
  anything with it — confirmed by reading the code before building this,
  not assumed; the dashboard just never rendered that field. A dedicated
  ICMP ping would have been redundant work: SSH failing is a *stronger*
  signal than a failed ping (proves the SSH service itself is down, not
  just that the network stack doesn't respond), and it's already computed
  every poll cycle for free. `HotspotStatus.offline_since` (new field) is
  set once — guarded by `if status.offline_since is None` — the moment
  status first flips to `"Offline"`, so repeated failures don't keep
  resetting the clock; both `_check_one_wpsd`'s and `_check_one_asl3`'s
  success paths clear it back to `None` on the very next successful poll.
  Follows the same "epoch timestamp + browser-local `timerState` ticking"
  pattern already used for `tx_start`/`last_heard` in `dashboard.html`,
  just with its own `'off'` timer type and `fmtOfflineDuration()` (h/m/s
  rather than tx/lh's fixed `M:SS`, since an outage can plausibly run for
  hours/days unlike a single transmission).
- **Offline takes precedence over active/favorite tinting and the
  active-call/last-heard info block** in `renderCards()` — checked before
  `hs.is_active`, not after. If a hotspot drops mid-call, `is_active`/
  `active_call`/`tx_start` are still whatever they were the instant before
  it went offline (monitor.py never clears them on failure), so without
  this ordering the card would show a live-looking "📡 ACTIVE" call timer
  ticking away for a call that's actually long over, alongside a
  contradictory offline badge.
- **`.card-header`'s right-side badges (update badge, map pin, uptime) are
  grouped in a `.card-header-right` wrapper, not left as bare siblings of
  `.card-name`.** `.card-header` is `justify-content: space-between` —
  with exactly two children (name, one badge) that reads fine, but the
  original code had `${updateBadge}${mapPin}` concatenated directly with
  no wrapper, so a card with *both* present (a real, not hypothetical,
  combination) got 3 flex items spread across the full card width instead
  of clustering on the right — confirmed by actually reproducing it before
  fixing, not assumed. Any future badge added to that corner (uptime was
  the third) goes inside `.card-header-right`, never as a fourth bare
  sibling, or the spreading bug comes back.

- **DigiPi's Direwolf process is NOT a systemd service — assume it's a
  plain background process started by a shell script, not `systemctl`.**
  Confirmed against a real DigiPi (`ps aux` showed `/bin/bash -x
  /home/pi/direwolf.tnc.sh` → `direwolf ...`, and `systemctl list-units
  --state=running` didn't show it at all) before writing `digipi.py` —
  DigiPi's own docs don't mention this. It logs to `/run/direwolf.log`
  (not `/var/log/...`), which is only reliable because `direwatch.py`
  (bundled with DigiPi, drives the device's physical screen) already
  depends on that exact path staying put.
- **DigiPi's port-8055 web app is DigiPi's own custom "WebChat" UI
  (Flask/SocketIO), not the unrelated open-source `aprsd` project** —
  `systemctl status aprsd` returns "unit could not be found" on a real
  device even though the port matches aprsd's common default. Don't
  assume a port number implies a specific known project; this was
  checked directly (`curl` the page, read what it actually serves)
  before concluding anything about it.
- **`digipi.py`'s packet-line regex (`^\[([\w.>]+)\]\s+([\w-]+)>([^:]+):
  (.*)$`) was built against two real captured `/run/direwolf.log`
  excerpts (30 and 150 lines, spanning many repeats), not DigiPi's/
  Direwolf's own docs, which don't document this exact text format at
  all.** Confirmed three tag forms: `[0.N]` (heard directly on RF),
  `[ig]` (received from the APRS-IS internet feed), `[ig>tx]` (heard on
  RF, gated out to the internet) — plus non-packet lines that must NOT
  match (`DCD 0 = 0/1` carrier-detect toggles, `<CALL> audio level = ...`
  diagnostics + its 3-line "input too high" warning, blank lines). The
  regex's job is entirely to let packet lines through and let everything
  else fail to match — there's no separate exclusion list, so a change
  that makes the regex too permissive would silently start showing
  diagnostic noise as if it were packet data. Re-verify against a fresh
  real capture (not memory of the format) before loosening this pattern.
- **APRS position payloads (the `@`/`!`/`=`-prefixed text after the
  packet's `:`) are deliberately NOT decoded into lat/lon — shown as raw
  text.** The digipeater's own beacon uses APRS's compressed position
  format (e.g. `!R8\&Q<R\`C&`), which is real, fiddly binary-ish encoding
  this project hasn't verified the way `wspr_activity.py`'s
  `grid_to_latlon()` was verified against a real row before being
  trusted. Decoding it wrong would mean showing an incorrect position on
  a map — a worse failure than just not showing a position. Don't add
  position decoding here without the same real-data verification
  discipline used everywhere else in this file.
- **`direwatch.php`'s screen-mirror image needs no new backend code at
  all — confirmed by `curl`ing the real page before assuming otherwise.**
  It's a plain `<canvas>` that loads `/direwatch.png` (unauthenticated
  static image) and refreshes it via a cache-busted `img.src` every
  1000ms. `dashboard.html` just points an `<img>` at
  `http://{DIGIPI_IP}/direwatch.png?t=<timestamp>` directly from the
  browser, same "link straight to the device's own web UI" pattern
  already used for `href="http://${hs.ip}"` on WPSD cards — no proxying,
  no new route, same trusted-LAN assumption already documented elsewhere
  in this file.
- **DigiPi card packet callsigns link to aprs.fi, not QRZ (v3.87)** --
  shipped originally copying the QRZ-link pattern every other callsign
  in this app uses (Recent Contacts, hotspot card history, ASL
  favorites), but a heard *APRS packet* is a materially different
  context than a DMR/D-Star contact or a linked ASL node: what's
  actually useful here is that station's live position/track on
  aprs.fi, not a QRZ bio lookup. `renderDigipi()`'s packet row template
  just points at `https://aprs.fi/${escapeHtml(p.call)}` -- aprs.fi
  accepts a bare callsign (SSID included, e.g. `KC1ABC-9`) directly in
  the URL path, no API key or extra lookup needed for this link-only
  use (unlike `aprs.py`'s own aprs.fi API client, which resolves a
  position server-side and does need a key). Don't copy this pattern
  back onto QRZ-linked callsigns elsewhere in the app without the same
  "is this actually an APRS-heard station" reasoning -- QRZ is still the
  right destination for DMR/D-Star/ASL callsigns.

- **A WPSD/MMDVM "screen mirror" (DigiPi-style) for the small OLED HATs
  was explored and rejected as infeasible — not just unbuilt.** Checked
  live against a real WPSD hotspot (SSH): `/var/www/dashboard/admin/
  OLED_ajax.php` is a write-only power toggle (`i2cset ... 0xAE`/`0xAF`
  at I2C address `0x3c`), not a render/capture endpoint; `/dev/fb0`
  exists but is the Pi's stock HDMI framebuffer (`BCM2708 FB`,
  1280x720), unrelated to the OLED; no `ssd1306fb`/`fbtft` kernel module
  is loaded, so the OLED isn't exposed as a Linux framebuffer device at
  all — MMDVMHost bit-bangs it directly over I2C. On top of that, the
  SSD1306 controller's own datasheet documents that GDDRAM (its pixel
  memory) is only readable over the parallel 8080/6800 interface, never
  over I2C or SPI — a hardware limitation, not a missing driver, so this
  isn't a "someone hasn't written the code yet" gap. Unlike DigiPi's
  `direwatch.png` (confirmed servable with a live `curl` before writing
  any code), there is no live-tested path to actually read back OLED
  pixel content on this hardware class. A "recreate the display from
  already-polled data (mode/TG/RSSI/BER/callsign) styled to look like an
  OLED" version was mocked up and would be technically buildable, but
  the user disregarded the feature entirely rather than pursue that
  fallback — don't resurrect this without the user asking again, and if
  revisited, the I2C-readback wall above is the reason a true mirror
  specifically (not a recreation) is off the table.

- **openSPOT 4 (SharkRF) support (v3.38) was built entirely from a live
  protocol reverse-engineering session, not SharkRF's own published API
  docs.** SharkRF publishes `github.com/sharkrf/osp-http-api` (and
  `osb-http-api`/`osw-http-api` for openSPOT3/openSPOT2), but a real
  openSPOT 4 Pro returned a live 404 on that doc's documented
  `checkauth.cgi` endpoint — confirmed via `curl -v` against the real
  device (`Server: SharkRF httpsrv`, so definitely a real openSPOT, just
  a firmware generation the docs don't describe). Found the real API by
  watching the device's own web UI talk to itself in browser dev tools
  (Network tab, all requests, not just Fetch/XHR) rather than continuing
  to guess from mismatched docs — the same "verify against the real
  thing" discipline as DigiPi's `direwatch.png` discovery. Confirmed
  live: `GET /checktok` (not `checkauth.cgi` — this firmware drops the
  `.cgi` suffix and renames things) validates a JWT via
  `Authorization: Bearer`; live status/call data streams over a
  **WebSocket**, not polling (`ws://<ip>/<jwt>`, JWT in the URL path
  since browser JS can't set custom WS handshake headers,
  `Sec-WebSocket-Protocol: openspot4`) — found by noticing a Network-tab
  row whose *name* was literally the JWT string, which turned out to be
  a WS upgrade request. The `gettok`/`login` endpoints (how a fresh JWT
  is obtained) were never actually captured live — the browser session
  already had a JWT when capture started — so `openspot.py`'s `_login()`
  ports the older-gen docs' endpoint names as a starting guess; this
  turned out to be correct when tested against a real device (Settings
  → Hotspots → "Test openSPOT4" succeeded on the first real attempt),
  but re-verify with a live logout/login capture if it ever stops
  working, rather than assuming the names drifted for no reason.
- **openSPOT4's WebSocket message shapes were captured from real traffic
  during two actual calls on two different modes/connectors (DMR via
  Homebrew/BrandMeister, then C4FM/YSF via a YSF Reflector/"TGIF"
  profile), not inferred from field names.** `{"type":"status",...}`
  heartbeats (~1/sec) have empty `rssi_values_dbm`/`ber_values` arrays at
  idle, populated during a call — same for both modes. The two modes'
  human-readable `"log"` lines turned out to have genuinely DIFFERENT
  shapes: DMR emits `"dmrct: [0] grp voice call started, dst: <TG> src:
  <DMR ID> id: <hex>"` (with a `[N]` channel bracket and an explicit
  "started" line) and later `"dmrct: [0] call ended, dur <s>s ber
  <pct>%25 loss <pct>%25 rssi <dbm>"` (note the **literal `%25`** —
  confirmed real, not a decoding bug); C4FM/YSF only ever emits
  `"c4fmct: call ended, dur <s>s ber <pct>%25 loss <pct>%25 rssi <dbm>"`
  (**no `[N]` bracket at all**, and no equivalent "started" line was ever
  observed) plus a separate `"ysfref: call ended"` line from the
  connector itself. A first implementation parsed these `"log"` lines
  directly with mode-specific regexes and completely failed to detect
  C4FM/YSF calls at all (the bracket-requiring regex just never matched)
  — confirmed live as a real, reported bug ("card not picking up an
  active conversation"), not a hypothetical.
  **Fixed by switching to `"calllog"` messages as the universal call-
  lifecycle signal instead of the `"log"` text**: a `"calllog"` entry
  with `duration: 0.0` is a call starting; the SAME `"id"` reappearing
  later with a real (`> 0`) `duration` is that call ending, complete
  with its own `ber`/`loss`/`rssi` — confirmed to have this exact shape
  for BOTH modes tested, unlike the `"log"` lines which differ per mode.
  `"calllog"` entries also get periodically **rebroadcast** even after
  finalizing (the same `id`/`duration` pair re-appearing ~10s later,
  unchanged) — `openspot.py`'s `_finalized_call_ids` dedupes this so a
  rebroadcast doesn't re-trigger "call ended" processing repeatedly. The
  `"log"` lines are now only used opportunistically to learn the human
  mode name (via each mode's own `"<x>ct:"` prefix, `_MODE_LOG_RE`/
  `_MODE_BY_PREFIX`) — not for call state at all anymore.
  **Also discovered live**: DMR's `calllog.src` is a numeric DMR ID
  (needs the separate `"csd"` message to resolve a callsign, as before);
  C4FM/YSF's `calllog.src` is **already a real callsign** (`"KH7EH"`,
  not a DMR ID) — YSF addresses stations by callsign natively, so there's
  no equivalent `"csd"` resolution step for this mode. `openspot.py`
  distinguishes the two with a simple `src.isdigit()` check
  (`_apply_call_start`) and enriches directly via the shared
  `_enrich_caller()` helper when `src` isn't numeric, skipping the `csd`
  wait entirely. The `"homebrew: ignoring call from 4000 ended"`
  distractor (a differently-worded line for a call the device isn't
  relaying) is naturally irrelevant now too, since it was only ever a
  `"log"` line and calllog-driven tracking doesn't look at `"log"` text
  for state at all. **D-STAR/NXDN/P25 remain completely unverified** —
  neither their `calllog.src` shape (DMR-ID-like vs. already-a-callsign)
  nor their mode-log prefix has been tested against a real call.
- **`calllog.dst` arrives partially MASKED BY THE DEVICE ITSELF for at
  least some C4FM/YSF group calls** — confirmed live via a real user's
  WebSocket capture (2026-07): `"dst":"*****FEWaf/127"`, rendered on the
  dashboard as a garbled-looking "Linked: *****FEWaf/127". Confirmed this
  isn't a parsing bug on this app's side two ways: (1) the SAME openSPOT4's
  own free-text `"log"` message for the identical call already contains
  the identical masked text (`"log":"c4fmct: data call started, dst:
  *****FEWaf dgid: 127 src: 9Z3MG ..."`), so the masking happens before
  the data ever reaches this app; (2) cross-checked the masked fragment
  itself against that same device's own admin call-log page, which
  listed caller 9Z3MG as `"9Z3MG (Radio ID: FEWaf, ...)"` — i.e. `FEWaf`
  is the CALLER's own YSF Radio ID leaking into the `dst` field, not a
  destination/room name fragment, so showing it verbatim is actively
  misleading, not just cosmetically ugly. The one part of this field
  confirmed both real and stable is the trailing `/<n>`: YSF's DG-ID
  (Digital Group ID), spelled out explicitly in that same device log
  line (`dgid: 127`) — every masked sample captured (DG-ID 127 → "..FEWaf",
  DG-ID 0 → all-asterisks with nothing revealed) was consistent with this.
  Fixed via `openspot.py`'s `_clean_dst()`: if `dst` contains both `*` and
  `/`, show `"DG-ID <n>"` instead of the raw masked string; otherwise pass
  it through unchanged (a no-op for DMR, whose `dst` is a plain talkgroup
  number with neither character). If this ever needs revisiting -- e.g.
  if SharkRF's firmware changes what gets masked, or D-STAR/NXDN/P25 turn
  out to have their own version of this -- re-verify against a real
  WebSocket capture cross-checked with the device's own admin UI the same
  way, don't assume the masking pattern generalizes without checking.
- **openSPOT4 needed a manager shape closer to `camera_stream.py`'s
  `CameraStreamManager` than `aprs_inbox.py`'s `AprsInbox`, even though
  a persistent WebSocket connection sounds more like the latter.** The
  deciding factor: openSPOT4 hotspots are a list (like wpsd/asl3), so
  the app needs **N independent persistent connections**, not one
  global one — `aprs_inbox.py`'s generation-counter trick (for a stale
  thread to detect it's been superseded) isn't needed here since
  `OpenSpot4Manager.reconcile()` tears down a replaced/removed device's
  worker explicitly and synchronously (`worker.stop()`), the same
  add/diff/stop-old shape `CameraStreamManager` already uses for its own
  per-camera workers. A reconnect here is also heavier than
  `aprs_inbox.py`'s (which just reuses a static APRS-IS passcode): every
  reconnect redoes the full HTTP auth handshake (fresh token → digest →
  login → new JWT) before reopening the WebSocket, since a JWT can't be
  reused indefinitely the way an APRS-IS passcode can.
- **`FleetMonitor._apply_last_heard_ttl()` had to be explicitly wired
  into a new `_check_one_openspot4()`, not left as a no-op `pass`, or a
  last-heard caller would never clear on an openspot4 card.** This
  method is pure state-based aging logic (no log line triggers it) that
  every WPSD/ASL3 poll tick already calls regardless of whether new data
  arrived — easy to miss when a new hotspot type is push-based and the
  first instinct is "there's nothing to poll, so do nothing." By
  contrast, `config.ACTIVE_TIMEOUT`'s "stale log header" safety net is
  purely about SSH log-tail-window scrolling and genuinely doesn't apply
  here, since openSPOT4 gets explicit call-start/call-end push events
  with no tail window to scroll out of — don't port that one over if
  another push-based type is ever added.
- **An openSPOT4's admin password is NOT one fixed device-wide
  credential — each config profile can have its own separate password.**
  Confirmed live, the hard way: a hotspot configured and successfully
  tested against a real device started failing `Test openSPOT4` with a
  401 immediately after switching that device to a different profile
  (profile switches reboot the device — see `cpsettings`/profile-related
  entries below) — same IP, same password field in Settings, but the new
  profile simply required a different password. There's no API to query
  or auto-detect which profile is active (and no way to know a password
  without the user supplying it), so this isn't solved by picking the
  "right" one automatically — instead `openspot.py`'s `_collect_passwords()`
  gathers the primary `pass` field plus an optional multi-line
  `openspot4_extra_pass` (Settings' "Additional profile passwords"
  textarea) into an ordered list, and `_login_any()` tries each in turn
  at login/reconnect time until one authenticates, since only one
  profile is ever active on a given physical device at once. A 401 after
  trying every stored password (`_describe_login_error()`) explicitly
  says how many were tried and that they were all rejected, rather than
  a bare `HTTP Error 401: Unauthorized` — if a card is stuck Offline
  after a profile change and this message appears, the fix is adding
  that profile's password to the textarea, not a network diagnosis.
- **A repeated `httpsrv-ws: [2] websocket opened with token ...` line in
  the openSPOT4's own device-side log (roughly every 20 seconds) turned
  out to be completely unrelated to this app's connection — it was
  SharkRF's own browser-based admin web UI reconnecting, not
  `openspot.py`.** This took three rounds of live investigation to
  narrow down, worth recording in full since each earlier hypothesis
  was plausible and each was wrong:
  1. First hypothesis (from the device's own UI showing "connection is
     used from another location" after this dashboard connected): the
     device only tolerates one active session — corrected by a real
     device-side log export showing multiple numbered WebSocket slots
     (`[0]`, `[2]`) open simultaneously, a limited pool, not a strict
     single-session rule.
  2. Second hypothesis: a self-inflicted 20-second drop/reconnect loop
     in `openspot.py` itself, since 20s is exactly
     `OPENSPOT4_RECV_TIMEOUT` (10s, original default) +
     `OPENSPOT4_RECONNECT_BACKOFF` (10s) — bumped the timeout default to
     60s on this theory. **This turned out to be the wrong fix for the
     wrong problem**: after confirming the 60s value was actually
     deployed (checked live, `docker exec ... grep
     OPENSPOT4_RECV_TIMEOUT /app/config.py` on the running container,
     not just the git source dir), the exact same ~20s pattern kept
     appearing in fresh device-log captures — proving the recv-timeout
     theory false, not just unconfirmed.
  3. The actual resolution: asked directly whether the **dashboard
     card** itself was flickering Offline during this same window. It
     wasn't — it stayed solidly Online throughout every capture. The
     repeated `"opened"` log lines were the user's own browser tab open
     against the device's admin UI reconnecting on its own (unrelated to
     this app entirely), not `openspot.py`'s connection.
  The `OPENSPOT4_RECV_TIMEOUT=60` bump from step 2 was kept anyway (a
  reasonable, harmless tolerance increase either way) but should NOT be
  cited as "the fix" for this symptom, since there was nothing on this
  app's side to fix. **Lesson for next time a device-log line looks
  alarming**: check whether the *dashboard card itself* is actually
  unstable before assuming a raw device log line implicates this app's
  connection — a device can log plenty of activity that has nothing to
  do with any specific client.
- **The literal `%25` originally documented as "a confirmed real
  firmware quirk" in openSPOT4's `"log"` messages is more precisely a
  WebSocket-transport artifact, not something in the device's real log
  text.** A raw on-device log export (distinct from the browser-captured
  WebSocket JSON) shows the actual stored log line as `"c4fmct: call
  ended, dur 0.7s ber 0.3% loss 0.0% rssi -48"` — a plain `%`, never
  `%25`. Something specifically in the path from internal log to
  WebSocket JSON payload double-encodes it. No code change needed
  (`openspot.py`'s regexes correctly match what actually arrives over
  the WebSocket, which is `%25`, and that's the only thing this app ever
  parses) — this is purely a documentation correction so a future reader
  doesn't conclude the device's *real* logs contain `%25`, only what's
  serialized into the WS JSON does. Also seen in that same raw log: a
  third log-line format for the same event (`"ysfref-c4fm: call
  started"`/`"call stopped"`), and much more verbose per-packet lines
  (`"c4fmct: got header"`/`"got terminator"`) that never appear over the
  WebSocket at all — confirms the WS `"log"` feed is a filtered subset
  of a far more verbose internal log, not the complete picture.
- **openSPOT4's "Active config profile" display was investigated and its
  data source was NOT found, despite thorough live network capture.** A
  full fresh-page-load capture (dev tools open before the reload, "All"
  filter, not just Fetch/XHR) showed no dedicated profile-fetching
  request at all — just the known `gettok`/`login`/`checktok`/WS/static-
  asset requests. The WebSocket's own Messages tab was also checked for
  a one-time startup message and came up empty. Most likely explanation:
  the profile name is server-side rendered directly into the initial
  HTML/JS for that page, not fetched via any separate API call — getting
  it would mean scraping the device's own admin HTML rather than calling
  a documented JSON endpoint the way every other piece of this
  integration does. Deliberately NOT pursued for that reason (same
  cost/fragility call as the WPSD OLED mirror) — if revisited, start by
  viewing the raw page source of that specific admin page rather than
  the network tab, since the network capture already ruled out a
  separate request.
- **This session also incidentally confirmed the `gettok`/`login`
  endpoint names `openspot.py`'s `_login()` had only guessed from
  older-gen docs** — a later, unrelated dev-tools capture (while
  chasing the profile question above) caught a full fresh page load
  including both `gettok` and `login` requests by name, both real and
  both matching the guessed paths exactly. What was previously flagged
  as "ported from older-gen docs, unverified" is now directly confirmed
  live, not just indirectly (via a successful login) inferred.
- **The Big Ass Clock card (v3.44) is the second card with zero backend
  module** (Band Plan was the first) — everything is `Date`/
  `Intl.DateTimeFormat` in `dashboard.html`'s own script block, ticking
  on a plain `setInterval(renderClock, 1000)`, gated by `SHOW_BIG_CLOCK`
  the same way `fetchHfConditions`/`fetchDigipi` etc. gate their own
  polling. Style/12-hour-format/second-clock/timezone are `localStorage`
  only, never `settings.json` — same split as map style/grey-line
  default, since none of it needs to sync across devices viewing the
  same dashboard. The TIX style is a mockup-stage interpretation from
  general recollection, **not verified against a specific real TIX
  clock product** — each digit is a dot grid sized to what that digit
  actually needs (hour tens 0-1 → 3 dots; minute/second tens 0-5 → 6
  dots in a 2x3 grid; every units digit 0-9 → the full 3x3 grid), TIX is
  always 12-hour and never shows a second clock, kept deliberately
  simple/glanceable rather than configurable. If a reference photo of a
  specific real product ever surfaces, re-check the dot-grid layout
  against it rather than this recollection.
- **ADIF-imported QSOs (v3.44) each get a line back to the QTH they were
  worked from, not just a bare pin.** Per-QSO priority: that specific
  record's own `MY_GRIDSQUARE` field first (handles a portable/rover log
  where the operating location changes between QSOs — confirmed this is
  a real per-QSO ADIF field, not just a header field, by testing a
  multi-record log with different `MY_GRIDSQUARE` values per record),
  falling back to settings' `station_grid` otherwise. A QSO with neither
  gets `qth_lat`/`qth_lon` omitted entirely (`app.py`'s
  `api_import_adif`) and is plotted as a bare pin with no line, same as
  before this feature existed — don't force a fallback that would draw a
  misleading line to nowhere. The lines live in their own plain
  `qsoLinesLayer` (`L.layerGroup()`), not inside `qsoCluster`
  (`L.markerClusterGroup()`) — a MarkerClusterGroup only ever clusters
  point markers, so polylines don't belong inside one. Both layers
  toggle together under the same "Show on map" checkbox.
- **`wsjtx.py` (v3.45)'s WSJT-X UDP message parser was verified against
  bmo/py-wsjtx's real, working open-source implementation
  (github.com/bmo/py-wsjtx/blob/master/pywsjtx/wsjtx_packets.py) before
  writing this, field-for-field — not from memory, not from WSJT-X's own
  C++ source comments alone.** Confirmed exact byte layout: header is
  `magic(u32) schema(u32) pkt_type(u32)`, every packet type then reads
  `id` as a length-prefixed UTF-8 QString, and `QSOLoggedPacket` (type 5)
  is `datetime_off(QDateTime) call grid frequency(i64) mode report_sent
  report_recv tx_power comments name datetime_on op_call my_call my_grid
  exchange_sent exchange_recv` in that exact order. Verified by hand-
  building synthetic packets matching this byte layout and confirming
  `parse_packet()` round-trips them correctly (call/grid/band/mode/date/
  my_grid all extracted right, including the Julian-day-to-calendar-date
  math ported from py-wsjtx's own `JDToDateMeeus`) — same "verified
  against hand-built sample data" discipline as `adif.py`'s tokenizer.
  **This module was NOT live-tested against a real running WSJT-X
  instance** (none reachable from this dev environment) — if it ever
  silently stops picking up QSOs, re-verify against a real packet capture
  before assuming the parser is still correct, the same "verify against
  the real thing" discipline as openspot.py/digipi.py.
- **Deliberately only handles the `QSOLogged` message type (5), not the
  much chattier `Decode` message (every single decode attempt, most
  without a usable grid square, several per second across a wide
  waterfall).** Streaming every decode would reproduce exactly the
  "worldwide spotter clutter" the WSPR spots map overlay was removed for
  a few releases earlier in this same document. `QSOLogged` fires once
  per actually-logged QSO with clean structured fields — functionally one
  ADIF record's worth of data, arriving live instead of in a bulk file,
  so `wsjtx.py`'s `_handle_qso` reuses the exact same grid-square-first/
  `monitor.lookup_caller_info()`-fallback position resolution as
  `app.py`'s `api_import_adif`, including the QTH-line logic (WSJT-X's
  own `my_grid` field takes priority over settings' `station_grid`, not
  the other way around — it's the live grid WSJT-X itself is configured
  with, which could legitimately differ from this dashboard's
  `station_grid` on a portable/rover setup).
- **Live-logged QSOs use `storage.append_qso()` (read-modify-write, one
  QSO at a time), not `save_qsos()` (wholesale replace) that the bulk
  ADIF importer uses.** Both write to the same `qsos.json` under the same
  `_file_lock`, so a live QSO landing mid-request from `/api/import_adif`
  or `/api/clear_qsos` can't interleave with either. This does mean a new
  ADIF import or "Clear imported log" click also wipes out any
  WSJT-X-logged QSOs accumulated so far — a deliberate simplification
  (one unified list, not two tracked separately) rather than an
  oversight; don't build separate storage for the two sources unless a
  real complaint about this surfaces.
- **The Live map's QSO layer used to be fetched once at map init only
  ("the data only changes on explicit import/clear," a comment that was
  true before this feature existed) — now it's also polled every 20s**
  (`setInterval(fetchQsos, 20000)` near `qsoCluster`'s init in
  `dashboard.html`), since a live WSJT-X QSO can append to `qsos.json` at
  any moment, not just on an explicit user action. Cheap either way — it's
  a local JSON file read, not a third-party API call.
- **`wsjtx.py`'s UDP listener needed an explicit port PUBLISHED from the
  Docker container, not just the app-level Settings toggle -- shipped in
  v3.45 without this, a real gap caught on review, not before.** Docker's
  bridge network (what `docker-compose.yml`/`docker-update.sh`/
  `unraid-template.xml` all use) forwards NOTHING into the container
  unless a port is explicitly published -- only TCP 5000 (the web UI) was
  published before this was caught, so WSJT-X packets sent to the
  Unraid box's IP:2237 were silently dropped at the Docker network
  boundary, never reaching the socket inside the container, regardless of
  the `wsjtx_enabled`/`wsjtx_port` Settings fields being configured
  correctly. Fixed by adding `-p 2237:2237/udp` (`docker-update.sh`),
  `"2237:2237/udp"` (`docker-compose.yml`), and a matching `Type="Port"
  Mode="udp"` `<Config>` (`unraid-template.xml`) -- same "keep the
  container config descriptions in sync, nothing does it automatically"
  caution already documented above for the WebUI port. If the in-app
  `wsjtx_port` Settings field is ever changed away from 2237 on a Docker/
  Unraid install, the container's port mapping needs updating to match,
  or the feature silently stops receiving anything with no error
  surfaced anywhere -- a standalone Pi/systemd install has no such
  gap, since the process binds directly to the host's network stack and
  whatever port is picked in Settings just works immediately.
- **Per-hotspot map colors (v3.46) is a deliberate trade-off, not a pure
  improvement -- called out explicitly to the user and confirmed before
  building, mockup-first (`map-colors-mockup.html`), same workflow as
  every other UI feature this session.** The old scheme (`#2ecc71`
  green = active caller anywhere, `#5bc0de` blue = recent, fixed purple
  for every "Your hotspot" marker) gave one glanceable fleet-wide signal
  ("is ANYTHING active right now") but couldn't distinguish which
  hotspot a caller came in through. The new scheme
  (`HOTSPOT_COLOR_PALETTE`, cycled per hotspot in `/api/map_data`'s own
  node order, applied to that hotspot's own marker + every caller pin
  heard through it + its distance line) answers "which hotspot" instead,
  moving active-vs-recent to MOTION (`.caller-pin-active`'s expanding
  `hotspotActivePing` box-shadow ring vs. `.caller-pin-recent`'s plain
  dimmed opacity) rather than color. If a future ask wants BOTH signals
  back (fleet-wide "anything active" AND per-hotspot identity), that's a
  bigger design problem (two independent visual channels needed
  simultaneously), not a small tweak to this scheme.
- **QSO pins (ADIF import + live WSJT-X logging) lost their per-band
  color scheme in the same change** -- `QSO_BAND_COLORS` (11 band-keyed
  hex colors) was deleted entirely and replaced with one fixed
  `QSO_COLOR` (`#ff3fa4`, a hot pink deliberately not used anywhere else
  on the map), so "is this HF/FT8 traffic or repeater traffic" is a
  single-glance answer independent of which hotspot colors happen to be
  assigned. Band is still shown in each pin's tooltip text -- only the
  color encoding was dropped, not the data itself. Don't resurrect
  per-band QSO coloring without re-solving how it'd coexist with
  per-hotspot coloring using the same color space (they'd collide, e.g.
  `HOTSPOT_COLOR_PALETTE`'s green/blue/orange overlap with band colors
  a prior version used).
- **The bottom-left "Map key" (`#map-key`, `renderMapKey()`) is rebuilt
  from scratch on every call, but is now the ONE function allowed to
  write `#map-key`'s innerHTML, not just something `refreshMap()` calls.**
  Originally only `refreshMap()` called it (with fresh `nodes`/
  `hotspotColors` args each time); once POTA/PSK Reporter overlays needed
  their own key rows toggled independently of the hotspot poll cycle,
  `renderMapKey()` was changed to cache its last `nodes`/`hotspotColors`
  in `lastMapNodes`/`lastHotspotColors` (only overwritten when called
  WITH args) and to read the POTA/PSK checkboxes' live `.checked` state
  directly on every call, args or not. `togglePota()`/`togglePsk()`/
  their `update*()` counterparts all call `renderMapKey()` with NO args
  now, relying on those cached values -- if this function had stayed
  "always needs nodes+hotspotColors passed in," the toggle handlers would
  have needed to re-fetch `/api/map_data` just to redraw a legend, or
  there'd have been two independent writers racing to rebuild the same
  DOM node (whichever poll cycle fired last would silently clobber the
  other's row). Don't add a third direct writer to `#map-key` -- route
  any new legend row through this same function.
- **PSK Reporter's `flowStartSeconds` means something different on each
  side of the same query** -- confirmed by reading a real response
  against a real request, not assumed from the field name alone. As a
  REQUEST parameter it's a negative relative offset ("how many seconds
  of history to fetch", e.g. `-1800` for the last 30 min). In the
  RESPONSE, each `<receptionReport>`'s `flowStartSeconds` attribute is an
  ABSOLUTE Unix epoch (confirmed by comparing it against the response's
  own `currentSeconds` attribute, which matched wall-clock "now" at
  request time). `psk_reporter.py`'s `_fetch()` sends the negative form
  and stores the absolute form as `heard_at` -- don't reuse one variable
  name across both without re-reading which side of the call you're on.
- **PSK Reporter's rate limit is real and was hit within minutes of
  starting to test it, not just a documented suggestion.** A handful of
  test queries (varying `senderCallsign`/window) from the same dev-
  environment IP a couple of minutes apart got a real
  `{"message": "Your IP has made too many queries too often..."}` JSON
  error back -- notably JSON, even though every successful response is
  XML. `psk_reporter.py`'s `CACHE_TTL` (600s) is set with real margin
  above their documented "no more than once every five minutes" guidance
  specifically because of this -- don't lower it to "feel more live"
  without expecting the shared IP (every dashboard instance behind the
  same egress IP, e.g. a whole household) to eventually get blocked.
- **POTA's spot feed needed zero position resolution** -- confirmed live
  that `api.pota.app/spot/activator` returns `latitude`/`longitude`
  directly on every spot, unlike PSK Reporter (grid square, needs
  `grid_to_latlon()`) or SOTA (considered, not built -- its spots carry
  only an association+summit code, no coordinates at all; getting a
  position would need a SECOND lookup per distinct summit against
  `api-db2.sota.org.uk/api/summits/{assoc}/{code}`, confirmed live but
  never implemented since SOTA was explicitly deprioritized). If SOTA is
  ever revisited, that second-lookup requirement is real and should be
  cached indefinitely per summit code (a summit's location never
  changes), not re-fetched like a live spot.
- **The first-run empty state + one-time feature tour (v3.48) had exactly
  one hard requirement: never surprise an existing install.** A naive
  "add a new settings key defaulting to False, show the tour when it's
  False" design fails this completely -- `storage.py`'s `load_settings()`
  merges `dict(config.DEFAULT_SETTINGS)` with whatever's actually saved
  (`merged.update(saved)`), so ANY existing `settings.json` that predates
  this feature is simply missing the key and would inherit whatever
  static default `DEFAULT_SETTINGS` gives it -- there's no way, from a
  single static default value alone, to tell "genuinely fresh install"
  apart from "existing install upgrading to this version," because both
  cases hit the exact same merge code path with the exact same missing
  key. The actual fix uses `load_settings()`'s OTHER branch -- the one
  that already existed for a completely different reason (`if not
  os.path.exists(config.SETTINGS_FILE): return dict(config.DEFAULT_SETTINGS)`,
  i.e. this specific install's settings.json has literally never been
  saved, not even once) -- as the one genuinely one-time, reliable
  fresh-vs-existing signal. `config.DEFAULT_SETTINGS['onboarding_tour_seen']`
  is `True` (safe default for the "existing file merge" case); the
  "file doesn't exist at all" branch explicitly overrides it to `False`
  only in that one code path. Verified with three live `test_client()`
  scenarios before considering this done, not just reasoned about: (1) a
  totally fresh `CONFIG_DIR` -> `onboarding_tour_seen` comes back `False`;
  (2) a `CONFIG_DIR` pre-seeded with a real `settings.json` (containing
  unrelated real keys, no `onboarding_tour_seen` at all, simulating an
  upgrade) -> comes back `True`, tour never fires, and every pre-existing
  setting in that file is untouched; (3) the same pre-seeded scenario
  with hotspots.json also pre-seeded -> still `True`. Don't touch this
  default-flip logic without re-running an equivalent live check -- this
  is exactly the kind of thing that looks obviously correct on a read-
  through and is subtly backwards in practice.
- **The empty-state card lives INSIDE `.cards-grid` (`grid-column: 1/-1`),
  not as a replacement for it**, specifically so it doesn't hide any
  always-on extra card (Band Plan, License Quiz, Big Ass Clock, etc.)
  that doesn't depend on hotspots at all and might already be enabled
  even with zero hotspots configured. `checkOnboarding()` (called from
  `renderCards()`, which already receives the hotspot array from every
  `/api/data` poll) is the only thing that toggles its visibility --
  there's no server-side hotspot check on the `/` route at all, since
  `dashboard.html` has never received a `hotspots` template variable in
  the first place (unlike `setup.html`) -- the entire card grid, and now
  this too, is client-driven from `/api/data`'s own array length.
- **`/setup#<tabname>` deep-linking was generalized from a `#version`-only
  special case to any tab**, reusing the exact same TDZ-safety
  requirement documented elsewhere in this file (must run after every
  `let`/`const` the tab code touches) -- the empty-state card's "Add your
  first hotspot" button links to `/setup#hotspots` and "Restore from a
  backup" links to `/setup#backup`, neither of which worked before this
  generalization (only `#version` was ever wired up). The generalized
  version degrades safely for an unrecognized hash (`document.getElementById('tab-btn-'+tabName)`
  returns null, the `if (btn)` guard just no-ops) rather than throwing.
- **A real, reported bug: "card position doesn't save" (first seen on Big
  Ass Clock, then reported as APRS Messages "persisting first" even
  after two rounds of fixes) had THREE layered causes.** The first two
  fixes were each individually correct and each independently verified
  live, but neither was the actual dominant cause -- `/api/settings` was
  persisting every `*_position` value correctly at every single stage of
  this investigation; all three bugs were purely in how a saved value got
  RENDERED (or, for #3, in what value there was to render at all).
  1. *Tie-break disagreement* (minor): `setup.html`'s Cards-tab drag-list
     template broke an EXACT position tie by fixed template source order,
     while `dashboard.html`'s `computeCardOrders()` broke the identical
     tie ALPHABETICALLY BY KEY NAME (`a.key.localeCompare(b.key)`) --
     confirmed live the two files disagreed for a real tie. Fixed by
     making the JS sort rely on `Array.prototype.sort`'s ES2019+
     stability guarantee instead, and keeping `renderCards()`'s
     `sentinels.push()` order in sync with `setup.html`'s declared order.
  2. *Overflow collapse* (bigger, but still not the dominant cause):
     both files independently clamped/checked each sentinel's position
     against `data.length`/`hotspots|length` -- ANY position at or past
     the hotspot count got treated as one bucket, discarding the actual
     numeric value beyond that threshold. Confirmed live with 1 hotspot:
     positions 9/3/5 all collapsed to "after the 1 hotspot," rendering in
     declared order regardless of their real relative values. Fixed by
     sorting on the real (unclamped) position and only merging genuinely-
     overflowing sentinels together at insertion time.
  3. **The actual dominant cause, only found after the first two fixes
     still didn't resolve the reported symptom**: `saveCardOrder()`'s
     `sentinelIndex(key)` computes a card's position as "count of REAL
     HOTSPOT rows preceding it" by filtering every OTHER sentinel out
     before computing the index. This means ANY two-or-more cards dragged
     to the SAME side of every hotspot (the ordinary case with few
     hotspots and several extra cards -- confirmed live with a hand
     trace: 2 hotspots + 3 extra cards all placed after both, in ANY
     relative order, ALL THREE compute the identical saved position, no
     matter how they were actually dragged) -- a single "count of
     preceding hotspots" integer genuinely cannot represent relative
     order among siblings that share a hotspot boundary. This is why
     APRS Messages specifically kept "winning" no matter how many times
     it was dragged and saved: with few hotspots, it and several other
     enabled cards all save the exact same position, and the earliest-
     declared one in `_SENTINEL_DEFS` always wins that tie, REGARDLESS of
     how fixes #1/#2 render ties -- there was no distinguishing
     information left to render correctly by the time it reached them.
     Fixed with a genuinely new mechanism, not a rendering tweak: a new
     setting, `card_order_tiebreak` (list of `"__key__"` ids in last-
     dragged order, default `[]`), computed by `saveCardOrder()` as
     `allIds.filter(isSentinel)` (the full sentinel-only sub-order,
     capturing relative order directly instead of trying to derive it
     from a lossy hotspot-count encoding) and used as an explicit
     SECONDARY sort key in both `app.py`'s `_overflow_sentinels()` and
     `dashboard.html`'s `computeCardOrders()` (via `SENTINEL_KEY_TO_DATA_IP`,
     since the two files use different naming schemes for the same
     cards -- short JS keys like `bigclock` vs. drag-list ids like
     `__big_clock__`). Deliberately additive/non-breaking: an empty
     tiebreak (every existing install's actual state on upgrade, and any
     card never yet explicitly reordered against a same-boundary sibling)
     falls through to exactly today's `_SENTINEL_DEFS`-order behavior via
     stable sort, so this never silently reorders anyone's existing setup
     -- it only takes effect the next time `saveCardOrder()` actually runs.
  **If either file's sentinel declaration order (`_SENTINEL_DEFS` in
  `app.py`, the `sentinels.push()` sequence and `SENTINEL_KEY_TO_DATA_IP`
  in `dashboard.html`) ever changes again, it must change in both places
  together** -- these are independent descriptions of the same ordering,
  same class of gotcha as `unraid-template.xml`/`docker-update.sh`
  needing to stay in sync elsewhere in this file, except here a drift is
  silent (no error, just a wrong-looking card position) rather than loud.
  The INLINE (interleaved-with-hotspots) rendering in `setup.html` was
  deliberately left untouched throughout all three fixes -- it already
  respects individual position values correctly for positions that fall
  WITHIN the hotspot range (each hotspot slot can only ever hold one
  sentinel's exact position match); only the overflow/same-boundary case
  had these bugs. **Lesson for next time a "fix" doesn't resolve a
  reported symptom**: re-verify the ACTUAL reported scenario live (exact
  hotspot count, exact drag sequence) rather than assuming the most
  recent fix was sufficient -- two real, independently-correct fixes
  shipped here before the true root cause was found, each verified in
  isolation but neither actually reproducing (or fixing) the user's real
  configuration.
- **The true final layer of the card-order saga above wasn't in the
  ordering logic at all -- it was a genuine concurrent-write race in
  `/api/settings` itself, affecting far more than card order.** Even
  with `card_order_tiebreak` correctly computed and sent,
  `setup.html`'s `saveCardOrder()` fires MANY separate `/api/settings`
  POSTs in one `Promise.all(saves)` call (one per changed position field
  plus the tiebreak) -- genuinely concurrent from the server's point of
  view, not sequential. The old `/api/settings` POST handler did
  `settings = load_settings(); settings[key] = value; save_settings(settings)`
  with NO lock spanning that whole sequence (`storage.py`'s `_file_lock`
  only ever wrapped the individual file read or individual file write,
  never the Python-level modification in between). Two concurrent
  requests could each read the identical stale snapshot before either
  had written back, then each save their own full dict -- whichever
  wrote LAST won and silently discarded the other's change in its
  entirety, not just conflicting fields. **Confirmed by directly
  reproducing it, not by inspection**: firing 3 concurrent
  `/api/settings` POSTs (mirroring `saveCardOrder()`'s real pattern) via
  `threading.Thread` against a live `test_client()` reliably lost 2 of
  the 3 updates, reverting them to their defaults -- reproduced 100% of
  the time before the fix, 0% of the time after (10/10 trials). This is
  exactly why the two EARLIER, individually-correct-and-verified fixes
  above didn't resolve the reported symptom: it didn't matter how
  correct the tiebreak computation was if the concurrent save requests
  were clobbering each other's writes before the value ever reached
  disk intact. Fixed by making `storage._file_lock` an `RLock` (not a
  plain `Lock` -- needed so the same thread can safely re-enter it while
  already holding it) and adding `storage.settings_transaction()`, a
  context manager wrapping the ENTIRE load-modify-save sequence, not
  just the file I/O. `app.py`'s `/api/settings` POST handler now calls
  `settings_transaction().__enter__()`/`__exit__()` manually around its
  existing ~150-line field-handling body, specifically to avoid
  re-indenting that whole block for a `with` statement (a real,
  deliberate tradeoff -- a stray exception between those two calls would
  leak the lock and deadlock every future settings save, which the
  existing per-field `try/except ValueError` guards on numeric fields
  make unlikely in practice, but isn't literally impossible; revisit if
  this class of bug ever recurs). Rebuild calls (`_rebuild_qrz_client()`
  etc.) deliberately happen AFTER releasing the lock, since they don't
  touch `settings.json` and some (MQTT reconnect) can take a moment --
  no reason to hold up other concurrent `/api/settings` requests for
  that. **Any other future feature that fires multiple parallel
  `/api/settings` calls is protected by this same fix automatically** --
  the bug was in the shared save path, not anything specific to card
  ordering.

- **HamAlert notification card (v3.50, `hamalert.py`) was built entirely
  from real third-party sources, not HamAlert's own docs -- HamAlert
  publishes none for its Telnet interface.** Its own maintainer confirmed
  on the support forum (forum.hamalert.org/t/documentation-for-telnet-
  interface/682) that the only supported commands are `sh/dx N` and
  `set/json`, plus one undocumented `echo foo` (echoes `foo` back --
  used here as a keepalive, since there's no other heartbeat). The login
  handshake (send `username\r\n` then `password\r\n`, no prompt-waiting
  or matching needed) was confirmed against a real, working third-party
  integration (WhiskeyTangoHotel's Pimoroni Galactic Unicorn project,
  whiskeytangohotel.com/2023/05/hamalertorg-integration-with-pimoroni.html)
  rather than guessed from classic packet-cluster login conventions.
  `set/json` switches the session to one JSON object per matched spot;
  confirmed field shape (`callsign`, `fullCallsign`, `band`, `mode`,
  `frequency`, `spotter`, `source` -- one of dxcluster/rbn/pota/sota/
  wwff/pskreporter -- `dxcc`, `entity`, `comment`, `triggerComment`) from
  that same source plus HamAlert forum discussion. **This module was NOT
  live-tested against a real HamAlert account** (none available in this
  dev environment) -- if it ever fails to connect or silently stops
  showing alerts, re-verify the login sequence and JSON shape against a
  live account/packet capture before assuming the parser is still
  correct, same discipline as openspot.py/digipi.py/wsjtx.py.
- **HamAlert's Telnet destination was chosen over its URL GET/POST
  webhook alternative specifically because the webhook would require
  this dashboard to accept a public inbound connection** -- the first
  such requirement anywhere in this app; every other integration either
  polls outward (QRZ, RadioID, Brandmeister, PSK Reporter, POTA) or
  listens only on the local network (WSJT-X UDP, openSPOT4's WebSocket
  is initiated outbound by this app too). Telnet keeps that invariant
  intact: `HamAlertListener._run()` opens the connection outbound to
  `hamalert.org:7300`, same shape as `aprs_inbox.py`'s APRS-IS
  connection.
- **An openSPOT4-style admin-password-per-profile gotcha does NOT apply
  here** -- HamAlert credentials are one fixed username/password per
  HamAlert account, not per physical device/profile, so
  `hamalert.py` has no equivalent of `openspot.py`'s
  `_collect_passwords()`/multi-password-textarea pattern. Don't add one
  without a real reported case of it being needed.
- **Since March 2024, HamAlert has a dedicated Telnet password
  (hamalert.org → Destinations), separate from the main website/app
  login -- confirmed directly from the developer's own forum post, not
  assumed.** HB9DQM (forum.hamalert.org/t/api-key-generation-limited-
  access/683): "I have just added the option to set a separate Telnet
  password on the Destinations page. This can only be used to login to
  the Telnet interface, not to the website or app." The original
  implementation's Settings field was just labeled "HamAlert password,"
  which risked someone typing their actual account login there instead
  -- fixed by relabeling it "HamAlert Telnet password" plus explanatory
  text linking to the Destinations page. `hamalert.py` itself needed NO
  code change for this -- `configure()`/`test_connection()` already just
  pass through whatever password is supplied; this was purely a
  Settings-UI-copy gap, not a protocol bug.
- **`HamAlertListener.test_connection()` (Settings "Test connection"
  button) has no documented explicit login-accepted/-rejected signal to
  key off of** -- HamAlert's Telnet interface, per its own forum, only
  documents `sh/dx N`/`set/json`/`echo`, nothing about a login response.
  The heuristic used instead: open a fresh one-off connection, log in,
  and see whether the server closes the socket within a few seconds
  (`chunk == b""`) vs. leaves it open. **This heuristic itself is
  unverified against a real invalid-login case** (no real account
  available in this dev environment) -- if it ever reports a false
  positive/negative, re-check against a live account before assuming
  the heuristic is wrong, not just the credentials.

- **A real, previously-unnoticed production bug: hotspot cards' "Last heard"
  timer showed a nonsense number like "495860h" instead of an elapsed time.**
  Found while eyeballing the beta redesign against real data (a D-STAR
  contact's "Last heard" line) -- NOT a bug introduced by the redesign
  itself, since `dashboard_beta.html` started as a byte-for-byte `cp` of
  `dashboard.html` and neither `fmtAgo`/`fmtTx` was touched; confirmed the
  identical bug was already live in production `dashboard.html` before
  fixing either file. Root cause: **two functions named `fmtAgo` existed in
  the same top-level script scope with incompatible signatures** --
  `fmtAgo(ts)` (hotspot cards' "Last heard" timer, takes an absolute Unix
  timestamp) and a second `fmtAgo(secs)` added later for PSK Reporter's map
  tooltips (takes an already-computed elapsed-seconds duration). JS lets a
  later `function` declaration in the same scope silently redeclare/replace
  an earlier one with the same name -- no error, no warning -- so the PSK
  one always won, and every hotspot card's `fmtAgo(hs.last_heard)` call ran
  a ~1.7-billion-second Unix timestamp through `secs / 3600` math meant for
  a duration, producing exactly this kind of huge bogus "hours" figure.
  Fixed by renaming the PSK-specific one to `fmtAgoCompact` and updating its
  one call site -- not by touching the hotspot-card version, which was
  always correct. **If you ever see a `function` declared twice with the
  same name in this file's single big `<script>` block, that's this same
  class of bug waiting to happen again** -- there's no linter catching it,
  so a quick `grep -n "^function <name>("` sanity check is worth doing
  after adding any new same-named helper, the same discipline that caught
  this one.

- **`dashboard_beta.html`'s RSSI/BER S-meter bars deliberately treat the two
  values differently -- researched, not guessed, before picking a scale for
  either.** MMDVM's own calibration docs
  (github.com/g4klx/MMDVM/wiki/MMDVM-Calibration) confirm RSSI-to-dBm
  mapping comes from a per-device `RSSI.dat` calibration file generated
  against a reference radio (MMDVMCal's "S mode") -- a real calibration
  file checked directly (`RSSI_gm340uhf_RA4NHY.dat`) confirms this
  explicitly varies **~10dB between individual units of the same hardware**
  and spans a raw range of -43 to -142 dBm for just that one reference
  radio. There is no universal MMDVM RSSI dBm-to-quality spec to be
  rigorous against -- claiming one would be dishonest. `rssiBarPct()`'s
  -120..-60 dBm scale is therefore explicitly illustrative (own code
  comment says so, plus a user-facing tooltip on the bar itself), not
  presented as calibrated fact.
  BER has no official spec either, but DOES have consistent, sourced
  real-world convention from actual MMDVM/hotspot tuning writeups: a
  well-tuned hotspot runs well under 1% BER (M1GEO's Pi-Star BER tuning
  writeup, george-smart.co.uk/2020/06/getting-the-best-bit-error-rate-ber-
  from-your-pi-star-mmdvm, documents an optimized result of 0.0887%), a
  repeater-builder groups.io thread on DMR BER testing treats >1% as
  something worth investigating/adjusting RX offset for, and that same
  M1GEO writeup's *before* baseline of 3.5% is explicitly called out as
  "significantly higher than other users." `berColor()` uses these real
  numbers directly (<1% good, 1-3% fair, >3% poor) rather than a generic
  percentile split -- if this ever needs revisiting, re-derive from real
  MMDVM/hotspot-tuning sources the same way, not from a plausible-looking
  guess.

- **`config.HOTSPOT_INFO_CHECK_CMD` (WPSD radio frequency/duplex/identity
  display) went through a real wrong-guess-then-correction, worth
  recording so it isn't repeated.** A WebSearch summary claimed upstream
  MMDVMHost stores RX/TXFrequency under an `[Info]` section in
  `MMDVM.ini` -- fetching the actual current `g4klx/MMDVMHost` repo
  directly (not trusting the search summary) showed the real template
  file is named `MMDVM-Host.ini` (a 404 on the guessed `MMDVM.ini`
  filename was the first clue something was off), and its actual section
  list has no `[Info]` section and no frequency field anywhere -- the
  WebSearch result was simply wrong/stale. The real answer came from
  asking the user to SSH into an actual WPSD hotspot and grep for it
  live: WPSD generates its own separate config at **`/etc/mmdvmhost`**
  (no `.ini`, no hyphen -- this is WPSD's own generated file, not
  upstream MMDVMHost's shipped template), confirmed via a full real dump
  to have a genuine `[Info]` section after all (WPSD's own, unrelated to
  upstream's naming) containing `RXFrequency=433750000`/
  `TXFrequency=433750000` (Hz, matches WPSD's own admin dashboard's
  displayed "433.750 MHz" exactly), `Location="Barrington, NH"`, and
  more; plus a `[General]` section with `Callsign=W1ZLA`, `Id=3100486`,
  `Duplex=0`.
  That same real file also has `CallsignFrequency` (CW ID interval),
  `AckFrequency` (ack tone Hz), `CTCSSFrequency` (FM sub-tone Hz), and a
  bare `Frequency=` key -- all real, all unrelated to RX/TX radio
  frequency, and easy to grab by mistake with a loose `grep -i
  frequency`. Because `Callsign=`/`Id=` are also plausible, common key
  names other sections (DMR/D-Star/NXDN network blocks, etc.) could
  reasonably reuse for unrelated purposes, `HOTSPOT_INFO_CHECK_CMD`
  doesn't key off field name alone -- it's an `awk` state machine scoped
  to the CURRENT `[section]` header (`s=="[General]" && /^(Callsign|Id|
  Duplex)=/`, `s=="[Info]" && /^(RXFrequency|TXFrequency|Location)=/`),
  confirmed against the user's real full file to match only the 6
  intended lines, nothing from elsewhere in the file. `Location`'s value
  arrives double-quoted in the file (`Location="Barrington, NH"`) --
  `monitor.py`'s parsing strips surrounding quotes (`val.strip('"')`)
  generically, a no-op for the other, unquoted keys. **Lesson**: a
  WebSearch/WebFetch summary of a config format is not verification --
  fetching the actual current source (or, better, asking for a live grep/
  dump against the real device) is what actually caught this before any
  code shipped against the wrong assumption, twice over (first the wrong
  file/section entirely, then the real file turning out to have
  plausible key-name collisions a naive grep would have silently
  mismatched).

- **The APRS Messages and HamAlert cards were merged into one
  "Notifications" card (v3.51) at the presentation layer only -- the two
  integrations themselves stay fully independent.** `aprs_inbox_enabled`/
  `hamalert_enabled` are untouched (own Settings toggles, own connections,
  own `/api/aprs_inbox`/`/api/hamalert` routes) -- only the CARD got
  merged: one `#notifications-card` (rendered if `aprs_inbox_enabled OR
  hamalert_enabled`), one `notifications_position` setting, one entry in
  `app.py`'s `_SENTINEL_DEFS`. The one real backend change this required:
  `_overflow_sentinels()`'s `enabled_key` lookup used to always be a
  single settings key string (`settings.get(enabled_key, False)`) --
  Notifications needed an OR across two independent keys, so
  `_SENTINEL_DEFS` now allows `enabled_key` to be a tuple
  (`("aprs_inbox_enabled", "hamalert_enabled")`), and
  `_overflow_sentinels()` branches on `isinstance(enabled_key, tuple)` to
  check `any()` across it -- every existing single-string sentinel entry
  is unaffected. The merged card's connection header shows an independent
  dot per enabled source (APRS/HamAlert can be up/down independently, so
  one combined dot would hide that), and filter chips (All/APRS/HamAlert)
  only render when BOTH sources are enabled -- with just one on, there's
  nothing to filter and the chips would be dead UI. `aprs_inbox_position`/
  `hamalert_position` are now unused dead settings keys, kept in
  `config.DEFAULT_SETTINGS` only for backward compat with old
  `settings.json` files that already have them -- don't resurrect them as
  live position sources if this is ever touched again, `notifications_position`
  is the only one either template's JS or `setup.html`'s drag list reads.

- **`dashboard_beta.html`'s "instrument panel" reskin was promoted to be
  THE dashboard (v3.53), retiring the classic look and the side-by-side
  `/beta` split entirely -- not a new design, a straight promotion of
  what had already been built and iterated on.** Verified safe before
  doing it: diffed every top-level JS `function` name and every `id="..."`
  attribute between `dashboard.html` and `dashboard_beta.html` and found
  ZERO present in classic but missing from beta (beta was a strict
  superset -- this session's own discipline of updating both files
  together for every single change, all the way back to the Notifications
  card merge and the License Quiz class dropdown, is exactly what made
  this a safe mechanical swap rather than a risky one). What actually
  changed: `templates/dashboard_beta.html`'s content became
  `templates/dashboard.html`'s content (old file deleted); `app.py`'s
  `dashboard()` route lost its `?view=`/`settings.use_beta_dashboard`
  branching and just renders the one template now; `/beta` is kept as a
  bare `redirect("/")` rather than removed outright, so an old bookmark
  still lands somewhere real instead of 404ing; `setup.html`'s "Use the
  new beta look by default" toggle and the "✨ Try new look"/"← Back to
  classic" toolbar links are gone (nothing left to switch between). The
  `beta_courtesy_tone`/`beta_spotlight_dimming` setting keys were
  deliberately NOT renamed to drop the "beta_" prefix -- doing so would
  be pure churn (breaks nothing to rename, but fixes nothing either) for
  zero functional gain, so they keep their pre-promotion names
  permanently; `use_beta_dashboard` itself was left in
  `config.DEFAULT_SETTINGS` as an inert, unused key for the same backward-
  compat-with-old-settings.json reason every other orphaned setting key
  in this file is kept rather than deleted. If a similar "evaluate a
  full-duplicate reskin side-by-side, then decide" effort is ever done
  again, this same before-you-promote check (diff every function name AND
  every element id between the two files) is the right way to confirm
  nothing unique to the original would be silently lost in the swap.
- **`setup.html`'s reskin to match the dashboard (v3.54) was a pure
  token swap, not a rewrite -- confirmed BEFORE building anything that
  every single color reference in the file's ~2000 lines of CSS already
  went through a `var(--...)`, with zero stray hardcoded hex outside the
  `:root`/`[data-theme="light"]` blocks themselves.** That's what made
  it safe to just replace those two token blocks with the dashboard's
  own palette (graphite panels, amber `--accent`, cyan `--live`) and get
  every tab (General, Weather, Integrations, Hotspots, Cameras,
  Favorites, Cards, Backup/Restore, Version) re-themed for free, the
  same "keep the aliases, don't rename 700 call sites" approach the
  dashboard reskin itself used. Two of setup.html's own pre-existing
  token names don't exist in the dashboard's palette and were re-aliased
  rather than dropped: `--amber` (warn-box text, credential badges, star
  icons) -> `var(--caution)`, `--row-bg` (toggle-group/drag-row/cat-row
  backgrounds) -> `var(--panel-2)`. A handful of hover-tint/background
  `rgba()` literals were hardcoded to the OLD hex values (button hovers,
  the warn-box background/border, the credentials badge) -- these don't
  auto-update just by changing the token block, so each was repointed to
  the matching `-dim` token (`--accent-dim`/`--live-dim`/`--danger-dim`/
  `--caution-dim`) or, for the one that needed a specific border opacity
  no `-dim` token matches, `color-mix(in srgb, var(--caution) 35%,
  transparent)` -- the same `color-mix()` pattern the dashboard file
  already uses elsewhere, not a new technique. Two plain black
  `rgba(0,0,0,...)` overlay/shadow values were left untouched -- they're
  theme-agnostic dimming effects, not part of the color identity. If
  this page is ever visually touched again, re-run the same check first
  (grep for hex/rgba literals outside the token blocks) before assuming
  a palette change is still a clean swap -- it only stays this cheap as
  long as no one introduces a hardcoded color into the component CSS.
- **`aprs_messaging.py`'s favorite-alert text used to silently truncate at
  `MAX_MSG_LEN` (67 chars) with no indication -- a long hotspot name +
  talkgroup + favorite label combination could genuinely lose the tail of
  the message with nothing showing it was cut.** Found while evaluating
  a third-party APRS client (CtrlAltDel-Irl/APRS-Messenger, which splits
  overlong messages into multiple packets instead) -- full multi-part
  splitting was considered and explicitly rejected as more complexity
  than this app's one-shot "FYI" alerts need (message numbering/
  reassembly makes sense for a back-and-forth chat client, less so for a
  single outbound notification). Fixed two ways instead: (1) the
  talkgroup is now ordered BEFORE the hotspot name in the message
  (`"{call} active on {talkgroup} ({node})"`) -- if truncation still
  happens, the more operationally useful info (what channel/net they're
  on) survives instead of the hotspot's own name, which you already know
  since you configured it; (2) a genuinely-too-long message now ends in
  a plain ASCII `"..."` marker so it's visibly cut off rather than
  silently dropped -- same "plain ASCII, not a Unicode character"
  reasoning as the existing `-` separator in this file (7-bit-ASCII old
  TNCs/handheld displays; aprslib encodes UTF-8 rather than erroring, so
  a fancy ellipsis character would reach the air as mangled bytes
  instead of failing loudly).
- **Satellite tracking (v3.55, `satellites.py`) was verified against real
  ground truth at every layer before being trusted, not assumed correct
  from the math alone.** Three separate, real findings worth recording:
  - **SatNOGS DB's transmitter API silently ignores the obvious query
    parameter name.** `?norad_cat_id=<id>` doesn't error and doesn't
    filter -- it returns the entire ~5000-row transmitter table
    unfiltered, and the wrong-satellite response still LOOKED
    plausible (real transmitter records, just for NORAD 965 instead of
    the requested satellite) until cross-checking the response's own
    `norad_cat_id` field caught the mismatch. The correct parameter is
    `satellite__norad_cat_id`. If this is ever touched again, verify
    the returned records' own `norad_cat_id`/`sat_id` field matches
    what was requested -- don't trust a plausible-looking filtered-size
    response as proof the filter actually worked.
  - **The SGP4 position math (TEME->ECEF->geodetic, plus a GMST
    approximation) was checked against a live ground-truth ISS position
    from api.wheretheiss.at**: computed lat/lon/alt matched to within
    ~0.005 deg (~500m) and ~25m altitude -- irrelevant error for "is
    this satellite visible right now," but confirms the conversion
    chain isn't silently wrong. The separate topocentric elevation/
    azimuth math (used for pass prediction) was sanity-checked three
    more ways: an observer directly under the satellite computes ~90deg
    elevation with range equal to the satellite's own altitude; an
    antipodal-ish observer computes a deeply negative elevation; an
    observer offset by a few hundred km computes a plausible mid-range
    elevation with range greater than (not equal to) altitude. All
    three matched geometric expectations exactly before any of this
    shipped.
  - **`DEFAULT_SATELLITES` was built from a LIVE status check, not
    recollection of "well-known easy sats"** -- AO-92 (FOX-1D) looked
    like an obviously-safe default from general ham radio knowledge,
    but a live SatNOGS DB query shows it's actually marked
    `"re-entered"` right now. AO-85 (FOX-1A) is marked alive but its FM
    transponder specifically is marked `"inactive"` in SatNOGS DB. Both
    were dropped from the default list after finding this live, not
    guessed. If this list is ever revisited, re-verify status AND
    transponder-active-state the same way -- satellite operational
    status genuinely changes over time, unlike e.g. a band plan's
    frequency allocations.
  Separately: the Live map's ground-track polyline splits into multiple
  segments wherever consecutive points jump more than 180deg in
  longitude (`updateSatellitesMap()` in dashboard.html) -- a satellite's
  ground track crosses the antimeridian roughly once per orbit, and a
  single unsplit polyline would draw one long incorrect line wrapping
  the entire width of the map instead of two separate tracks at each
  edge. The footprint circle is a real `L.circle` with a radius in
  meters (`_footprint_radius_km()`'s tangent-line-from-altitude
  geometry, sanity-checked against the ~2000-2300km figure commonly
  cited for the ISS's own visibility footprint), not a CSS-drawn shape
  -- it stays correctly sized as the map is panned/zoomed, which a
  fixed-pixel circle wouldn't.
- **The HF Propagation (MUF) overlay (added v3.56 as `propagation.py`,
  a cropped/geo-registered prop.kc2g.com world MUF map) was removed
  entirely in v3.57 -- the user tried it live and it just looked bad on
  the map, same "built it, looked at it, decided against it" outcome as
  the Gulf-of-America map label and the RainViewer precipitation radar
  overlay elsewhere in this file.** The equirectangular-projection
  verification work that went into it (Chrome-headless screenshot
  comparison across two different `?grid=` values, exact clipPath/tick-
  mark geometry confirming -90/-180 to 90/180 bounds) was real and
  correct -- this wasn't a technical failure, purely a visual/product
  judgment call. `propagation.py`, its `/api/propagation_map.svg` route,
  and the Live map's "Propagation (MUF)" checkbox are all gone. If a
  similar MUF/propagation-map overlay is ever revisited, the
  projection-verification approach documented in git history for this
  entry is still the right starting point -- but treat the visual
  result as an open question again, not a foregone conclusion.
- **The Recent Contacts card (v3.57) is frontend-only -- no new backend
  module, no new route.** It reads the same `/api/qsos` (`qsos.json`)
  the Live map's QSO layer already fetches, just sorted newest-first by
  `logged_at` (falling back to `date` for older entries that predate
  this field) and rendered as a `.msg-row` list, same component the
  Notifications/Satellites cards already use. What DID need real
  plumbing: `qsos.json` entries previously only carried enough fields to
  plot a pin (call/band/mode/date/lat/lon/grid) -- city/state/country
  and exact frequency existed upstream (QRZ's XML response, WSJT-X's own
  UDP packet) but were being discarded before reaching storage. Fixed
  across the whole chain: `qrz.py`'s `_lookup_remote()` now returns
  `city`/`state`/`country` as separate keys alongside the existing
  combined `location` string (which drops country whenever a state is
  present -- too lossy for a flag lookup); `monitor.py`'s
  `_lookup_caller()` passes them through; `wsjtx.py`'s `_handle_qso()`
  now ALWAYS calls `lookup_caller_info()` (previously only as a position
  fallback when the grid square was absent) since it's one QSO at a
  time and QRZ's client caches per callsign anyway; `wsjtx.py`'s packet
  parser also keeps `frequency_hz` (was extracted then discarded, only
  `band` was kept). `app.py`'s ADIF importer deliberately did NOT get
  the same always-on QRZ lookup treatment -- kept the existing
  grid-absent-only conditional, since a bulk import can be hundreds of
  QSOs and a synchronous QRZ round-trip per QSO could make a large
  import slow/time out; it does gain `frequency_hz` (a plain
  `_adif_freq_to_hz()` MHz-string-to-Hz conversion) and `logged_at` (a
  spec-correct `QSO_DATE`+`TIME_ON` UTC-epoch parser,
  `_adif_datetime_to_epoch()`, needed since QSO_DATE alone can't order
  same-day contacts).
- **A real bug caught by testing, not inspection, in the ADIF importer's
  country resolution: the QRZ fallback was written but never actually
  wired up.** `_adif_datetime_to_epoch`/city/state were read correctly
  from `monitor.lookup_caller_info()`'s result in the grid-absent
  branch, but the `qsos.append()` dict's `"country"` line still read
  `(r.get("COUNTRY") or "").strip() or None` -- the QRZ-sourced country
  variable was computed and then silently never referenced. Confirmed
  live with a mocked `lookup_caller_info()` (patched to return a
  synthetic `country: "ENGLAND"`) before believing the fix: the first
  attempt still came back `null`, the corrected version (`... or
  qrz_country`) came back `"ENGLAND"`. Same "verify against real
  behavior, not against having written the line" discipline the
  `card_order_tiebreak`/`_file_lock` concurrent-write bug elsewhere in
  this file was caught with -- code that looks obviously correct on a
  read-through can still be wired to nothing.
- **Country name -> flag emoji (`COUNTRY_FLAGS` in `dashboard.html`) is
  a plain ISO-3166 English-country-name table, deliberately NOT a DXCC
  callsign-prefix guess.** This was an explicit scope decision after
  evaluating three DXCC-table options and rejecting all of them:
  `pyhamtools` (pulls in `redis`/`ephem`/`lxml`/`beautifulsoup4` --
  architecturally mismatched for an app with zero other external
  service dependencies), raw `cty.dat` from country-files.com (blocked,
  HTTP 403 on a direct fetch), and `api.hamdb.org` (free/keyless/
  confirmed live, but confirmed via its own `/about` page to cover only
  5 countries -- US/Canada/Australia/Germany/Czech Republic -- not a
  general solution). The table only covers common English country
  names as QRZ/ADIF logs actually spell them, NOT the full ~340-entry
  DXCC entity list (many DXCC entities are sub-national --
  "ENGLAND"/"SCOTLAND"/"ASIATIC RUSSIA" -- and don't map to one ISO flag
  anyway). An unmatched name just shows no flag, same
  graceful-degradation contract as every other optional field here --
  don't "fix" a missing flag by guessing a DXCC prefix mapping instead.
- **The Satellites card's Live map overlay gained its own independent
  on/off toggle (v3.57), separate from `show_satellites` (settings.json).**
  Previously `updateSatellitesMap()` unconditionally added its layer to
  the map whenever satellite data was fetched, with no way to turn just
  the map drawing off while keeping the card's pass predictions. Now a
  `localStorage`-backed `satellites-map-toggle` checkbox (same pattern
  as grey-line/aurora/POTA/PSK) gates whether `updateSatellitesMap()`'s
  built layer group actually gets `.addTo(leafletMap)` -- the group and
  its per-satellite markers (`satelliteMarkers`, keyed by `norad_id`)
  are still built every poll regardless, so `jumpToSatelliteOnMap()` (a
  clickable satellite name in the Satellites card's pass rows, mirroring
  the existing `jumpToNodeOnMap(ip)` pattern for hotspot cards) always
  has a marker to find. Clicking a satellite name turns the map overlay
  on first if it was off (checks the box, calls `toggleSatellitesMap()`)
  before panning/opening its popup -- otherwise the marker would exist
  in memory but never have been added to the map to pan to.
- **Recent Contacts gained signal report/operator name/distance worked,
  and Satellite passes gained rise/set compass direction + uplink
  frequency (v3.58) -- both were mostly "stop discarding data already
  in hand" rather than new lookups.** `wsjtx.py`'s QSOLoggedPacket
  parser was already reading past `report_sent`/`report_recv`/`name`
  with a bare `r.qstring()  # ..., unused` -- these are now captured and
  passed through (`rst_sent`/`rst_rcvd`/`name` in the QSO dict). ADIF's
  equivalent fields (`RST_SENT`/`RST_RCVD`/`NAME`) get the exact same
  "own-log-data-first, QRZ-fallback-second" priority the `COUNTRY` field
  already established -- confirmed live with a synthetic ADIF record
  before trusting it. `uplink_mhz` was already present on every pass
  dict returned by `satellites.py`'s `_find_passes()` (from the tracked-
  satellite config) -- the Satellites card just never rendered it
  alongside `downlink_mhz`. The one piece that DID need new backend
  work: AOS/LOS azimuth. `_elevation_azimuth()` was already computing
  `az` every 30-second step to detect the horizon crossing, but only
  `el` got used for that decision -- `az` was discarded immediately
  after. Fixed by capturing it at the exact moment `in_pass` flips true
  (`aos_azimuth`) and at the step where `el` finally goes negative
  (`los_azimuth`), then converting degrees to a 16-point compass label
  (`COMPASS_POINTS`) client-side in `dashboard.html`, same "send raw
  data, format for display in JS" split as the RSSI/BER bars. Distance
  worked reuses the map's own existing `distanceKm()`/`fmtDistance()`
  helpers verbatim (already used for the map's hotspot-distance display,
  itself keyed off `settings.weather_unit` for miles-vs-km) rather than
  writing a second haversine implementation -- only computed when a QSO
  has both its own position and a resolved QTH position, same graceful-
  omission contract as the line-back-to-QTH feature on the map itself.

- **Five more optional cards/notification sources shipped together
  (v3.59): QSO Stats, Top 5 Activity, SOTA spots, and three new
  Notifications sources (fleet offline/online, geomagnetic-storm,
  Brandmeister network-wide favorite activity).** Grouped here since
  they share a few real patterns worth reusing:
  - **"Surface a transition, not a state" is now a 3-times-repeated
    pattern**, first established by the offline-detection feature much
    earlier in this file: `monitor.py`'s fleet online/offline events
    (`FleetMonitor._record_fleet_event`, called from `_record_failure`
    and the three success paths) and `hf_conditions.py`'s geomagnetic
    alerts (`_check_alert_transition`, comparing the new K-index against
    the PREVIOUS cache before overwriting it) both fire an event only on
    a threshold CROSSING, never on every single poll while the state
    stays elevated/offline -- otherwise a multi-hour storm or outage
    would spam the same alert forever. Both are plain bounded
    `collections.deque` event logs read by a new `/api/fleet_events`/
    `/api/hf_alerts` route -- no new persistent connection for either,
    since the underlying data (offline_since, hourly K-index) was
    already being tracked/fetched for existing cards.
  - **The Notifications card's "how many sources are enabled" check
    generalized from a hardcoded pair to a Jinja-computed count**
    (`notif_source_count` in `dashboard.html`) once a third/fourth/fifth
    source joined APRS+HamAlert -- the filter-chip row now shows
    whenever 2+ of the (now five) sources are enabled, not just "both
    APRS and HamAlert." `_SENTINEL_DEFS`' Notifications entry's
    `enabled_key` tuple grew from 2 to 5 strings across this session
    (`_overflow_sentinels()`'s existing `any()`-across-a-tuple handling
    needed zero changes -- it was already written to handle any tuple
    length, not just a pair).
  - **`storage_activity.py`'s `activity_log` gained `target`/
    `target_type` columns via the exact guarded-ALTER-TABLE migration
    pattern this file already documented as a requirement for the Top 5
    card, before ever building it** -- `_get_conn()` checks
    `PRAGMA table_info` for the columns' presence on every connection
    open (cheap, and safe to run unconditionally) rather than a one-time
    startup migration, so an existing install's `activity.db` gains the
    columns transparently the first time it's opened post-upgrade.
    Verified live: seeded a real pre-migration `activity.db` file by
    hand, confirmed `log_activity()`/`top_targets()` both work against
    it without error.
  - **The Top 5 card's FIRST implementation ranked by talkgroup/node,
    not callsign -- a real, reported wrong-scope bug, not a hypothetical
    ("I was actually looking for the top 5 call signs not the top 5
    nodes").** `monitor.py`'s `_log_activity()` originally preferred
    `status.talkgroup` (WPSD) over `status.active_call`, so a WPSD
    hotspot's rows ranked by *which talkgroup* was busiest, not *who*
    transmitted -- easy to reach for by analogy with the map/Recent
    Contacts world where talkgroup is often the more prominent field,
    but wrong for what "top 5 activity" actually meant here. Fixed by
    always setting `target = status.active_call` for every hotspot type
    (dropping the talkgroup branch entirely) and `target_type` to a
    fixed `"callsign"` -- `active_call` already holds the right value
    either way: WPSD's own caller's resolved callsign, or ASL3's linked
    node's resolved callsign (falling back to its bare node number only
    when no callsign could be resolved, still a meaningful identity, just
    not a real callsign). Do NOT resurrect the talkgroup-preference
    branch if this is ever touched again -- verified live after the fix
    with synthetic WPSD and ASL3 log_activity() calls that the ranked
    output is callsigns (`W1ABC`, `W2ECR`), not talkgroup/node numbers.
    The Top 5 card's rows are now clickable (QRZ if configured, else
    RadioID.net), same pattern as Recent Contacts' callsigns -- a natural
    fit once the ranking is genuinely callsign-based.
  - **The Brandmeister Last Heard live feed (`brandmeister_lastheard.py`)
    required a real, multi-step reverse-engineering session --
    Brandmeister documents none of this anywhere.** Worth recording the
    full path since it's a good template for the next time this kind of
    live protocol needs cracking:
    1. Brandmeister's own OpenAPI spec (`api.brandmeister.network/
       api-docs`) confirmed there's no REST equivalent -- real-time
       last-heard activity only exists over a persistent connection,
       matching what an EARLIER investigation in this file (for a
       different, network-wide "top 5" card idea, ultimately NOT built
       that way -- see the Top 5 Activity card above, which is own-fleet
       only) had already concluded.
    2. Guessing `api.brandmeister.network/socket.io/` got a clean 404 --
       wrong host entirely, not a sandbox block (confirmed by the
       response being a normal, well-formed 404 body, same "is this a
       real app-level response or the sandbox's own firewall" check
       this file has needed before).
    3. Fetched `brandmeister.network`'s own bundled frontend JS
       (`assets/index-*.js`, a modern minified SPA bundle) and searched
       it directly for `socket.io` usage -- found the client connects via
       a `fb.lh.host`/`fb.lh.path` config object whose actual VALUES
       aren't literal strings anywhere in the 2.4MB bundle (almost
       certainly injected at build time), so the exact host/path
       couldn't be read out directly.
    4. Found the real host by brute-force probing candidate subdomains
       against the STANDARD Engine.IO polling handshake path
       (`/socket.io/?EIO=4&transport=polling`) until one returned a real
       Engine.IO open packet (`0{"sid":...,"pingInterval":25000,...}`)
       instead of a 404/503 -- `ws.brandmeister.network` responded, but
       only once the path was ALSO changed from the default
       `/socket.io/` to `/lh/socket.io/` (found by trying a short list of
       plausible last-heard-specific path variants against that one
       host, since the JS confirmed a non-default `path:` option was
       being passed).
    5. With host+path confirmed, the actual event wiring (what to `emit`
       after connecting, what event to `.on()` for live data) was read
       directly out of the SAME bundled JS, not guessed -- multiple
       different call sites in the bundle connect to `fb.lh.host` for
       different scoped views (a specific repeater's history via
       `emit('searchMongo',{query:{sql:...}})`, a specific station's
       history the same way), but the homepage's own global ticker
       widget does `e.on('connect',()=>e.emit('join','everything'))` --
       joining a room literally named `"everything"` is what unlocks the
       GLOBAL firehose; skipping this step was confirmed live to mean
       zero events ever arrive, no error, just silence.
    6. The live event shape (`42["mqtt",{"topic":"LH","payload":"<json
       string>"}]`, with the inner JSON having `SourceCall`/`SourceName`/
       `DestinationID`/`DestinationName`/`LinkTypeName`/`SessionID`/
       `Start`/`Stop`/`RSSI`/`BER`/etc.) was confirmed against several
       REAL live events captured during this session, not inferred from
       the minified variable names alone (`_E`/`fE`/`hE`/etc. formatter
       function calls in the JS gave hints, but the actual field names
       came from the live JSON payloads themselves).
    7. **The full round-trip was verified end-to-end before considering
       this done**: captured a live `SourceCall` from the real firehose,
       monkey-patched `favorites_set()` to return exactly that callsign,
       ran the real `BrandmeisterLastHeardListener` class against it, and
       confirmed a matching event landed in `.events()` with the right
       fields -- then confirmed the same thing again through the actual
       Flask app (`/api/settings` enabling it, `/api/brandmeister_lh`
       reporting `connected: true`), not just the standalone module.
    Implemented over plain `websocket-client` (already a project
    dependency, per openspot.py) with hand-rolled Engine.IO/Socket.IO v4
    framing (`0{...}`/`40`/`2`↔`3`/`42[...]`) rather than adding a new
    `python-socketio` pip dependency -- same reasoning as openspot.py's
    own custom framing over a full protocol library elsewhere in this
    file. If Brandmeister ever changes their frontend build (new host,
    new path, new room name), this whole discovery process needs
    redoing the same way -- there's no vendor doc to fall back on.

- **Country flags in Recent Contacts weren't showing for most rows --
  two independent, real bugs, not one (v3.60), caught by the user, not
  by inspection.** (1) **Windows does not render flag emoji at all** --
  Segoe UI Emoji has no flag glyphs by design (a deliberate Microsoft
  choice, unlike Apple/Google's emoji fonts), so a regional-indicator
  pair like the US flag falls back to showing the literal letters "US"
  as plain text instead of composing an icon. No choice of emoji
  character fixes this -- it needed a real image instead. Fixed by
  rendering an actual small SVG flag (`hjnilsson/country-flags` via
  jsdelivr) keyed off the same ISO2 code the old emoji-generation logic
  already computed -- confirmed live this CDN serves real flags before
  using it, and it's the exact same CDN Brandmeister's own production
  frontend uses for this exact purpose (noticed by chance while
  reverse-engineering their Last Heard feed above). (2) **The ADIF
  importer only ran the QRZ/RadioID country/city/state/name lookup when
  GRIDSQUARE was absent** -- but most real-world ADIF logs DO have
  GRIDSQUARE and DON'T populate COUNTRY (plenty of logging programs
  never write it), so this "only look up when grid is missing"
  shortcut left country (and therefore the flag) blank for nearly every
  imported QSO in practice, not just an edge case. Fixed by always
  running the lookup for name/city/state/country regardless of whether
  GRIDSQUARE already gave a position -- matching wsjtx.py's live-logging
  path, which already always enriches. Position resolution itself is
  unchanged (grid still wins over a QRZ-derived position when both are
  available) -- only the enrichment fields' lookup got less conditional.
  `QrzClient.lookup()`'s own per-callsign cache plus its near-instant
  short-circuit when QRZ isn't configured (`QrzClient.enabled`) keeps
  this cheap even for a large bulk import.
- **The Top 5 Activity card's first ship ranked by talkgroup/node, not
  callsign -- a real, reported wrong-scope bug ("I was actually looking
  for the top 5 call signs not the top 5 nodes"), fixed the same
  session it shipped.** See the earlier Top 5 Activity gotcha entry
  above for the full fix; this entry just adds what came after the
  fix, in response to a follow-up ask for MORE context, not less: each
  ranked callsign now also shows `via` -- the WPSD talkgroup that
  callsign was MOST RECENTLY heard on (a new `via` column on
  `activity_log`, resolved per-callsign with a correlated subquery
  picking the latest row's `via`, not an aggregate/most-common value --
  simpler and more useful at a glance). ASL3 deliberately gets no `via`
  value at all -- `status.talkgroup` is never set for that hotspot
  type, and the linked node captured in `active_call` already IS the
  "channel" in the WPSD-talkgroup sense, so there's nothing distinct
  left to show.
- **The Notifications card's header-icon row and filter-chip row said
  the same thing twice -- reported directly by the user with a
  screenshot ("this card is kinda busy, the notification type seems
  redundant"), fixed via a mockup-first redesign (same workflow as every
  other card this project has built).** Each of the 5 possible sources
  (APRS/HamAlert/Fleet/Solar/Brandmeister) used to render BOTH a
  `.conn-item` in the header (icon + connection dot) AND a separate
  `.filter-chip` below (icon + label, click to filter) -- two full rows
  of near-duplicate source identity before a single notification ever
  showed. Merged into one `.source-row` of `.source-pill`s that do both
  jobs: the connection dot still lives on the pill (`#aprs-inbox-conn-
  dot`/etc. -- same element IDs, so the existing JS that toggles
  `.down`/sets `.title` on them needed zero changes), and clicking a
  pill both shows which source it is AND filters to it
  (`toggleNotifFilter()`, replacing `setNotifFilter()`) -- clicking the
  ALREADY-selected pill again returns to showing every source, so there's
  no separate "All" chip needed either. The old `notif_source_count > 1`
  guard (which used to hide the filter-chip row entirely when only one
  source was enabled, since filtering a single source to itself was
  meaningless) is gone -- the source-row now always renders whenever at
  least one source is enabled, since even a lone pill still usefully
  shows that source's identity/connection state; clicking it is just a
  harmless no-op toggle in that case. Verified live: Jinja-rendered the
  card at zero/one/all-five sources enabled and confirmed the card
  appears/disappears and the pill list matches in each case.
- **The Recent Contacts card's actual shipped design didn't match the
  mockup the user had already approved -- reported directly ("why
  doesn't my recent contacts card look as good as the mockup"), root
  cause was reusing `.msg-row` (the Notifications card's message-thread
  layout) instead of building the mockup's own dedicated row shape.**
  Fixed by adding real `.qso-row`/`.qso-flag`/`.qso-body`/`.qso-top`/
  `.qso-call`/`.qso-band-badge`/`.qso-time`/`.qso-meta`/`.qso-grid` CSS
  matching the mockup's actual property values (not approximated from
  memory), plus a `.fresh` highlight on contacts logged within
  `QSO_NEW_WINDOW_SEC` (the same 10-minute "just happened" window the
  map's own QSO pins already use, reused rather than inventing a second
  threshold). Fields added to the schema AFTER the original mockup
  (operator name, RST, distance) had to be fit into this same layout
  rather than the mockup's own markup verbatim, since the mockup
  predates those fields entirely. **Lesson for next time a mockup gets
  approved**: build the ACTUAL approved CSS/markup, don't reach for the
  nearest existing component that "shape of" fits -- reuse only after
  confirming the visual result actually matches what was shown and
  agreed to, not just that it's plausible-looking.
- **Hotspot card detail drawer (v3.61) -- built after THREE separate
  mockup rounds (generic placeholder cards, then real card CSS copied in,
  then a `stopPropagation()` demo with a toast confirming which action
  actually fired), each one requested by the user before building for
  real.** The core interaction question the whole feature turned on:
  making the ENTIRE card clickable (`openHotspotDrawer('${hs.ip}')` on
  the outer `.hotspot-card` div) without breaking any of its existing
  nested links (card name -> the hotspot's own web UI, 📍 map pin,
  the update badge, the active/last-heard/recent-history callsigns ->
  QRZ, ASL3's linked-node callsigns). Plain DOM event bubbling solves
  this cleanly: every one of those existing elements got
  `onclick="event.stopPropagation()"` added (the map-jump-btn already had
  an onclick for `event.preventDefault()`, so stopPropagation was added
  alongside it, not as a second handler), which stops the click from
  ever reaching the card's own listener -- no new event-delegation
  library or per-element class-based exclusion logic needed. `.hotspot-
  card` also gained `cursor: pointer` and a subtle hover box-shadow, kept
  as a SEPARATE rule from the existing `.active`/`.favorite`/`.offline`
  border-color states so the two don't fight over the `border-color`
  property.
  `openHotspotDrawer(ip)` reads `lastApiData` (the most recent `/api/data`
  poll) rather than a new fetch -- that global previously only got
  populated `if (SHOW_ASL_FAVORITES)` (it existed solely for that card's
  own lookup need), which would have silently broken the drawer for
  anyone without ASL Favorites enabled; fixed by making the assignment
  unconditional in `refresh()`, since it now has two independent
  consumers. Drawer content is genuinely type-aware, not a single
  template: WPSD gets `history` (recent talkers) + `bm_status_text`/
  `bm_static_tgs` (Brandmeister) + `dashboard_update_available`/
  `dashboard_outdated_repos` (WPSD update status); ASL3 gets
  `asl_linked_nodes` (the full link topology, with `keyed` nodes
  highlighted the same live-pulsing-dot language used elsewhere) instead
  -- these are real fields that already existed on `HotspotStatus` with
  nowhere in the compact card to show in full, not new backend work.
  One real data-shape gotcha surfaced while building this: `history`
  entries (`{call, name, location, lat, lon}`) have NO timestamp field at
  all, confirmed by re-reading `models.py` before writing the drawer's
  render code -- the "Recent talkers" section can only show who and
  where, never "X min ago" per talker. Don't add a fabricated relative
  time here without first adding a real timestamp to `history` entries
  server-side.
- **Two more drawers (v3.62), reusing the exact `.hs-drawer`/`.hs-drawer-
  backdrop` mechanism from the hotspot card drawer above rather than each
  inventing its own -- one shared backdrop, `closeAllDrawers()` closes
  whichever of the three (`#hs-drawer`/`#qso-drawer`/`#settings-drawer`)
  is open, and each `open*Drawer()` calls it first so opening one always
  cleanly replaces another if one was already open.**
  - **Recent Contacts row -> QSO detail drawer.** The user explicitly
    asked for the callsign link to keep behaving exactly as it already
    did (opens QRZ/RadioID) rather than being swallowed into the row's
    new click-to-open-drawer behavior -- same `stopPropagation()`
    pattern as the hotspot card's existing links, not a new mechanism.
    `lastRenderedContacts` (set by `renderRecentContactsCard()` each
    poll) lets `openQsoDrawer(i)` look up the full record by array index
    without a second fetch, same shape as `lastApiData`/
    `openHotspotDrawer(ip)`. The drawer's "View on map" button does a
    plain `leafletMap.setView([lat,lon], ...)` rather than embedding a
    second live Leaflet map instance inside the drawer -- deliberately
    simpler than a real embedded map (which would need its own teardown
    on drawer close to avoid a leaked map instance), and this is exactly
    what an early mockup's own honest "map preview" placeholder caveat
    already flagged as the real answer once actually built.
    `q.source === 'wsjtx'` vs. anything else (including simply absent)
    distinguishes "Live (WSJT-X)" from "ADIF import" in the drawer --
    confirmed by grepping that only `wsjtx.py`'s `_handle_qso()` ever
    sets a `source` key on a QSO record at all; the ADIF importer never
    has and still doesn't, so its absence IS the "ADIF import" signal,
    not a separate field to add.
  - **Quick Settings drawer, answering a real design question asked
    before building: does it make sense to include Live-map toggles in
    a drawer reachable from every tab, unless the drawer is ALSO
    reachable from the map tab itself?** Resolved by confirming the
    toolbar (where the gear icon lives, alongside the existing theme
    toggle) is persistent chrome rendered once, outside every
    `.tab-panel` -- it does NOT get torn down/rebuilt when switching
    tabs, so the drawer is already reachable from the Live map tab too,
    same as from the dashboard cards tab. The remaining real constraint
    wasn't reachability, it was FUNCTIONALITY: `setMapStyle()`/
    `toggleGreyline()`/`toggleAurora()` all silently no-op before
    `leafletMap` exists (confirmed by reading `setMapStyle()`'s own
    `if (!leafletMap) return`), and `initMap()` only ever runs lazily,
    the first time the Live map tab is opened (`mapInitialized` flag).
    So the drawer's "Live map" section is gated on `mapInitialized`
    specifically -- not on which tab is currently active -- showing a
    real, working set of controls once the map has been opened at least
    once this session, and a plain explanatory note (not a broken
    dropdown) before that. Map style/grey line/aurora are NOT given a
    second independently-tracked state in the drawer -- the drawer's own
    controls directly call the map's existing `setMapStyle()`/
    `toggleGreyline()`/`toggleAurora()` functions (and, for the two
    checkbox-backed ones, first sync the real `#greyline-toggle`/
    `#aurora-toggle` checkbox elements' `.checked` state before calling
    them), so there is exactly one source of truth for these regardless
    of which control -- the map legend's own checkbox, or the drawer's
    copy -- the user last touched.
    `SPOTLIGHT_DIMMING_ENABLED`/`COURTESY_TONE_ENABLED` had to change
    from `const` to `let` -- every other similar `SHOW_*`/`*_ENABLED`
    constant in this file is baked in once from Jinja at page load and
    never changes without a reload, but these two specifically needed to
    become live-toggleable from the drawer without one, so this is a
    deliberate, narrow exception, not a precedent for converting the
    others -- don't casually turn more of these into `let` without an
    equivalent real need to flip them client-side.
- **Two settings entry points sitting next to each other (the toolbar's
  gear-icon Quick Settings button and the pre-existing "⚙ Settings" text
  link) was a real, reported point of confusion (v3.63) -- both used gear
  iconography for two different destinations.** Fixed by removing the
  separate gear button entirely and repointing the existing "⚙ Settings"
  link's `onclick` to `openSettingsDrawer()` (`event.preventDefault()`
  keeps the real `href="/setup"` as a working no-JS fallback) -- one
  settings entry point now, common toggles first, the drawer's own
  "More settings →" link one step deeper for everything else. This is
  the same "merge two things that both say X" resolution already used
  for the Notifications card's header-icon-row + filter-chip-row
  overlap earlier in this file -- don't reintroduce a second parallel
  settings affordance without applying the same merge instinct.
- **Quick Settings' "Live map" section grew 5 more proxy toggles (POTA,
  SOTA, PSK Reporter, Satellites overlay, logged-QSO visibility) via one
  small helper, `mapProxyToggleRow(checkboxId, toggleFnName, label)`,
  instead of hand-writing each one out like grey line/aurora originally
  were.** The helper returns `''` if `document.getElementById(checkboxId)`
  is null rather than throwing -- needed specifically because the
  Satellites overlay checkbox (`#satellites-map-toggle`) only exists in
  the DOM at all when `settings.show_satellites` is on (a Jinja `{% if
  %}`), unlike grey line/aurora/POTA/SOTA/PSK/QSO-visibility, which are
  all unconditionally rendered. Confirmed which was which by grepping
  each checkbox's surrounding markup before assuming -- guessing this
  wrong either way (guarding one that didn't need it, or not guarding
  the one that did) would have been a real bug, not just defensive
  overkill.
- **The QSO detail drawer's mini map is a genuinely embedded second
  Leaflet map instance, not a placeholder or an iframe of the main map
  -- built once (`ensureQsoDrawerSkeleton()`), reused on every later
  open.** Leaflet doesn't handle a container element being destroyed out
  from under an existing map instance well, and `openQsoDrawer()`
  otherwise fully replaces the drawer's `innerHTML` on every call (same
  as the hotspot/settings drawers) -- so the skeleton (including the
  `#qso-mini-map` div) is only ever written once, guarded by `if
  (qsoMiniMap) return`, and every later call just moves the existing
  marker/QTH-marker/line via `.setLatLng()`/`.setLatLngs()` rather than
  recreating them. `qsoMiniMap.invalidateSize()` runs on a `setTimeout`
  after the drawer's `.open` class is added -- a well-known Leaflet
  gotcha (a map initialized/resized while its container's true on-screen
  size hasn't settled yet can render a misaligned/grey tile grid) that
  applies here even though the drawer uses a CSS `transform` slide
  (not `display:none`), so the container technically already has real
  dimensions -- the safe fix is the same either way. Deliberately always
  uses the dark CARTO tile layer regardless of the dashboard's own
  light/dark theme or the main map's own style selector -- a third style
  toggle for one small preview would be more complexity than the value
  it adds; a fixed, always-legible background was the simpler and
  better call. The QSO pin reuses `QSO_COLOR` (`#ff3fa4`) verbatim from
  the main map's own QSO layer for visual consistency (same "this is a
  logged QSO" color everywhere) rather than picking a new one; the QTH
  marker uses a plain white/dark-bordered dot with no existing
  established color to match, since the main map's own QSO-to-QTH lines
  were never drawn with a distinct marker AT the QTH end to begin with.
- **The Live map's own floating "Import ADIF log" control
  (`.qso-import-control`) was retired in v3.64 -- moved into Quick
  Settings' "Live map" section instead of duplicating it there**, once it
  was pointed out that Quick Settings already had a proxy row for
  logged-QSO visibility right next to the very control this was a proxy
  *of*. Unlike grey line/aurora/POTA/SOTA/PSK/Satellites (each a genuine
  legend checkbox still worth reaching for while actually looking at the
  map), the import/stats/clear controls had no other reason to stay
  pinned to the map surface, so they moved wholesale
  (`qsoLogSectionHtml()`) rather than getting a second proxied copy --
  `#qso-stats`/`#qso-show-toggle`/`#qso-clear-btn`/`#adif-file-input` kept
  their exact element IDs, so `updateQsoStats()`/`toggleQsoVisibility()`/
  `handleAdifFile()` needed zero changes, just a new home for the markup.
  This didn't introduce a new precondition -- `renderQsoLayer()` (and
  therefore `updateQsoStats()`, called at its end) already silently
  no-ops via `if (!qsoCluster) return` before the map's ever been
  initialized, so nesting the whole QSO Log section under the same
  `mapInitialized` gate as the style/overlay toggles just makes an
  existing constraint explicit, not a new limitation for the control's
  reachability (it could only ever physically be clicked after visiting
  the map tab once anyway, back when it lived on the map itself).
  Because `initMap()` itself directly references `#qso-show-toggle`
  (`document.getElementById('qso-show-toggle').checked = ...`, to seed
  it from `localStorage` on first init) and that element no longer
  exists in static HTML at all, `initMap()` now calls
  `renderSettingsDrawer()` itself, immediately after setting
  `mapInitialized = true` and before that reference -- if you ever add
  another element that lives only inside a lazily-built drawer section
  but gets referenced directly from outside the drawer's own render
  path, it needs this same "render the section before touching it"
  ordering, not just a null-guard. `renderSettingsDrawer()` also went
  tab-aware in the same change: it checks
  `document.getElementById('panel-map')?.classList.contains('active')`
  and swaps the style/overlay toggles for a short note pointing at the
  map's own legend specifically while already on the Live map tab (true
  duplication only happens there) -- the QSO Log section itself has no
  such swap, since it has nowhere else to be shown regardless of which
  tab is active. A `let lastFetchedQsos` global (set inside
  `renderQsoLayer()`) lets `renderSettingsDrawer()` repaint the stats box
  immediately from the last-known poll result whenever the drawer
  re-renders (theme toggle, tab switch, reopen) instead of leaving it
  blank until the next 20s `fetchQsos()` tick happens to land.
- **Notifications card history now survives a restart (`storage_notifications.py`,
  new module) -- each of the 5 independent sources (`aprs_inbox.py`,
  `hamalert.py`, `monitor.py`'s fleet online/offline events, `hf_conditions.py`'s
  geomagnetic-storm alerts, `brandmeister_lastheard.py`) still owns its own
  in-memory `deque(maxlen=N)` exactly as before -- this only adds a SQLite-
  backed shadow copy each source reseeds itself from at construction time,
  same "own db file, connection-per-call" shape as `storage_activity.py`,
  kept as a separate file/table since it's a different concern (external
  notification events, not fleet talker activity).** Deliberately did NOT
  add one shared "last 25-50 total" cutoff across all five sources --
  each keeps its OWN existing cap (50 for aprs/hamalert/solar/brandmeister,
  100 for fleet events) as its own independent SQLite prune limit too, so
  a chatty source can never crowd out a quiet one's history. Every
  per-source payload is stored as an opaque JSON blob (`log_notification(source,
  at, payload, limit)` / `recent(source, limit)`) rather than columns,
  since the five sources' entry dicts have genuinely different shapes
  (aprs: `from`/`to`/`message_text`/...; fleet: `ip`/`name`/`kind`/...;
  etc.) -- a single `source`/`at`/`payload` table avoids either five
  separate tables or one wide table NULL for four sources out of five on
  every row. **The seeding order matters and had to match each source's
  own insertion convention, not just "load them in DB order":**
  aprs_inbox.py/hamalert.py insert new events via `appendleft()` (newest
  at index 0), while monitor.py/hf_conditions.py/brandmeister_lastheard.py
  use plain `append()` (newest at the end, reversed only when read via
  their own `fleet_events()`/`alert_events()`/`events()` accessors) --
  `recent()` always returns oldest-first specifically so every caller can
  replay it through its OWN normal insert method (`appendleft()` for the
  first two, `append()` for the other three) and land on the exact same
  final deque state live traffic would have produced, without
  `storage_notifications.py` itself needing to know which convention any
  given source uses. `log_notification()`/`recent()` both swallow every
  exception and return silently (`None`/`[]`) rather than raising --
  `_record_fleet_event()`/`_check_alert_transition()` are called with
  their module's own lock already held (documented in their own
  docstrings), and a DB hiccup must never turn into a stuck lock or a
  crashed listener thread, same degrade-gracefully contract as every
  other integration in this project. One accepted, deliberate gap:
  `brandmeister_lastheard.py`'s `_seen_sessions` dedupe cache (which
  SessionIDs have already been recorded, to survive Brandmeister's own
  periodic event rebroadcast) is NOT persisted -- only the `_events`
  history is. A restart resets that dedupe window, so a still-ongoing
  session's rebroadcast could in principle produce one duplicate row
  right after a restart; this is a cosmetic/minor gap (an extra row, not
  lost data) not worth the added complexity of persisting a second,
  purely-internal cache alongside the user-visible one.

- **DVSwitch (Analog_Bridge) support on the Fleet Activity card was
  investigated and built against a hard real constraint: DVSwitch's own
  repos are closed-source, binary-only distributions -- confirmed live
  via a real GitHub issue thread (`DVSwitch/Analog_Bridge#16`, someone
  else asking the same question), NOT assumed from the repo just "looking
  small."** `github.com/DVSwitch/Analog_Bridge`/`MMDVM_Bridge`/
  `Analog_Reflector` are filesystem-mirror trees (default branch literally
  named `bookworm`) of shipped `.ini`/`.sh`/systemd files plus precompiled
  `bin/Analog_Bridge.{amd64,arm64,armhf,i386}` binaries -- no C++ source
  to grep for log format strings the way this project's other
  reverse-engineering efforts (openspot.py, digipi.py) could lean on.
  Every fact below came from real shipped config/script files or real
  third-party quotes, same "verify against the real thing" discipline as
  everywhere else in this file -- and where that search came up empty,
  the gap is disclosed here rather than papered over with a guess.
  - **First, an important premise correction, found only by actually
    reading the existing Fleet Activity code before proposing anything**:
    there is NO existing per-mode, per-time-bucket stacked series --
    what reads as "5 stacked modes" is `renderModeBreakdown()`'s single
    horizontal bar, sized by each mode's % SHARE OF THE WHOLE WINDOW (no
    x-axis at all), completely separate from the Chart.js line chart
    (which plots aggregate-or-per-hotspot, never per-mode). DVSwitch was
    added to the existing whole-window bar (the smaller of two possible
    scopes, confirmed with the user before building) -- the Chart.js line
    chart is untouched by this feature entirely.
  - **`Analog_Bridge.ini` has NO `[Log]` section despite the name
    suggesting one** -- confirmed by fetching the real file directly.
    Logging level is a bare `logLevel` key under `[GENERAL]`; the log
    DIRECTORY comes from an env var, `AnalogBridgeLogDir`, set in the
    real shipped `systemd/analog_bridge.service` unit
    (`Environment=AnalogBridgeLogDir=/var/log/dvswitch`), with the
    resulting filename (`Analog_Bridge.log`) confirmed via the repo's own
    `logrotate/Analog_Bridge` config. `config.DVSWITCH_LOG_PATH` hardcodes
    this confirmed default (`/var/log/dvswitch/Analog_Bridge.log`) rather
    than adding a per-hotspot override field -- same tradeoff already
    made for DigiPi's `/run/direwolf.log`, accepting that a
    nonstandard install (customized `AnalogBridgeLogDir`) won't work
    without code changes, rather than adding config surface for a case
    with no reported need yet.
  - **The sibling `MMDVM_Bridge.ini` DOES have a real `[Log]` section**
    (`DisplayLevel`/`FileLevel`/`FilePath`/`FileRoot`) -- a different
    component (the network/TG-facing bridge, not the audio bridge this
    feature reads from) with a different, MMDVMHost-style log config.
    Don't confuse the two if this is ever extended to read MMDVM_Bridge
    instead/also.
  - **Only ONE DVSwitch log line format was ever confirmed real** -- a
    directly-quoted `Begin TX: src=9268283 rpt=26045440 dst=40 slot=2
    cc=1 metadata=26045499` from a real GitHub issue
    (`DVSwitch/Analog_Bridge#5`). No end-of-transmission line, no bridge
    connect/disconnect line, and no talkgroup/reflector-change line were
    found anywhere after a genuine search (GitHub issue search, forum
    search, blog search) -- none of these were fabricated to fill the
    gap. **This is why DVSwitch activity is counted differently from
    every other mode on this card**: DMR/D-Star/YSF/P25/NXDN all log one
    Fleet Activity row on the END of a transmission (a start→end
    transition detected by `config.END_OF_TRANSMISSION_MARKERS`);
    DVSwitch, with no confirmed end marker, instead logs one row every
    time `monitor.py`'s `_check_dvswitch_tx()` sees a NEW `Begin TX:`
    line that's different from the last one recorded for that hotspot
    (tracked in `FleetMonitor._dvswitch_last_tx`, an in-memory-only dict,
    not persisted -- losing it on restart just means the very next poll's
    already-seen line gets counted once more, a one-time cosmetic
    over-count, not lost data). If DVSwitch's log format is ever
    confirmed to include a real end-of-transmission line (re-verify
    against a real device/log capture, not another web search), switching
    to a start→end transition the way every other mode works would be a
    real improvement worth making -- don't assume today's design is
    final, it's an explicitly disclosed compromise forced by a real gap
    in available information, not a preference.
  - **A live JSON status file, `/tmp/ABInfo_<port>.json`, was found via
    DVSwitch's own real `dvswitch.sh` control script source** (confirmed
    real fields: `last_tune`, `tlv.ambe_mode`, `tlv.rx_port`/`tx_port`,
    `digital.ts`, `mute`) as a materially more reliable alternative to
    log-tailing -- offered to the user as the recommended option
    (poll-and-diff the JSON, same transition-detection shape ASL3's own
    `RPT_ALINKS` keyed-state parsing already uses) but NOT what was
    built; the user explicitly chose the log-tail approach instead,
    accepting its disclosed TX-start-only limitation. If DVSwitch
    activity tracking is ever revisited, re-evaluate `/tmp/ABInfo_*.json`
    polling as the stronger option -- its reachability/permissions from
    an SSH session were never actually confirmed live, so that would need
    checking first, but the field shapes themselves are real and sourced,
    unlike the never-found end-of-transmission log line.
  - **`dvswitch_enabled` (per-hotspot, ASL3 only, default `False`) is a
    genuine opt-in, not inferred from `asl_node` being set** -- DVSwitch/
    Analog_Bridge is an optional add-on most ASL3 installs don't run.
    Follows the exact same "absent means off" checkbox convention as the
    existing `enabled` field (`app.py`'s hotspot POST handler: `"enabled"
    in request.form`), and the exact same visible-field/hidden-form-field
    sync shape `setup.html` already uses for `asl_node`
    (`hs-dvswitch-enabled` -> `form-dvswitch-enabled` in
    `submitHotspot()`, restored in `editHotspot()`). No new validation
    needed on import (`/api/import_backup`) -- unlike `asl_node`, a plain
    bool isn't shell-interpolated, so it needs no digits-only check.
  - **`config.build_asl_status_cmd()`'s new `dvswitch_enabled` param
    appends the log tail onto the SAME one-shot SSH command string**
    (`; tail -n 20 ...`), not a second connection -- confirmed from
    `monitor.py`'s own `_ssh_exec()` that every poll opens a fresh
    connection, runs exactly one command, and closes it immediately, with
    no persistent session for a second command to attach to. This is the
    same shape `rpt xnode` itself already uses alongside the shared
    temp/uptime/CPU one-liner (`_LINUX_HOST_STATS_CMD`).
  - **`FLEET_MODE_COLOR_VARS` needed a real 6th entry, not just "add
    DVSwitch to the loop and it'll work"** -- it's indexed positionally
    (`mode_breakdown[i] % length`), so a 6th mode with only 5 colors
    would silently wrap back to index 0 and collide with whichever mode
    happens to be busiest in the window, not necessarily DMR. Added
    `--danger` (the one token in the "instrument panel" palette not
    already used in this array) rather than inventing a new color.

- **The DVSwitch CARD (v3.67, distinct from the Fleet Activity mode above)
  turned an "unconfirmed, likely fabricated" finding into a confirmed one
  -- a real user directly SSH'd into their own device and pasted real,
  live `Analog_Bridge.log` output, which is a stronger source than
  anything either research pass before it had found.** That live paste
  confirmed three things at once: (1) `call=<callsign>` on the `Begin TX:`
  line IS real (the earlier research had traced it to a suspicious,
  minutes-old GitHub account and flagged it as likely fabricated -- both
  can be true: that specific account was still untrustworthy, but the
  field itself turned out to be real on at least some Analog_Bridge
  versions); (2) a genuine LIVE vocoder-degradation signal exists at
  Analog_Bridge startup (`DV3000 not found... (Reset failed)` -> `Using
  software MBE decoder version 1.2.3`), upgrading what was previously
  scoped as "config intent only, no live health signal exists" to a real
  detected-fallback state; (3) `PTT off (keyed for 2527 ms)` lines exist
  but their correlation to a specific `Begin TX:` event was never
  confirmed -- deliberately NOT used for a transmission-duration field in
  this card, since assuming a 1:1 pairing without confirming it would be
  exactly the kind of unverified inference this project's research
  discipline exists to avoid. **Lesson**: when a live user capture
  contradicts an earlier, more heavily-researched-but-secondhand
  conclusion, the live capture wins -- don't keep treating the secondhand
  research as more authoritative just because more effort went into it.
- **`monitor.py`'s DVSwitch parsing now composes THREE distinct SSH
  sub-commands onto the one existing ASL3 connection** (log tail, a
  targeted grep for the vocoder line, and one
  `cat /tmp/ABInfo_<port>.json` per configured bridge port), each wrapped
  in an `echo`'d marker (`config.DVSWITCH_TAIL_MARKER`/`_VOCODER_MARKER`/
  `_ABINFO_MARKER`) so `_split_dvswitch_sections()` can reliably split one
  combined command's output back into named sections, rather than
  guessing by line position the way the simpler Begin-TX-only version
  (Fleet Activity mode) got away with. The vocoder line needed its OWN
  `grep` over the whole log file, not just a read of the existing 20-line
  tail -- the fallback message only ever appears once, at Analog_Bridge
  process startup, and would scroll out of a short tail almost
  immediately once any real transmission activity accumulates.
- **Multi-bridge support (multiple Analog_Bridge instances on one ASL3
  node, each addressed by its own port) is confirmed real** -- from
  Analog_Bridge.ini's own `[USRP]` section comment ("make two ini files
  ... launch each instance with its own ini file") and `dvswitch.sh`'s
  real source (each instance writes its own `/tmp/ABInfo_<port>.json`).
  `dvswitch_ports` (hotspots.json) is a comma/newline-separated string,
  same free-text-then-split convention as `openspot4_extra_pass`
  elsewhere in this app, not a JSON list -- parsed independently in THREE
  places that all need to agree (`app.py`'s `/setup` POST handler,
  `/api/import_backup`, and `monitor.py`'s `_dvswitch_port_list()`), each
  re-validating digits-only since these are shell-interpolated over SSH.
- **`last_tune` turned out to be the WRONG field for the common case,
  confirmed by a real `/tmp/ABInfo_<port>.json` capture from the same
  live device the Begin TX/vocoder findings above came from -- a genuine
  bug caught within hours of shipping, not a hypothetical.** The original
  version used `last_tune` as the sole tuned/idle signal, explicitly
  flagging its idle-state shape as unverified. The real capture showed
  `last_tune: ""` (empty) on a bridge that was, at that exact moment,
  demonstrably active -- the same instance had just logged real `Begin
  TX: ... dst=603 ... call=W1ZLA` transmissions. So the original code
  would have shown this bridge as "idle" while it was genuinely
  configured and relaying traffic -- confirmed wrong, not just
  theoretically risky. `last_tune` is specific to setups that do DYNAMIC
  reflector retuning (still unconfirmed shape, no real example seen) --
  it's simply blank for a bridge with a fixed/static target talkgroup,
  which is the common case. The real signal for that case is the
  `digital` object: `digital.tg` (`"603"` in the real capture -- exactly
  matches the same Begin TX line's own `dst=` value) and `digital.call`
  (`"W1ZLA"` -- matches that line's `call=`, this bridge's own registered
  callsign, not a live caller's). Fixed in `_parse_dvswitch_bridges()` to
  prefer `digital.tg` (formatted as `"TG <n>"`), falling back to
  `last_tune` only when `digital.tg` is absent -- for whatever the
  dynamic-tuning shape eventually turns out to be, still unverified.
  Independently useful cross-check from the same real capture:
  `use_fallback: "true"` matched what the log-grep-based vocoder
  detection had already found independently for this exact device --
  two different data sources agreeing is good evidence the vocoder
  detection approach itself is sound, not just lucky once.
- **`dvswitch_vocoder` is intentionally a 3-state field
  (`"software"`/`"hardware"`/`None`), not a boolean** -- collapsing
  `None` (no DVSwitch log output read at all -- Analog_Bridge not
  running, wrong log path, permission issue) into `"hardware"` would
  falsely imply a healthy hardware vocoder when the real situation is
  "no data." `_parse_dvswitch_vocoder()` only returns `None` when BOTH the
  tail section AND the vocoder-grep section came back empty; if the tail
  has real content but the grep found no fallback message, that's a
  genuine (if inherently unprovable-as-positive) "hardware, no fallback
  seen" result, kept distinct from true "unknown."
- **The DVSwitch card WAS deliberately left out of the Card order drag
  list at first ship (v3.67), then wired in for real in v3.70 -- exactly
  the "generalize `computeCardOrders()` properly" path the original
  scope-cut note said to take if it became a real ask.** Confirmed while
  researching this that `computeCardOrders()` itself needed ZERO changes
  -- it already takes an arbitrary `sentinels` array and is completely
  agnostic to what a sentinel represents; only the callers needed new
  code, mirroring Cameras' existing dynamic-list shape exactly (same
  precedent the original note pointed at):
  - **`app.py`**: `_overflow_sentinels()` gained a third block (after
    `_SENTINEL_DEFS` and the camera block) iterating `hotspots` filtered
    on `dvswitch_enabled`, reading each one's own `dvswitch_position`
    field (default 0) rather than a `settings.get(...)` lookup -- there's
    no `show_*` settings gate for this feature the way cameras have
    `show_cameras`, since enablement is purely per-hotspot. A new
    `/api/reorder_dvswitch` route mirrors `/api/reorder_cameras`'s exact
    `{id: position}` shape, but loads/mutates/saves `hotspots.json`
    (matched by `ip`) instead of `cameras.json` (matched by `id`) --
    DVSwitch cards don't have their own separate config store the way
    cameras do, position just lives on the same hotspot dict
    `dvswitch_enabled` already does. `/api/data` now also echoes back
    `dvswitch_position` per hotspot (straight passthrough from
    hotspots.json, same as `lat`/`lon`/`type` already were) so the
    frontend has it without a second fetch.
  - **`dashboard.html`**: `sentinelDataIp()` gained a `dvs-` prefix
    branch (parallel to `cam-`). `renderCards()`'s `sentinels.push()`
    sequence gained a DVSwitch block, sourced from the SAME `data`
    argument `renderCards(data)` already has synchronously -- a real,
    deliberate improvement over how cameras have to do this: cameras
    poll `/api/cameras` on their own independent 30s interval into a
    module-level `cameras` array, which has a real (self-healing but
    real) race window on first load if `renderCards()` fires before that
    first fetch resolves; DVSwitch enablement/position both arrive on
    the exact same `/api/data` response `renderCards(data)` is already
    called with, so there's no equivalent race to reason about.
    Resolved order gets stashed in a new module-level `lastSentinelOrders`
    right after `computeCardOrders()` runs, since DVSwitch cards don't
    exist as DOM nodes yet at that point for a direct `.style.order`
    assignment the way the 13 fixed sentinels/cameras get -- unlike
    those, `renderDvswitchCards()` is a SEPARATE function (called right
    after `renderCards()` in `refresh()`) that fully rebuilds its own
    `#dvswitch-cards` container from scratch every poll, so the order
    value gets baked directly into each card's inline `style="order:N"`
    at generation time instead.
  - **`setup.html`**: a new inline Jinja interleave block
    (`{% for hs2 in hotspots if hs2.get('dvswitch_enabled') and
    hs2.get('dvswitch_position', 0) == loop.index0 %}`) placed in the
    exact same relative template position cameras' own interleave block
    occupies -- immediately after it, right before the real hotspot row
    -- since `computeCardOrders()`'s stability-guarantee tiebreak (see
    its own comment) depends on `sentinels.push()` order in
    `dashboard.html` and template declaration order in `setup.html`
    staying in sync; declaring DVSwitch anywhere else here would silently
    desync the two files' tie-break behavior for genuinely tied
    positions, the exact bug class the "card position doesn't save"
    saga earlier in this file was about. `saveCardOrder()` gained a
    `dvswitchPositions` object built the identical way `cameraPositions`
    already is (`allIds.filter(id => id.startsWith('__dvswitch__'))`),
    POSTed to the new `/api/reorder_dvswitch` route. The existing
    `isSentinel`/`cardOrderTiebreak` logic needed ZERO changes -- both
    are blanket `__`-prefix checks, already correctly generic across any
    sentinel type including this new one.
- **Settings → Cards' "Optional dashboard cards" section (v3.69) split
  into two labeled subgroups ("Dashboard cards" / "Notifications card
  sources") after growing to 17 undifferentiated toggles in one flat
  list** -- purely a relabel/regroup via a new `.toggle-subhead` CSS
  class, no settings keys, toggle element IDs, or `saveCards()` JS
  changed (it already read every field by `getElementById`, not DOM
  position, which is what made this safe to reorder freely). Also
  renamed the two oldest notification-source toggles -- "Show APRS
  Messages card" and "Show HamAlert card" -- to "APRS messages in
  Notifications"/"HamAlert alerts in Notifications", matching the
  "...in Notifications" phrasing the three newer sources (fleet/solar/
  Brandmeister alerts) already used. Those two labels were stale relics
  of the v3.51 Notifications-card merge (see that entry above) -- they
  read like they still created their own separate cards, which stopped
  being true the moment they became sources feeding one merged card. If
  another notification source is ever added, use the same "...in
  Notifications" phrasing and put it in the second subgroup, not the
  first.

- **A real "hardware AMBE vocoder working" success message is now
  confirmed live (v3.71) -- the same user, same device, same day,
  walked from a real software-fallback failure through a real config fix
  to a real hardware success, all captured live over SSH.** After fixing
  a duplicate/uncommented `address =` line in `Analog_Bridge.ini`'s
  `[DV3000]` section (two active `address` keys in one section --
  Analog_Bridge's ini parser was silently using the first, `127.0.0.1`,
  ignoring the intended `/dev/ttyUSB0` line below it) and restarting
  Analog_Bridge, the log showed `Connecting to DV3000 hardware......` ->
  `Begin DV3000 decode` -> **`Using hardware AMBE vocoder`** -- directly
  contradicting this file's own earlier claim ("there's no confirmed
  *success* message"). `config.DVSWITCH_HARDWARE_VOCODER_PATTERN` now
  matches this directly; `_parse_dvswitch_vocoder()`'s "hardware" result
  is a genuine positive detection now, not an inference from "no fallback
  message seen" -- the frontend's `dvs-vocoder-tag` text changed from
  "no fallback seen" to "confirmed" to match.
- **This same real sequence exposed a genuine bug in the vocoder grep
  itself: `grep -m1` (first match) is wrong when a single day's
  (not-yet-rotated) log can contain BOTH an older fallback line and a
  newer success line, which is EXACTLY what happened here** -- the
  earlier software-fallback message from before the config fix was still
  in the same file as the new hardware-success message after it, and
  `-m1` would have kept reporting the stale first-seen result forever
  (until the daily log rotation happened to clear it) even though the
  real current state had changed. Fixed by matching both patterns in one
  `grep -E` and piping through `tail -1` to take the LAST (most recent)
  match instead -- the same "want the newest, not the first, occurrence"
  principle `_check_dvswitch_tx()`'s own Begin-TX-line scan already
  applies to its 20-line tail, just needed applying to the vocoder grep
  too once a real scenario surfaced that could contain more than one
  match. `config.DVSWITCH_VOCODER_GREP_PATTERN` combines both patterns
  for this single alternated grep.

- **Live RX/TX state + link status for the DVSwitch card (v3.72) reads a
  SECOND, genuinely different DVSwitch component's log --
  `MMDVM_Bridge.log`, not `Analog_Bridge.log`** -- confirmed real via
  another live capture from the same user/device, prompted by them
  showing a screenshot of the OFFICIAL DVSwitch Dashboard's own "TRX
  Info: RX DMR" indicator and asking whether the same thing was viable
  here. It's viable, and the data turned out richer than
  `Analog_Bridge.log` ever had:
  - **Real, confirmed end-of-transmission lines with actual duration/
    loss/BER** -- `Analog_Bridge.log` never had one of these (see the
    Fleet Activity DVSwitch mode entries above, which is why THAT
    feature counts transmission-starts, not completions). This log has
    both: `"DMR Slot 2, received network voice header from W1ZLA to TG
    603"` (start) paired with `"DMR Slot 2, received network end of
    voice transmission, 2.6 seconds, 0% packet loss, BER: 0.0%"` (end),
    and the D-Star equivalents (`"D-Star, received network header
    from..."` / `"D-Star, received network end of transmission..."`).
  - **Explicit link-status lines** -- D-Star has a direct, literal one:
    `D-Star link status set to "Not linked          "`. DMR's is
    implicit, inferred from which of four possible lines was seen most
    recently: `"DMR, Logged into the master successfully: ..."` (linked)
    vs. `"DMR, Closing DMR Network"` / `"DMR, Connection to the master
    has timed out, retrying connection"` (not linked).
  - **Deliberately scoped to DMR + D-Star only, by explicit user choice**
    -- YSF/P25/NXDN each write to their OWN separate gateway log file
    (`YSFGateway-*.log`/`P25Gateway-*.log`/`NXDNGateway-*.log`, confirmed
    real via a live `ls /var/log/mmdvm/`, not tailed here). Covering
    those too would mean tailing up to 3 more log files.
  - **The path is DATE-STAMPED** (`MMDVM_Bridge-YYYY-MM-DD.log`, confirmed
    via a real `ls -la /var/log/mmdvm/`) -- resolved with the exact same
    "newest matching file via `ls -1tr | tail -1`" pattern
    `config.SSH_STATUS_CMD` already uses for WPSD's own differently-
    pathed `MMDVM-*.log`, not a new technique. A same-named but empty
    `MMDVM_Bridge.log` (no date) also exists in that directory and is
    NOT the active file -- confirmed live, don't tail that one by mistake
    if this is ever touched.
  - **A real edge case, confirmed live, not hypothetical**: `"DMR Slot 2,
    network watchdog has expired, 0.1 seconds, ..."` fired mid-
    transmission (a brief network hiccup) in the actual captured log,
    immediately followed by a `"late entry"` line resuming the SAME
    transmission a few milliseconds later. It shares the same trailing
    "N.N seconds, X% packet loss, BER: X.X%" shape as a real end-of-
    transmission line, so `_parse_dvswitch_mmdvm_live()` matches it as an
    end-like event too -- this can make the live indicator flicker
    idle-then-active-again within milliseconds, invisible at this app's
    5s poll cadence, so no extra de-flicker logic was added for it.
  - **`_parse_dvswitch_mmdvm_live()` is stateless -- re-scans the tail
    fresh every poll, no state carried between polls**, same shape as
    every other DVSwitch/WPSD log-tail parser in this file. Real,
    accepted limitation: if a still-ongoing transmission's own START line
    has already scrolled out of the (40-line) tail window by the time of
    a given poll -- plausible for an unusually long call with a lot of
    interleaved `DMR Talker Alias` lines in between -- this under-reports
    idle rather than guessing. Not treated as a bug; a stateless re-scan
    can't do better than what's actually visible in its own window.
  - **Deliberately did NOT try to enrich the existing `dvswitch_heard`
    list (sourced from `Analog_Bridge.log`'s `Begin TX:` lines) with this
    log's real duration data.** The two logs' events aren't easily
    correlated (end-of-transmission lines here carry no callsign, only
    slot/duration/loss/BER, so matching one to a specific earlier
    Begin-TX-sourced heard entry would need tracking "which call is
    currently open per slot" state purely to attribute a duration after
    the fact) -- scoped out to keep this an additive, independent
    capability (a new live-status row) rather than risking the already-
    working Last Heard list. Worth revisiting later if a real want for
    accurate per-entry durations surfaces.

- **Per-card gear-icon drawer (v3.73) replaced whole-card click entirely
  on both hotspot cards and camera cards -- mocked up and iterated with
  the user (three rounds: coexist-with-click, then full replacement/
  merge, then per-field save + test buttons + ASL3 quick actions + a
  Camera variant) before any of this was built for real.** `.hotspot-card`
  lost its `onclick`/`cursor:pointer`/hover-shadow entirely; `.card-gear-
  btn` (in `.card-header-right`) is now the only entry point into
  `openHotspotDrawer()`. The pre-existing info drawer (Recent talkers/
  Linked nodes, Brandmeister, Update status) is unchanged in substance,
  just joined by a new editable Settings block above it in the same
  drawer -- not a second drawer.
  - **`/api/data`'s live-polled snapshot deliberately never carries SSH/
    admin credentials** (it's read by every open browser tab every 3s) --
    so the drawer's Settings block can't just read `lastApiData` the way
    the rest of the drawer does. A new `GET /api/hotspot_config/<ip>`
    (returns the raw stored dict straight from `hotspots.json`, including
    `user`/`pass`) is fetched once, asynchronously, right when the drawer
    opens -- same "send credentials to the browser once, on an explicit
    action" posture `setup.html`'s edit form already has (server-rendered
    into the page), not a new precedent.
  - **A new `POST /api/update_hotspot` JSON endpoint exists because there
    was no existing one to reuse.** `/setup`'s POST handler (form-encoded,
    always `redirect("/setup")`) and `/api/toggle_hotspot/<ip>` (also a
    redirect) are the only two hotspot-upsert paths in the app, and both
    return HTML, not JSON -- unusable for a per-field instant-save UI with
    no page reload. The new endpoint mirrors `/setup`'s validation rules
    by hand (same digits-only guards on `asl_node`/`dvswitch_ports`, same
    checkbox-absent-means-off convention, expressed as JSON) -- **kept in
    sync manually; if you touch one, check the other**, same discipline
    already documented elsewhere in this file for paired config
    descriptions that don't share code.
  - **The new endpoint merges onto the EXISTING stored hotspot dict
    (`dict(existing)` then overlay validated fields) rather than
    rebuilding one from scratch the way `/setup`'s handler does.** This
    matters specifically because `/setup`'s fresh-rebuild approach means
    editing ANY field via the full Settings form already silently drops
    `dvswitch_position` (set by the totally separate `/api/reorder_dvswitch`
    call, never round-tripped through the main edit form/its hidden
    fields) back to its default -- a pre-existing latent bug, not
    introduced here. It was never especially visible before, because the
    full form only saves once per explicit submit; the drawer's per-field
    auto-save fires on every single blur, so the identical bug would have
    silently reset a DVSwitch card's position on every keystroke-worth of
    edit if the new endpoint rebuilt fresh the same way. Verified live
    with a real `test_client()` sequence: set `dvswitch_position` via
    `/api/reorder_dvswitch`, then save an unrelated field
    (`pass`) via `/api/update_hotspot`, confirm `dvswitch_position`
    survives. `/setup`'s own handler was deliberately NOT touched to fix
    this -- out of scope for this change, and lower risk to leave a
    known, narrower gap in an existing stable path than to alter it while
    adding a new one.
  - **Field values are set via `.value` PROPERTY assignment
    (`setVal(id, value)` -> `element.value = value`), never interpolated
    into a `value="..."` HTML attribute string.** A password or name
    containing a literal `"` would otherwise break out of the attribute
    -- the same class of bug as the documented onclick-string-
    interpolation gotcha elsewhere in this file (HTML-attribute-escaping
    a value doesn't protect it if something later re-parses the
    attribute), just for a `value` attribute instead of an `onclick`
    handler. The Settings fields are rendered with empty/no `value`
    attributes and populated via JS property assignment immediately
    after, which is safe regardless of what characters the stored
    credential contains.
  - **ASL3's Linked nodes section gained real quick actions** (a "Node #
    to connect…" field + Connect button, and a per-row Connect/Disconnect
    button) that call the SAME `/api/asl_connect` route the ASL Favorites
    card's `aslConnect()`/`aslSendConnect()` already use -- no new backend
    action, no duplicated SSH/ilink logic. Deliberately skips the
    Favorites card's "disconnect other favorites first" step, since this
    is a direct per-node action from the hotspot's own drawer, not a
    favorites-style exclusive select.
  - **A new `last_poll_at` field on `HotspotStatus`** (set in
    `monitor.py`'s `_check_one_wpsd`/`_check_one_asl3`, alongside the
    existing `offline_since`-clearing/failure-counter-reset logic, on
    SUCCESS only) backs the drawer's "Last SSH check Xs ago · OK" health
    line, using the existing `fmtAgoCompact(secs)` helper -- passed an
    already-computed ELAPSED-seconds value, not the raw epoch timestamp,
    the exact distinction whose violation caused the real `fmtAgo`
    double-declaration bug documented earlier in this file. Stays `None`
    for openSPOT4 (push-based, no poll cycle to time) -- that type's
    health line reads `status`/`offline_since` alone instead, phrased as
    "WebSocket connected/disconnected" rather than a check interval.
  - **The camera drawer needed NO new backend endpoint at all** -- unlike
    hotspots, `/api/cameras` GET already returns the full stored camera
    dicts (including RTSP credentials/Bambu access code), already polled
    into the client-side `cameras` array every 30s, and `/api/cameras`
    POST already merges onto an existing camera dict when `id` matches.
    The drawer's per-field save (`saveCameraField()`) just builds the
    full merged camera object client-side and POSTs it to that same
    existing route, the same "send the whole object, not just the
    changed field" shape `saveHotspotField()` uses for the (necessarily
    new) hotspot endpoint. "Test stream" reuses `/api/test_camera`
    verbatim; the health line reuses the existing `/api/camera_status`
    route and `CAMERA_STATE_LABELS` map (`fetchCameraStatus()` already
    established both) rather than inventing new status plumbing.
    Cameras have no `enabled` field in their schema at all (confirmed by
    reading `api_cameras_post()` before assuming one existed) -- the
    drawer's camera variant has no Enabled toggle, unlike the hotspot
    variant.
  - **No Delete button in either drawer, by explicit design** -- deletion
    stays a full-Settings-only action (`/setup#hotspots` /
    `/setup#cameras`, reached via each drawer's "Open in full Settings →"
    link), not something reachable from a quick per-card drawer.
  - **Verified with a live route/decorator-count sanity check per this
    file's own `str_replace`-decorator-loss warning above** (64
    `@app.route`-decorated functions via a plain grep, 64 confirmed via
    an AST walk, 65 total Flask URL rules = 64 + the implicit
    `/static/<path:filename>` route) before considering the two new
    routes done -- not just a green `py_compile`.

- **Quick Settings drawer "Jump to Settings" shortcuts (v3.74) needed NO
  backend changes at all** -- `SETTINGS_TABS`/`shortcutGridHtml()`
  (`dashboard.html`) are plain `<a href="/setup#<tab>">` chips, reusing
  the exact same generalized `/setup#<tabname>` deep-link mechanism
  already documented above (the one that replaced the old `#version`-only
  special case) -- no new route, no new JS mechanism, just more chips
  pointing at an already-general capability.
  - **Both "attention" badges reuse existing signals rather than
    computing anything new server-side.** The Version info chip's dot
    reads a new module-level `dashboardUpdateAvailable` variable, set
    inside the EXISTING `checkDashboardUpdateIndicator()` (which already
    polled `/api/check_for_updates` every 5 min to drive the toolbar's
    lightning-bolt icon) -- the function just also stashes the result now,
    instead of only touching `#update-indicator`'s `style.display`. The
    Hotspots chip's count badge is a plain
    `(lastApiData || []).filter(hs => hs.status === 'Offline').length`,
    computed fresh every time the drawer renders from data every other
    part of the dashboard's 3s poll cycle already has in hand -- no
    separate fetch, no new state to keep in sync.
  - **All 9 tabs got a chip, deliberately not a curated subset** -- a
    2-column grid of short labels is barely bigger than the single link
    it replaced, and picking a "favorites" few would just relocate the
    "how do I reach the other ones" problem rather than solve it. If this
    ever feels cluttered, dropping the least-used ones (Backup/Version
    are the likely candidates -- Version already has its own separate
    entry point via the toolbar's lightning bolt) behind a smaller
    fallback link is the documented alternative, not assumed away.
  - **A real mistake caught while cutting this release, not hypothetical:
    v3.73 was originally codenamed "Cobain", duplicating v3.52's Kurt
    Cobain entry already in `VERSION_CODENAMES` -- missed at the time
    because nothing checks the list for duplicates, only that a name is a
    real, verifiable deceased rock musician.** Corrected to "Morrison"
    (Jim Morrison, The Doors, 1943-1971) in the same commit that added
    v3.74 "Vaughan" (Stevie Ray Vaughan, 1954-1990) -- fixed forward with
    a new entry, not by rewriting already-pushed git history. If a codename
    is ever picked again, grep the existing `VERSION_CODENAMES` list first
    for the candidate surname, not just for whether the person is real.
- **Quick Settings' "About this dashboard" status panel (v3.95) --
  version, dashboard uptime, HOST uptime, and live CPU/temp/memory --
  reuses data that was already being fetched, with one deliberate
  exception.** `/api/host_stats` was already polled unconditionally
  every few seconds for the toolbar's own CPU/memory bar (only the
  RENDER was gated by `SHOW_HOST_STATS`, not the fetch itself) --
  `fetchHostStats()` now also stashes the raw response into a shared
  `lastHostStats` global before calling `updateSysbar()`, so a second
  consumer can read the exact same in-flight data with zero new
  requests. Version/codename needed the one genuinely new fetch (a
  one-time `/api/version` call, cached in `dashboardVersionInfo` --
  that data never changes without a restart, so unlike the update
  check it isn't re-polled on an interval); the update-available badge
  reuses the already-existing `dashboardUpdateAvailable` global the
  Version info shortcut chip already set.
  **Two different uptimes, deliberately not collapsed into one** --
  "Dashboard uptime" is `time.time() - START_TIME` (the Flask process's
  own age, already computed for `/api/activity`'s Fleet-Activity-only
  consumer, now ALSO added to `/api/host_stats` so it's available
  regardless of whether that card is enabled) vs. "Host uptime", a
  genuinely new reading of `/proc/uptime`'s first field
  (`HostStats._sample_uptime()`) -- the actual MACHINE's age, which can
  differ substantially from the app's own uptime after a container/
  service restart with no real reboot. Both are sent as raw seconds
  (not pre-formatted strings, unlike `temp`/`mem_pct`/etc. elsewhere in
  this same response) specifically so one shared client-side formatter
  (`fmtDaysHours()`) can handle both -- extracted from what used to be
  `renderFleetActivity()`'s own inline day/hour math, now used by both
  call sites instead of two copies of the same arithmetic.
  **`renderSettingsDrawer()` couldn't get the same "return early if not
  open" guard every other drawer's render function has** -- it's ALSO
  called once, deliberately, while the drawer is still closed (from
  `initMap()`, to pre-seed the QSO Log section's `#qso-show-toggle`
  checkbox into the DOM before it's ever referenced) -- an internal
  guard would silently break that call and null-ref the line right
  after it. Fixed by guarding at the POLL-LOOP call site instead
  (`refresh()` checks `#settings-drawer`'s own `.open` class before
  calling), leaving the function's existing "always rebuild
  unconditionally" contract intact for its other callers.
  **Verification needed monkeypatching the sampler METHODS, not just
  the instance attributes** -- a first attempt at a live-rendered check
  set `host_stats._cpu`/`_temp`/etc. directly after import, but
  `HostStats`'s own background thread (already running since
  `app.py`'s module-level `host_stats.start()`) sampled again on its
  normal 5s interval before the screenshot fired, silently overwriting
  the injected values with real (N/A, this is a Windows dev box) ones.
  Fixed by patching `HostStats._sample_cpu`/`_sample_temp`/`_sample_
  mem`/`_sample_uptime` on the class itself (so every future loop
  iteration keeps returning the fake values, not just the one
  instant right after import) and waiting a full interval before
  taking the screenshot -- confirmed correct output (real-looking
  numbers, both uptimes showing genuinely different values) before
  trusting the feature, then deleted the throwaway script, same "never
  committed, exists only to prove the styling renders correctly"
  discipline as the ASL Control "recently active" verification above.

- **ASL Favorites' "ASL Control" sidebar (v3.75) is styled and laid out
  closer to [AllScan](https://github.com/davidgsd/AllScan)'s node-control
  panel (a local-node status strip, a keyed banner, denser per-node rows)
  than the compact card, but is deliberately NOT a skin-copy of AllScan's
  own look -- every token/class it uses is this app's own, same as every
  other mockup-to-real-feature in this project.** Reuses the compact
  card's existing state/functions wholesale rather than duplicating them:
  `aslFavorites`/`aslSelectedHotspotIp`/`lastApiData` are the same
  globals; `aslConnect()`/`addAslFavorite()` gained optional trailing id
  params (`statusElId`, `nodeInputId`/`labelInputId`) so the sidebar can
  point them at its own DOM elements -- every existing call site (no
  extra args) is unchanged, defaulting back to the compact card's own
  element ids. `saveAslFavorites()` now calls `renderAslDrawer()`
  alongside `renderAslFavorites()` on every save, and the main poll
  loop's `refresh()` does the same every 3s -- `renderAslDrawer()` itself
  is a no-op (`if (!drawer.classList.contains('open')) return;`) whenever
  the sidebar isn't open, the same "cheap to call unconditionally, real
  work only happens if open" shape every other drawer-in-a-poll-loop
  pattern in this app already uses.
  - **The local-node status strip is genuinely new UI, not new data** --
    the controlling hotspot's own `is_active`/`active_call`/
    `temperature`/`uptime` were already in `lastApiData` every poll
    (same fields the hotspot card drawer's own stats row reads); the
    compact ASL Favorites card just never surfaced them, since it only
    ever displays the FAVORITES' live state, never the controlling
    node's own.
  - **"Quick connect" (any node #, not just a saved favorite) reuses
    `/api/asl_connect` directly, with no new endpoint** -- it's
    `aslConnect(ip, node, 'connect', 'asl-drawer-status')` called with a
    node that was never added to `aslFavorites` at all. This still
    respects the compact card's "Keep existing connections" checkbox
    (looked up by a fixed id, `asl-fav-keep-connections`, which is always
    in the DOM regardless of whether the sidebar is open, since the card
    itself never goes away) -- same behavior, not a second implementation
    of the "disconnect others first" logic.
  - **Monitor mode (v3.76) was added only after the code above was
    directly fetched and read, not guessed.** `raw.githubusercontent.com/
    davidgsd/AllScan/master/astapi/connect.php` was pulled fresh and its
    `switch($button)` block read in full: `case 'monitor':` maps to ilink
    `2` (non-permanent) / `12` (permanent) -- confirming `ASL_ILINK_MONITOR
    = 2` is correct, not assumed from the "code 2 is probably Monitor"
    guess this file's own earlier entry had explicitly flagged as
    unverified. The same source also revealed codes this app does NOT
    implement and deliberately isn't adding here: `13`/`12`/`18` are
    "permanent" variants of Connect/Monitor/Local-Monitor (persist across
    an Asterisk restart -- this app has no "permanent" concept anywhere),
    `8`/`18` are "Local Monitor" (a stricter receive-only mode that
    doesn't relay onward to other connected links, distinct from plain
    Monitor), and `6` with `remotenode=0` is "disconnect all" (this app's
    existing "Keep existing connections" logic achieves a similar effect
    already, by disconnecting each of *this app's own* favorites
    individually rather than one blanket ilink-6 call -- not the same
    mechanism, kept as-is). **If Local Monitor, permanent links, or
    disconnect-all are ever requested, re-fetch and re-read this same
    file rather than assuming the pattern generalizes** -- confirmed once
    for Monitor specifically, not for the whole `case` block.
  - **`build_asl_ilink_cmd()`'s own validation guard
    (`ilink_code not in (...)`) had to be updated too** -- easy to miss
    since adding a new module-level constant doesn't itself widen a
    separate allow-list check elsewhere. Caught by a live end-to-end test
    (mocked SSH client, real `/api/asl_connect` call with
    `action: "monitor"`, asserting the actual built command string
    contains `ilink 2`) rather than trusting a green `py_compile` --
    without the guard fix, every real Monitor click would have raised a
    500 despite the route and JS both being correct.
  - **The "Keep existing connections" toggle got its own copy in the
    sidebar** (`#asl-drawer-keep-connections`), not shared with the
    compact card's (`#asl-fav-keep-connections`) -- `aslConnect()` gained
    a `keepCbId` param (same generalization pattern as `statusElId`
    before it) so each surface's buttons point at its own checkbox.
    Deliberately independent, not synced: this is a per-action modifier
    read fresh at click time, not a persisted setting, so there's nothing
    to keep in sync -- whichever surface you click Connect from, that
    surface's own checkbox applies. Matches AllScan's own `connect.php`
    too: `autodisc` there only affects the `case 'connect':` branch, not
    `monitor`/`localmonitor`/`disconnect` -- this app's existing
    `if (action === 'connect' && ...)` guard already matched that
    real behavior before Monitor was even added, so it needed no change.
  - **Monitor mode is sidebar-only, not added to the compact card's own
    table** -- that table is a plain 2-column layout with a single
    action button per row; adding a third button there would mean
    redesigning it, and the whole point of the gear-icon pattern
    elsewhere in this app is that the compact view stays simple while
    the drawer/sidebar is where a deeper feature set lives. Don't add
    Monitor to the card without deciding that tradeoff is worth it, not
    as an oversight to quietly fix.
  - **ASL3 hotspots' card-name link (v3.77) points at `/allscan`, not
    the bare IP every other hotspot type uses** -- `cardNameHref` in
    `renderCards()` branches on the same `isAsl` flag everything else in
    that function already checks, since an ASL3 node's web root has
    nothing useful at it (unlike WPSD, which serves its own admin
    dashboard there) -- AllScan, when installed, lives at `/allscan`
    specifically. This is a URL-shape assumption from the user, not
    independently re-verified against a live AllScan install the way
    e.g. the ilink codes above were -- if a real device ever shows
    AllScan living at a different path, fix this one spot plus the
    matching link in the ASL Control sidebar's local-node strip
    (`asl-ctrl-allscan-btn`, see next entry), which was deliberately
    built to reuse the exact same `http://<ip>/allscan` shape rather
    than a second guess.
  - **Reverted in v3.94, reported directly as a bug: "when you click the
    title it is going to the allscan page instead of the nodes main
    address."** The v3.77 assumption above (every ASL3 node's own web
    root has nothing useful at it) doesn't hold universally -- a node
    without AllScan installed just gets a dead/wrong link where the bare
    IP would have worked fine, and there's no way to know from this
    app's side whether a given node even has AllScan. `cardNameHref` is
    back to the bare IP for every hotspot type uniformly, `isAsl` no
    longer branches on it at all (still used elsewhere in the same
    function for plenty of other ASL3-specific rendering, so it wasn't
    dead-coded by this revert). The ASL Control drawer's own
    `asl-ctrl-allscan-btn` (still `http://<ip>/allscan`) is untouched --
    that's still there as an explicit, opt-in way to reach AllScan,
    just no longer also hijacking the card title for everyone.
  - **The sidebar's AllScan link (v3.81) went through a real mockup-
    iteration cycle before landing on its current shape, worth noting
    since it's the kind of thing that looks obviously fine on the first
    try and isn't.** Originally a plain text link (`asl-ctrl-allscan-
    link`) stacked under the local-node strip's uptime/temp figures --
    the user flagged, from a live screenshot of the running app, that it
    read as just another stat rather than an actionable link. Three
    placements were mocked up (a header button, a pill next to the node
    name, a full-width button below the strip); the user picked the
    full-width button but asked for it smaller than shown. Landed on
    `asl-ctrl-allscan-btn` -- a bordered, `--live`-tinted full-width
    button (`padding: 6px`, `font-size: 11px`, deliberately smaller than
    the mockup's `9px`/`12.5px`) sitting as its own element right after
    `.asl-ctrl-localnode`, not inside it. If this ever needs revisiting,
    a live screenshot from the user (like the one that caught this) is
    worth more than reasoning about the markup alone -- the corner-link
    version looked reasonable in isolation and only read as wrong once
    seen rendered in the actual app.
  - **Moved again in v3.82** -- from right after `.asl-ctrl-localnode`
    (near the top) to the very last element in the drawer, after the
    Add-favorite row and status span. Purely a position change (the
    button markup/CSS itself is untouched) -- if this drifts again,
    it's currently the last child of `.hs-drawer-body` in
    `renderAslDrawer()`'s template, with an inline
    `style="margin:14px 0 0"` overriding the class's own
    `margin-bottom` (which only made sense in the old top-of-drawer
    position).
  - **Dimmed again in v3.83** -- solid `--live`-filled background/border/
    text replaced with a muted grey border/text (`color-mix(in srgb,
    var(--live) 35%, var(--border))` border, `--text-muted` text) that
    only lights up to full `--live` on hover. Purely visual, no markup
    change.

- **ASL Control: per-favorite live stats + favorites.ini import (v3.83)
  -- built from real, fetched sources at every step, not assumed.**
  - **`stats.allstarlink.org/api/stats/<node>`'s FULL response shape was
    fetched live** (a real hub node, 27339) before writing
    `aslstats.py`'s new `favorite_stats()` -- confirmed real fields:
    top-level `node.Status`/`node.access_webtransceiver` (the
    Web-Transceiver flag AllScan's own Favorites Table colors on), and
    `stats.data.apprptuptime`/`totaltxtime`/`links` (an array of that
    node's own current link numbers). Rx% (`totaltxtime / apprptuptime
    × 100`) and LCnt (`len(links)`) match AllScan's own documented
    definitions exactly. A bogus node number returns a real, clean 404 --
    confirmed live, not assumed -- which is what `favorite_stats()`
    reports back as `{"found": False}` for the "Not in ASL DB" state.
  - **Deliberately does NOT use the stats API's own `keyed` field for
    "keyed now" coloring.** AllScan's real changelog (fetched from the
    actual repo, not summarized secondhand) documents this field as
    unreliable: *"ASL stats API data for many nodes shows a 0
    stats.keyed value even when the node is in fact keyed... keyed
    status can be detected from changes in \[totalkeyups/totaltxtime\]
    between stats requests"* -- AllScan's own fix is a two-poll diff.
    This app already has a genuinely reliable keyed signal for anything
    actually linked to a controlling hotspot (`asl_linked_nodes`,
    SSH-sourced, no known reliability gap), so `favorite_stats()`
    doesn't return `keyed` at all, and `renderAslDrawer()`'s row
    coloring only ever lights up "keyed" (red) from that existing link
    data -- a favorite the stats API calls unkeyed-but-you're-not-sure
    never gets guessed at.
  - **Caching/rate limit**: AllScan's changelog also documents a real
    30-requests/minute cap on this API. `favorite_stats()` reuses
    `AslStatsClient`'s existing per-node cache (`ASLSTATS_CACHE_TTL`,
    120s) under a separate `fav:<node>` cache-key prefix (distinct from
    `linked_node_info()`'s own keys, since this reads the queried node's
    OWN top-level stats, not a `linkedNodes` sub-array entry). The new
    `/api/asl_favorite_stats?nodes=a,b,c` route is ONE shared server-side
    poll across every open browser tab (same pattern as every other
    integration in this app) -- AllScan's own architecture has each
    browser tab poll independently, which its own changelog flags as a
    real risk of hitting the rate limit with multiple open tabs; this
    app's shared-cache design avoids that class of problem by
    construction. `dashboard.html`'s own `fetchAslFavoriteStats()` adds a
    second, client-side throttle (`ASL_FAVORITE_STATS_INTERVAL_MS`,
    60s) on top of the server cache, so the 3s-poll-driven
    `renderAslDrawer()` calls don't even issue a request every time they
    run.
  - **`asl_stats_client` is a SEPARATE `AslStatsClient` instance from
    `monitor.py`'s own private one** -- same reasoning as `bm_write_client`
    being separate from `monitor.py`'s read-only Brandmeister client
    elsewhere in this file: different call shape (`favorite_stats()`,
    not `linked_node_info()`), own cache keys, no need to reach into
    `FleetMonitor`'s internals from `app.py`.
  - **favorites.ini's real format was fetched from AllScan's actual
    `favorites-Sample.ini` (GitHub, `main` branch -- note: `main` is the
    real default branch per the repo's own API metadata, even though
    `master` also resolves for raw file fetches; found by discovery, not
    assumed) before writing the parser.** `label[]`/`cmd[]` are PARALLEL
    PHP-ini arrays -- the Nth label pairs with the Nth cmd by POSITION,
    there is no single key=value per favorite, and the remote node
    number lives inside the `cmd` string itself (`rpt cmd %node% ilink 3
    27339`), extracted via `/ilink\s+\d+\s+(\d+)/`, not a separate field.
    Verified this regex-pairing approach against a synthetic sample
    matching the real file's exact shape (including the real sample
    file's own blank-cmd placeholder row) before trusting it.
  - **EchoLink favorites (node number >= 3000000, confirmed from
    AllScan's own `astapi/nodeInfo.php` range check) are detected and
    skipped VISIBLY during import, not silently dropped** -- this app has
    no Asterisk Manager Interface connection to do the `echolink dbget
    nodename` lookup AllScan uses to resolve those, so importing one as
    if it were a normal ASL node would be actively wrong, not just
    incomplete.
  - **Import writes through the exact same `/api/asl_favorites` POST
    route the compact card's manual add already uses** -- no new
    backend endpoint for the import itself, only for the stats lookup.
    `importFavoritesIni()` merges parsed entries onto the existing
    `aslFavorites` list by node (import wins on a collision, same
    dedup-by-node convention `addAslFavorite()` already has for a single
    manual add) and POSTs the whole merged list.
  - **A real, shipped bug, fixed in v3.84: almost every favorite showed
    "Not in ASL DB" even for well-known, definitely-registered nodes.**
    Root cause: `stats.data.apprptuptime`/`totaltxtime` arrive as JSON
    STRINGS from the real API (`"apprptuptime":"73285"`, confirmed via
    the actual raw response), not numbers -- despite printing as
    indistinguishable digits through a bare Python `print()` during the
    original research, which is exactly how this got missed the first
    time. `rx_pct = round(txtime / uptime * 100, 1)` raised a silent
    `TypeError` for every node that WAS found; the broad `except
    Exception` caught it and returned the same `found: False` shape a
    genuine 404 returns, which `renderAslDrawer()` renders as "Not in
    ASL DB" either way. The one case that looked "correct" (a real 404,
    e.g. node 1999) worked precisely BECAUSE it fails before reaching
    the division at all -- making the bug look like the opposite of what
    it was. Fixed two ways: (1) `_to_int()` casts both fields before
    dividing -- confirmed against the exact real string-typed response
    shape via a mocked test (`rx_pct` came back `33.0`, matching the
    live AllScan screenshot that prompted building this feature in the
    first place); (2) `found` is now three-valued, not boolean --
    `True` (real data), `False` (confirmed 404, genuinely not in the
    ASL DB), `None` (a fetch/parse/rate-limit error -- NOT the same
    claim). `renderAslDrawer()` now shows "Stats unavailable" for the
    `None` case instead of confidently asserting a node doesn't exist.
    **Also discovered while fixing this**: `stats.allstarlink.org`
    started timing out entirely (both `curl` and `urllib`) partway
    through this debugging session, most likely from the sheer volume of
    requests made testing this one feature (initial research, repo/file
    fetches, a deliberate 20-node burst test, several retries) -- a
    real, observed reminder that this API's documented 30-req/min limit
    is a genuine constraint, not just a note in someone else's
    changelog. Re-verify any future change here with a SINGLE request
    and a mocked-data test for the logic itself, not a fresh live burst,
    to avoid tripping the same limit while debugging.
- **ASL Control's favorites list went from two-line rows with a
  permanent Connect/Monitor/Disconnect trio to single-line clickable
  rows (v3.85) -- a direct, reported "taking up a lot of room" complaint
  once Rx%/LCnt joined each row, mocked up and approved before
  building, same workflow as every other feature this session.** Each
  row is now dot + name/node + Rx%/LCnt-or-status + a small ✕ remove --
  clicking anywhere else on the row calls `selectAslFavorite(node)`,
  which sets a new module-level `aslSelectedFavoriteNode` and
  re-renders; clicking the already-selected row again deselects (sets
  it back to `null`). Quick Connect (`renderAslDrawer()`'s
  `qcSelectedHtml`/`qcButtonsHtml`) reads this selection each render:
  no selection -> the original freeform "any node #" field with
  Connect+Monitor; a selection -> the field is pre-filled with that
  favorite's node, a "Selected: **name** · node" line appears above it,
  and the buttons collapse to a single Disconnect if `linkedByNode`
  (this app's own SSH-sourced `asl_linked_nodes`, not the stats API)
  shows that node currently connected, else stay Connect+Monitor. The
  remove button keeps its own `event.stopPropagation()` so clicking ✕
  deletes the favorite instead of also selecting the row underneath it
  first -- same pattern already used elsewhere in this drawer (map-pin/
  update-badge clicks inside a clickable hotspot card). `removeAslFavorite()`
  now also clears `aslSelectedFavoriteNode` if the removed node was the
  selected one, and `renderAslDrawer()` itself defensively clears the
  selection if the selected node ever isn't found in `aslFavorites`
  (e.g. removed via the compact card or an import while the sidebar
  happened to be open) rather than rendering a Quick Connect block for a
  favorite that no longer exists. This was a pure frontend change --
  `aslConnect()`/`aslDrawerQuickConnect()`/`/api/asl_connect` needed no
  changes at all, since the new Disconnect button just calls
  `aslDrawerQuickConnect(ip, 'disconnect')`, an action that function
  already passed through generically.

- **A real, user-reported recurrence of "every ASL Control favorite shows
  Stats unavailable" (v3.86) -- root-caused live, not guessed, and this
  time the network path itself was confirmed innocent before touching
  any code.** A `curl` from the Unraid host itself succeeded (200, valid
  JSON, `X-RateLimit-Remaining: 15`); the exact same request run via
  `docker exec` inside the actual `hotspot-dashboard` container also
  succeeded (200, valid JSON) -- but `X-RateLimit-Remaining` had dropped
  to **2** in the few moments between the two checks, confirming
  `stats.allstarlink.org`'s real, documented 30-req/min cap (see the
  v3.83 entry above) is shared across the box's whole public IP and was
  genuinely close to exhausted, not a DNS/firewall/cert problem local to
  the container. Root cause: with N favorites saved, `favorite_stats()`
  cached every one of them at nearly the same instant (one batch fetch
  from `/api/asl_favorite_stats`), so they ALL expire in lockstep too --
  every ~120s (the old shared `ASLSTATS_CACHE_TTL`), the app fired N
  outbound requests to stats.allstarlink.org back-to-back with zero
  spacing between them. Combined with `monitor.py`'s own SEPARATE
  `AslStatsClient` instance polling the same API independently for link
  topology, an unlucky burst landing when the shared budget was already
  partly spent tripped a 429 for every request in that burst
  simultaneously -- exactly the "every favorite fails at once, including
  ones confirmed live to be real and registered" symptom, both times
  it's been reported.
  Fixed two ways, both in `aslstats.py`, without touching the actual
  request/parsing logic that was already correct as of the v3.83/v3.84
  fix:
  1. **Self-throttling** -- `AslStatsClient._throttle_live_request()`
     (a separate `_rate_lock`/`_last_live_fetch_at` pair from the
     existing cache `_lock`, deliberately not reusing it so a throttled
     wait never blocks another thread's pure cache-hit lookup) enforces
     a minimum spacing (`config.ASLSTATS_MIN_LIVE_INTERVAL_SEC`, default
     0.6s) between this client instance's own outbound live requests,
     called at the top of both `_fetch_favorite_stats()` AND
     `_lookup_remote()` -- a cold-cache burst of 15 favorites now spreads
     over ~9 seconds instead of landing in under 1. Verified with a
     mocked-`urlopen` test (not a fresh live burst, learning directly
     from the "re-verify with a mocked test, not another live burst"
     lesson the v3.83/v3.84 entry above already recorded) asserting the
     real minimum gap between recorded call timestamps, plus a second
     assertion that a same-node call still within the cache TTL makes
     ZERO live calls at all (pure cache hit, unaffected by the throttle).
  2. **A separate, longer cache TTL for favorites specifically**
     (`ASLSTATS_FAVORITE_CACHE_TTL`, default 300s vs. `ASLSTATS_CACHE_TTL`'s
     120s) -- Rx%/LCnt/status is a busy-ness percentage over a node's
     entire uptime, not live link topology, so it doesn't need the same
     freshness `linked_node_info()` does. A longer TTL directly cuts how
     often the lockstep-expiry burst happens at all, on top of the
     throttle making each burst, when it does happen, far gentler.
  Also fixed the actual undiagnosability that made this take a live
  debugging session to pin down at all: both `_fetch_favorite_stats()`'s
  and `_lookup_remote()`'s exception handlers now `print()` the real
  failure (HTTP status code, explicitly flagging a 429 as "likely
  rate-limited", or the exception type/message for anything else)
  instead of silently swallowing it -- keeps the "never raises out of a
  client's public methods" degrade-gracefully contract intact (still
  returns the same `found`/`error` shape either way), but means a future
  recurrence shows up in `docker logs`/journalctl immediately instead of
  needing a `curl`-from-host vs. `docker exec`-from-container comparison
  to even confirm the network path is innocent. If this recurs a THIRD
  time, check whether `monitor.py`'s own separate `AslStatsClient`
  instance's traffic (never throttled against app.py's instance's own
  budget usage, since they're two independent objects with two
  independent `_last_live_fetch_at` clocks) is the dominant remaining
  contributor before assuming the fix here was insufficient.

- **A real, reported bug: text typed into the ASL Control drawer's "Add
  favorite"/"Quick connect" fields (and the "Keep existing connections"
  checkbox) would vanish mid-typing (v3.88).** Root cause was structural,
  not a typo -- `renderAslDrawer()` fully replaces the drawer's
  `innerHTML` on every call, and it's called far more often than "the
  user did something": every `refresh()` poll tick (every `POLL_MS`,
  currently a few seconds), after every favorite save/select, and after
  `fetchAslFavoriteStats()`'s own throttled resolve. Rebuilding
  `innerHTML` destroys and recreates every `<input>` element from
  scratch, which silently wipes whatever's typed into one -- there was
  no code doing this on purpose, it's just what `innerHTML =` does to
  live DOM nodes. This was a pre-existing latent issue in this same
  drawer well before it was ever reported -- every earlier version of
  `renderAslDrawer()` in this file (going back to the sidebar's original
  build) had the identical unconditional `innerHTML` rebuild, just never
  got flagged until a user actually hit it directly.
  Fixed by snapshotting the affected fields' values (and, if one of them
  currently has focus, the focused element's id + cursor position) right
  before the `innerHTML` rebuild, then restoring them immediately after.
  Two different restore rules, not one, because `asl-drawer-qc-node` has
  competing logic no other field has: it's ALSO auto-populated from
  `aslSelectedFavoriteNode` (see the click-to-select entry above) via a
  `value="..."` attribute baked directly into the template string. If
  its typed value were unconditionally restored the same way as the
  other fields, that restore would immediately stomp the fresh
  selection-driven value on every single re-render after a row click --
  the field would never actually update to show the newly selected
  node. So `asl-drawer-add-node`/`asl-drawer-add-label`/
  `asl-drawer-keep-connections` restore unconditionally (nothing else
  ever writes to them), while `asl-drawer-qc-node`'s typed value is only
  snapshotted+restored when it's the actually-focused element at the
  moment of the rebuild -- i.e. only while the user is the one actively
  typing into it, never on an unrelated poll tick or a deliberate
  selection click. If another field is ever added to this drawer that
  BOTH accepts free typing AND has some other code path that also writes
  a value into it, it needs this same focus-gated (not unconditional)
  restore rule, not the simpler one.
- **The compact ASL Favorites card gained the same live Rx%/LCnt/status
  dot the ASL Control drawer already had (v3.90) -- mocked up, approved,
  then caught and fixed a real layout bug the mockup itself couldn't
  have shown, since it wasn't constrained by the actual card's real
  pixel width.** First attempt added a 4th `<td>` (a whole new stats
  column) to `asl-fav-table`'s 3-column row -- confirmed via
  `generate_screenshots.py` (not just reading the code) that this stole
  width from the name column, wrapping a longer favorite name
  ("Northeast Repeater Group") onto two lines and clipping it against
  `.asl-fav-scroll`'s fixed 118px max-height. The mockup itself never
  hit this since it was a static page with no scroll-height constraint
  and short enough names to fit either way -- a reminder that a mockup
  validates the DESIGN, not necessarily the real layout math once it's
  dropped into the actual, narrower card. Fixed by folding Rx%/LCnt into
  the EXISTING secondary text line (`.asl-fav-desc`, already used for
  "Node NNNN") as plain joined text ("Node 27339 · 62% rx · 8 links")
  instead of a separate column -- same information, but costing far less
  horizontal room than the drawer's own pill-styled `.asl-ctrl-rx`/
  `.asl-ctrl-lcnt` spans (which have more breathing room in the sidebar
  than this compact card does). Re-verified with a second screenshot
  pass after the fix: all three rows fit cleanly, real live stats
  ("20.7% rx · 17 links" for a genuinely resolved node) rendered
  correctly. `fetchAslFavoriteStats()` now refreshes both
  `renderAslFavorites()` (the compact card, always visible when the
  feature's enabled) and `renderAslDrawer()` (no-op while closed) from
  the same fetch/throttle -- one shared 60s-throttled request, two
  renderers kept in sync, no new endpoint. Monitor mode deliberately
  stayed drawer-only, per the original tradeoff discussion -- a third
  button per row would reintroduce exactly the density problem the
  drawer's own click-to-select redesign (the entry above) moved away
  from, and this compact card has even less width to spare than the
  drawer did.
- **"Recently active" amber indicator (v3.93) for ASL favorites NOT
  currently linked to your hub -- built after the user clarified their
  actual intent ("I think there is value in seeing what node in my
  favorites list to I want to listen to") once it became clear the
  existing red/keyed dot only ever reflects nodes linked to YOUR OWN
  hub, never activity elsewhere on the wider network.** Implements the
  exact diff-based workaround AllScan's own changelog describes for the
  stats API's single-poll `keyed` field being unreliable ("shows a 0
  value even when the node is in fact keyed") -- `AslStatsClient` now
  keeps `self._prev_counters: dict[node, (totalkeyups, totaltxtime)]`,
  a SEPARATE, never-expiring dict from `self._cache` (which exists
  purely to be diffed against the NEXT live fetch, however far apart
  that ends up being -- unlike the TTL cache, this must never get
  cleared just because the cached RESULT expired). `_check_recent_
  activity()` returns `True` if either counter increased since the
  previous live fetch, `False` if unchanged, `None` if there's no prior
  sample yet (first check for that node, e.g. right after a restart) --
  three-valued for the same reason `found` already is (`None` isn't
  "not active", it's "don't know yet"). Verified with a live
  three-sample mocked sequence (sample 1: baseline, `recently_active`
  is `None`; sample 2: counters moved, `True`; sample 3: unchanged from
  sample 2, `False`) before trusting the diff logic, not just read
  through once.
  **No new network requests** -- `totalkeyups` was already sitting
  unused in the same JSON response `_fetch_favorite_stats()` was
  already parsing for Rx%/LCnt/Web-Transceiver.
  **Precedence, in both the compact card and the drawer**: `isKeyed`
  (SSH-linked, keyed right now) always wins first; only when NOT
  currently linked does `stats.recently_active === true` get to color
  the dot amber -- a linked favorite already has a strictly more
  precise, real-time signal, so the coarser several-minutes-wide stats
  diff never overrides it. Compact card folds "recently active" into
  the existing plain-text desc line (amber-colored inline span) rather
  than a new element, same "don't reintroduce the column-width-squeeze
  bug from earlier this session" discipline as the Rx%/LCnt entry
  above; the drawer gets its own `.asl-ctrl-recent` pill-styled span,
  matching its existing `.asl-ctrl-notdb` shape.
  **Verification needed a real trick, not just a normal screenshot
  run**: `recently_active` can only ever be `True` on a node's SECOND
  live fetch, and `generate_screenshots.py`'s seeded demo hotspots start
  a fresh process every run (empty `_prev_counters`), so it can never
  naturally show the amber state in one screenshot pass. Confirmed the
  actual CSS/JS rendering (not just the backend logic) by writing a
  throwaway script that monkeypatched
  `AslStatsClient._check_recent_activity` to unconditionally return
  `True`, screenshotted both the compact card and drawer, deleted the
  script and its output images once confirmed -- never committed, since
  it exists only to prove the styling renders correctly, not as
  reusable tooling. That check also caught a real, narrow-viewport-only
  false alarm: an initial pass at 900px width showed the SAME
  wrapping/clipping bug the Rx%/LCnt entry above already fixed once,
  which turned out to be purely an artifact of testing at an
  unrealistically narrow width -- re-running at the dashboard's real
  1600px grid width showed clean, unwrapped rows. Don't assume a
  narrow-viewport screenshot regression is real without also checking
  at a realistic width; it can just as easily be the test harness's own
  choice of viewport, not the app.

- **Brandmeister talkgroup link/unlink (v3.79) was built and shipped;
  TGIF link/unlink was investigated in the same session and deliberately
  NOT built -- a real, live-verified structural gap, not a skipped
  feature.** The inspiration for this whole feature was the third-party
  `rupret007/WPSD-Dashboard` project's "TGIF Manager," but that project's
  own README admits it "does not work until TGIF's API is made
  available" -- confirmed there's no working TGIF write API to build
  against. Brandmeister's v2 API (`POST`/`DELETE .../device/{id}/
  talkgroup`) was real and workable instead -- see `brandmeister.py`'s
  module docstring for that build, including a real live-tested
  correction (the write body's field is `group`, not `talkgroup` as the
  OpenAPI spec's own schema summary claimed -- caught via a real
  `HTTP 455 {"error":"The group field is required."}` response, fixed,
  then re-verified end-to-end against a real account).
  A parallel TGIF investigation, done the same "verify against the real
  device" way (a real hotspot's actual `/etc/dmrgateway` -- not
  `DMRGateway.ini`, same no-extension/no-hyphen WPSD-generates-its-own-
  file pattern as `/etc/mmdvmhost` -- plus a live `ss -tulnp` process
  check), found:
  - `[Dynamic TG Control] Port=3769` / `[Remote Control] Port=7643` are
    REAL raw UDP sockets on this WPSD build, confirmed by the process
    actually listening on both -- NOT the MQTT-topic-based protocol
    `g4klx/DMRGateway`'s current `master` branch uses (that refactor
    landed in commit `04146fa`, 2023-07-07; WPSD evidently still ships
    the pre-MQTT build). Don't trust current upstream source for this
    without checking which protocol generation is actually deployed the
    same way -- confirmed via the parent commit
    (`a28aa7c549acf7b017c0a4a3b704154baea75603`)'s `DMRGateway.cpp`/
    `RemoteControl.cpp`, not `master`.
  - The real, confirmed Dynamic TG Control protocol: a UDP packet to
    port 3769 containing literal ASCII `"DynTG <slot>,<tg>"` (comma or
    space both work, parsed via `strtok(..., ", \r\n")`) sets talkgroup
    `<tg>` on timeslot `<slot>` -- confirmed byte-for-byte against the
    real pre-MQTT `CDMRGateway::processDynamicTGControl()` source.
  - **This mechanism only retargets pre-existing "Dynamic Rewrite
    Rules"** (`CRewriteDynTGRF` instances, each binding one fixed RF-side
    TG/range to one network-side TG) -- it doesn't create a talkgroup
    membership the way Brandmeister's API does. `grep -i "Dynamic
    Rewrite\|DynTG" /etc/dmrgateway` on the real test hotspot came back
    completely empty -- zero rules configured, meaning the `m_dynRF`
    list `processDynamicTGControl()` iterates is empty and a `DynTG`
    command would be silently accepted and do *nothing* on this device
    as configured.
  - **The deeper reason this isn't worth building even with rules
    configured**: TGIF has no server-side persistent "static talkgroup"
    concept at all, unlike Brandmeister -- presence on a TG is purely a
    function of the most recent RF transmission's DMR header, and this
    hotspot's TGIF network is configured as `Primary=4` with
    `PassAllTG0=1`/`PassAllTG1=2` (every talkgroup passes through
    unrewritten on both slots), so any TGIF talkgroup is *already*
    reachable by just keying up on it directly -- no dashboard feature
    needed for the common case. Even with Dynamic Rewrite Rules manually
    configured, the resulting feature would only be a handful of
    fixed-slot "retarget this preset channel" controls, not a
    Brandmeister-style "type any TG, click Link" experience -- a real,
    structurally smaller feature for real setup cost, not just an
    unverified one. If this is ever revisited, it needs a user who
    actually wants fixed-channel remote retargeting AND is willing to
    configure Dynamic Rewrite Rules first -- don't build speculatively
    against a hotspot with none configured.

- **The Awards / DXCC progress card (v3.78) was built, then removed
  outright in v3.80 -- a product/taste call, not a technical failure.**
  It ranked logged QSOs against classic ham awards (DXCC entities/next
  milestone, Worked All Continents, Worked All States, band x mode
  worked), pure client-side aggregation over the same `qsos.json`
  Recent Contacts/QSO Stats already read -- functionally correct, and
  even got a mockup-driven resize pass to shrink it toward the other
  cards' footprint (collapsing the DXCC/WAC/WAS bars into one compact
  stat-tile row, matching `.hf-stats-grid` verbatim). Pulled anyway,
  same "built it, looked at it, decided against it" outcome as the
  RainViewer precipitation radar overlay and the MUF propagation
  overlay elsewhere in this file -- the user just didn't want it on the
  dashboard. Every settings key (`show_awards`/`awards_position`), the
  `_SENTINEL_DEFS` entry, and all card CSS/HTML/JS were removed clean
  rather than left dead/hidden -- unlike settings orphaned for
  backward-compat with an already-deployed feature (e.g.
  `aprs_inbox_position`/`hamalert_position` after the Notifications
  merge), this shipped and was reverted within a day, so there was no
  real installed base with a saved `show_awards: true` to stay
  compatible with. If DXCC/award-tracking is ever asked for again,
  don't just re-add this -- confirm what specifically didn't work about
  the previous version (the concept, the visual weight, the specific
  stats shown) rather than assuming a straight resurrection is wanted.
  APRS symbol icons on the Live map, shipped in the same v3.78 release,
  were NOT part of this removal and are still live.

No test suite/framework is set up — verification has been done ad hoc but
consistently with this pattern; reuse it for any nontrivial change:

```bash
# 1. Syntax
python3 -m py_compile app.py monitor.py models.py config.py <touched files>

# 2. Template rendering (catches Jinja errors, missing settings keys, etc.)
python3 -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
settings = {...}  # a realistic full settings dict
hotspots = [{...}]
for name in ['setup.html','dashboard.html','version.html']:
    print(name, len(env.get_template(name).render(settings=settings, hotspots=hotspots, favorites=[])))
"

# 3. Route registration + a real request cycle (use a throwaway CONFIG_DIR)
mkdir -p /tmp/appdata_test
CONFIG_DIR=/tmp/appdata_test python3 -c "
import app
c = app.app.test_client()
for path in ['/', '/setup', '/version', '/readme', '/api/data', '/api/map_data']:
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
print('OK')
"
rm -rf /tmp/appdata_test

# 4. For monitor.py changes: mock paramiko.SSHClient rather than needing a
#    real hotspot -- see prior session history for the exact
#    unittest.mock.patch pattern used to test check_one_slow().

# 5. For dashboard.html/setup.html JS: strip Jinja tags and run `node --check`
python3 -c "
import re
s = open('templates/dashboard.html').read()
js = re.sub(r'{{.*?}}', '0', re.findall(r'<script>(.*?)</script>', s, re.S)[0])
js = re.sub(r'{%.*?%}', '', js)
open('/tmp/check.js','w').write(js)
"
node --check /tmp/check.js
```

Always clean up `__pycache__` before zipping/packaging a build.

## Deployment

- **Docker/Unraid**: source lives at
  `/mnt/user/appdata/hotspot-dashboard-src` on the user's box (built
  locally, not pulled from a registry). `docker-update.sh` is the
  one-command rebuild+recreate script — see its header comment before
  changing it, the port/volume/env values there match a real deployed
  container, not the template defaults.
- **Pi/Linux standalone**: `install.sh`/`update.sh`/`uninstall.sh`,
  systemd service. `update.sh` diffs `requirements.txt` and reinstalls
  automatically, so new **pip** dependencies (this project has four:
  `paho-mqtt`, `aprslib`, `bambulabs_api`, `websocket-client`) don't need
  special handling there. A new **system/apt** package (only `ffmpeg` so far, for camera
  cards) is NOT covered by that diff-and-reinstall logic — it needs its
  own explicit, idempotent `command -v ffmpeg || apt-get install ...`
  check in `update.sh` (see the "Ensure ffmpeg" block), same spirit as
  the polkit-rule/self-update-unit re-provisioning already there.
- `unraid-template.xml` defines the Unraid GUI's Add Container form
  fields — keep it in sync if you add a new env-var-based setting (rare;
  most new settings go in `config.DEFAULT_SETTINGS`/Settings UI instead,
  not the Unraid template).
- **`sync-wiki.sh` splits README.md into grouped Forgejo wiki pages, not a
  single Home.md dump (rewritten from the original single-page version
  once README.md grew past ~1500 lines / 41 `## ` sections).** Every
  `## ` heading in README.md must be preceded by a
  `<!-- wiki-group: <Group Name> -->` HTML comment -- invisible in both
  GitHub's rendering and this app's own `/readme` page, since
  `templates/readme.html` runs the raw markdown through `marked.parse()`
  client-side and HTML comments pass straight through into the DOM
  without the browser displaying them. Each distinct group name becomes
  its own wiki page (`Getting Started`, `Dashboard and Live Map`,
  `Hotspot Types`, `Integrations`, `Optional Cards`, `Admin and
  Maintenance` as of this writing); the script also generates `Home.md`
  (intro + a linked table of contents) and `_Sidebar.md` (Forgejo's own
  hidden file that replaces the default flat alphabetical wiki page list
  with custom grouped navigation — confirmed against Forgejo's own docs
  before building this, not assumed). **A `## ` heading with no marker
  above it is a hard Python `sys.exit()` error, not a silent drop** —
  when adding a new README section, add a `<!-- wiki-group: ... -->`
  line right above it (reusing an existing group name, or introducing a
  new one) or `sync-wiki.sh` refuses to run rather than quietly leaving
  that section out of the wiki. Stale pages are pruned safely: the
  script writes a `.wiki-sync-manifest` listing every file it generated,
  and on the next run diffs the OLD manifest (captured before Python
  overwrites it) against the new one to delete only pages this script
  itself created previously — a hand-added wiki page would never be
  touched, since it was never in any manifest to begin with. Internal
  links use Forgejo's `[[Page Name]]` wiki-link syntax (not a
  hand-computed slug in an explicit Markdown link) specifically so this
  script doesn't have to independently reproduce Forgejo's own
  name-to-filename slugification rules to get cross-links right — the
  wiki engine resolves those itself. Verified end-to-end against the
  real wiki repo (not just a scratch dir) before considering this done:
  ran it once (6 group pages + Home + Sidebar pushed, replacing the old
  single-page dump), then ran it again immediately after with no README
  changes and confirmed it correctly reported "already up to date,
  nothing to push" rather than force-committing an identical tree.
- **`generate_screenshots.py` produces real screenshots of the actual
  running dashboard for wiki illustrations, seeded with synthetic demo
  data (fake hotspots/QSOs/activity, never the real fleet) -- a dev-only
  tool (`requirements-dev.txt`, Playwright + a Chromium binary via
  `playwright install chromium`), not part of the deployed app.** The
  core problem this had to solve: a `HotspotStatus` is normally only ever
  populated by `monitor.py`'s real SSH polling -- writing a fake
  `hotspots.json` alone only supplies STATIC config (ip/name/type), there
  was no existing way to make a card show a realistic "active call" state
  without either real hardware or faking an entire SSH server. Solved by
  importing `app.py` directly (confirmed live, by reading `app.py`, that
  background polling threads only ever start inside `main()`'s
  `if __name__ == "__main__":` block, never as an import side effect) and
  writing fake `HotspotStatus` objects straight into `app.monitor._data`
  under its lock, bypassing SSH/the network entirely -- then running
  `app.py`'s own `waitress.serve()` (same as production, not the Flask
  dev server) in a background thread so a real Playwright-driven browser
  hits the real routes/templates/JS against this seeded state. Two real
  gotchas found and fixed live, not assumed:
  - **openSPOT4 is deliberately excluded from the demo hotspot set.**
    `app.py` eagerly calls `openspot_manager.reconcile(load_hotspots())`
    at import time (confirmed by reading app.py) -- a demo openSPOT4
    entry would spin up a REAL WebSocket worker trying to reach whatever
    fake IP was used, and that worker's own real reconnect-with-backoff
    logic could call `mark_external_offline()` and overwrite the seeded
    "Online"/active status before the screenshot ever fires. WPSD/ASL3
    have no such risk -- the only thing that ever touches their
    `monitor._data` entries is `monitor.run_forever()`'s poll loop, which
    (like every other background thread) only starts inside `main()`.
  - **A hotspot card's element id embeds its IP address**
    (`id="card-198.51.100.10"`) -- a bare `#card-198.51.100.10` CSS
    selector misparses the dots as class-selector separators (confirmed
    live: Playwright raised `Unexpected token '.51'`). Fixed by using the
    `[id="..."]` attribute-equality selector form for every per-card
    screenshot instead of `#id` -- safe regardless of what characters the
    id contains, not just a fix for this one case.
  Fake hotspot IPs use RFC 5737 TEST-NET-2 (`198.51.100.0/24`), which is
  reserved for documentation and never assignable on a real network --
  deliberate, so a generated screenshot can never be mistaken for
  pointing at a real reachable address. HF Conditions/Satellites/Band
  Activity/weather are left enabled in the demo settings and genuinely
  hit their real free/no-auth APIs during generation (same ones already
  used elsewhere in this app's own dev workflow) rather than being
  faked -- credential-gated integrations (QRZ/RadioID/Brandmeister/MQTT/
  cameras/DigiPi/DVSwitch/HamAlert/WSJT-X/APRS messaging) are left
  disabled instead of faked, since they'd just show an unconfigured empty
  state either way and faking believable credentials/hardware for each
  would be a lot of added complexity for little screenshot value. Output
  goes to `screenshots/` (gitignored -- a generated artifact, same
  category as `BUILD_COMMIT`), which `sync-wiki.sh` then copies into the
  wiki's own `images/` folder (tracked in the same `.wiki-sync-manifest`
  pruning scheme the text pages already use -- see that script's own
  updated header comment) if the directory exists and isn't empty.
  Deliberately does NOT auto-insert any `![...]()` image reference into a
  generated wiki page -- which screenshot illustrates which page is an
  editorial call, left to be added by hand.
- **A real, live-confirmed incident: renaming this repo on Forgejo
  (`W1ZLAHotspot_Dashboard` -> `W1ZLA_Hotspot_Dashboard`) silently
  redirected `sync-wiki.sh`'s hardcoded `<old-name>.wiki.git` URL to the
  renamed MAIN repo instead of the renamed WIKI repo -- two consecutive
  "successful" `sync-wiki.sh` runs (the ones that added image support)
  actually pushed 26 wiki files, including all 17 screenshots, onto this
  repo's own `main` branch, while the real wiki sat stale and untouched.**
  Confirmed directly, not guessed: `git ls-remote` against the OLD
  `<old-name>.wiki.git` URL printed a `redirecting to
  .../W1ZLA_Hotspot_Dashboard/` warning and returned the MAIN repo's
  current commit hash -- proving Forgejo's rename-redirect strips/ignores
  the `.wiki` suffix and resolves straight to the renamed repo itself,
  not `<new-name>.wiki`. `git push`/`clone`/`ls-remote` all silently
  follow this redirect with no error, which is exactly why two full
  `sync-wiki.sh` runs reported "Wiki updated"/"already up to date" with
  no indication anything was wrong -- the destination was simply wrong,
  not unreachable. Fixed in two parts: (1) reverted the errant commit
  from `main` with a plain `git revert` (an additive fix, not a
  history-rewriting force-push, since this was already-pushed shared
  history) before re-pushing the real, intended commit on top; (2)
  updated every hardcoded reference to the old repo name across the
  whole project -- confirmed via a full-repo grep sweep before
  considering this done, not just the two wiki scripts -- covering
  `sync-wiki.sh`/`release.sh` (both dev-tooling, would have kept silently
  misfiring), AND three real production-facing defaults that would have
  otherwise shipped the old name to every future install:
  `config.DEFAULT_SETTINGS["update_check_repo"]`, and
  `install.sh`/`update.sh`'s `DEFAULT_REPO_URL` (the persistent
  `/opt/hotspot-dashboard-src` git-checkout fallback source). **If this
  repo is ever renamed again, re-verify with the exact same live
  `git ls-remote` check against the OLD `<name>.wiki.git` URL before
  trusting that `sync-wiki.sh`/`release.sh` still point at the right
  place** -- don't assume a redirect "probably" preserves the wiki
  suffix just because it correctly preserves the main repo path; this
  is a confirmed, live-reproduced case where it doesn't.
- **Screenshots actually SHOW UP on wiki pages via a second marker type,
  `<!-- wiki-image: <filename> -->`, not by hand-editing the wiki repo.**
  Hand-editing would be silently destroyed the next time anyone runs
  `sync-wiki.sh`, since every group page is fully regenerated from
  README.md on each run by design (that's the whole point of the
  wiki-group scheme -- one source of truth). So a `wiki-image` marker
  placed right after a `## ` heading in README.md (or at the very top,
  before the first heading, for Home.md's own hero image) gets turned
  into a real `![heading](images/<filename>)` line at that exact
  position in the GENERATED wiki page -- same "invisible HTML comment
  everywhere else README.md is rendered, meaningfully interpreted only
  by sync-wiki.sh's own split step" trick `wiki-group` already
  established, reused rather than inventing a second mechanism. If the
  referenced file isn't found in `screenshots/` at sync time, the
  script warns (non-fatal, since generate_screenshots.py might just not
  have been re-run yet) rather than silently producing a dead link with
  no indication anything's wrong. Current placements (16 of the 17
  generated images -- `hotspot-card-wpsd-idle.png` deliberately has no
  placement, redundant with the active-card example, but still sits in
  the wiki's `images/` folder and is reachable directly if wanted):
  Home's hero is `dashboard-full.png`; the rest are one-per-matching-
  README-section, most densely in Optional Cards (a real screenshot for
  literally every card in that group) since that's where the 1:1
  section-to-card correspondence is cleanest.

## Conventions

- Every hotspot dict (from `hotspots.json`) is a plain dict, not a
  dataclass — optional keys (`lat`, `lon`, `brandmeister_id`) are simply
  absent rather than null when unset. Use `.get()`, never assume a key
  exists.
- **`enabled`** (bool, default `True` via `.get("enabled", True)` --
  absent entirely on any hotspot saved before this field existed, same
  backward-compat convention as every other optional key here) gates a
  hotspot out of polling (`monitor.py`'s `run_forever()`/
  `run_slow_checks_forever()` filter it out before `prune_stale`/
  `executor.map`) and out of `openspot.py`'s `OpenSpot4Manager.reconcile()`
  the same way a `type != "openspot4"` hotspot already is. Since
  `monitor._data` only ever gets an entry via `_ensure_entry()` (called
  from `check_one()`/`check_one_slow()`, both skipped for a disabled
  hotspot) and `prune_stale()` actively drops anything not in the
  current poll's keep-set, a disabled hotspot is absent from
  `/api/data`/`/api/map_data` with no route changes needed -- don't
  reintroduce a separate "is this disabled" filter in either route, the
  absence from `monitor._data` already is that filter.
- New per-hotspot fields go on `HotspotStatus` in `models.py` with a
  sensible default, so old `hotspots.json`/`settings.json` files from
  before the field existed still load fine.
- **One shared `HotspotStatus`, not a per-node-type dataclass.** ASL3
  support (v3.0) reused this same dataclass rather than introducing a
  second one, even though it's a structurally different node type --
  every cross-cutting consumer (`/api/data`, `mqtt_publisher.py`,
  `aprs_messaging.py`, favorites matching, `storage_activity.py`, the
  dashboard's timer-tick JS) only cares about a handful of shared field
  names (`ip`, `is_active`, `active_call`, `tx_start`, `last_heard`,
  `name`, `mode`, `is_favorite`, `favorite_label`) and needed zero changes
  as a result. A hotspot's `type` ("wpsd"/"asl3") lives only in
  `hotspots.json` as a plain dict key, attached to `/api/data` entries in
  `app.py` the same way `lat`/`lon` already are -- not on the dataclass
  itself, since it's static config, not live status. Follow this pattern
  for any future node type rather than forking the data model.
- Version bumps + changelog entries live in `templates/version.html`
  (`<div class="release">` blocks) — bump for real app-behavior changes,
  not for deployment-script-only edits like `docker-update.sh`.
- **Since v3.49, every version also gets a codename** — a deceased rock
  & roll musician, one per release, purely cosmetic (shown on `/version`
  as "Current build: vX.Y \"Name\""), same spirit as Ubuntu's animal
  names. Source of truth is `config.py`'s `VERSION_CODENAMES` list
  (`APP_VERSION`/`APP_CODENAME` derive from it, `APP_CODENAME` is always
  `VERSION_CODENAMES[-1]`) — when bumping the version, append the next
  name to that list and bump `APP_VERSION` together, don't just edit
  `version.html`'s changelog in isolation or the two will disagree. Only
  use real, verifiable deceased rock musicians (don't invent one or
  guess whether someone's still alive) — pre-v3.49 releases were never
  retroactively named, so the list only needs to grow forward from here.
- **`release.sh` tags and creates a Forgejo Release for the current
  version — the last step of the version-bump workflow, once
  `APP_VERSION`/`VERSION_CODENAMES`/`version.html`'s changelog entry are
  committed AND PUSHED to `main`.** Run it as `bash release.sh` after
  that push (not before — it tags whatever commit is currently HEAD).
  It reads `config.APP_VERSION`/`APP_CODENAME` for the tag/title, pulls
  the release notes straight from `version.html`'s current (first)
  `<div class="release">` block so the two never drift apart, tags+pushes
  `vX.Y` if it doesn't already exist, then `POST`s to Forgejo's
  `/repos/{owner}/{repo}/releases` (confirmed live against this host's
  own `swagger.v1.json` before writing this — `tag_name` is the only
  required field; `name`/`body` optional — rather than assumed from
  GitHub's differently-shaped API). Needs a Forgejo API token
  (`$FORGEJO_TOKEN` env var, or a `.forgejo-token` file next to the
  script — gitignored, never commit it) since tag pushes reuse existing
  git credentials but the Release itself is a REST API call, which git
  credentials don't cover. A 409 (release for that tag already exists)
  is treated as success/no-op, same idempotent spirit as `sync-wiki.sh`'s
  "already up to date" case, not an error.
  **A real encoding bug was caught and fixed before this shipped**: this
  dev shell's default Python stdout encoding is `cp1252`, which silently
  mangled the em-dashes ("—") every changelog entry in this project uses
  — confirmed live (`python3 -c "print(sys.stdout.encoding)"` -> `cp1252`)
  before assuming the extraction was correct, not caught by inspection.
  Every `python3` invocation in this script that touches changelog text
  is prefixed with `PYTHONIOENCODING=utf-8`, and the notes text is passed
  between the two `python3` steps over stdin rather than as a command-line
  argument — argv decoding is a second, independent encoding risk on
  Windows (locale-dependent), so this sidesteps it entirely rather than
  assuming it round-trips. If this script is ever touched again and starts
  producing garbled `—`/curly-quote characters in a release's notes, this
  is the first thing to re-check, not the JSON encoding logic itself
  (verified separately and confirmed correct).
- No JS build step, no npm — all frontend libraries (Leaflet, marked.js,
  leaflet.markercluster) are loaded from CDN via plain `<script>` tags.
