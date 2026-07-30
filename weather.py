"""
weather.py -- NOAA/NWS current conditions + active severe alerts.

Same shape as market.py/satellite.py/flights.py/sports.py/news.py: all
I/O lives here so the mode that draws it stays pure.

Location is NOT duplicated here -- it reuses satellite.py's
location_config.json via satellite.FEED.get_location(), same as
flights.py already does. One home location, one source of truth.

Source: api.weather.gov (NOAA/NWS). Free, no key, no registration. US
only, which is a real limitation worth knowing rather than discovering:
a non-US location returns no gridpoint and this module reports that
honestly instead of showing nothing.

Verified live against the real API before writing this, not assumed:

  * /points/{lat},{lon} 301-REDIRECTS if the coordinates carry more than
    4 decimal places ("Adjusting Precision Of Point Coordinate"). Our
    real stored location has 6+, so coordinates MUST be rounded before
    the request -- this is a silent-failure trap, since the 301 body is
    valid JSON with no usable fields rather than an error.
  * Observations come from a nearby STATION, not the gridpoint: you must
    follow /points -> /gridpoints/.../stations -> /stations/{id}/
    observations/latest. There is no one-call "current conditions".
  * NWS returns METRIC despite being a US agency: temperature in degC,
    windSpeed in km_h-1. Converted for display at the render layer, same
    as every other mode (see engines.py's conversion helpers).
  * Individual observation fields are frequently null even on a healthy
    station (relativeHumidity and windGust were both null on a real
    clear-weather reading). Every field is treated as optional.
  * /alerts/active?point={lat},{lon} is the right alert query and
    returns a real, populated feature list where alerts exist (verified
    against an area with 28 active alerts). Alert properties carry
    event, severity, urgency, headline, areaDesc, expires.

Two rules, same as every other feed: never block the render loop, never
invent a number. A missing field comes back as None and the engine
renders around it honestly.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import satellite

# NWS requires a User-Agent identifying the application; they explicitly
# ask for contact info and may block generic/absent agents.
_UA = "HenderburghArcade/1.0 (LED matrix display; github.com/dylanhenderson315-lab/henderburgh-arcade)"

POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
ALERTS_URL = "https://api.weather.gov/alerts/active?point={lat},{lon}"

CONDITIONS_REFRESH = 600.0    # observations update ~hourly; 10 min is plenty
ALERTS_REFRESH = 120.0        # severe alerts are the time-critical half
POINT_REFRESH = 86400.0       # gridpoint/station for a location never really changes
MAX_STATION_TRIES = 4         # see _fetch_point: nearest station often under-reports
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 10.0

# NWS severity values, most severe first. Used to pick which alert to
# show when several are active, and to drive the engine's styling.
SEVERITY_ORDER = ["Extreme", "Severe", "Moderate", "Minor", "Unknown"]


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _round_coord(v):
    """NWS 301s on high-precision coordinates -- see module docstring."""
    return round(float(v), 4)


def _val(field):
    """NWS wraps most numbers as {'value': x, 'unitCode': ...} and a null
    value is normal even on a healthy station."""
    if isinstance(field, dict):
        v = field.get("value")
        return v if isinstance(v, (int, float)) else None
    return field if isinstance(field, (int, float)) else None


def _fetch_point(lat, lon):
    """Resolve a location to its observation station + place name."""
    d = _get_json(POINTS_URL.format(lat=_round_coord(lat), lon=_round_coord(lon)))
    props = d.get("properties") or {}
    stations_url = props.get("observationStations")
    if not stations_url:
        return None
    rel = (props.get("relativeLocation") or {}).get("properties") or {}
    city = (rel.get("city") or "").upper()
    state = (rel.get("state") or "").upper()
    sd = _get_json(stations_url)
    feats = sd.get("features") or []
    if not feats:
        return None
    # Keep several candidates, not just the nearest. Stations differ a lot
    # in what they actually report: verified live that the nearest station
    # here (KMYR) returns null heatIndex AND null humidity, while KCRE and
    # KCPC a few miles away report both. Taking feats[0] blindly means
    # "feels like" would silently never appear for some locations.
    stations = [f["properties"]["stationIdentifier"] for f in feats[:MAX_STATION_TRIES]]
    return {"stations": stations, "station": stations[0], "city": city, "state": state}


def _read_observation(station):
    d = _get_json(f"https://api.weather.gov/stations/{station}/observations/latest")
    p = d.get("properties") or {}
    return {
        # Celsius and km/h -- NWS returns metric despite being a US
        # agency. Kept in source units here; converted at the render
        # layer like every other mode.
        "station": station,
        "temp_c": _val(p.get("temperature")),
        "heat_index_c": _val(p.get("heatIndex")),
        "wind_chill_c": _val(p.get("windChill")),
        "wind_kmh": _val(p.get("windSpeed")),
        "gust_kmh": _val(p.get("windGust")),
        "wind_dir_deg": _val(p.get("windDirection")),
        "humidity": _val(p.get("relativeHumidity")),
        "text": (p.get("textDescription") or "").upper(),
    }


def feels_like_c(obs):
    """The 'feels like' temperature, in Celsius, or None.

    NWS reports heatIndex and windChill as separate fields and populates
    at most one of them -- heat index only above roughly 80F, wind chill
    only below roughly 50F. Between those it reports neither, because
    feels-like genuinely IS the air temperature in that band. So:
    whichever of the two is present wins, otherwise fall back to the
    actual temperature. This never computes a value NWS didn't give us --
    the fallback is the real measured temperature, not an approximation
    of a heat index we don't have the inputs for."""
    if not obs:
        return None
    for key in ("heat_index_c", "wind_chill_c"):
        v = obs.get(key)
        if isinstance(v, (int, float)):
            return v
    return obs.get("temp_c")


def _fetch_conditions(stations):
    """Try candidate stations in order. Prefer one that reports a real
    feels-like (heatIndex/windChill) or humidity alongside temperature;
    fall back to the first that at least has a temperature."""
    first_usable = None
    for st in stations:
        try:
            obs = _read_observation(st)
        except Exception:                              # noqa: BLE001
            continue
        if obs.get("temp_c") is None:
            continue                                    # station reporting nothing useful
        if first_usable is None:
            first_usable = obs
        if (obs.get("heat_index_c") is not None or obs.get("wind_chill_c") is not None
                or obs.get("humidity") is not None):
            return obs
    return first_usable


def _fetch_alerts(lat, lon):
    d = _get_json(ALERTS_URL.format(lat=_round_coord(lat), lon=_round_coord(lon)))
    out = []
    for f in d.get("features") or []:
        p = f.get("properties") or {}
        event = (p.get("event") or "").upper()
        if not event:
            continue
        out.append({
            "event": event,
            "severity": p.get("severity") or "Unknown",
            "urgency": p.get("urgency") or "Unknown",
            "headline": (p.get("headline") or "").upper(),
            "area": (p.get("areaDesc") or "").upper(),
        })
    out.sort(key=lambda a: SEVERITY_ORDER.index(a["severity"])
             if a["severity"] in SEVERITY_ORDER else len(SEVERITY_ORDER))
    return out


class WeatherFeed:
    """Background poller with a last-good cache -- same contract as every
    other FEED in this project."""

    def __init__(self):
        self._lock = threading.Lock()
        self._point = None
        self._cond = None
        self._alerts = []
        self._cond_updated = 0.0
        self._alerts_updated = 0.0
        self._point_try = 0.0
        self._cond_try = 0.0
        self._alerts_try = 0.0
        self._last_config_check = 0.0
        self._last_read = 0.0
        self._thread = None
        self._err = None
        self._home = satellite.FEED.get_location()

    def get(self):
        """Returns {conditions, alerts, place, configured, age, err}.
        Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            cond = dict(self._cond) if self._cond else None
            alerts = [dict(a) for a in self._alerts]
            point = dict(self._point) if self._point else None
            updated, err = self._cond_updated, self._err
            label = self._home[2]
        self._ensure_thread()
        place = label
        if point and point.get("city"):
            place = point["city"]
        return {
            "conditions": cond, "alerts": alerts, "place": place,
            "configured": satellite.FEED.configured,
            "age": (now - updated) if updated else None,
            "err": err,
        }

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
            self._maybe_reload_location()
            self._refresh_point()
            self._refresh_conditions()
            self._refresh_alerts()
            time.sleep(2.0)

    def _maybe_reload_location(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        home = satellite.FEED.get_location()
        with self._lock:
            if home != self._home:
                self._home = home
                # Location changed -- station and alerts are now wrong.
                self._point = None
                self._point_try = 0.0
                self._cond_try = 0.0
                self._alerts_try = 0.0

    def _refresh_point(self):
        now = time.time()
        with self._lock:
            if self._point and now - self._point_try < POINT_REFRESH:
                return
            if not satellite.FEED.configured:
                return
            self._point_try = now
            lat, lon, _ = self._home
        try:
            point = _fetch_point(lat, lon)
            with self._lock:
                self._point = point
                if point is None:
                    # Most likely a non-US location: NWS has no gridpoint.
                    self._err = "no NWS coverage"
                else:
                    self._err = None
        except Exception as e:                          # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"

    def _refresh_conditions(self):
        now = time.time()
        with self._lock:
            if now - self._cond_try < CONDITIONS_REFRESH:
                return
            point = self._point
            if not point:
                return
            self._cond_try = now
            stations = point.get("stations") or [point["station"]]
        try:
            cond = _fetch_conditions(stations)
            with self._lock:
                self._cond = cond
                self._cond_updated = time.time()
                self._err = None
        except Exception as e:                          # noqa: BLE001
            with self._lock:
                self._err = f"{type(e).__name__}"

    def _refresh_alerts(self):
        now = time.time()
        with self._lock:
            if now - self._alerts_try < ALERTS_REFRESH:
                return
            if not satellite.FEED.configured:
                return
            self._alerts_try = now
            lat, lon, _ = self._home
        try:
            alerts = _fetch_alerts(lat, lon)
            with self._lock:
                self._alerts = alerts
                self._alerts_updated = time.time()
        except Exception:                                # noqa: BLE001
            pass          # conditions already report errors; don't double up


FEED = WeatherFeed()
