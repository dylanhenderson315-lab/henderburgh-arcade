"""
flights.py -- free ADS-B flight data for the flights mode.

Same shape as market.py and satellite.py, deliberately: all I/O lives here
so the mode that draws it stays pure, background thread with a last-good
cache, never blocks the render loop, never invents a number.

Two keyless sources:
  * api.adsb.lol         -- live aircraft positions within a radius of a
    point. A free, keyless, community ADS-B aggregator (no registration,
    no rate-limit friction at this call volume).
  * api.adsbdb.com       -- route/aircraft/airline enrichment by callsign.
    Free, keyless, explicitly recommended in PRODUCTION.md over FlightAware
    AeroAPI (which has a real $100/month minimum past its free tier and
    doesn't scale per-unit-sold for the production device).

Home location is NOT duplicated here -- it reuses satellite.py's
location_config.json via satellite.FEED.get_location(). "Near home
coordinates" is the same concept the ISS tracker already needed a home
location for; a second config file for the same lat/lon would just be two
places that could disagree.

Route enrichment is cached per callsign in a plain dict for the life of
the process, since a callsign's route doesn't change between lookups and
adsbdb has no reason to be asked the same question twice. Lookups are
capped per refresh (see MAX_LOOKUPS_PER_REFRESH) so a sky full of new
aircraft can't turn one refresh cycle into dozens of outbound requests --
new callsigns just enrich over the following cycles instead.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import satellite

POSITION_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"

RADIUS_NM = 40                  # "in the local sky" -- roughly a 15 min drive's worth of horizon
MAX_TRACKED = 8                 # nearest N, so the mode has a bounded, meaningful list
MAX_LOOKUPS_PER_REFRESH = 4     # be polite to adsbdb; new callsigns just enrich next cycle

POSITION_REFRESH = 15.0
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _ident(ac):
    """Best available identifier: callsign, then registration, then the
    ICAO24 hex address -- some aircraft (mostly small GA) don't broadcast
    a callsign at all, and showing nothing would be a worse gap than
    falling back to whatever they DO broadcast."""
    for key in ("flight", "r", "hex"):
        v = (ac.get(key) or "").strip()
        if v:
            return v.upper()
    return "UNKNOWN"


def _fetch_positions(lat, lon):
    url = POSITION_URL.format(lat=lat, lon=lon, radius_nm=RADIUS_NM)
    data = _get_json(url)
    out = []
    for ac in data.get("ac") or []:
        # .get(key, default) only supplies the default when the key is
        # MISSING -- a real payload with "gs": null still hands back None,
        # and None < 30 raises. Coerce explicitly instead.
        gs = ac.get("gs")
        gs = gs if isinstance(gs, (int, float)) else 0
        if ac.get("alt_baro") in (None, "ground") and gs < 30:
            continue          # parked/taxiing clutter, not "in the sky" traffic
        out.append({
            "ident": _ident(ac),
            "callsign": (ac.get("flight") or "").strip(),
            "type": (ac.get("t") or "").strip(),
            "alt_ft": ac.get("alt_baro") if isinstance(ac.get("alt_baro"), (int, float)) else None,
            "gs_kt": ac.get("gs"),
            "track_deg": ac.get("track"),
            "dist_nm": ac.get("dst"),
            "dir_deg": ac.get("dir"),
        })
    out.sort(key=lambda a: a["dist_nm"] if a["dist_nm"] is not None else 1e9)
    return out[:MAX_TRACKED]


def _fetch_route(callsign):
    url = ROUTE_URL.format(callsign=callsign)
    data = _get_json(url)
    resp = data.get("response")
    if not isinstance(resp, dict):
        return None                     # adsbdb returns a plain string for "not found"
    route = resp.get("flightroute")
    if not route:
        return None
    origin = route.get("origin") or {}
    dest = route.get("destination") or {}
    airline = route.get("airline") or {}
    return {
        "origin": origin.get("iata_code") or origin.get("icao_code"),
        "dest": dest.get("iata_code") or dest.get("icao_code"),
        "airline": airline.get("name"),
    }


class FlightFeed:
    """Background poller with a last-good cache -- same contract as
    MarketFeed/SatelliteFeed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._aircraft = []
        self._updated = 0.0
        self._last_try = 0.0
        self._last_read = 0.0
        self._last_config_check = 0.0
        self._thread = None
        self._err = None
        self._route_cache = {}          # callsign -> route dict or None (looked up, no route)
        self._home = satellite.FEED.get_location()   # (lat, lon, label)

    def get(self):
        """Returns {aircraft: [...], age, home_label, configured, err}.
        Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            aircraft = [dict(a) for a in self._aircraft]
            updated, err = self._updated, self._err
            home_label = self._home[2]
        self._ensure_thread()
        age = (now - updated) if updated else None
        return {
            "aircraft": aircraft, "age": age,
            "home_label": home_label,
            "configured": satellite.FEED.configured,
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
            self._refresh_once()
            time.sleep(2.0)

    def _maybe_reload_location(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        home = satellite.FEED.get_location()
        with self._lock:
            self._home = home

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < POSITION_REFRESH:
                return
            self._last_try = now
            lat, lon, _ = self._home
            configured = satellite.FEED.configured

        if not configured:
            with self._lock:
                self._aircraft = []
                self._updated = time.time()
                self._err = None
            return

        try:
            aircraft = _fetch_positions(lat, lon)
        except Exception as e:                        # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"
            return

        # Enrich with cached/looked-up routes, capped per cycle.
        lookups_left = MAX_LOOKUPS_PER_REFRESH
        for ac in aircraft:
            cs = ac["callsign"]
            if not cs:
                ac["route"] = None
                continue
            if cs in self._route_cache:
                ac["route"] = self._route_cache[cs]
                continue
            if lookups_left <= 0:
                ac["route"] = None
                continue
            lookups_left -= 1
            try:
                route = _fetch_route(cs)
            except Exception:                          # noqa: BLE001
                route = None
            self._route_cache[cs] = route
            ac["route"] = route

        # Cap cache growth over a long-running process -- a simple full
        # clear past a generous ceiling is fine here: it just means a
        # handful of callsigns re-enrich once, not a meaningful cost.
        if len(self._route_cache) > 2000:
            self._route_cache.clear()

        with self._lock:
            self._aircraft = aircraft
            self._updated = time.time()
            self._err = None


FEED = FlightFeed()
