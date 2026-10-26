# External Resources Referenced

A consolidated list of external APIs, source repos, protocol docs, and
other outside references used while building this dashboard. Most of
these are documented in depth (with live-verification notes, gotchas,
and dates) in `CLAUDE.md` — this file is just an index/quick-reference,
grouped by category. If you need the full story behind any entry
(why it was chosen, what was verified live, what broke), search
`CLAUDE.md` for the module name or API name.

## Live APIs / data feeds

| Resource | Used by | Notes |
|---|---|---|
| QRZ XML API | `qrz.py` | Callsign lookup, paid subscription |
| QRZ Logbook API (`logbook.qrz.com/api`) | `qrz_logbook.py` | Separate from XML lookup; API-key auth, no subscription needed |
| RadioID.net | `radioid.py` | DMR ID lookup |
| APRS-IS (`aprs-is.net`) | `aprs.py`, `aprs_messaging.py`, `aprs_inbox.py` | Spec for `javAPRSFilter`, `b/` buddy filter |
| Brandmeister REST API v1 + v2 (`api.brandmeister.network`) | `brandmeister.py` | v2 added for talkgroup link/unlink write support |
| Brandmeister Last Heard feed (`wss://api.brandmeister.network/lh`) | `brandmeister_lastheard.py` | Reverse-engineered Socket.IO, undocumented by Brandmeister. Moved from `ws.brandmeister.network/lh/socket.io/` (now dead) in Oct 2026 |
| Brandmeister device list (`/v2/device`) | `nearby.py` | ~9.7 MB bulk list, no auth; repeater-vs-hotspot is a heuristic |
| AllStarLink node DB (`allmondb.allstarlink.org`) | `nearby.py` | No coordinates; nodes matched to repeaters by callsign + frequency |
| AllStarLink stats API (`stats.allstarlink.org`) | `aslstats.py` | Rate-limited ~30 req/min |
| wspr.live (ClickHouse HTTP API) | `wspr_activity.py` | Rate-limited 20 req/min, non-commercial terms |
| NOAA SWPC OVATION aurora feed | `aurora.py` | Full download verified, not WebFetch summary |
| N0NBH HF Conditions XML feed | `hf_conditions.py` | Solar-index based prediction |
| PSK Reporter (`retrieve.pskreporter.info`) | `psk_reporter.py` | XML, not JSON; aggressively rate-limited |
| POTA API (`api.pota.app`) | `pota.py` | Free, no auth |
| SOTA API (`api2.sota.org.uk`, `api-db2.sota.org.uk`) | `sota.py` | Free, no auth; summit lookup is a second call |
| Reverse Beacon Network telnet (`telnet.reversebeacon.net:7000`) | `rbn.py` | No auth, genuine firehose |
| DX cluster telnet (DXSpider/AR-Cluster) | `dxcluster.py` | User-configurable host |
| HamAlert telnet (`hamalert.org:7300`) | `hamalert.py` | Dedicated Telnet password since March 2024 |
| CelesTrak TLE data | `satellites.py` | Needs a browser-style User-Agent |
| SatNOGS DB API | `satellites.py` | Real param is `satellite__norad_cat_id`, not `norad_cat_id` |
| api.wheretheiss.at | `satellites.py` | Ground-truth ISS position, used to verify SGP4 math |
| openSPOT4 HTTP+WebSocket API | `openspot.py` | Reverse-engineered live; newer firmware gen than SharkRF's published docs |
| hearham.com repeater directory API | `repeaters.py` | Free, no auth, ~22k rows |
| HFCC broadcast schedule (`hfcc.org/data/<season>/<season>allx2.zip`) | `shortwave.py` | Free, no auth; plain `http://` (the `https://www.` URL failed from the dev box) |
| EiBi shortwave schedule (`eibispace.de/dx/sked-<season>.csv`, `README.TXT`) | `shortwave.py` | Free, no auth; README has the language, target and transmitter-site tables |
| Open-Meteo | `weather.py` | Free weather API |
| Forgejo REST API | `update_check.py`, `release.sh` | Self-update check + release creation |
| aprs.fi | `aprs_inbox.py` (`what=msg` backfill), plain callsign links elsewhere | |
| NA7Q APRS SMS gateway (aprs.wiki) | `aprs_inbox.py` | Replaced the dead SMSGTE; recipients must opt in |
| WXBOT (KI6WJP) | `aprs_inbox.py` | APRS weather-by-message bot |
| wfweb audio WebSocket | `sstv_rx.py`, `wfweb_power.py` | Undocumented; reverse-engineered live against a real IC-7300 MK2 |
| Hamlib `rigctld` (via WFView/wfweb) | `rig_panel.py`, tap-to-tune | |

## Source code read directly (not just docs)

- `g4klx/MMDVMHost`
- `g4klx/DMRGateway`
- `g4klx/YSFClients`, `P25Clients`, `NXDNClients` (YSFGateway/P25Gateway/NXDNGateway)
- `DVSwitch/Analog_Bridge`, `MMDVM_Bridge`, `Analog_Reflector`
- `davidgsd/AllScan` (`astapi/connect.php`, `nodeInfo.php`) — AllStarLink ilink codes
- `bmo/py-wsjtx` (`wsjtx_packets.py`) — WSJT-X UDP packet format
- `hjnilsson/country-flags` (CDN) — flag SVGs
- `CtrlAltDel-Irl/APRS-Messenger`
- `rupret007/WPSD-Dashboard` — TGIF Manager idea (not reused; no working write API)
- `kd9taw/Nexus` (Rust/Tauri) — inspired the Spots card (POTA+SOTA+RBN merge) and Beacons card
- `russolsen/ham_radio_question_pool` — License Quiz question pools (Apache-2.0)

## Protocol / hardware / firmware references

- Asterisk docs — `ChanSpy`, `AudioSocket`, Local Channel Optimization, `res_clioriginate`
- `app_rpt` / AllStarLink `RPT_ALINKS` behavior — verified against real device output, not docs
- SSD1306 OLED datasheet — WPSD screen-mirror feasibility (investigated, rejected as infeasible)
- ADIF spec
- WSJT-X UDP telemetry protocol
- NCVEC license-pool exports + errata PDFs (Technician 2026-2030, General 2023-2027, Extra 2024-2028)
- ARRL Considerate Operator's Frequency Guide (QST Nov 2013) — SSTV calling frequencies
- Unraid source (`dynamix.docker.manager`) — Docker "Edit" link requires the `net.unraid.docker.managed` label

## Forums / community writeups

- forum.hamalert.org — Telnet interface documentation threads
- repeater-builder groups.io — DMR BER testing thread
- george-smart.co.uk — M1GEO's Pi-Star BER tuning writeup
- Home Assistant forum — CARTO tile "API key required" complaint (prompted the Esri basemap swap)
- WhiskeyTangoHotel blog (Pimoroni Galactic Unicorn project) — HamAlert Telnet login sequence reference

## PyPI packages

`Flask`, `waitress`, `paramiko`, `paho-mqtt`, `aprslib`, `bambulabs_api`, `websocket-client`, `sgp4`, `pywebpush`, `sstv`, `numpy`, `Pillow`, `tzdata`

## Map / CDN

- Esri "Dark Gray Canvas" raster basemap (keyless) — replaced CARTO after it retired free anonymous access
- OpenStreetMap, Esri World Imagery (satellite layer)
- Leaflet + `leaflet.markercluster`
- marked.js — renders README.md client-side at `/readme`
