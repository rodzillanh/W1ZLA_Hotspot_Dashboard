# Hotspot Types

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

## AllStarLink (ASL3) nodes

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

## DVSwitch card

A separate, optional card for an ASL3 node that also runs a DVSwitch
(Analog_Bridge) audio bridge — distinct from the DVSwitch mode on the
**Fleet Activity** card above; that's just an activity count, this is a
full status card. Turn it on the same place as the Fleet Activity mode
(Settings → Hotspots → edit the ASL3 node → "This node also runs DVSwitch
(Analog_Bridge)"), then list one or more **bridge ports** (comma or
newline separated — Analog_Bridge supports running multiple instances on
one node, each identified by its own port). All of this reuses the same
SSH connection/credentials already configured for that node — no separate
login or remote-access setup needed.

The card shows:

- **Header badge** — how many configured bridges are currently tuned to
  something vs. idle.
- **Bridge chips** — one per configured port, with the talkgroup/reflector
  it's currently tuned to when tuned. "Tuned" reflects whether
  Analog_Bridge's own live status file has a value set, not a fully
  verified connect/link state — see the caveat below.
- **Last heard** — the two most recent callers heard through any of this
  node's bridges, with elapsed time. Callsign links go to QRZ if
  configured, RadioID.net otherwise, same as every other clickable
  callsign in this app.
- **Vocoder line** — whether Analog_Bridge is using its hardware AMBE chip
  or has fallen back to a software vocoder, based on a real log line
  logged the moment that fallback happens (not just what's configured to
  be tried).
- **Sparkline** — a small recent-activity trend, reusing the same logged
  data the Fleet Activity card's DVSwitch mode already writes.
- **Live RX/TX row** — a pulsing "RX DMR — W1ZLA → TG 603" style
  indicator (or "Listening" when idle) for a transmission that's actually
  in progress right now, plus DMR master / D-Star link status. Unlike
  everything else on this card, this is sourced from a *second* DVSwitch
  log file (`MMDVM_Bridge.log`, not `Analog_Bridge.log`) that has real
  start-and-end transmission markers — scoped to DMR and D-Star only;
  YSF/P25/NXDN each log to their own separate file and aren't covered.

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

Like camera cards, its position is part of the same drag-and-drop
**Card order** list as everything else (Settings → Hotspots) — drag it
anywhere in that list to move it.

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
does — there's no Linux host access, so temperature/CPU/uptime don't
appear on an openSPOT4 card.

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

## ASL Favorites & Control

Optional dashboard card (Settings → Cards → "Show ASL favorites &
control card", off by default), inspired by
[AllScan](https://github.com/davidgsd/AllScan)'s favorites/scan/connect
model. Lets you keep a list of ASL node numbers you care about (a
different list from the DMR callsign Favorites tab) and connect or
disconnect them from one of your ASL3 hotspots with one click.

- **Add a favorite** — node number + optional label, right on the card.
  A resolved callsign (via the same `aslstats.py` lookup ASL3 cards
  already use) is shown automatically if the label is left blank.
- **Control from** — a dropdown picks which of your configured ASL3
  hotspots originates the connect/disconnect command, if you have more
  than one.
- **Live status** — 🔴 **Keyed**, 🟢 **Connected**, or **Not connected**,
  derived entirely from the selected hotspot's already-polled link table
  (the same data its own card's "Linked:" row uses) — no extra polling or
  external API calls for status. A favorite not currently linked to the
  selected hotspot has no live status to show, since keyed/connected state
  is only knowable from your own node's link table in the first place.
- **Connect / Disconnect** — runs `asterisk -rx "rpt cmd <node> ilink
  <code> <remotenode>"` over the same SSH connection already used for
  polling. This is **not** the DTMF-simulated `rpt fun <node> *3<node>`
  form (which requires replicating `app_rpt`'s digit-collection state
  machine and proved unreliable in testing) — `rpt cmd` takes the
  function code and node as plain separate arguments, confirmed both
  against a real node and by checking how AllScan itself — a mature,
  widely-used tool — does the same thing.
- Connects are temporary (transceive), not permanent — there's no
  "connect permanently" option in this card. Use WPSD/AllStarLink's own
  admin tools for permanent link changes.
- **Auto-disconnect on connect** — by default, connecting to a favorite
  first disconnects any *other* favorite currently connected/keyed on
  that same hotspot, so you don't end up stacking links by accident.
  Check "Keep existing connections when connecting" (above the list) to
  skip that and just connect, same as before this existed. This only
  ever touches favorites tracked in this card — it won't disconnect a
  link made some other way (e.g. a permanent link configured directly on
  the node).
- **List stays compact** — the row list caps at ~3 visible rows and
  scrolls internally once you have more favorites than that, so the card
  doesn't keep growing taller. Whichever favorite is currently keyed or
  connected always sorts to the top, so it's visible without scrolling.

By default the card appears first, before any hotspot cards. Its position
is part of the same drag-and-drop **Card order** list as the hotspot cards
(Settings → Hotspots) — drag it anywhere in that list to move it.
