"""One-shot pull of wfweb's own server-side QSO log (wfweb v0.9.1+, server
build) for Settings -> Integrations' "Import wfweb logbook" button.

Read from wfweb's own source (github.com/adecarolis/wfweb, src/webserver.cpp
handleRestRequest() / src/logbook.cpp), not guessed:
  - `GET /api/v1/logbook/adif` returns the whole logbook.adi file as plain
    ADIF (`application/x-adif`). It's served by the same request handler on
    both of wfweb's HTTP listeners -- the HTTPS web-UI port (self-signed
    cert) and the plain-HTTP REST port -- so this reuses the wfweb host/port/
    SSL settings the Rig Panel power control and SSTV card already use.
  - The full download keeps wfweb's own `APP_WFWEB_ID` per record (a UUID
    that never changes), which is what lets a re-import skip QSOs already
    pulled. `?new=1` would drop it, so this module never uses that form.
  - Fields written per record: APP_WFWEB_ID, CALL, QSO_DATE, TIME_ON, FREQ
    (MHz), BAND, MODE, RST_SENT, RST_RCVD, GRIDSQUARE (theirs),
    MY_GRIDSQUARE, STATION_CALLSIGN, COMMENT, NAME -- all already handled by
    adif.py's parser and app.py's ADIF-record conversion.
  - A wfweb older than v0.9.1, or the standalone (non-server) build, has no
    server logbook: that path falls through to a 404.

Never raises -- returns (text, None) or (None, error message), same
degrade-silently contract as every other integration client here."""
import ssl
import urllib.error
import urllib.request

TIMEOUT = 30


def fetch_adif(host, port, use_ssl):
    host = (host or "").strip()
    if not host:
        return None, "wfweb host is not set (Settings -> Integrations)"
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8080
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{host}:{port}/api/v1/logbook/adif"
    ctx = None
    if use_ssl:
        # wfweb's certificate is self-signed -- same trusted-LAN assumption
        # as wfweb_power.py's wss:// connection.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hotspot-dashboard"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ("wfweb has no server logbook at this address "
                          "(needs wfweb v0.9.1 or newer, server build)")
        return None, f"wfweb answered HTTP {e.code}"
    except Exception as e:
        reason = getattr(e, "reason", None) or e
        return None, f"Could not reach wfweb at {host}:{port}: {reason}"
    return raw.decode("utf-8", errors="replace"), None
