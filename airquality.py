"""
airquality.py -- real US AQI for the configured home location.

Same shape as weather.py/flights.py/satellite.py: a background-thread
poller with a last-good cache, a FEED singleton, a get() that never
blocks, self-limiting (IDLE_STOP) so an unread mode doesn't poll
forever. Location is NOT duplicated -- reuses satellite.py's
location_config.json via satellite.FEED.get_location(), same as
weather.py already does.

Source: Open-Meteo's air-quality API (air-quality-api.open-meteo.com).
Confirmed live and free 2026-08-19 -- no key, no signup, no card, no
documented rate limit at this project's real polling volume (one
request per AQI_REFRESH). Real fields returned: hourly `us_aqi` (the
real EPA US Air Quality Index, 0-500+).

AQI CATEGORY BREAKPOINTS ARE REAL EPA STANDARD BANDS, not invented:
0-50 Good, 51-100 Moderate, 101-150 Unhealthy for Sensitive Groups,
151-200 Unhealthy, 201-300 Very Unhealthy, 301+ Hazardous. Never
interpolated or guessed -- the raw AQI number and its real published
band.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import satellite

URL = ("https://air-quality-api.open-meteo.com/v1/air-quality"
       "?latitude={lat}&longitude={lon}&hourly=us_aqi&timezone=auto&forecast_days=1")

AQI_REFRESH = 1800.0   # 30min -- air quality does not swing minute to minute
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

# Real EPA US AQI category bands (0-50 Good ... 301+ Hazardous),
# published reference data, not derived or guessed.
_BANDS = (
    (50, "GOOD"), (100, "MODERATE"), (150, "UNHEALTHY-SG"),
    (200, "UNHEALTHY"), (300, "VERY UNHEALTHY"),
)


def category(aqi):
    """Real EPA US AQI category name for a real aqi int, or None."""
    if not isinstance(aqi, (int, float)):
        return None
    for ceiling, name in _BANDS:
        if aqi <= ceiling:
            return name
    return "HAZARDOUS"


class AirQualityFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._aqi = None
        self._updated = 0.0
        self._try = 0.0
        self._err = None
        self._last_read = 0.0
        self._thread = None
        self._home = satellite.FEED.get_location()

    def get(self):
        """{"aqi": int|None, "category": str|None, "age": float|None,
        "err": str|None, "configured": bool}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            aqi, updated, err = self._aqi, self._updated, self._err
        self._ensure_thread()
        return {
            "aqi": aqi,
            "category": category(aqi),
            "age": (now - updated) if updated else None,
            "err": err,
            "configured": satellite.FEED.configured,
        }

    def peek(self):
        """PASSIVE read -- same contract as weather.FEED.peek() (see
        CLAUDE.md's "Cross-engine opportunistic reads" section). Never
        calls _ensure_thread(), never touches _last_read. None when the
        thread isn't already alive for some other real reason."""
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return None
            if self._aqi is None:
                return None
            return {"aqi": self._aqi, "category": category(self._aqi)}

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
            if now - self._try < AQI_REFRESH:
                return
            self._try = now
            if not satellite.FEED.configured:
                return
            lat, lon, _ = satellite.FEED.get_location()
        try:
            req = urllib.request.Request(
                URL.format(lat=round(lat, 4), lon=round(lon, 4)),
                headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read())
            hourly = data.get("hourly") or {}
            aqi_list = hourly.get("us_aqi") or []
            # First non-null value is "now" -- Open-Meteo's hourly series
            # starts at the current hour for forecast_days=1.
            aqi = next((v for v in aqi_list if isinstance(v, (int, float))), None)
            if aqi is None:
                raise ValueError("no real us_aqi value in response")
            with self._lock:
                self._aqi = aqi
                self._updated = now
                self._err = None
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError) as e:              # noqa: BLE001
            with self._lock:
                self._err = f"{type(e).__name__}"


FEED = AirQualityFeed()
