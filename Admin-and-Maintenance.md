# Admin and Maintenance

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
