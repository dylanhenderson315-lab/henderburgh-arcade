"""
satellite.py -- free ISS tracking data for the tracker mode.

Same shape as market.py on purpose: all I/O lives here so the mode that
draws it stays pure, and the pattern (background thread, last-good cache,
never invent numbers) is identical.

One keyless source: api.wheretheiss.at, for the ISS's current position,
altitude and velocity -- chosen over the older api.open-notify.org because
open-notify only returns lat/lon, no altitude, no speed, and this mode
wants both. This is CONTINUOUS telemetry: it exists whether or not a pass
is happening, which is exactly what nothing else in the satellite system
has, and is the reason the ISS still gets a dedicated data slot in the
unified sky view (see engines.SatelliteEngine).

PASS PREDICTION USED TO LIVE HERE TOO, against iss-api.polluxlabs.io. It
was retired 2026-08-01 when the satellite modes were unified: skypass.py's
CelesTrak `visual` catalogue already includes the ISS (it is genuinely one
of the ~157 naked-eye-visible objects tracked there), and its SGP4-based
predictions were cross-validated against polluxlabs before the cut --
rise times agreed within 3-14s (resolution-limited by the validation's own
20s scan step) and peak elevation within 0.1-0.4 degrees, and both
independently reported zero visible ISS passes over this location for the
same three-day window. Maintaining two ISS-pass pipelines that already
proved they agree was pure duplication -- skypass.py is now the only pass
predictor in the project, for every object including the ISS.

Home location is config-driven (location_config.json), same pattern as the
ticker's market_config.json -- the production device needs a real owner
location, and there's no way to guess Dylan's coordinates, so this ships
with an unmistakable placeholder (0,0 -- Null Island) rather than a wrong
real-looking default. See SatelliteFeed.configured.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "location_config.json"

# 0,0 is deliberately not a plausible home -- it's Null Island, in the
# Gulf of Guinea. A wrong-looking default (like defaulting to some real
# city) would produce a distance number that LOOKS legitimate while being
# entirely made up. This one can't be mistaken for real data.
DEFAULT_LAT, DEFAULT_LON, DEFAULT_LABEL = 0.0, 0.0, "HOME"

POSITION_URL = "https://api.wheretheiss.at/v1/satellites/25544"

POSITION_REFRESH = 12.0     # ISS covers ~7.7km/s -- this wants to feel live
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

EARTH_RADIUS_KM = 6371.0


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle ground distance -- the number people actually mean by
    'how far away is it' (as opposed to 3D slant range, which is a
    different, less intuitive number for something directly overhead)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def load_location():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            lat = float(data.get("lat", DEFAULT_LAT))
            lon = float(data.get("lon", DEFAULT_LON))
            label = str(data.get("label", DEFAULT_LABEL)).strip()[:10] or DEFAULT_LABEL
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon, label
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    save_location(DEFAULT_LAT, DEFAULT_LON, DEFAULT_LABEL)
    return DEFAULT_LAT, DEFAULT_LON, DEFAULT_LABEL


def save_location(lat, lon, label):
    CONFIG_PATH.write_text(json.dumps(
        {"lat": lat, "lon": lon, "label": label[:10]}, indent=2))


def _fetch_position():
    d = _get_json(POSITION_URL)
    return {
        "lat": float(d["latitude"]), "lon": float(d["longitude"]),
        "alt_km": float(d["altitude"]), "vel_kmh": float(d["velocity"]),
        "sunlit": d.get("visibility") == "daylight",
    }


# NOTE ON CREW COUNT: deliberately NOT shipped. The obvious free source
# (open-notify.org/astros.json) still responds 200, but returns the
# Expedition 71 crew -- who were aboard in 2024 -- with no timestamp
# field to detect the staleness from. Showing a two-year-old crew list as
# current would be exactly the kind of confident-but-wrong number this
# project refuses to display. Revisit if a maintained free source turns up.


class SatelliteFeed:
    """Background poller with a last-good cache -- same contract as
    market.MarketFeed: never blocks the caller, never invents a number,
    stops polling once nothing has read from it for a while."""

    def __init__(self):
        self._lock = threading.Lock()
        self.lat, self.lon, self.label = load_location()
        self._pos = None
        self._pos_updated = 0.0
        self._pos_try = 0.0
        self._last_config_check = 0.0
        self._last_read = 0.0
        self._thread = None
        self._err = None

    @property
    def configured(self):
        return (self.lat, self.lon) != (DEFAULT_LAT, DEFAULT_LON)

    # ---- reading ---------------------------------------------------------
    def get(self):
        """Returns a dict, never blocking:
        {configured, label, pos: {...}|None, pos_age, err}

        Pass prediction is NOT here -- that is skypass.py now, for every
        object including the ISS. This feed is continuous LIVE telemetry
        only (see the module docstring for why that stayed separate)."""
        now = time.time()
        with self._lock:
            self._last_read = now
            pos = dict(self._pos) if self._pos else None
            pos_age = (now - self._pos_updated) if self._pos_updated else None
            lat, lon, label, err = self.lat, self.lon, self.label, self._err
        self._ensure_thread()

        if pos is not None and self.configured:
            pos["distance_km"] = haversine_km(lat, lon, pos["lat"], pos["lon"])

        return {
            "configured": self.configured, "label": label,
            "pos": pos, "pos_age": pos_age, "err": err,
        }

    def get_location(self):
        with self._lock:
            return self.lat, self.lon, self.label

    def set_location(self, lat, lon, label):
        lat = max(-90.0, min(90.0, float(lat)))
        lon = max(-180.0, min(180.0, float(lon)))
        label = (str(label).strip()[:10] or DEFAULT_LABEL)
        with self._lock:
            self.lat, self.lon, self.label = lat, lon, label
        save_location(lat, lon, label)
        return lat, lon, label

    # ---- polling -----------------------------------------------------
    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read
            if idle > IDLE_STOP:
                return
            self._maybe_reload_config()
            self._refresh_position()
            time.sleep(2.0)

    def _maybe_reload_config(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        lat, lon, label = load_location()
        with self._lock:
            if (lat, lon, label) != (self.lat, self.lon, self.label):
                self.lat, self.lon, self.label = lat, lon, label

    def _refresh_position(self):
        now = time.time()
        with self._lock:
            if now - self._pos_try < POSITION_REFRESH:
                return
            self._pos_try = now
        try:
            pos = _fetch_position()
            with self._lock:
                self._pos = pos
                self._pos_updated = time.time()
                self._err = None
        except Exception as e:                        # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"

FEED = SatelliteFeed()
