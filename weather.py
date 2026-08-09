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
import calendar
import json
import math
import re
from datetime import date as _date
import math
import calendar
import threading
import time
import urllib.error
import urllib.request

import paneltext

import satellite

# NWS requires a User-Agent identifying the application; they explicitly
# ask for contact info and may block generic/absent agents.
_UA = "HenderburghArcade/1.0 (LED matrix display; github.com/dylanhenderson315-lab/henderburgh-arcade)"

POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
ALERTS_URL = "https://api.weather.gov/alerts/active?point={lat},{lon}"

CONDITIONS_REFRESH = 600.0    # observations update ~hourly; 10 min is plenty
FORECAST_REFRESH = 3600.0     # high/low changes slowly; one request an hour
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


# ---- sun times ---------------------------------------------------------
# NWS does NOT provide sunrise/sunset. Checked the whole /points and
# /gridpoints/.../forecast payloads: no sunrise, sunset, or daylight key
# anywhere. So these are COMPUTED locally with the standard NOAA solar
# equations rather than fetched.
#
# That is calculation, not invention -- the same category as the haversine
# distance in satellite.py. Sun times are a deterministic function of
# latitude, longitude and date; there is nothing to guess. It also costs
# no network request and cannot go stale.
#
# Verified against an independent source for the configured location
# before being shipped (see the commit message for the comparison).

def _solar_event(lat, lon, when, rising):
    """UTC hour (float) of sunrise/sunset, or None above/below the polar
    circles where the sun may not cross the horizon that day."""
    n = when.toordinal() - _date(when.year, 1, 1).toordinal() + 1
    lng_hour = lon / 15.0
    t = n + ((6.0 if rising else 18.0) - lng_hour) / 24.0
    m = 0.9856 * t - 3.289                                  # sun's mean anomaly
    l = (m + 1.916 * math.sin(math.radians(m))
         + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360.0
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360.0
    # Right ascension must land in the same quadrant as L.
    ra = (ra + (math.floor(l / 90.0) * 90.0 - math.floor(ra / 90.0) * 90.0)) / 15.0
    sin_dec = 0.39782 * math.sin(math.radians(l))
    cos_dec = math.cos(math.asin(sin_dec))
    zenith = math.radians(90.833)                            # includes refraction + solar disc
    cos_h = ((math.cos(zenith) - sin_dec * math.sin(math.radians(lat)))
             / (cos_dec * math.cos(math.radians(lat))))
    if cos_h > 1 or cos_h < -1:
        return None                                          # sun never rises/sets here today
    h = (360.0 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    return (h / 15.0 + ra - 0.06571 * t - 6.622 - lng_hour) % 24.0


def sun_times(lat, lon, when=None):
    """(sunrise_epoch, sunset_epoch) in local time, or (None, None)."""
    when = when or _date.today()
    out = []
    for rising in (True, False):
        ut = _solar_event(lat, lon, when, rising)
        if ut is None:
            return (None, None)
        secs = int(round(ut * 3600))
        out.append(calendar.timegm((when.year, when.month, when.day,
                                    secs // 3600, (secs % 3600) // 60, secs % 60, 0, 0, 0)))
    return tuple(out)


def _fetch_forecast(forecast_url):
    """Today's high and low from the NWS forecast periods.

    VERIFIED present: periods carry `temperature`, `temperatureUnit` and
    `isDaytime`. Note the forecast endpoint returns FAHRENHEIT directly
    (unlike the observations endpoint, which is metric) -- so this is one
    of the few places no conversion is wanted.
    """
    d = _get_json(forecast_url)
    periods = (d.get("properties") or {}).get("periods") or []
    high = low = None
    for p in periods[:3]:                    # today + tonight is enough
        t, unit = p.get("temperature"), (p.get("temperatureUnit") or "F").upper()
        if not isinstance(t, (int, float)):
            continue
        f = t if unit == "F" else t * 9.0 / 5.0 + 32.0
        if p.get("isDaytime") and high is None:
            high = round(f)
        elif not p.get("isDaytime") and low is None:
            low = round(f)
    return {"high_f": high, "low_f": low}


def _fetch_point(lat, lon):
    """Resolve a location to its observation station + place name."""
    d = _get_json(POINTS_URL.format(lat=_round_coord(lat), lon=_round_coord(lon)))
    props = d.get("properties") or {}
    stations_url = props.get("observationStations")
    if not stations_url:
        return None
    rel = (props.get("relativeLocation") or {}).get("properties") or {}
    city = paneltext.panel_text(rel.get("city"))
    state = paneltext.panel_text(rel.get("state"))
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
    return {"stations": stations, "station": stations[0], "city": city, "state": state,
            "forecast_url": props.get("forecast")}


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
        "text": paneltext.panel_text(p.get("textDescription")),
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


# ---- storm position/motion, for the severe-alert screen's mini-scope
# (2026-08-09) -------------------------------------------------------------
# NWS's real polygon-based warnings (Severe Thunderstorm/Tornado/Special
# Marine Warning -- NOT the broader watches/advisories) carry a real
# `eventMotionDescription` parameter: a fixed-format string like
# "2026-08-09T17:03:00-00:00...storm...273DEG...36KT...42.74,-87.69",
# verified against real live nationwide alert data before writing this
# (only ~3.6% of active alerts nationwide carry ANY geometry at all, and
# it's specifically the polygon-warning tier, not zone-based watches --
# checked, not assumed). This is REAL storm centroid position + REAL
# motion direction/speed, straight from the forecaster's own warning
# text -- not derived, not estimated. Absent on the (much more common)
# zone-based watch/advisory alerts, which is an honest gap: those alerts
# genuinely have no point-position to plot, only a county/zone list.
_MOTION_RE = re.compile(
    r"(\d+)DEG\.\.\.(\d+)KT\.\.\.(-?\d+\.?\d*),(-?\d+\.?\d*)")


def _parse_storm_motion(parameters):
    """Real (motion_dir_deg, motion_speed_kt, lat, lon) from NWS's
    eventMotionDescription parameter, or None if this alert doesn't carry
    one (most don't -- see the module note above)."""
    vals = (parameters or {}).get("eventMotionDescription")
    if not vals:
        return None
    text = vals[0] if isinstance(vals, list) else vals
    m = _MOTION_RE.search(text or "")
    if not m:
        return None
    return {
        "motion_dir_deg": float(m.group(1)),
        "motion_speed_kt": float(m.group(2)),
        "lat": float(m.group(3)),
        "lon": float(m.group(4)),
    }


def _bearing_distance_nm(lat1, lon1, lat2, lon2):
    """(bearing_deg, distance_nm) from point 1 to point 2 -- same
    great-circle math as flights.bearing_distance()/satellite.haversine_km,
    kept as its own small local copy rather than importing flights here
    (this module has never depended on flights.py, and the math is a
    handful of lines -- matches this project's existing precedent of each
    feed module keeping its own copy rather than adding a cross-feed
    import for a shared formula, see satellite.py's own haversine_km)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    dp = p2 - p1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d_km = 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))
    brg = math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360.0
    return brg, d_km * 0.539957


def _fetch_alerts(lat, lon):
    d = _get_json(ALERTS_URL.format(lat=_round_coord(lat), lon=_round_coord(lon)))
    out = []
    for f in d.get("features") or []:
        p = f.get("properties") or {}
        event = paneltext.panel_text(p.get("event"))
        if not event:
            continue
        entry = {
            "event": event,
            "severity": p.get("severity") or "Unknown",
            "urgency": p.get("urgency") or "Unknown",
            "headline": paneltext.panel_text(p.get("headline")),
            "area": paneltext.panel_text(p.get("areaDesc")),
            # Real storm position/motion when NWS's warning carries one --
            # see _parse_storm_motion()'s module note. None on the (much
            # more common) zone-based watch/advisory, an honest gap, never
            # guessed at.
            "storm_bearing_deg": None,
            "storm_dist_nm": None,
            "storm_motion_dir_deg": None,
            "storm_motion_speed_kt": None,
        }
        motion = _parse_storm_motion(p.get("parameters"))
        if motion:
            brg, dist_nm = _bearing_distance_nm(lat, lon, motion["lat"], motion["lon"])
            entry["storm_bearing_deg"] = brg
            entry["storm_dist_nm"] = dist_nm
            entry["storm_motion_dir_deg"] = motion["motion_dir_deg"]
            entry["storm_motion_speed_kt"] = motion["motion_speed_kt"]
        out.append(entry)
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
        self._fc = {}
        self._fc_try = 0.0
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
            fc = dict(self._fc)
            updated, err = self._cond_updated, self._err
            label = self._home[2]
        self._ensure_thread()
        place = label
        if point and point.get("city"):
            place = point["city"]
        lat, lon, _ = self._home
        sunrise, sunset = sun_times(lat, lon)
        return {
            "conditions": cond, "alerts": alerts, "place": place,
            "high_f": fc.get("high_f"), "low_f": fc.get("low_f"),
            "sunrise": sunrise, "sunset": sunset,
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
            self._refresh_forecast()
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

    def _refresh_forecast(self):
        """Today's high/low. Forecasts change slowly, so this is polled far
        less often than observations -- one extra request per hour."""
        now = time.time()
        with self._lock:
            if now - self._fc_try < FORECAST_REFRESH:
                return
            point = self._point
            if not point or not point.get("forecast_url"):
                return
            self._fc_try = now
            url = point["forecast_url"]
        try:
            fc = _fetch_forecast(url)
            with self._lock:
                self._fc = fc
        except Exception:                                # noqa: BLE001
            pass          # conditions already report errors; don't double up

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
