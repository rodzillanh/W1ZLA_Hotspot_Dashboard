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
                   client -- LicenseQuizPool loads extra_2024_2028.json
                   (the bundled Extra/Element 4 question pool) once at
                   startup and serves random questions from memory, no
                   cache/TTL/fetch involved. See its docstring for the
                   pool's source/license before ever touching the data
                   file.

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
                           frontend framework
templates/setup.html       Settings UI: General / Weather / Integrations /
                           Hotspots / Cameras / Favorites tabs
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
  notes cite 603 for this cycle — a small, unreconciled discrepancy
  between snapshots, called out rather than silently accepted. 27
  questions referencing a circuit diagram figure were excluded (the
  images aren't bundled), leaving the 572 in `extra_2024_2028.json`.
  If this ever needs updating for the next pool cycle (2028), re-fetch
  from one of those two sources — don't hand-edit or add questions from
  training-data recall.
- **`E0` is a real graded subelement in the current Extra pool** ("Safety"
  — RF exposure, tower/climbing safety, grounding), not a bonus/appendix
  section invented by the data export. Confirmed via a second, independent
  web source before trusting the E0 entries in the bundled data — don't
  assume subelement codes match older, more commonly-cited "E1-E9 only"
  descriptions of the Extra pool structure.
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
- **A real, reported bug: "Big Ass Clock's card position doesn't save"
  turned out to be a tie-break disagreement between two independently-
  correct pieces of code, not a save failure at all.** `/api/settings`
  was persisting `big_clock_position` correctly the whole time (confirmed
  live) -- the actual problem was that `setup.html`'s Cards-tab drag-list
  template breaks a POSITION TIE (two sentinels sharing the same saved
  position, e.g. both still at their default of 0 right after being
  enabled) by fixed template source order, while `dashboard.html`'s
  `computeCardOrders()` used to break the identical tie ALPHABETICALLY
  BY KEY NAME (`a.key.localeCompare(b.key)`). For a real tie between
  `bigclock` and `licensequiz`, template order puts `licensequiz` first
  (it's declared earlier in `setup.html`) while alphabetical order put
  `bigclock` first (`'b' < 'l'`) -- confirmed by literally reproducing
  the exact tie live (a real `settings.json` with both at position 0)
  and finding the two files disagreed on which one renders first. From
  the user's side this reads exactly like "I dragged it, saved it, and
  it didn't take" -- the position value saved fine, but what they saw in
  the editor never matched what the real dashboard showed for a tied
  card, so every attempt to "fix" it by dragging again looked like it
  silently failed. Fixed by making `computeCardOrders()`'s sort rely on
  `Array.prototype.sort`'s ES2019+ stability guarantee instead of an
  independent tiebreak rule (`sentinels.slice().sort((a,b) => a.pos -
  b.pos)`, no `|| ...localeCompare(...)`), and by re-ordering
  `renderCards()`'s `sentinels.push()` calls to match `setup.html`'s
  fallback-block order EXACTLY, including moving the cameras push from
  right-after-`aslfav` (where it happened to sit, for no particular
  reason) to dead last, matching `setup.html`'s cameras block (which was
  already last there). **If either file's sentinel declaration order
  ever changes again, it must change in both places together** -- these
  are two independent descriptions of the same ordering, same class of
  gotcha as `unraid-template.xml`/`docker-update.sh` needing to stay in
  sync elsewhere in this file, except here a drift is silent (no error,
  just a wrong-looking card position) rather than loud.

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
- No JS build step, no npm — all frontend libraries (Leaflet, marked.js,
  leaflet.markercluster) are loaded from CDN via plain `<script>` tags.
