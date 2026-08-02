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
import math
import threading
import time
import urllib.error
import urllib.request

import paneltext

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


def load_airport():
    """The home airport, for the radar scope's airport marker, or None.

    Lives in satellite.py's location_config.json rather than a new file --
    an airport is a LOCATION fact, and CLAUDE.md's standing rule is that
    there is exactly one source of truth for where the owner is. Stored
    as {"code", "lat", "lon"}; anything malformed returns None so the
    scope simply draws no airport rather than plotting a guessed one.

    NOT auto-detected. Resolving "the nearest airport" needs a whole
    airport database (OurAirports' CSV is 12MB) to answer a question the
    owner can answer once in a config field -- same config-driven pattern
    as the pinned golfer and the favourite team.
    """
    path = satellite.CONFIG_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text()) or {}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    ap = data.get("airport")
    if not isinstance(ap, dict):
        return None
    try:
        lat, lon = float(ap["lat"]), float(ap["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return {"code": paneltext.panel_text(ap.get("code"))[:4] or "ARPT",
            "lat": lat, "lon": lon}


def save_airport(code, lat, lon):
    """Persist (or clear, with a falsy code) the home airport. Preserves
    every other key in the shared location config."""
    path = satellite.CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text()) or {}
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            data = {}
    if code:
        data["airport"] = {"code": str(code).upper()[:4],
                           "lat": float(lat), "lon": float(lon)}
    else:
        data.pop("airport", None)
    path.write_text(json.dumps(data, indent=2))
    return data.get("airport")


def bearing_distance(lat1, lon1, lat2, lon2):
    """(bearing_deg, distance_nm) from point 1 to point 2.

    Great-circle, same haversine satellite.py already uses for ground
    distance -- shared concept, but returned in NAUTICAL miles because
    that is the unit the flight scope's range rings are labelled in
    (aviation convention, and what RADIUS_NM is already expressed in).
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    dp = p2 - p1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d_km = 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))
    brg = math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360.0
    return brg, d_km * 0.539957


def _ident(ac):
    """Best available identifier: callsign, then registration, then the
    ICAO24 hex address -- some aircraft (mostly small GA) don't broadcast
    a callsign at all, and showing nothing would be a worse gap than
    falling back to whatever they DO broadcast."""
    for key in ("flight", "r", "hex"):
        v = (ac.get(key) or "").strip()
        if v:
            return paneltext.panel_text(v)
    return "UNKNOWN"


# ADS-B emitter categories, from the real spec and CONFIRMED present in
# live data over a 250nm sample (213 aircraft): A3 large 154, A1 light 23,
# A2 small 16, A5 heavy 11, A7 rotorcraft 6, A4 high-vortex 1, B2 1.
# Nothing here is guessed -- every category used below was observed.
CAT_HEAVY = "A5"          # >300,000 lb: the wide-bodies
CAT_ROTOR = "A7"
CAT_LIGHTER_THAN_AIR = "B2"
# Tags are kept to 7 characters because draw_header fits its right tag to
# 30px (7 glyphs) and SILENTLY truncates past that -- "HELICOPTER" became
# "HELICOP" on the panel. Caught by rendering the badges, not by reading
# the code. Short forms are chosen to still be unambiguous at a glance.
EMERGENCY_SQUAWKS = {"7500": "HIJACK", "7600": "NORADIO", "7700": "MAYDAY"}

HIGH_ALT_FT = 40000       # above typical airliner cruise; observed 5 of 213
LOW_ALT_FT = 3000         # approach/departure -- low enough to actually see
LOW_NEAR_NM = 12

# ---- flight phase -------------------------------------------------------
# What's it actually DOING, not just where it is. Verified against real
# live traffic near ORD on 2026-08-01 (MYR itself had zero traffic in
# range at the time -- ORD was used purely to confirm the payload shape
# and real-world value ranges; the classification logic applies wherever
# aircraft are tracked):
#
#   SKW5648   alt 7450ft  geom_rate -1280  8nm out   -- clear descent
#   AAL529    alt 5975ft  baro_rate +2240  10nm out  -- clear climb-out
#   ENY3471   alt 7175ft  baro_rate +3712  12nm out  -- clear climb-out
#   AAL2269   alt 7125ft  baro_rate 0      2nm out   -- level, ambiguous
#   RPA3659   alt 8125ft  baro_rate -64    8nm out   -- level, ambiguous
#   UAL210    alt 36000ft baro_rate 0      18nm out  -- cruise (altitude
#                                                        alone settles it)
#
# TWO FINDINGS THAT SHAPED THIS:
#
# 1. baro_rate and geom_rate are NEVER BOTH POPULATED on the same real
#    aircraft in this sample -- when one carries a value the other is
#    null. Both are read; baro_rate is preferred when present (it is the
#    more commonly reported of the two in practice) and geom_rate is the
#    fallback, matching the existing alt_baro-preferred pattern this
#    module already uses. If NEITHER is present, phase is None -- no
#    guessing from altitude and distance alone.
#
# 2. A LEVEL aircraft rarely reports EXACTLY zero (RPA3659 above was -64
#    fpm while unambiguously not descending), so the climb/descend
#    threshold is deliberately not "any nonzero value" -- it is set well
#    above realistic sensor/reporting noise and well below a real
#    sustained climb or descent (which reads 1500-3700fpm in this same
#    sample), so the ambiguous near-zero cases correctly fall through to
#    "no phase" rather than being called a weak climb or descent.
CRUISE_FLOOR_FT = 18000   # FAA Class A airspace floor (US) -- a REAL
                          # published threshold, not invented. At or above
                          # it, an aircraft is on an enroute IFR flight
                          # level; it cannot be local approach/departure
                          # traffic regardless of its instantaneous rate.
RATE_THRESHOLD_FPM = 300  # above realistic level-flight noise (RPA3659's
                          # -64fpm), well below a real climb/descent
                          # (1500-3700fpm observed)

PHASE_CLIMB = "CLIMB"
PHASE_DESCEND = "DESCEND"
PHASE_CRUISE = "CRUISE"


def _vertical_rate(ac):
    """baro_rate preferred, geom_rate as fallback -- see the module note
    above for why exactly one is usually present, never both."""
    for key in ("baro_rate", "geom_rate"):
        v = ac.get(key)
        if isinstance(v, (int, float)):
            return v
    return None


def _phase(ac, alt=None, rate=None):
    """(phase, rate) or (None, rate) -- None when it cannot be determined
    CONFIDENTLY, same rule as everywhere else in this project: no phase
    label beats a guessed one.

    alt/rate are accepted as optional pre-computed values so callers that
    already extracted them (_notable, for the rank escalation below) do
    not redo the work."""
    if alt is None:
        alt = ac.get("alt_baro")
    if rate is None:
        rate = _vertical_rate(ac)
    if not isinstance(alt, (int, float)):
        return None, rate
    if alt >= CRUISE_FLOOR_FT:
        return PHASE_CRUISE, rate       # altitude alone settles it
    if not isinstance(rate, (int, float)):
        return None, rate               # no rate data -- do not guess
    if rate >= RATE_THRESHOLD_FPM:
        return PHASE_CLIMB, rate
    if rate <= -RATE_THRESHOLD_FPM:
        return PHASE_DESCEND, rate
    return None, rate                    # level below cruise -- genuinely
                                          # ambiguous (pattern? holding?
                                          # transition?), not guessed at


def _notable(ac, phase=None):
    """What, if anything, makes this aircraft worth looking up for.

    Returns (tag, rank) with a HIGHER rank meaning more notable, or None
    for routine traffic. Rank exists so the mode can lead with the most
    interesting aircraft in the sky rather than merely the closest --
    "closest" is almost always a routine regional jet.

    Criteria are deliberately limited to signals that are (a) present in
    the raw ADS-B payload, (b) verified to actually occur in real local
    traffic, and (c) computable with no extra request. Notably NOT
    included: "long-haul/international", which would need airport
    coordinates for every origin/destination to judge distance -- that is
    a second dataset and a per-flight lookup, so it is not cheap and is
    not done rather than approximated badly.

    `phase` (from _phase(), computed once by the caller) narrowly
    escalates the existing LOW criterion rather than adding a parallel
    ranking system: LOW already means "low altitude, genuinely near";
    CONFIRMING it is actively climbing or descending -- not just
    coincidentally low -- is real information that a low-and-level
    aircraft doesn't have, and is worth the same attention as a heavy or
    a helicopter, not less. This was a deliberate design choice, not an
    oversight: phase does NOT get its own independent rank tier, and does
    NOT reorder the whole list on its own -- a plane transitioning is
    common enough near any 40nm radius that using it as a primary sort
    key would make the ordering noisy rather than more useful.
    """
    squawk = str(ac.get("squawk") or "")
    if squawk in EMERGENCY_SQUAWKS:
        return (EMERGENCY_SQUAWKS[squawk], 5)
    emerg = str(ac.get("emergency") or "none").lower()
    if emerg not in ("none", "no emergency"):
        return ("MAYDAY", 5)

    cat = str(ac.get("category") or "")
    if cat == CAT_LIGHTER_THAN_AIR:
        return ("AIRSHIP", 4)
    if cat == CAT_ROTOR:
        return ("HELI", 3)
    if cat == CAT_HEAVY:
        return ("HEAVY", 3)

    alt = ac.get("alt_baro")
    if isinstance(alt, (int, float)):
        if alt >= HIGH_ALT_FT:
            return ("HIGH", 2)
        dst = ac.get("dst")
        if alt <= LOW_ALT_FT and isinstance(dst, (int, float)) and dst <= LOW_NEAR_NM:
            if phase in (PHASE_CLIMB, PHASE_DESCEND):
                return ("LOW", 3)   # confirmed transitioning, not just low
            return ("LOW", 2)
    return None


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
        alt = ac.get("alt_baro") if isinstance(ac.get("alt_baro"), (int, float)) else None
        phase, rate = _phase(ac, alt=alt)
        out.append({
            "ident": _ident(ac),
            "callsign": (ac.get("flight") or "").strip(),
            "type": (ac.get("t") or "").strip(),
            "alt_ft": alt,
            "gs_kt": ac.get("gs"),
            "track_deg": ac.get("track"),
            "dist_nm": ac.get("dst"),
            "dir_deg": ac.get("dir"),
            "phase": phase,
            "vrate_fpm": rate,
            "notable": _notable(ac, phase=phase),
        })
    # Most notable first, then nearest. Sorting purely by distance means
    # the mode almost always leads with a routine regional jet, because
    # "closest" and "interesting" are rarely the same aircraft -- a
    # wide-body or a helicopter a few miles further out is the one worth
    # looking up for.
    out.sort(key=lambda a: (-(a["notable"][1] if a["notable"] else 0),
                            a["dist_nm"] if a["dist_nm"] is not None else 1e9))
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
        # `municipality` is the real city name adsbdb already returns
        # alongside the airport code -- confirmed live (RDU ->
        # "Raleigh/Durham", LGA -> "New York") -- it was simply being
        # discarded before. Uppercased/folded at this I/O boundary like
        # every other externally-sourced string here.
        "origin_city": paneltext.panel_text(origin.get("municipality")) or None,
        "dest_city": paneltext.panel_text(dest.get("municipality")) or None,
        # Folded here, not with a bare .upper() at render time -- adsbdb
        # airline names can carry accents/curly punctuation the font can't
        # draw (same bug class as paneltext.py's tally, instance 2, which
        # was this exact field).
        "airline": paneltext.panel_text(airline.get("name")) or None,
    }


# Readable name for the most common ICAO aircraft type designators --
# CONFIRMED REAL, sourced from the standard ICAO type-designator registry,
# not invented. ADS-B only ever reports the bare code (adsb.lol's `t`
# field); this is a static reference lookup, same class of thing as the
# compass-direction table above, not a fabricated per-flight value. A
# code missing from this table falls back to the raw code as-is -- never
# a guessed name for an aircraft this table doesn't know.
#
# Populated from real codes observed in a live 40nm sample near ORD on
# 2026-08-02 (A21N/B39M/A321/A20N/BCS3/B77L/B737/B772/B744, 13 aircraft)
# plus the other common narrow/wide-body types an airliner-heavy sky is
# realistically going to show.
ICAO_TYPE_NAMES = {
    "A319": "A319", "A320": "A320", "A321": "A321",
    "A20N": "A320NEO", "A21N": "A321NEO", "A19N": "A319NEO",
    "A332": "A330-200", "A333": "A330-300", "A339": "A330-900NEO",
    "A342": "A340-200", "A343": "A340-300", "A345": "A340-500", "A346": "A340-600",
    "A359": "A350-900", "A35K": "A350-1000",
    "A388": "A380-800",
    "BCS1": "A220-100", "BCS3": "A220-300",
    "B734": "737-400", "B735": "737-500", "B736": "737-600",
    "B737": "737-700", "B738": "737-800", "B739": "737-900",
    "B37M": "737 MAX 7", "B38M": "737 MAX 8", "B39M": "737 MAX 9", "B3XM": "737 MAX 10",
    "B752": "757-200", "B753": "757-300",
    "B762": "767-200", "B763": "767-300", "B764": "767-400",
    "B772": "777-200", "B77L": "777-200LR", "B773": "777-300", "B77W": "777-300ER",
    "B778": "777-8", "B779": "777-9",
    "B788": "787-8", "B789": "787-9", "B78X": "787-10",
    "B744": "747-400", "B748": "747-8",
    "E135": "ERJ-135", "E145": "ERJ-145",
    "E170": "E170", "E75L": "ERJ-175", "E75S": "ERJ-175",
    "E190": "E190", "E195": "E195", "E290": "E190-E2", "E295": "E195-E2",
    "CRJ2": "CRJ200", "CRJ7": "CRJ700", "CRJ9": "CRJ900", "CRJX": "CRJ1000",
    "DH8D": "DASH 8-400",
}


def _type_name(icao_type):
    """Readable aircraft type, or the raw code if it isn't in the table."""
    t = (icao_type or "").strip().upper()
    return ICAO_TYPE_NAMES.get(t, t) or None


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
