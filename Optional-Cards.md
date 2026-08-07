# Optional Cards

## HF Conditions card

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

## Band Plan card

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

## License Quiz card

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
  - **Extra** ([2024-2028](https://ncvec.org/index.php/2024-2028-extra-class-question-pool-release), effective through June 30, 2028): 572
    questions (27 diagram-based excluded).
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

## Satellites card

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

## Recent Contacts card

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

## QSO Stats card

Optional dashboard card (Settings → Cards → "Show QSO Stats card", off
by default) — a quick summary over the same logged QSOs Recent Contacts
shows: total contacts, unique countries worked, unique grid squares
worked (at 4-character Maidenhead precision — e.g. "FN42" — the
standard grid-square-award granularity, not the finer 6-character
precision some logs store), unique bands worked, and a small breakdown
of your most-used bands. Pure client-side aggregation, no new data
source or backend query.

## Top 5 Activity card

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

## Big Ass Clock card

Optional dashboard card (Settings → Cards → "Show Big Ass Clock card",
off by default) — a large clock, purely client-side (no backend module,
no network call — same as the static Band Plan card). Three selectable
styles via a dropdown in the card header:

- **Digital** — large tabular time + date. A 12/24-hour toggle in the
  card's own controls (UTC/Zulu is always forced 24-hour — the real
  ham radio/aviation convention).
- **Analog** — a proper clock face: 60 minute ticks with bolder hour
  marks, numerals, tapered hands with a counterweight tail, and a
  subtle gradient face/bezel.
- **TIX** — a dot-matrix "TIX clock" style display: each digit shown as
  a grid of lit dots, count = digit value. Tens digits get a smaller
  grid sized to what they actually need (hour tens only ever needs 0-1,
  minute/second tens only ever needs 0-5) rather than the full 9-dot
  grid units digits use. Always 12-hour, no second clock — kept
  deliberately simple/glanceable.
- **Second clock** — Digital and Analog can both show a second
  clock alongside the first, in any of several common timezones or
  UTC/Zulu. Digital's pair stacks vertically; Analog's sits side by
  side (two round faces read naturally next to each other).
- Style, format, and second-clock/timezone choices are saved in the
  browser's **localStorage**, not settings.json — nothing here needs to
  sync across every device viewing the same dashboard.
- Same drag-and-drop **Card order** placement as every other extra card.

## Band Activity card

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
