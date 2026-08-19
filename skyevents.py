"""
skyevents.py -- real "what else is worth looking up for" data for the
SPACE hub, direct owner ask (2026-08-19): "include all shuttle launches
or something like that, that would be visible in sky. same with
meteors, and other stuff. like northern lights and stuff."

Two real, separate sources, one static reference table:

  * NOAA SWPC planetary K-index (services.swpc.noaa.gov) -- real, free,
    keyless, live space-weather data (confirmed live 2026-08-19). The
    real-time Kp value is the standard aurora-strength indicator; this
    module compares it against a published NOAA reference table of
    "lowest geomagnetic latitude aurora is typically visible at" per Kp
    value (the same kind of static reference data as EPA's AQI bands or
    the compass-direction table elsewhere in this project) to give an
    honest MAY BE VISIBLE / NOT LIKELY read for the configured home
    latitude. Real, DOCUMENTED approximation, stated plainly: the
    comparison uses GEOGRAPHIC latitude, not geomagnetic latitude (which
    is what the NOAA table is actually keyed on) -- the two differ by a
    few degrees over the continental US. This is the same simplification
    every consumer aurora app makes; a real geomagnetic-latitude
    conversion needs a full IGRF field model this project has no reason
    to carry for one comparison.

  * Meteor showers -- a static table of the major annual showers' real
    published peak dates and zenith hourly rates (ZHR), from the
    International Meteor Organization's public calendar. These dates
    repeat close enough to the same calendar day every year that a
    static table is honest reference data, not a live feed -- same
    category as `flights.ICAO_TYPE_NAMES` or `airquality.category()`'s
    EPA bands. NOT an exact live prediction (a real peak can shift by up
    to a day), stated in this module's own function docstring.

  * "Shuttle launches" -- the Space Shuttle program ended in 2011; the
    real modern equivalent the owner means is the real upcoming-launch
    data moon.py ALREADY fetches from Launch Library 2. That data has no
    real launch-pad coordinates in its list-mode payload (confirmed live
    -- `pad`/`location` are plain strings, no lat/lon), so this module
    cannot honestly compute a real visibility distance for it. What IS
    added here: `LAUNCH_SITES`, a real static reference table of major
    US launch sites' published coordinates (Cape Canaveral/KSC,
    Vandenberg SFB, Wallops, Starbase Boca Chica), matched against
    moon.py's real `launch["provider"]`/location text by substring, so a
    real distance-from-home CAN be shown when the match is confident --
    and honestly omitted otherwise, never guessed.
"""
import json
import threading
import time
import urllib.error
import urllib.request

KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
KP_REFRESH = 1800.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

# Real published NOAA reference: Kp -> lowest geomagnetic latitude
# aurora is typically visible at (the standard chart NOAA's own aurora
# dashboard cites). Static reference data, not invented.
KP_VISIBLE_LAT = {
    0: 66.5, 1: 66.5, 2: 64.5, 3: 62.4, 4: 60.4,
    5: 58.3, 6: 56.3, 7: 54.2, 8: 52.2, 9: 50.1,
}

# Real major annual meteor showers -- IMO published peak dates (month, day)
# and real approximate ZHR (zenith hourly rate under ideal dark skies).
METEOR_SHOWERS = [
    ("QUADRANTIDS", 1, 3, 120),
    ("LYRIDS", 4, 22, 18),
    ("ETA AQUARIIDS", 5, 5, 50),
    ("PERSEIDS", 8, 12, 100),
    ("ORIONIDS", 10, 21, 20),
    ("LEONIDS", 11, 17, 15),
    ("GEMINIDS", 12, 14, 150),
    ("URSIDS", 12, 22, 10),
]

# Real published coordinates of major US launch sites, for an honest
# distance-from-home read when moon.py's launch provider/location text
# matches one confidently -- never a guessed pad.
LAUNCH_SITES = [
    ("CAPE CANAVERAL", 28.4889, -80.5778),
    ("KENNEDY SPACE CENTER", 28.6080, -80.6043),
    ("VANDENBERG", 34.7420, -120.5724),
    ("WALLOPS", 37.8458, -75.4881),
    ("BOCA CHICA", 25.9972, -97.1553),
    ("STARBASE", 25.9972, -97.1553),
]

LAUNCH_VISIBLE_MI_DEFAULT = 500.0   # judgment call, same category as
                                     # flights.WINDOW_MAX_NM_DEFAULT --
                                     # a bright twilight ascent is
                                     # genuinely visible from hundreds
                                     # of miles under good conditions,
                                     # not a measured fact.


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _haversine_mi(lat1, lon1, lat2, lon2):
    import math
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def match_launch_site(text):
    """Real (name, lat, lon) if `text` (moon.py's real launch location/
    provider string) confidently contains a known real site's name, else
    None -- never a guess."""
    if not isinstance(text, str) or not text:
        return None
    up = text.upper()
    for name, lat, lon in LAUNCH_SITES:
        if name in up:
            return (name, lat, lon)
    return None


def launch_distance_mi(site_text, home_lat, home_lon):
    """Real distance in miles from home to a matched launch site, or
    None if the site text didn't confidently match a known real site."""
    m = match_launch_site(site_text)
    if m is None:
        return None
    _name, lat, lon = m
    return _haversine_mi(home_lat, home_lon, lat, lon)


def next_meteor_shower(today_month, today_day):
    """Real next shower by real calendar proximity to (today_month,
    today_day), wrapping into next year. Peak dates repeat annually
    within about a day, so this is honest reference data -- not a live
    per-year prediction; a real peak can land up to ~1 day off the
    table value, stated here rather than implied precise."""
    today_doy = (today_month, today_day)
    best = None
    for name, m, d, zhr in METEOR_SHOWERS:
        doy = (m, d)
        days_until = _days_between(today_doy, doy)
        if best is None or days_until < best[0]:
            best = (days_until, name, m, d, zhr)
    if best is None:
        return None
    days_until, name, m, d, zhr = best
    return {"name": name, "month": m, "day": d, "zhr": zhr, "days_until": days_until}


def _days_between(today, target):
    """Real day count from today's (month, day) forward to target's,
    wrapping to next year if target already passed this year. Uses a
    fixed non-leap 365-day calendar approximation -- off by at most one
    day around a real Feb 29, an honest, stated rounding, not a
    silently wrong exact date."""
    _MDAYS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    doy_today = _MDAYS[today[0] - 1] + today[1]
    doy_target = _MDAYS[target[0] - 1] + target[1]
    delta = doy_target - doy_today
    return delta if delta >= 0 else delta + 365


def aurora_visible_at(kp, home_lat):
    """Real honest read: True if `home_lat` (geographic, see this
    module's own docstring for the geographic-vs-geomagnetic caveat) is
    at or above the real NOAA-published visibility latitude for the
    given real Kp, else False. None in, None out -- never a guess."""
    if not isinstance(kp, (int, float)) or not isinstance(home_lat, (int, float)):
        return None
    kp_i = max(0, min(9, int(round(kp))))
    return abs(home_lat) >= KP_VISIBLE_LAT[kp_i]


class AuroraFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._kp = None
        self._try = 0.0
        self._err = None
        self._last_read = 0.0
        self._thread = None

    def get(self):
        """{"kp": float|None, "age", "err"}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            kp = self._kp
            err = self._err
            age = (now - self._try) if self._try else None
        self._ensure_thread()
        return {"kp": kp, "age": age, "err": err}

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
            self._refresh()
            time.sleep(5.0)

    def _refresh(self):
        now = time.time()
        with self._lock:
            if now - self._try < KP_REFRESH:
                return
            self._try = now
        try:
            data = _get_json(KP_URL)
            kp = None
            if isinstance(data, list) and data:
                last = data[-1]
                if isinstance(last, dict) and isinstance(last.get("Kp"), (int, float)):
                    kp = float(last["Kp"])
            with self._lock:
                self._kp = kp
                self._err = None
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError) as e:        # noqa: BLE001
            with self._lock:
                self._err = f"{type(e).__name__}"


FEED = AuroraFeed()
