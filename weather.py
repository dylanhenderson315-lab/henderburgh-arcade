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
HOURLY_REFRESH = 1800.0       # hourly forecast periods change slowly too -- 30 min is plenty
HOURLY_MAX_PERIODS = 18       # ~18 real hours ahead is plenty for a wall-panel forecast view
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


_WIND_MPH_RE = re.compile(r"(\d+)")


def _parse_wind_mph(s):
    """"12 mph" -> 12, "10 to 15 mph" -> 10 (leading number -- the sustained
    low end, same conservative choice as reading a single number off a
    range rather than guessing which end matters more). None if the real
    field is missing or unparseable -- never a guessed speed."""
    if not s:
        return None
    m = _WIND_MPH_RE.search(str(s))
    return int(m.group(1)) if m else None


def _heat_index_f(temp_f, rh_pct):
    """NWS's own public Rothfusz regression -- the SAME formula NWS uses
    to compute the `heatIndex` field the observation endpoint reports
    directly (see feels_like_c()'s own docstring on why THAT function
    never computes a value: it doesn't need to, a real reported field
    already exists there). The hourly forecast endpoint carries no such
    field at all (confirmed live -- only temperature/dewpoint/
    relativeHumidity), so there is nothing to prefer over computing it;
    this is the one place in the project a "feels like" number is
    genuinely derived rather than read, and it's a published National
    Weather Service formula, not a guess. Meaningful above ~80F; NWS's
    own simpler average is used below that (their own documented
    threshold), so this never overstates heat index at a mild
    temperature."""
    t, r = float(temp_f), float(rh_pct)
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + r * 0.094)
    if (hi + t) / 2.0 < 80.0:
        return hi
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r
          - 0.00683783 * t * t - 0.05481717 * r * r + 0.00122874 * t * t * r
          + 0.00085282 * t * r * r - 0.00000199 * t * t * r * r)
    if r < 13 and 80 <= t <= 112:
        hi -= ((13 - r) / 4.0) * (((17 - abs(t - 95.0)) / 17.0) ** 0.5)
    elif r > 85 and 80 <= t <= 87:
        hi += ((r - 85) / 10.0) * ((87 - t) / 5.0)
    return hi


def _wind_chill_f(temp_f, wind_mph):
    """NWS's own public wind chill formula -- same "genuinely derived,
    real published formula, not a guess" reasoning as _heat_index_f()
    above. Only meaningful at real NWS thresholds: <=50F and >=3mph."""
    t, v = float(temp_f), float(wind_mph)
    return 35.74 + 0.6215 * t - 35.75 * (v ** 0.16) + 0.4275 * t * (v ** 0.16)


def _hourly_feels_like_f(temp_f, rh_pct, wind_mph):
    """Whichever of heat index / wind chill genuinely applies at NWS's
    own real thresholds, else None -- the actual temperature already IS
    the "feels like" value outside those bands, same as feels_like_c()'s
    own reasoning, so this returns None there rather than a redundant
    duplicate of the real temp."""
    if not isinstance(temp_f, (int, float)):
        return None
    if temp_f >= 80 and isinstance(rh_pct, (int, float)):
        return _heat_index_f(temp_f, rh_pct)
    if temp_f <= 50 and isinstance(wind_mph, (int, float)) and wind_mph >= 3:
        return _wind_chill_f(temp_f, wind_mph)
    return None


def _fetch_hourly(hourly_url):
    """Real next-N-hours forecast: temp (already Fahrenheit -- verified
    live, same as _fetch_forecast()'s own note), precip chance, a short
    conditions phrase, and wind. NWS's hourly endpoint returns up to 156
    hours (6.5 real days); only the next HOURLY_MAX_PERIODS are kept --
    a wall panel forecast view has no use for a week of hourly data, and
    keeping less means less to poll-parse every refresh."""
    d = _get_json(hourly_url)
    periods = (d.get("properties") or {}).get("periods") or []
    out = []
    for p in periods[:HOURLY_MAX_PERIODS]:
        pop = p.get("probabilityOfPrecipitation") or {}
        rh = (p.get("relativeHumidity") or {}).get("value")
        t = p.get("temperature")
        temp_f = round(t) if isinstance(t, (int, float)) else None
        wind_mph = _parse_wind_mph(p.get("windSpeed"))
        feels = _hourly_feels_like_f(temp_f, rh, wind_mph)
        out.append({
            "start": p.get("startTime"),
            "temp_f": temp_f,
            "feels_f": round(feels) if isinstance(feels, (int, float)) else None,
            "precip_pct": pop.get("value"),
            "short": paneltext.panel_text(p.get("shortForecast")),
            "wind_mph": wind_mph,
            "wind_dir": p.get("windDirection"),
        })
    return out


def _zone_id_from_url(url):
    """https://api.weather.gov/zones/county/SCC051 -> "SCC051". NWS's
    /points response gives full zone URLs, not bare UGC codes; the
    alerts endpoint wants the bare code."""
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


def _fetch_point(lat, lon):
    """Resolve a location to its observation station + place name + the
    real UGC zone codes NWS alerts are actually filed against."""
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
    # ZONE-BASED ALERTS (2026-08-10): the forecast zone AND the county
    # zone -- a real Severe Thunderstorm Warning was confirmed live to be
    # geocoded against BOTH SCC051 (county) and a different set than the
    # forecast zone SCZ054, so both are kept and deduped downstream
    # rather than picking just one. See _fetch_alerts_active()'s own
    # docstring for why point-based alerts alone aren't enough.
    zones = [_zone_id_from_url(props.get("county")),
             _zone_id_from_url(props.get("forecastZone"))]
    zones = [z for z in dict.fromkeys(zones) if z]   # dedup, drop None, keep order
    return {"stations": stations, "station": stations[0], "city": city, "state": state,
            "forecast_url": props.get("forecast"), "hourly_url": props.get("forecastHourly"),
            "zones": zones}


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


def _param_first(parameters, *keys):
    """First real value on an NWS CAP parameter, or None.

    NWS wraps most of these as a one-item list of strings. Missing key
    or empty list is the honest gap -- never a guessed hail size / gust.
    """
    params = parameters or {}
    for k in keys:
        vals = params.get(k)
        if vals is None:
            continue
        v = vals[0] if isinstance(vals, list) else vals
        if v is None or v == "":
            continue
        return v
    return None


def _parse_storm_cells(parameters):
    """Every real storm cell NWS put in eventMotionDescription.

    A single warning often tracks TWO or THREE cells (confirmed live
    shape: '260DEG...35KT...33.72,-78.91 270DEG...40KT...33.80,-79.10').
    The old parser kept only the first match, so extra cells never
    appeared on the board. Empty / unparseable -> [].
    """
    vals = (parameters or {}).get("eventMotionDescription")
    if not vals:
        return []
    if isinstance(vals, list):
        text = " ".join(str(v) for v in vals if v)
    else:
        text = str(vals)
    out = []
    for m in _MOTION_RE.finditer(text):
        out.append({
            "motion_dir_deg": float(m.group(1)),
            "motion_speed_kt": float(m.group(2)),
            "lat": float(m.group(3)),
            "lon": float(m.group(4)),
        })
    return out


def _hail_in(parameters):
    """Max hail size in inches, or None. NWS `maxHailSize` is a real
    string/number when the warning carries one ('0.88', '1.00')."""
    v = _param_first(parameters, "maxHailSize", "hailSize")
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        m = re.search(r"(\d+(?:\.\d+)?)", v)
        if m:
            x = float(m.group(1))
            return x if x > 0 else None
    return None


def _gust_mph(parameters):
    """Max wind gust in mph, or None. Alert parameters are already
    imperial on this endpoint (unlike observations, which are metric)."""
    v = _param_first(parameters, "maxWindGust", "windGust")
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        m = re.search(r"(\d+(?:\.\d+)?)", v)
        if m:
            x = float(m.group(1))
            return x if x > 0 else None
    return None


def _tornado_flag(parameters):
    """NWS tornadoDetection / tornadoPossible text, folded, or None."""
    v = _param_first(parameters, "tornadoDetection", "tornadoPossible")
    if isinstance(v, str) and v.strip():
        return paneltext.panel_text(v) or None
    return None


def _iso_epoch(s):
    """NWS ISO timestamp -> unix seconds, or None. Keeps the offset
    NWS sent (usually local-with-offset or Z) -- never assumes UTC
    on a naive slice."""
    if not isinstance(s, str) or not s.strip():
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def _polygon_centroid(coords):
    """Mean lat/lon of a GeoJSON ring, or None. Closing vertex (same
    as the first) is dropped so it isn't double-counted. This is the
    warned-area centre when NWS sent a polygon but no motion cell --
    a real position, not a guessed storm track."""
    if not coords:
        return None
    pts = list(coords)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return None
    try:
        lat = sum(float(p[1]) for p in pts) / len(pts)
        lon = sum(float(p[0]) for p in pts) / len(pts)
    except (TypeError, ValueError, IndexError):
        return None
    return lat, lon


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


def _fetch_alerts(lat, lon, zones=None):
    """Real alerts covering this location. Uses NWS's own UGC zone query
    (county + forecast zone) when known, falling back to the point-based
    query before the first successful _fetch_point() call resolves them.

    WHY ZONE, NOT JUST POINT -- verified live 2026-08-10, not assumed.
    /alerts/active?point={lat},{lon} only returns an alert whose actual
    geometry (a storm-tracked POLYGON for Severe Thunderstorm/Tornado/
    Flash Flood warnings) contains that exact coordinate. A real,
    concurrently-active Severe Thunderstorm Warning for this project's
    configured home county (Horry, SC / SCC051) was confirmed to be
    completely ABSENT from the point query while the storm's real
    polygon simply hadn't swept over the literal home coordinates yet --
    even though the warning covers the whole county, which is exactly
    the granularity a phone's Wireless Emergency Alert fires at. A
    point-only query is more geographically precise but misses real
    warnings a person would otherwise be notified about on their phone
    for the same location. Querying by zone (comma-separated, confirmed
    live NWS accepts multiple: `?zone=SCC051,SCZ054`) matches that real
    phone-alert experience instead of a stricter-than-necessary point
    match. Zone-based watches/advisories (which cover a whole zone
    anyway, not a polygon) return identically either way -- this only
    changes behavior for the polygon-warned severe event class, which is
    exactly the class most worth not missing."""
    if zones:
        url = "https://api.weather.gov/alerts/active?zone=" + ",".join(zones)
    else:
        url = ALERTS_URL.format(lat=_round_coord(lat), lon=_round_coord(lon))
    d = _get_json(url)
    out = []
    for f in d.get("features") or []:
        p = f.get("properties") or {}
        event = paneltext.panel_text(p.get("event"))
        if not event:
            continue
        # REAL BUG FIXED 2026-08-11, confirmed against a real live NWS
        # product, not hypothetical: a real "TEST TSUNAMI WARNING" (the
        # National Tsunami Warning Center's own periodic nationwide
        # system test -- real, scheduled, covers essentially the whole
        # US coastline at once, which is why its area list is enormous
        # and legitimately includes a real owner's configured zone) was
        # rendering as a genuine Extreme-severity alert with no
        # indication anywhere that it was a drill. This is exactly the
        # kind of false alarm this project's own "never invent, never
        # mislead" rule exists to prevent -- showing a TEST product with
        # the same visual weight as a real one is a form of inventing
        # urgency that isn't real.
        #
        # Two independent layers, since a false negative here (a REAL
        # tsunami warning silently dropped) would be far worse than a
        # false positive:
        #   1. NWS's own CAP `status` field is the authoritative signal
        #      (real values: Actual/Exercise/System/Test/Draft, per the
        #      CAP spec NWS alerts implement) -- skip anything that is
        #      present and not "Actual".
        #   2. A literal "TEST" prefix on the real event/headline text is
        #      a second, redundant check (confirmed present on the real
        #      product that exposed this bug: "TEST TSUNAMI WARNING") --
        #      catches a real product that for any reason didn't carry a
        #      trustworthy status field, without relying on status alone.
        status = p.get("status")
        headline_raw = (p.get("headline") or "").upper()
        if (status and status != "Actual") or event.upper().startswith("TEST ") \
                or headline_raw.startswith("TEST "):
            continue
        params = p.get("parameters") or {}
        hail = _hail_in(params)
        gust = _gust_mph(params)
        tornado = _tornado_flag(params)
        expires = _iso_epoch(p.get("expires") or p.get("ends"))
        alert_id = str(p.get("id") or f.get("id") or event)
        entry = {
            "alert_id": alert_id,
            "event": event,
            "severity": p.get("severity") or "Unknown",
            "urgency": p.get("urgency") or "Unknown",
            "headline": paneltext.panel_text(p.get("headline")),
            "area": paneltext.panel_text(p.get("areaDesc")),
            "hail_in": hail,
            "gust_mph": gust,
            "tornado": tornado,
            "expires": expires,
            # First cell copied here so existing radar/alert mini-scope
            # readers keep working. Full list is `cells`.
            "storm_bearing_deg": None,
            "storm_dist_nm": None,
            "storm_motion_dir_deg": None,
            "storm_motion_speed_kt": None,
            "polygon": None,
            "cells": [],
        }
        cells = _parse_storm_cells(params)
        # REAL WARNED-AREA POLYGON -- GeoJSON [lon, lat] order, verified
        # live. Used as a fallback position when NWS sent a polygon but
        # no motion cell, so the cell still plots on the scope.
        geom = f.get("geometry")
        coords = None
        if geom and geom.get("type") == "Polygon":
            coords = (geom.get("coordinates") or [[]])[0]
            poly = []
            for lon_v, lat_v in coords:
                pbrg, pdist_nm = _bearing_distance_nm(lat, lon, lat_v, lon_v)
                poly.append((pbrg, pdist_nm))
            entry["polygon"] = poly
        if not cells and coords:
            cen = _polygon_centroid(coords)
            if cen:
                cells = [{"motion_dir_deg": None, "motion_speed_kt": None,
                          "lat": cen[0], "lon": cen[1]}]
        built = []
        for c in cells:
            brg, dist_nm = _bearing_distance_nm(lat, lon, c["lat"], c["lon"])
            built.append({
                "bearing_deg": brg,
                "dist_nm": dist_nm,
                "motion_dir_deg": c.get("motion_dir_deg"),
                "motion_speed_kt": c.get("motion_speed_kt"),
            })
        entry["cells"] = built
        if built:
            first = built[0]
            entry["storm_bearing_deg"] = first["bearing_deg"]
            entry["storm_dist_nm"] = first["dist_nm"]
            entry["storm_motion_dir_deg"] = first["motion_dir_deg"]
            entry["storm_motion_speed_kt"] = first["motion_speed_kt"]
        out.append(entry)
    out.sort(key=lambda a: SEVERITY_ORDER.index(a["severity"])
             if a["severity"] in SEVERITY_ORDER else len(SEVERITY_ORDER))
    return out


def tracks_from_alerts(alerts):
    """Flatten alerts into selectable tracks -- one per real storm cell,
    plus one zone-only row when an alert has no point to plot.

    Identity is alert_id + cell index, never a list position. Sorted:
    on-scope nearest first, then zone advisories by severity.
    """
    out = []
    for a in alerts or []:
        cells = a.get("cells") or []
        aid = a.get("alert_id") or a.get("event") or "alert"
        shared = {
            "alert_id": aid,
            "event": a.get("event"),
            "severity": a.get("severity") or "Unknown",
            "urgency": a.get("urgency"),
            "headline": a.get("headline"),
            "area": a.get("area"),
            "hail_in": a.get("hail_in"),
            "gust_mph": a.get("gust_mph"),
            "tornado": a.get("tornado"),
            "expires": a.get("expires"),
        }
        if cells:
            for i, c in enumerate(cells):
                rec = dict(shared)
                rec["id"] = "%s:%d" % (aid, i)
                rec["bearing_deg"] = c.get("bearing_deg")
                rec["dist_nm"] = c.get("dist_nm")
                rec["motion_dir_deg"] = c.get("motion_dir_deg")
                rec["motion_speed_kt"] = c.get("motion_speed_kt")
                rec["on_scope"] = c.get("bearing_deg") is not None
                out.append(rec)
        else:
            rec = dict(shared)
            rec["id"] = str(aid)
            rec["bearing_deg"] = None
            rec["dist_nm"] = None
            rec["motion_dir_deg"] = None
            rec["motion_speed_kt"] = None
            rec["on_scope"] = False
            out.append(rec)
    def _key(t):
        sev = SEVERITY_ORDER.index(t["severity"]) if t["severity"] in SEVERITY_ORDER \
            else len(SEVERITY_ORDER)
        dist = t.get("dist_nm")
        on = 0 if t.get("on_scope") else 1
        return (on, sev, dist if isinstance(dist, (int, float)) else 1e9)
    out.sort(key=_key)
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
        self._hourly = []
        self._hourly_try = 0.0
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
            hourly = [dict(h) for h in self._hourly]
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
            "tracks": tracks_from_alerts(alerts),
            "high_f": fc.get("high_f"), "low_f": fc.get("low_f"),
            "hourly": hourly,
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
            self._refresh_hourly()
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

    def _refresh_hourly(self):
        """Real next-N-hours forecast for the hourly forecast view
        (2026-08-10). Same cadence discipline as _refresh_forecast() --
        this data changes slowly, so it's polled far less than every 2s
        loop tick."""
        now = time.time()
        with self._lock:
            if now - self._hourly_try < HOURLY_REFRESH:
                return
            point = self._point
            if not point or not point.get("hourly_url"):
                return
            self._hourly_try = now
            url = point["hourly_url"]
        try:
            hourly = _fetch_hourly(url)
            with self._lock:
                self._hourly = hourly
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
            zones = list(self._point["zones"]) if self._point and self._point.get("zones") else None
        try:
            alerts = _fetch_alerts(lat, lon, zones=zones)
            with self._lock:
                self._alerts = alerts
                self._alerts_updated = time.time()
        except Exception:                                # noqa: BLE001
            pass          # conditions already report errors; don't double up


FEED = WeatherFeed()
