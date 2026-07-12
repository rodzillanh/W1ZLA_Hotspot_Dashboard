# CLAUDE.md

Context for Claude Code working in this repo. This is a self-hosted Flask
dashboard for monitoring a fleet of WPSD/Pi-Star amateur radio hotspots and
AllStarLink (ASL3) nodes — live status, active-call info, and a map, polled
over SSH, with optional QRZ/RadioID/APRS/Brandmeister/AllStarLink-stats/Home
Assistant/APRS-messaging integrations. Runs as Docker (Unraid) or standalone
via systemd on a Pi/Linux box.

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
                   `_check_one_wpsd()` / `_check_one_asl3()` -- this is
                   the pattern for any future node type
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
  hypothetical — the Version tab genuinely shows "unknown"/"no recorded
  build commit" in that case, and there's no way to fix it after the
  fact short of re-deploying from an actual git clone.** This is
  `install.sh`/`update.sh`'s own `git -C "$SCRIPT_DIR" rev-parse HEAD`
  failing (no `.git` directory at all -- a different failure than the
  WPSD "dubious ownership" gotcha above, which is about *ownership* of
  an existing `.git`, not its absence), silently swallowed by the
  `|| echo "unknown"` fallback. The bug that shipped alongside this
  (fixed at the same time this was diagnosed) was the *message* shown
  for this case in `setup.html`'s `renderUpdateStatus()`: it used to
  say "re-run update.sh (or docker-update.sh) once to enable update
  checks" — actively misleading for a non-git deployment, since
  re-running update.sh against the same non-git `$SCRIPT_DIR` hits the
  identical `git rev-parse` failure and writes "unknown" again, forever.
  Fixed by (a) `install.sh`/`update.sh` now `warn()`-ing loudly at
  deploy time when no git info is found, instead of failing silently,
  and (b) the Version tab message now correctly says to re-deploy via
  `git clone`, not to re-run the update script. If you touch this
  again, remember the two failure modes need different messages: "no
  `.git` at all" (this one) vs. "`.git` exists but `git` refuses to
  read it" (the dubious-ownership case) — don't conflate them.

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
- **The `b/CALLSIGN` "buddy" APRS-IS filter** was chosen deliberately
  over relying on undocumented "a verified logged-in station gets its
  own traffic automatically" assumptions, per the documented
  javAPRSFilter spec (includes packets addressed to the listed callsign,
  not just from it). This is the one piece of `aprs_inbox.py` NOT yet
  confirmed against real inbound traffic (unlike the packet-shape parts,
  which were) — if messages aren't arriving once this is live, re-check
  the filter syntax before assuming the parsing logic is at fault.
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
  of those same four places, not just one.
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

## Testing patterns used throughout this project

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
  automatically, so new **pip** dependencies (this project has three:
  `paho-mqtt`, `aprslib`, `bambulabs_api`) don't need special handling
  there. A new **system/apt** package (only `ffmpeg` so far, for camera
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
