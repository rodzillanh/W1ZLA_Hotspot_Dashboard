"""Live HF MUF (Maximum Usable Frequency, 3000km path) propagation map
for the Live map's optional "Propagation" overlay.

prop.kc2g.com is a free, keyless service (data from a real ionosonde
network via GIRO/INGV, confirmed live before building this -- a bare
`curl` with no User-Agent gets a 503 from what turned out to be their
bot-protection, not the service being down; a normal browser-style
User-Agent gets a clean 200). The map SVG is regenerated every 15
minutes per their own docs.

The API's `?grid=<gridsquare>` parameter does NOT re-project or
re-center the map -- confirmed by fetching the same map for two very
different grid squares (one in the US, one in Australia) and finding
identical world-map geometry in both, just a different reference
marker/highlighted path. The base map is always a plain equirectangular
(Plate Carree) world map, -90..90 latitude by -180..180 longitude, at a
CONSTANT pixel scale -- confirmed three independent ways before trusting
it enough to build an image overlay from it:
  1. The SVG's main-axes clipPath is a literal rectangle
     (x=35.304688..1128.104687, y=24.14175..570.54175 in the SVG's own
     pt coordinate space).
  2. The 19 x-axis tick marks are evenly spaced at exactly the positions
     for -180, -160, ..., 180 (20-degree steps); the 9 y-axis tick marks
     match -80, -60, ..., 80 exactly. Both axes compute to the identical
     3.035556 pt/degree scale.
  3. Extrapolating that scale from the ticks to the clipPath's own edges
     lands on EXACTLY lat=-90/+90 and lon=-180/+180 -- not an
     approximation, the numbers work out to whole degrees.
That means the map can be geo-registered as a Leaflet imageOverlay with
bounds [[-90,-180],[90,180]] with no distortion -- confirmed visually
too (a cropped render lines up coastlines cleanly at every edge).

CROP_VIEWBOX below crops the fetched SVG to just that data rectangle --
a pure string edit on the `viewBox`/`width`/`height` attributes, not a
rasterization step. SVG's viewBox only changes which region is mapped
to the visible output; every path's own coordinates stay in the
original space, so this is a correct, lossless crop, not an
approximation. This avoids needing an SVG rasterizer entirely (a real
constraint hit while verifying this: cairosvg failed in this dev
environment on a missing system libcairo). If prop.kc2g.com ever
changes their figure size/margins, this crop needs re-deriving the same
way -- re-fetch a real map and re-locate the clipPath/tick positions,
don't guess new numbers.
"""
import re
import threading
import time
import urllib.request

KC2G_URL = "https://prop.kc2g.com/api/moflof.svg"
CACHE_TTL = 15 * 60  # matches KC2G's own documented ~15 min regeneration cadence
TIMEOUT = 20
AGENT = "hotspot-dashboard/1.0"

# Calibrated against a real fetched map -- see module docstring for how.
_CROP_X, _CROP_Y = 35.304688, 24.14175
_CROP_W, _CROP_H = 1092.8, 546.4
MAP_BOUNDS = [[-90.0, -180.0], [90.0, 180.0]]  # [[south, west], [north, east]] for L.imageOverlay

_VIEWBOX_RE = re.compile(r'width="[\d.]+pt" height="[\d.]+pt" viewBox="0 0 [\d.]+ [\d.]+"')
_CROPPED_HEADER = f'width="{_CROP_W}pt" height="{_CROP_H}pt" viewBox="{_CROP_X} {_CROP_Y} {_CROP_W} {_CROP_H}"'


class PropagationMapClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}  # grid -> (fetch_time, cropped_svg_text)

    def get(self, grid: str) -> str | None:
        grid = (grid or "").strip().upper() or "FN42"
        with self._lock:
            cached = self._cache.get(grid)
            if cached and (time.time() - cached[0]) < CACHE_TTL:
                return cached[1]

        cropped = self._fetch(grid)
        with self._lock:
            if cropped is not None:
                self._cache[grid] = (time.time(), cropped)
                return cropped
            # Degrade gracefully like every other client here -- serve the
            # last good snapshot rather than a broken/missing overlay.
            cached = self._cache.get(grid)
            return cached[1] if cached else None

    @staticmethod
    def _fetch(grid: str) -> str | None:
        try:
            url = f"{KC2G_URL}?grid={grid}&metric=mof_sp"
            req = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                svg = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        if "<svg" not in svg:
            return None
        cropped, n = _VIEWBOX_RE.subn(_CROPPED_HEADER, svg, count=1)
        if n != 1:
            return None  # KC2G changed their SVG structure -- don't serve a
                          # mis-cropped map, fail closed like the rest of this app.
        return cropped
