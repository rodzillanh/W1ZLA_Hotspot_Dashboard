# W1ZLA Hotspot Dashboard

Live status dashboard for a fleet of Pi-Star / WPSD hotspots, AllStarLink
(ASL3) nodes (polled over SSH), and openSPOT 4 (SharkRF) nodes (monitored
over HTTP/WebSocket).

## Contents

- **[[Getting Started]]** — Project layout, Running on Raspberry Pi / Linux (standalone), Running on Unraid, Config via environment variables, First run
- **[[Dashboard and Live Map]]** — Dashboard look, Dashboard customization, Transmission timer, Live map, Fleet activity, Talkgroup / reflector display, Offline detection, Card uptime
- **[[Integrations]]** — QRZ caller lookup, RadioID.net lookup, APRS.fi live position, Brandmeister repeater profile, Camera cards, Home Assistant (MQTT), APRS favorite alerts, Notifications card, DigiPi card
- **[[Hotspot Types]]** — WPSD/Pi-Star update check, WPSD radio frequency / duplex / identity display, AllStarLink (ASL3) nodes, DVSwitch card, openSPOT 4 (SharkRF) nodes, ASL Favorites & Control
- **[[Admin and Maintenance]]** — Host power control, Self-update, Backup & Restore, Known tradeoffs (intentionally left as-is for now)
- **[[Optional Cards]]** — HF Conditions card, Band Plan card, License Quiz card, Satellites card, Recent Contacts card, QSO Stats card, Top 5 Activity card, Big Ass Clock card, Band Activity card
