"""
satellite.py -- free ISS tracking data for the tracker mode.

Same shape as market.py on purpose: all I/O lives here so the mode that
draws it stays pure, and the pattern (background thread, last-good cache,
never invent numbers) is identical.

Two keyless sources, covering two different things:
  * api.wheretheiss.at   -- current position, altitude, velocity. Chosen
    over the older api.open-notify.org because open-notify only returns
    lat/lon -- no altitude, no speed -- and this mode wants both.
  * iss-api.polluxlabs.io -- next-pass predictions, and it already computes
    real naked-eye visibility (rise/set against actual sun angle, not just
    "above the horizon"). This is why it was picked over N2YO: N2YO needs a
    registered API key, polluxlabs needs none, and PRODUCTION.md's own
    framing of this feature as an "ISS countdown" makes the pass predictor
    the more important of the two feeds, not a nice-to-have.

Home location is config-driven (location_config.json), same pattern as the
ticker's market_config.json -- the production device needs a real owner
location, and there's no way to guess Dylan's coordinates, so this ships
with an unmistakable placeholder (0,0 -- Null Island) rather than a wrong
real-looking default. See SatelliteFeed.configured.
"""
import calendar
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
PASS_URL = "https://iss-api.polluxlabs.io/iss-pass"

POSITION_REFRESH = 12.0     # ISS covers ~7.7km/s -- this wants to feel live
PASS_REFRESH = 900.0        # pass predictions are valid for hours; no need to hammer it
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


# Pass quality. Elevation is what actually determines how impressive a
# pass looks: the ISS at 15 degrees is a dim light low on the horizon,
# probably behind a tree; at 70 degrees it crosses nearly overhead and is
# briefly one of the brightest things in the sky. Both come straight from
# the pass prediction already being fetched -- no extra request.
#
# NOTE ON CREW COUNT: deliberately NOT shipped. The obvious free source
# (open-notify.org/astros.json) still responds 200, but returns the
# Expedition 71 crew -- who were aboard in 2024 -- with no timestamp
# field to detect the staleness from. Showing a two-year-old crew list as
# current would be exactly the kind of confident-but-wrong number this
# project refuses to display. Revisit if a maintained free source turns up.
ELEV_EXCELLENT = 60.0
ELEV_GOOD = 40.0


def pass_quality(nxt):
    """(tag, rank) for a predicted pass; higher rank = more worth setting
    an alarm for. Tags are <=7 chars to fit the panel header's tag slot."""
    if not nxt:
        return None
    elev = nxt.get("max_elev")
    if not isinstance(elev, (int, float)):
        return None
    if not nxt.get("visible"):
        # Still reported, because "it's up there but you won't see it" is
        # honest and useful -- it just isn't something to go outside for.
        return ("DAYLIT", 0)
    if elev >= ELEV_EXCELLENT:
        return ("BRIGHT", 3)
    if elev >= ELEV_GOOD:
        return ("GOOD", 2)
    return ("LOW", 1)


def _fetch_next_pass(lat, lon):
    url = f"{PASS_URL}?lat={lat}&lon={lon}&n=5&days_ahead=10"
    d = _get_json(url)
    passes = d.get("passes") or []
    if not passes:
        return None
    visible = [p for p in passes if p.get("visible")]
    p = visible[0] if visible else passes[0]
    return {
        "rise_iso": p["rise"]["time"],
        "compass": p["rise"]["compass"],
        "max_elev": float(p["culmination"]["elevation_deg"]),
        "duration_s": int(p["duration_sec"]),
        "visible": bool(p.get("visible")),
    }


def _parse_iso(s):
    # "2026-07-28T01:04:52Z" -> epoch seconds, stdlib only
    return time.calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) \
        if hasattr(time, "calendar") else __import__("calendar").timegm(
            time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


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
        self._pass = None
        self._pass_updated = 0.0
        self._pass_try = 0.0
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
        {configured, label, pos: {...}|None, pos_age, next_pass: {...}|None,
         pass_age, seconds_to_rise: float|None, err}
        """
        now = time.time()
        with self._lock:
            self._last_read = now
            pos = dict(self._pos) if self._pos else None
            pos_age = (now - self._pos_updated) if self._pos_updated else None
            nxt = dict(self._pass) if self._pass else None
            pass_age = (now - self._pass_updated) if self._pass_updated else None
            lat, lon, label, err = self.lat, self.lon, self.label, self._err
        self._ensure_thread()

        if pos is not None and self.configured:
            pos["distance_km"] = haversine_km(lat, lon, pos["lat"], pos["lon"])

        seconds_to_rise = None
        if nxt is not None:
            try:
                seconds_to_rise = _parse_iso(nxt["rise_iso"]) - now
            except (ValueError, KeyError):
                seconds_to_rise = None

        return {
            "configured": self.configured, "label": label,
            "pos": pos, "pos_age": pos_age,
            "next_pass": nxt, "pass_age": pass_age,
            "seconds_to_rise": seconds_to_rise, "err": err,
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
            self._pass_try = 0.0     # location changed -- passes are now stale
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
            self._refresh_pass()
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
                self._pass_try = 0.0

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

    def _refresh_pass(self):
        now = time.time()
        with self._lock:
            if now - self._pass_try < PASS_REFRESH:
                return
            if not self.configured:
                return
            self._pass_try = now
            lat, lon = self.lat, self.lon
        try:
            nxt = _fetch_next_pass(lat, lon)
            with self._lock:
                self._pass = nxt
                self._pass_updated = time.time()
        except Exception:                              # noqa: BLE001
            pass          # position feed already reports errors; don't double up


FEED = SatelliteFeed()
