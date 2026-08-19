"""
flights.py -- free ADS-B flight data for the flights mode.

Same shape as market.py and satellite.py, deliberately: all I/O lives here
so the mode that draws it stays pure, background thread with a last-good
cache, never blocks the render loop, never invents a number.

Two keyless sources, plus a position-aggregator fallback chain:
  * api.adsb.lol / api.airplanes.live / opendata.adsb.fi
    -- live aircraft positions within a radius of a point. All three are
    free, keyless, readsb/tar1090-family aggregators with the same per-
    aircraft field names. adsb.lol is tried first (historical primary);
    if it errors OR returns an empty 200 while another replica still
    has traffic, the next source is used. Confirmed 2026-08-11: adsb.lol
    `/v2/point` around the configured home returned total=0 out to 250nm
    (HTTP 200, "No error") while airplanes.live returned 17 and adsb.fi
    19 at the same coordinates -- an empty replica, not a quiet sky.
    Treating that 200/[] as gospel blacked out the radar.
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
from pathlib import Path

import paneltext

import events_log
import hangar
import satellite

# Each entry is (name, url_template, list_key). url_template is formatted
# with lat/lon/radius_nm (position) or callsign (follow). list_key is
# where that replica puts the aircraft array -- adsb.lol and
# airplanes.live use "ac"; adsb.fi's opendata path uses "aircraft".
# Order is preference: first non-empty success wins.
POSITION_SOURCES = (
    ("adsb.lol",
     "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}", "ac"),
    ("airplanes.live",
     "https://api.airplanes.live/v2/point/{lat}/{lon}/{radius_nm}", "ac"),
    ("adsb.fi",
     "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}",
     "aircraft"),
)
# Kept as the first source's URL so any leftover formatter / comment that
# still says POSITION_URL keeps pointing at a real endpoint, not a lie.
POSITION_URL = POSITION_SOURCES[0][1]
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
# GLOBAL per-callsign lookup (2026-08-09) -- "follow a specific flight"
# mode. Same replica family and the same fallback discipline as
# POSITION_SOURCES: a 200/{ac:[]} from one aggregator is not treated as
# terminal "not airborne" until the others agree (or every replica that
# answered said empty and none failed). Confirmed live that a callsign
# that isn't currently airborne (or simply wrong) returns "ac": [] -- an
# honest, non-error "not currently airborne" result, rendered as such,
# never as a guessed/stale position.
FOLLOW_SOURCES = (
    ("adsb.lol",
     "https://api.adsb.lol/v2/callsign/{callsign}", "ac"),
    ("airplanes.live",
     "https://api.airplanes.live/v2/callsign/{callsign}", "ac"),
    ("adsb.fi",
     "https://opendata.adsb.fi/api/v2/callsign/{callsign}", "aircraft"),
)
FOLLOW_URL = FOLLOW_SOURCES[0][1]

# GLOBAL per-REGISTRATION lookup, the fallback path _fetch_follow() uses
# when a callsign lookup comes back empty (2026-08-17). Famous aircraft
# in the curated FAMOUS_AIRCRAFT picker below are identified by tail
# number (N1A, N941NA, ...), not a flight-number callsign -- a Goodyear
# blimp or NASA's Super Guppy has no "airline ICAO + number" callsign to
# match, so the callsign endpoint never finds them. Same replica family
# and same {"ac":[...]} envelope as the callsign path.
#
# Deliberately NOT airplanes.live here: its /v2/reg endpoint returns a
# real 403 from this project's User-Agent (confirmed live 2026-08-17),
# and _fetch_ac_list() treats "a source errored AND none returned
# traffic" as a hard raise -- so a 403 replica would turn an honest
# "not airborne" into a NO SIGNAL error. adsb.lol (/v2/reg) and adsb.fi
# (/api/v2/registration) both return a clean empty envelope for a
# grounded reg, confirmed live, so only those two are used.
FOLLOW_REG_SOURCES = (
    ("adsb.lol",
     "https://api.adsb.lol/v2/reg/{reg}", "ac"),
    ("adsb.fi",
     "https://opendata.adsb.fi/api/v2/registration/{reg}", "aircraft"),
)

RADIUS_NM = 40                # "in the local sky" -- roughly a 15 min drive's worth of horizon
WELCOME_BACK_MIN_S = 86400.0  # a real absence, not a lap of the pattern -- see the WELCOME BACK note below


def _fmt_gap_days(gap_s):
    """Real elapsed time since a tail's last sighting, in whichever
    whole unit reads best -- days when it's been a real day-plus (the
    only range WELCOME_BACK_MIN_S ever calls this with), hours below
    that. Never a guessed round number."""
    days = gap_s / 86400.0
    if days >= 1:
        return f"{days:.0f}D AGO" if days >= 1.5 else "1D AGO"
    return f"{gap_s / 3600.0:.0f}H AGO"
MAX_TRACKED = 8                 # nearest N, so the mode has a bounded, meaningful list
MAX_LOOKUPS_PER_REFRESH = 8     # radar 8 + board candidates; cache means this is burst-only

POSITION_REFRESH = 15.0
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _aircraft_list(data, list_key):
    """Pull the aircraft array off a replica payload.

    Replicas in this family disagree on the key (`ac` vs `aircraft`) but
    agree on the per-aircraft field names. Falls back across both keys
    so a replica that flips its envelope still parses rather than
    looking like a quiet sky."""
    if not isinstance(data, dict):
        return None
    ac = data.get(list_key)
    if not isinstance(ac, list):
        ac = data.get("ac")
    if not isinstance(ac, list):
        ac = data.get("aircraft")
    return ac if isinstance(ac, list) else None


def _fetch_ac_list(sources, url_kwargs):
    """Try ADS-B replicas in order. Returns a real aircraft list.

    A successful empty list is NOT terminal -- the next source is tried.
    Confirmed 2026-08-11: adsb.lol `/v2/point` around home returned
    HTTP 200 / total=0 out to 250nm while airplanes.live (17) and
    adsb.fi (19) had real traffic at the same coordinates. Trusting
    that empty 200 blacked out the radar.

    Rules:
      * first non-empty success wins
      * every source succeeded with empty -> [] (honest quiet sky)
      * any source failed AND none returned traffic -> raise, so the
        feed keeps last-good and marks err rather than wiping a real
        sky because one replica hole + one 429 looked like "clear"
    """
    last_err = None
    saw_empty = False
    for _name, url_tmpl, list_key in sources:
        try:
            data = _get_json(url_tmpl.format(**url_kwargs))
        except Exception as e:                         # noqa: BLE001
            last_err = e
            continue
        ac = _aircraft_list(data, list_key)
        if ac is None:
            last_err = last_err or ValueError("unrecognized ADS-B envelope")
            continue
        if ac:
            return ac
        saw_empty = True
    if last_err is not None:
        raise last_err
    if saw_empty:
        return []
    return []


_airport_cache = None
_airport_mtime = None


def load_airport():
    """The home airport, for the radar scope's airport marker, or None.

    Lives in satellite.py's location_config.json rather than a new file --
    an airport is a LOCATION fact, and CLAUDE.md's standing rule is that
    there is exactly one source of truth for where the owner is. Stored
    as {"code", "lat", "lon"}; anything malformed returns None so the
    scope simply draws no airport rather than plotting a guessed one.

    Cached on mtime so DepartureBoardEngine.tick (and ambient warming
    it at 20 Hz) does not open the file every frame.

    NOT auto-detected. Resolving "the nearest airport" needs a whole
    airport database (OurAirports' CSV is 12MB) to answer a question the
    owner can answer once in a config field -- same config-driven pattern
    as the pinned golfer and the favourite team.
    """
    global _airport_cache, _airport_mtime
    path = satellite.CONFIG_PATH
    try:
        mt = path.stat().st_mtime if path.exists() else None
    except OSError:
        return dict(_airport_cache) if _airport_cache else None
    if mt == _airport_mtime:
        return dict(_airport_cache) if _airport_cache else None
    _airport_mtime = mt
    if mt is None:
        _airport_cache = None
        return None
    try:
        data = json.loads(path.read_text()) or {}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return dict(_airport_cache) if _airport_cache else None
    ap = data.get("airport")
    parsed = None
    if isinstance(ap, dict):
        try:
            lat, lon = float(ap["lat"]), float(ap["lon"])
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                parsed = {"code": paneltext.panel_text(ap.get("code"))[:4] or "ARPT",
                          "lat": lat, "lon": lon}
        except (KeyError, TypeError, ValueError):
            parsed = None
    _airport_cache = parsed
    return dict(parsed) if parsed else None


# North-American ICAO prefixes. US = K+IATA (MYR/KMYR), Canada = C,
# Mexico/central = M. This is the published ICAO-in-North-America
# pattern, not a worldwide converter -- EGLL will not invent LHR, and
# we do not guess that mapping.
_NA_ICAO_PREFIXES = ("K", "C", "M")


def airport_codes(code):
    """Set of equivalent airport identity strings for matching.

    A 3-letter code matches itself and K/C/M+code. A 4-letter
    K/C/M+XXX also matches XXX. Used so a home configured as MYR
    still classifies a route that only resolved as KMYR (and the
    reverse). Empty/None -> empty set, never a guessed airport.
    """
    if not code:
        return set()
    c = str(code).strip().upper()
    if not c:
        return set()
    out = {c}
    if len(c) == 3 and c.isalpha():
        for p in _NA_ICAO_PREFIXES:
            out.add(p + c)
    elif len(c) == 4 and c[0] in _NA_ICAO_PREFIXES and c[1:].isalpha():
        out.add(c[1:])
    return out


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


# ---- window filter (2026-08-07) ------------------------------------------
# Prioritize/flag aircraft currently visible out ONE specific real window,
# measured with a phone compass held at that window: centre bearing 296deg,
# 80deg total field of view (roughly 256deg -> 336deg). Stored in the SAME
# shared location_config.json as `airport` -- a window's bearing is a
# LOCATION fact tied to where the panel/owner actually is, exactly the
# reasoning that already put `airport` there instead of a new file.
#
# in_window()/load_window()/save_window() MOVED to satellite.py on
# 2026-08-08: satellite.py gained its own window feature (the sky dome /
# pass list also need to know what's in the same window), and satellite.py
# already owns location_config.json, so it -- not flights.py -- is the
# correct home for the SHARED implementation. satellite.py cannot import
# flights.py (this module already imports satellite.py; the reverse would
# be circular), so flights.py keeps calling the shared functions from here,
# same one-line-per-call-site shape as every other satellite.CONFIG_PATH
# read in this file (load_airport/save_airport above).
load_window = satellite.load_window
save_window = satellite.save_window
in_window = satellite.in_window


# ---- favorite aircraft / watched tail numbers (2026-08-09) ---------------
# A real, first-class favorites layer for flights -- same shape/reasoning
# as sports.py's favorite_teams (2026-08-08): a plain list of real
# registration strings the owner cares about, kept in its OWN config file
# (flights_config.json, this module's first -- everything else it owns
# lives in satellite.py's shared location_config.json, but a favorites
# list is not a location fact, so it does not belong there). Applied at
# the SAME enrichment site every other per-aircraft fact already comes
# from (_fetch_positions() below) as a real, computed `is_favorite` bool
# -- reused everywhere an aircraft dict already flows (scope, DETAIL,
# Hangar list), not a new parallel data path.
FAVORITES_CONFIG_PATH = Path(__file__).parent / "flights_config.json"

# Same ordering role as WINDOW_BOOST just above -- additive to the
# existing notable/window ranking, never replacing it, so "which aircraft
# leads the list" stays one real formula instead of a favorite silently
# overriding an otherwise-more-notable aircraft.
FAVORITE_BOOST = 1.5


def load_favorite_aircraft():
    """Real registration strings only, uppercased -- an empty list is the
    honest default (device just set up, or the owner hasn't picked any
    tail numbers to watch yet), never a guessed starter list."""
    if not FAVORITES_CONFIG_PATH.exists():
        return []
    try:
        data = json.loads(FAVORITES_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return []
    raw = data.get("favorite_aircraft")
    if not isinstance(raw, list):
        return []
    return sorted({str(r).strip().upper() for r in raw if str(r or "").strip()})


def save_favorite_aircraft(regs):
    """Read-modify-write, not a fresh dict -- same preemptive discipline
    every save_* in this project follows since the 2026-08-09 gap audit
    (this file is new, so there's no sibling key to preserve YET, but the
    pattern is the same regardless of whether today's data would notice
    the difference)."""
    data = {}
    if FAVORITES_CONFIG_PATH.exists():
        try:
            data = json.loads(FAVORITES_CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    cleaned = sorted({str(r).strip().upper() for r in (regs or []) if str(r or "").strip()})
    data["favorite_aircraft"] = cleaned
    FAVORITES_CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return cleaned


# Soft-priority boost applied to a window aircraft's sort rank (see the
# sort key in _fetch_positions below). `_notable()` ranks are small
# integers spaced AT LEAST 1 apart (0 for routine, 2/3/4/5 for the real
# notable criteria) -- 0.5 is deliberately HALF that minimum spacing, so
# the boost can NEVER push a window aircraft across a notable-rank tier
# boundary; it can only break a tie WITHIN a tier (nudging a window
# aircraft ahead of an equally-notable non-window one, or ahead of
# routine non-window traffic when both are otherwise rank 0).
#
# Verified against a real live 8-aircraft `flights.FEED.get()` snapshot
# on 2026-08-07 (window centre=296, fov=80, so window = 256deg-336deg):
#   VIR36VL  dir 283.6  notable HEAVY(3)  IN WINDOW  -> adjusted 3.5
#   SWA1065  dir 245.1  notable LOW(3)    not window -> adjusted 3.0
#   TIV685   dir 269.8  notable HIGH(2)   IN WINDOW  -> adjusted 2.5
#   N610CT   dir 275.0  notable None(0)   IN WINDOW  -> adjusted 0.5
#   JBU483   dir 324.6  notable None(0)   IN WINDOW  -> adjusted 0.5
#   N157JR   dir 223.7  notable None(0)   not window -> adjusted 0.0
#   FFT1785  dir 352.4  notable None(0)   not window -> adjusted 0.0
#   N773TA   dir 349.3  notable None(0)   not window -> adjusted 0.0
# Resulting order: VIR36VL, SWA1065, TIV685, N610CT, JBU483, N157JR,
# FFT1785, N773TA. This confirms the intended interaction: a window+
# routine aircraft (N610CT/JBU483) ranks above ALL non-window routine
# traffic regardless of distance, but still ranks below SWA1065 -- a
# real notable(rank 3) aircraft that isn't even in the window. VIR36VL
# (window AND notable, same rank tier as SWA1065) wins the tie against
# SWA1065, the intended "boost breaks ties within a tier" behaviour --
# not a window aircraft leapfrogging a strictly higher notable tier
# (which never happens: the emergency-squawk tier, rank 5, stays 1.5
# clear of the highest possible boosted rank, 3.5).
WINDOW_BOOST = 0.5


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


def destination_point(lat, lon, bearing_deg, dist_nm):
    """The inverse of bearing_distance(): given a start point and a real
    bearing+distance, the real lat/lon that sits there. Standard
    spherical "destination point" formula (same great-circle model
    bearing_distance() itself uses, just run the other direction).

    Built 2026-08-11 for the flight-path map's live flown-trail (real
    owner ask: a diversion needs to be visually SEEN, not just
    theoretically knowable). FlightEngine's own scope trail
    (`self._trail`) already accumulates each selected aircraft's real
    recent positions, but in a local nm-plane (x_nm/y_nm relative to
    home) built for the ground radar's projection -- this converts one
    of those real local points back to a real lat/lon so the SAME real
    trail can also be drawn on the world map, in the coordinate system
    that view actually uses. Not a new data source, not a new sample --
    the same real polled positions, read through a different lens."""
    R_nm = 6371.0088 * 0.539957
    lat1, brg = math.radians(lat), math.radians(bearing_deg)
    ang = dist_nm / R_nm
    lat2 = math.asin(math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg))
    lon2 = math.radians(lon) + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


# ---- coastline, for the ground radar scope ------------------------------
# REAL geography, not a decorative squiggle: extracted from Natural Earth's
# public-domain 10m coastline vector data
# (github.com/nvkelso/natural-earth-vector, ne_10m_coastline.geojson) on
# 2026-08-02, clipped to the contiguous run of points within 55nm of the
# configured home (33.735277, -78.9089469) -- a margin beyond the scope's
# 40nm radius so the line clips naturally at the rim instead of stopping
# abruptly right at the edge. Verified against the raw source before
# embedding: nearest real coastline point to home is 2.77nm at bearing
# 131.7deg (SE), consistent with the Grand Strand shoreline curving away
# to the southeast from a few miles inland -- not assumed, checked.
#
# STATIC AND LOCATION-SPECIFIC ON PURPOSE. This is reference geography for
# THIS deployment's configured home, the same category of fact as the MYR
# airport coordinates (also a one-time real lookup, not a live feed) --
# coastlines do not move, so there is no feed to build, and a live fetch
# (Overpass API's "natural=coastline" query) was tried first and reliably
# times out server-side on real coastline ways, a known limitation of that
# API for this query shape, not a transient failure. If the home location
# ever moves somewhere the Atlantic Ocean is not nearby, this data becomes
# wrong and needs re-extracting the same way -- same manual-refresh
# expectation as re-picking the home airport after a move.
COASTLINE = (
    (33.0138, -79.5848), (33.0193, -79.5701), (33.0363, -79.5387),
    (33.0412, -79.5200), (33.0292, -79.5118), (33.0217, -79.5090),
    (33.0019, -79.4957), (32.9914, -79.4913), (33.0001, -79.4770),
    (33.0067, -79.4576), (33.0110, -79.4354), (33.0124, -79.4125),
    (33.0173, -79.3870), (33.0297, -79.3798), (33.0463, -79.3787),
    (33.0640, -79.3715), (33.0788, -79.3518), (33.0954, -79.3077),
    (33.1087, -79.2926), (33.1300, -79.2899), (33.1375, -79.3076),
    (33.1425, -79.3298), (33.1558, -79.3404), (33.1539, -79.3199),
    (33.1491, -79.2973), (33.1394, -79.2792), (33.1223, -79.2722),
    (33.1241, -79.2660), (33.1267, -79.2609), (33.1306, -79.2563),
    (33.1360, -79.2517), (33.1452, -79.2628), (33.1566, -79.2700),
    (33.1661, -79.2689), (33.1701, -79.2551), (33.1668, -79.2472),
    (33.1497, -79.2393), (33.1422, -79.2306), (33.1579, -79.2229),
    (33.1678, -79.2102), (33.1752, -79.1959), (33.1838, -79.1835),
    (33.1879, -79.1972), (33.1929, -79.2030), (33.2111, -79.2101),
    (33.2227, -79.2124), (33.2340, -79.2113), (33.2425, -79.2140),
    (33.2501, -79.2483), (33.2615, -79.2720), (33.2777, -79.2914),
    (33.2968, -79.2995), (33.3178, -79.2964), (33.3393, -79.2882),
    (33.3591, -79.2771), (33.4121, -79.2323), (33.4284, -79.2120),
    (33.4370, -79.1896), (33.4308, -79.1896), (33.4221, -79.2010),
    (33.3865, -79.2372), (33.3403, -79.2632), (33.3173, -79.2720),
    (33.3067, -79.2685), (33.3063, -79.2269), (33.3007, -79.2137),
    (33.2868, -79.2033), (33.2685, -79.1986), (33.2323, -79.1974),
    (33.2179, -79.1896), (33.2179, -79.1835), (33.2413, -79.1826),
    (33.3578, -79.1561), (33.3688, -79.1487), (33.3756, -79.1561),
    (33.4174, -79.1331), (33.5336, -79.0315), (33.6421, -78.9380),
    (33.7045, -78.8675), (33.7786, -78.7633), (33.8319, -78.6570),
    (33.8458, -78.6151), (33.8480, -78.5733), (33.8572, -78.5772),
    (33.8637, -78.5829), (33.8676, -78.5904), (33.8690, -78.6000),
    (33.8759, -78.6000), (33.8895, -78.5454), (33.8833, -78.5454),
    (33.8798, -78.5501), (33.8690, -78.5590), (33.8963, -78.4709),
    (33.9106, -78.4083), (33.9226, -78.3849), (33.9509, -78.3747),
    (33.9509, -78.3673), (33.9324, -78.3617), (33.9242, -78.3624),
    (33.9168, -78.3673), (33.9101, -78.3335), (33.9251, -78.2525),
    (33.9206, -78.2269), (33.9155, -78.2140), (33.9168, -78.1587),
    (33.9152, -78.1396), (33.8939, -78.0493), (33.8923, -78.0323),
    (33.8963, -78.0178), (33.9047, -78.0089), (33.9526, -77.9722),
    (33.9895, -77.9532), (34.0027, -77.9494), (34.0372, -77.9505),
    (34.0537, -77.9486), (34.0676, -77.9420), (34.0917, -77.9523),
    (34.1632, -77.9629), (34.1912, -77.9625), (34.1912, -77.9563),
    (34.1476, -77.9407), (34.1086, -77.9216), (34.0798, -77.9296),
    (34.0010, -77.9213), (33.9714, -77.9359), (33.9652, -77.9359),
    (33.9577, -77.9323), (33.9489, -77.9338), (33.9305, -77.9420),
    (33.9582, -77.9111),
)


# ---- world coastline, for the global flight-path map --------------------
# Same category of fact as COASTLINE above and extracted the same way, but
# WHOLE-WORLD rather than clipped to this deployment's home: the flight-path
# map has to place a real origin and a real destination anywhere on Earth,
# so a 55nm local shoreline is useless to it.
#
# REAL geography, extracted 2026-08-09 from Natural Earth's public-domain
# 110m coastline vector data,
# https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_coastline.geojson
# (the 110m "small scale" cut, not the 10m one COASTLINE came from -- at
# whole-globe zoom on a 64px panel, 10m detail is thousands of points that
# all land on the same handful of pixels). Raw source: 134 real LineString
# features, 5128 total points, ~140KB of GeoJSON.
#
# DECIMATION ACTUALLY APPLIED, in this order:
#   1. dropped any feature whose bounding box spans under 6 degrees in BOTH
#      lat and lon -- at this projection one pixel is 5.6 deg of longitude,
#      so those features cannot draw as more than a dot or two anyway;
#   2. Ramer-Douglas-Peucker simplification at a 2.0-degree tolerance on
#      each surviving line (~0.36px of longitude, ~0.7px of latitude here,
#      i.e. deliberately below one pixel of visible error).
# Result: 45 segments, 415 points, from 134/5128 -- a ~92% point reduction,
# and the count that actually gets walked once per frame while the map view
# is open. Coordinates are (lat, lon) to match COASTLINE's own ordering,
# rounded to 2dp (~1.1km, far finer than one pixel at this scale).
#
# STATIC ON PURPOSE, exactly like COASTLINE: coastlines do not move, so a
# live fetch would be 140KB of network per boot to redraw the same line.
# One-time extraction embedded in source is the established precedent here
# -- see COASTLINE's own note for the full reasoning.
WORLD_COASTLINE = (
    (
      (-2.6, 141), (-10.58, 150.69), (-8.41, 137.61), (-5.39, 137.93),
      (-0.94, 130.52), (-2.6, 141),
    ),
    (
      (4.53, 114.2), (5.41, 119.18), (-4.01, 116.15), (-0.46, 109.09),
      (4.53, 114.2),
    ),
    (
      (74.39, -88.15), (76.75, -97.12), (75.71, -81.13), (74.39, -88.15),
    ),
    (
      (23.19, -82.27), (20.28, -74.18), (21.9, -84.97), (23.19, -82.27),
    ),
    (
      (51.32, -55.6), (46.66, -53.07), (47.6, -59.27), (51.32, -55.6),
    ),
    (
      (65.11, -83.88), (63.73, -80.1), (63.54, -87.22), (65.11, -83.88),
    ),
    (
      (72.35, -78.77), (66.86, -61.85), (66.26, -68.02), (61.93, -66.17),
      (64.23, -77.71), (68.07, -73.31), (72.24, -90.21), (72.35, -78.77),
    ),
    (
      (75.85, -107.82), (75.22, -117.71), (75.85, -107.82),
    ),
    (
      (76.12, -122.85), (77.65, -116.2), (76.12, -122.85),
    ),
    (
      (74.45, -121.54), (73.48, -115.51), (70.9, -123.09),
      (71.87, -125.93), (74.29, -124.92), (74.45, -121.54),
    ),
    (
      (-12.47, 49.54), (-25.6, 45.41), (-17.41, 43.96), (-12.47, 49.54),
    ),
    (
      (-78.05, -48.66), (-80.03, -43.33), (-80.63, -54.16),
      (-78.05, -48.66),
    ),
    (
      (-80.26, -66.29), (-80.04, -59.57), (-80.26, -66.29),
    ),
    (
      (-71.27, -73.92), (-68.88, -70.25), (-71.41, -68.33),
      (-71.27, -73.92),
    ),
    (
      (-71.89, -102.33), (-72.52, -96.2), (-71.89, -102.33),
    ),
    (
      (-40.07, 176.89), (-41.28, 174.65), (-34.53, 172.64),
      (-37.7, 178.52), (-40.07, 176.89),
    ),
    (
      (-43.56, 169.67), (-41.35, 174.25), (-46.64, 169.33),
      (-45.85, 166.51), (-43.56, 169.67),
    ),
    (
      (-32.22, 126.15), (-34.2, 115.03), (-22.48, 113.74),
      (-11.13, 132.36), (-11.86, 136.49), (-15, 135.5), (-17.71, 140.22),
      (-10.67, 142.52), (-28.11, 153.57), (-37.43, 150), (-39.04, 146.32),
      (-38.02, 140.64), (-32.9, 137.81), (-34.89, 135.99),
      (-31.5, 131.33), (-32.22, 126.15),
    ),
    (
      (0.88, 122.93), (-0.52, 120.04), (-0.62, 123.34), (-5.34, 123.16),
      (-2.63, 120.97), (-5.67, 119.8), (0.15, 119.83), (0.88, 122.93),
    ),
    (
      (-6.42, 108.49), (-8.37, 115.71), (-6.42, 108.49),
    ),
    (
      (-1.08, 104.37), (-5.87, 104.71), (5.48, 95.29), (-1.08, 104.37),
    ),
    (
      (39.18, 141.88), (35.14, 140.25), (33.89, 130.99), (31.42, 130.2),
      (41.2, 140.31), (39.18, 141.88),
    ),
    (
      (58.55, -4.21), (51.29, 1.45), (50.16, -5.78), (51.43, -3.41),
      (56.79, -6.15), (58.55, -4.21),
    ),
    (
      (66.46, -14.51), (63.5, -18.66), (65.61, -24.33), (66.46, -14.51),
    ),
    (
      (53.7, 142.91), (48.98, 144.65), (45.97, 142.09), (53.7, 142.91),
    ),
    (
      (8.67, -77.35), (12.44, -71.75), (9.07, -71.7), (12.16, -69.94),
      (10.72, -61.88), (4.12, -51.3), (-0.08, -50.39), (-7.34, -34.73),
      (-21.94, -40.94), (-24.89, -47.65), (-34.4, -53.81),
      (-33.91, -58.43), (-38.18, -57.75), (-41.06, -65.12),
      (-52.35, -68.15), (-52.26, -74.95),
    ),
    (
      (7.22, -77.88), (-6.14, -81.25), (-19.76, -70.16), (-52.26, -74.95),
    ),
    (
      (-52.84, -74.66), (-54.7, -65.05), (-52.84, -74.66),
    ),
    (
      (80.59, 44.85), (80.7, 51.52), (80.59, 44.85),
    ),
    (
      (73.75, 53.51), (76.54, 68.85), (74.31, 58.48), (72.37, 55.42),
      (70.72, 57.54), (71.47, 51.6), (73.75, 53.51),
    ),
    (
      (80.06, 27.41), (80.32, 17.37), (80.06, 27.41),
    ),
    (
      (79.67, 15.14), (78.96, 21.54), (76.77, 15.91), (79.65, 10.44),
      (79.67, 15.14),
    ),
    (
      (7.22, -77.88), (18.29, -103.5), (31.57, -113.87), (23.36, -109.41),
      (24.74, -112.18), (40.31, -124.4), (49, -122.84), (58.12, -134.08),
      (59.16, -151.72), (61.28, -150.62), (54.4, -164.79),
      (58.92, -157.04), (61.5, -166.12), (64.79, -160.78),
      (65.67, -168.11), (66.12, -161.68), (68.36, -166.76),
      (71.36, -156.58), (67.38, -108.88), (67.29, -96.13),
      (71.92, -95.21), (67.2, -87.35), (69.16, -81.28), (60.9, -94.24),
      (57.09, -92.3), (55.15, -82.27), (51.21, -79.91), (56.53, -76.54),
      (62.32, -78.11), (62.44, -73.84), (58.21, -67.65), (60.34, -64.58),
      (52.15, -55.68), (46.82, -71.1), (49.23, -65.06), (46.24, -64.47),
      (45.92, -59.8), (45.14, -67.14), (39.15, -76.35), (35.55, -75.73),
      (31.44, -81.34), (25.21, -80.38), (30.09, -84.1), (27.83, -97.14),
      (18.83, -95.9), (21.54, -87.05), (15.89, -88.93), (15.27, -83.41),
      (9.57, -82.55),
    ),
    (
      (19.71, -71.71), (18.61, -68.32), (18.03, -73.92), (19.71, -71.71),
    ),
    (
      (19.1, -16.26), (26.25, -14.44), (35.76, -5.93), (37.35, 9.51),
      (33.79, 10.34), (30.27, 19.09), (32.84, 21.54), (30.97, 33.77),
      (36.65, 36.16), (36.66, 27.64), (39.46, 26.17), (41.54, 41.55),
      (45.24, 36.68), (47.26, 39.12), (44.36, 33.88), (46.58, 30.75),
      (41.05, 28.81), (40.26, 22.63), (36.41, 22.49), (45.74, 13.14),
      (40.17, 18.48), (37.99, 16.1), (44.37, 8.89), (43.08, 3.1),
      (36.67, -2.15), (36.87, -8.9), (43.03, -9.39), (44.02, -1.38),
      (48.68, -4.59), (53.53, 8.12), (57.11, 8.54), (54.01, 10.94),
      (54.43, 19.66), (59.19, 23.34), (60.03, 29.12), (60.72, 21.32),
      (66.01, 23.9), (62.75, 17.85), (56.1, 15.88), (58.59, 5.67),
      (61.97, 4.99), (69.82, 19.18), (71.19, 28.17), (67.93, 40.29),
      (66.63, 33.18), (63.85, 37.01), (66.07, 43.95), (68.57, 43.45),
      (68.09, 68.51), (71.03, 66.69), (73.04, 69.94), (72.22, 72.8),
      (66.17, 72.42), (72.83, 74.66), (71.75, 81.5), (73.65, 80.51),
      (77.7, 104.35), (75.85, 114.13), (74.18, 109.4), (73.57, 126.98),
      (70.79, 131.29), (72.85, 140.47), (68.96, 180),
    ),
    (
      (64.98, 180), (64.61, 177.41), (62.3, 179.23), (59.87, 163.54),
      (51.01, 156.79), (56.77, 155.91), (62.55, 164.47), (59.04, 142.2),
      (54.73, 135.13), (52.24, 141.38), (46.31, 138.22), (39.76, 127.53),
      (35.08, 129.09), (34.39, 126.49), (39.55, 125.32), (38.9, 121.05),
      (40.95, 121.64), (39.2, 118.04), (37.45, 122.36), (34.91, 119.15),
      (28.23, 121.68), (22.78, 115.89), (19.75, 105.88), (13.43, 109.34),
      (8.6, 105.16), (13.41, 100.1), (9.24, 99.22), (1.29, 104.23),
      (22.77, 91.42), (15.9, 80.32), (7.97, 77.54), (21.36, 72.63),
      (25.43, 66.37), (29.98, 47.97), (24.02, 51.79), (26.4, 56.36),
      (22.31, 59.81), (17.23, 55.27), (12.64, 43.48), (29.85, 32.42),
      (11.74, 42.72), (10.64, 51.05), (-4.68, 39.2), (-16.1, 40.09),
      (-33.94, 25.78), (-34.14, 18.38), (-27.09, 15.21), (3.73, 9.4),
      (6.27, 4.33), (4.83, -9), (12.17, -16.61), (19.1, -16.26),
    ),
    (
      (-84.71, -180), (-85.04, -143.11), (-83.69, -153.59),
      (-81.1, -156.84), (-80.34, -146.42), (-76.89, -158.37),
      (-75.2, -144.91), (-73.01, -68.94), (-66.88, -67.25),
      (-63.27, -57.81), (-67.95, -65.67), (-73.7, -60.83),
      (-76.71, -77.24), (-77.91, -73.66), (-79.18, -78.02),
      (-83.22, -58.22), (-80.34, -28.55), (-78.12, -35.33),
      (-70.93, -6.87), (-69.78, 38.65), (-65.82, 54.53), (-67.93, 68.89),
      (-72.26, 69.87), (-66.21, 87.99), (-65.31, 135.07), (-71.7, 171.21),
      (-76.24, 163.57), (-78.75, 167), (-80.95, 159.79), (-84.71, 180),
    ),
    (
      (68.96, -180), (65.98, -169.9), (64.98, -180),
    ),
    (
      (44.61, 46.68), (46.85, 53.04), (44.61, 50.31), (40.95, 54.74),
      (36.97, 53.83), (37.58, 49.2), (40.26, 50.39), (44.61, 46.68),
    ),
    (
      (73.08, -106.52), (68.75, -102.43), (71.56, -119.4),
      (73.08, -106.52),
    ),
    (
      (76.14, 138.83), (75.56, 145.09), (76.14, 138.83),
    ),
    (
      (81.02, 93.78), (79.78, 100.19), (81.02, 93.78),
    ),
    (
      (80.6, -96.02), (79.34, -85.81), (80.6, -96.02),
    ),
    (
      (81.89, -91.59), (82.63, -61.85), (76.18, -80.56), (76.47, -89.49),
      (77.54, -84.98), (80.25, -86.93), (80.46, -81.85), (81.89, -91.59),
    ),
    (
      (82.63, -46.76), (81.29, -12.21), (80.18, -20.05), (80.13, -17.73),
      (76.63, -21.68), (74.3, -19.37), (70.23, -26.36), (70.13, -22.35),
      (65.46, -39.81), (60.04, -44.79), (63.63, -51.63), (67.19, -53.97),
      (69.93, -50.87), (69.61, -54.68), (70.57, -51.39), (75.52, -58.59),
      (78.04, -73.3), (82.63, -46.76),
    ),
)


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
CAT_LIGHT = "A1"          # <15,500 lb: typical piston/small GA
CAT_SMALL = "A2"          # 15,500-75,000 lb
CAT_LARGE = "A3"          # 75,000-300,000 lb: the bulk of real airliner traffic
CAT_HIGH_VORTEX = "A4"    # large aircraft with a high-vortex wake (e.g. 757-class)
CAT_HEAVY = "A5"          # >300,000 lb: the wide-bodies
CAT_ROTOR = "A7"
CAT_LIGHTER_THAN_AIR = "B2"

# Real ICAO type designators for common business jets -- used to tell a
# bizjet apart from a piston/light GA aircraft on the radar scope, since
# BOTH share the same emitter category (A1 light or A2 small; nothing in
# the ADS-B category spec itself distinguishes them). Reference data, same
# category as ICAO_TYPE_NAMES below, but with an HONEST GAP that table
# doesn't have: this list is built from general real-world ICAO type-
# designator knowledge, NOT (yet) verified against an actual locally-
# observed sample the way ICAO_TYPE_NAMES was (no bizjet happened to be in
# range during this feature's build). Treat as a real but unconfirmed-
# locally reference table -- worth pruning/extending once real bizjet
# traffic near MYR is actually seen, same "confirm before trusting"
# standard the rest of this project holds itself to.
BIZJET_TYPES = {
    "C25A", "C25B", "C25C", "C500", "C510", "C525", "C550", "C560", "C56X",
    "C650", "C680", "C68A", "C700", "C750",             # Cessna Citation family
    "GLF4", "GLF5", "GLF6", "G150", "G200", "G280",     # Gulfstream
    "LJ35", "LJ40", "LJ45", "LJ60", "LJ75",             # Learjet
    "FA7X", "FA8X", "F2TH", "F900",                     # Dassault Falcon
    "E50P", "E55P",                                      # Embraer Phenom
    "CL30", "CL35", "CL60",                              # Challenger
    "H25B", "HA4T",                                      # Hawker
    "PC24",                                               # Pilatus PC-24
    # ---- added 2026-08-07, THE HANGAR bucket table build -------------
    # Confirmed against a REAL local sighting in hangar_log.jsonl (unlike
    # every entry above, which was general knowledge with no local
    # confirmation until this table gained one -- see HANGAR_*_TYPES
    # below for the rest of that story). Added here rather than a
    # separate table because they are the same real thing this set
    # already exists to describe (a business jet ICAO type designator),
    # just ones that happened to be confirmed by this session instead of
    # the original build.
    "GLEX",   # Bombardier Global Express -- real hangar entries N8762M, N843FF
    "FA10",   # Dassault Falcon 10 -- real hangar entry N707CX
    "HDJT",   # HondaJet (Honda HA-420) -- real hangar entry N420NJ
    "E35L",   # Embraer Legacy 600 -- real hangar entry N650LY
    "E545",   # Embraer Legacy 450 -- real hangar entries N411FX, N413FX
}

# ---- THE HANGAR's type -> bucket lookup, 2026-08-07 -----------------------
# Built for hangar.py's static per-entry sprite (owner decision #3: the
# Hangar is browsed one aircraft at a time, so it can afford the DETAIL
# card's 3-stroke budget, not the scope's tighter 2-stroke one -- see
# engines.py's FlightEngine._hangar_kind()/_draw_plane_icon() call site).
#
# SEEDED ONLY FROM THE 198 REAL TYPE CODES ALREADY IN hangar_log.jsonl ON
# DISK (owner decision #2) -- every code below was read directly out of
# that file (68 distinct real values, one seen as `null` on 4 entries),
# not invented from general aviation knowledge the way the ORIGINAL
# BIZJET_TYPES table above admittedly was (its own docstring says so).
# Every code assigned a bucket here is one this session could positively
# identify from real, specific type-designator knowledge (e.g. "A320" is
# unambiguously an Airbus A320 narrowbody airliner, "SR22" is
# unambiguously a Cirrus SR22 GA single) -- nothing here is a guess at
# what a code MIGHT be. Anything not confidently identifiable is left
# OUT of every bucket on purpose: `FlightEngine._hangar_kind()` falls
# back to GA for anything not in one of these sets (or BIZJET_TYPES
# above), per owner decision #1 -- the honest statistical default,
# matching the live radar scope's own "default to majority bucket"
# convention, not a distinct "unknown" mark. Two real seen codes were
# deliberately left unmapped for exactly this reason: `GA6C` (no
# confident real-world match) and `HUNT` (a Hawker Hunter jet warbird --
# real, but fits none of the four buckets honestly) -- both correctly
# render as GA rather than a guess.
HANGAR_HELI_TYPES = {
    "R44",    # Robinson R44 -- real hangar entries N3055Y, N118YL, N267AW, N220SG
    "AS65",   # Airbus/Eurocopter AS365 Dauphin -- real hangar entry "6540"
}

HANGAR_AIRLINER_TYPES = {
    # Real narrowbody/widebody jet airliners seen in hangar_log.jsonl --
    # same "large real jet transport" bucket the live scope's _ac_kind()
    # already maps ADS-B category A3/A4/A5 onto, just identified here by
    # real ICAO type code instead of a live category field (the Hangar's
    # persisted entries don't carry `category`, only `type`).
    "A319", "A320", "A321",           # Airbus A320 family
    "A20N", "A21N",                   # Airbus A320neo family
    "A333", "A35K",                   # Airbus A330-300 / A350-1000
    "B737", "B738", "B38M",           # Boeing 737NG / MAX
    "B712",                            # Boeing 717-200
    "B788", "B789",                    # Boeing 787
    "BCS3",                            # Airbus A220-300
    "CRJ9",                            # Bombardier CRJ900 regional jet
    "E145", "E75L",                    # Embraer regional jets
    "AT73",                            # ATR 72 regional turboprop airliner
    "SH36",                            # Shorts SD3-60 -- real hangar entry N688AN, AIR CARGO CARRIERS
    # Large military jet/turboprop transports -- no dedicated bucket for
    # these exists (this project's 4-bucket set is deliberately not
    # expanded, see CLAUDE.md), and physically they are the same "big,
    # heavy, multi-engine transport" class the airliner bucket already
    # exists to describe, not a GA aircraft by any honest reading.
    "C17",    # Boeing C-17 Globemaster III -- real hangar entries 99-0062, 08-8190
    "C30J",   # Lockheed C-130J Super Hercules -- real hangar entry 166472
}
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
    raw = _fetch_ac_list(POSITION_SOURCES,
                         {"lat": lat, "lon": lon, "radius_nm": RADIUS_NM})
    window = load_window()   # cheap local config read, same cost class as
                              # every other per-refresh config check here
                              # (e.g. satellite's own CONFIG_CHECK reload)
    favorites = set(load_favorite_aircraft())   # same cheap-per-refresh-read cost class
    out = []
    for ac in raw:
        # .get(key, default) only supplies the default when the key is
        # MISSING -- a real payload with "gs": null still hands back None,
        # and None < 30 raises. Coerce explicitly instead.
        gs = ac.get("gs")
        gs = gs if isinstance(gs, (int, float)) else 0
        if ac.get("alt_baro") in (None, "ground") and gs < 30:
            continue          # parked/taxiing clutter, not "in the sky" traffic
        alt = ac.get("alt_baro") if isinstance(ac.get("alt_baro"), (int, float)) else None
        phase, rate = _phase(ac, alt=alt)
        dir_deg = ac.get("dir")
        dist_nm = ac.get("dst")
        ac_lat = ac.get("lat") if isinstance(ac.get("lat"), (int, float)) else None
        ac_lon = ac.get("lon") if isinstance(ac.get("lon"), (int, float)) else None
        # Some replicas omit dst/dir even when lat/lon are real. Derive
        # them from the same great-circle math the scope already uses --
        # not a guessed position, just the home-relative polar form of
        # the coordinates the payload already stated.
        if (not isinstance(dist_nm, (int, float)) or dir_deg is None) and \
                ac_lat is not None and ac_lon is not None:
            brg, nm = bearing_distance(lat, lon, ac_lat, ac_lon)
            if dir_deg is None:
                dir_deg = brg
            if not isinstance(dist_nm, (int, float)):
                dist_nm = nm
        reg = paneltext.panel_text((ac.get("r") or "").strip()) or None
        out.append({
            "ident": _ident(ac),
            # Raw ICAO24 hex -- the one field ADS-B guarantees is stable
            # for a given aircraft for as long as it's in range, unlike
            # `ident` (a callsign can theoretically repeat across two
            # different real aircraft on different legs) or list position
            # (which reorders every refresh). Used as the SELECTION key,
            # not for display -- `ident` is still what's shown on screen.
            "hex": (ac.get("hex") or "").strip().upper() or None,
            # Real registration/tail number ("N8986Q"), for THE HANGAR
            # (hangar.py) -- confirmed present on 235/238 (98.7%) of a
            # real 238-aircraft live sample near ORD. Folded like every
            # other externally-sourced string here even though real
            # registrations are plain ASCII in practice -- no exception
            # carved out for "this one's probably fine".
            "reg": reg,
            # FAVORITE AIRCRAFT (2026-08-09) -- true only when this
            # aircraft's REAL registration matches one on the owner's real
            # watch list. An aircraft with no broadcast registration can
            # never match (honest -- there is nothing to compare), not a
            # false positive.
            "is_favorite": bool(reg and reg in favorites),
            "callsign": (ac.get("flight") or "").strip(),
            # Real ADS-B emitter category (A1 light .. A7 rotorcraft, B2
            # lighter-than-air -- see the CAT_* constants above). Was
            # already being read for _notable() but discarded before
            # reaching the engine; the scope's icon classification needs
            # it too, same "don't recompute what's already in the real
            # payload" reasoning as everything else here.
            "category": str(ac.get("category") or ""),
            "type": (ac.get("t") or "").strip(),
            "alt_ft": alt,
            "gs_kt": ac.get("gs"),
            "track_deg": ac.get("track"),
            "dist_nm": dist_nm,
            # REAL absolute position (2026-08-09) -- the replica already
            # returns these per aircraft and they were simply being
            # discarded, the same shape of gap `origin_city`/`dest_city`
            # and the route lat/lons closed in _fetch_route() below. ZERO
            # new I/O: same call, two more fields off the same real
            # payload. Confirmed present on 8/8 aircraft of a real live
            # sample at the configured home before adding this -- not
            # assumed from the API docs.
            #
            # `dist_nm`/`dir_deg` above are polar coordinates RELATIVE TO
            # HOME, which is all the local radar scope needs; the global
            # flight-path map needs the absolute lat/lon to place the
            # aircraft on a world projection, and recomputing it from
            # bearing+range would be a needless second-hand version of a
            # number the payload already states directly. Each validated
            # as an actual number so a malformed/missing value degrades
            # to None rather than a guessed 0.0 (which would plot the
            # aircraft in the Gulf of Guinea -- a silent lie).
            "lat": ac_lat,
            "lon": ac_lon,
            "dir_deg": dir_deg,
            "phase": phase,
            "vrate_fpm": rate,
            "notable": _notable(ac, phase=phase),
            # WINDOW FILTER (2026-08-07, distance-capped 2026-08-08) --
            # true if this aircraft's real bearing FROM home (dir_deg, not
            # track_deg) currently falls inside the configured window cone
            # AND it's within the configured max_nm. Bearing alone answers
            # "is this the right direction"; real visibility out an actual
            # window also depends on distance -- a plane at 35nm can sit
            # dead-centre in the same 296+/-40deg cone as one at 3nm while
            # being over the horizon or behind terrain. See
            # satellite.WINDOW_MAX_NM_DEFAULT's own note for the reasoning
            # behind the default distance and why it's config-driven, not
            # hardcoded. Exposed so FlightEngine can give it distinct
            # visual treatment with zero I/O of its own -- see
            # draw_scope_aircraft's window-ring call site.
            "in_window": (in_window(dir_deg, window["center_deg"], window["fov_deg"])
                          and isinstance(dist_nm, (int, float))
                          and dist_nm <= window["max_nm"]),
        })
    # Most notable first, then nearest -- ADDITIVELY adjusted by the
    # window boost (WINDOW_BOOST above) and now the favorite boost
    # (FAVORITE_BOOST, 2026-08-09), never replacing this ranking.
    # Sorting purely by distance means the mode almost always leads with
    # a routine regional jet, because "closest" and "interesting" are
    # rarely the same aircraft -- a wide-body or a helicopter a few miles
    # further out is the one worth looking up for; a window aircraft is
    # the same idea applied to "worth looking up for because it's
    # literally visible out the window right now". A favorite gets the
    # LARGEST single boost of the three (1.5 vs. window's 0.5) --
    # deliberately: this is the one signal that is entirely the owner's
    # own choice rather than something ADS-B/geometry decided, so it
    # should be able to outrank even a HEAVY/HELI (rank 3) on its own,
    # not just nudge the existing order.
    def _rank(a):
        base = a["notable"][1] if a["notable"] else 0
        return (base + (WINDOW_BOOST if a["in_window"] else 0.0)
                     + (FAVORITE_BOOST if a["is_favorite"] else 0.0))
    out.sort(key=lambda a: (-_rank(a),
                            a["dist_nm"] if a["dist_nm"] is not None else 1e9))
    return out


def _route_plausible(home_lat, home_lon, origin_lat, origin_lon, dest_lat, dest_lon,
                     tolerance_nm=150.0):
    """Real, physics-based sanity check on a route adsbdb hands back
    (2026-08-11, direct owner report: a real locally-tracked aircraft
    showed an adsbdb-enriched route of Osaka->Hawaii -- a real transpacific
    pair nowhere near this project's configured home, which cannot be
    correct for an aircraft ADS-B genuinely places within RADIUS_NM of
    home right now).

    ROOT CAUSE, not guessed: `_fetch_route()` looks up a route by
    CALLSIGN alone (adsbdb's own `/v0/callsign/{callsign}` endpoint) --
    a real, confirmed limitation of that API, not a bug in how this
    project calls it. Flight numbers are reused across genuinely
    different real routes (different day, different leg, a codeshare, a
    repositioning flight), and adsbdb's callsign lookup can return
    whichever real route it has on file for that callsign string, which
    is not guaranteed to be the SPECIFIC real flight this specific
    aircraft, right now, is actually flying. This project already
    documents route matching as unverified-shape in several places; this
    is the same class of gap one level deeper -- the shape is right, the
    CONTENT can be wrong.

    THE CHECK: a real aircraft on a real route passes reasonably close to
    every point along the great-circle path between its real origin and
    destination -- home, if the aircraft is genuinely flying that route
    and happens to currently be near home, must sit close to that path
    too. Computed via the standard "is the detour through home much
    longer than the direct route" test: home_to_origin + home_to_dest
    should be close to origin_to_dest for a real waypoint on the path;
    home would need to be almost perfectly instantaneously between the
    two real endpoints for that math to hold if the true answer wasn't
    "an aircraft over South Carolina cannot possibly be flying Osaka to
    Honolulu." `tolerance_nm` (150nm) is a generous real allowance for
    normal course deviation, not a strict geometric requirement.

    Returns True when origin/dest coordinates are missing (nothing to
    check -- an honest "can't verify" is not the same as "known wrong",
    so this never blocks a route ONLY because adsbdb didn't return
    coordinates for it)."""
    if None in (origin_lat, origin_lon, dest_lat, dest_lon):
        return True
    _, direct_nm = bearing_distance(origin_lat, origin_lon, dest_lat, dest_lon)
    _, home_to_origin_nm = bearing_distance(home_lat, home_lon, origin_lat, origin_lon)
    _, home_to_dest_nm = bearing_distance(home_lat, home_lon, dest_lat, dest_lon)
    detour_nm = (home_to_origin_nm + home_to_dest_nm) - direct_nm
    return detour_nm <= tolerance_nm


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
        # Keep the other code form too so home matching can compare
        # MYR against KMYR without inventing a conversion. Display
        # still prefers IATA (the `origin`/`dest` keys above).
        "origin_icao": origin.get("icao_code") or None,
        "dest_icao": dest.get("icao_code") or None,
        "origin_iata": origin.get("iata_code") or None,
        "dest_iata": dest.get("iata_code") or None,
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
        # REAL country + coordinates (2026-08-08) -- adsbdb's route
        # response already carries these per airport and they were simply
        # being discarded before, the same shape of gap `origin_city`/
        # `dest_city` closed above. `country_name` is real, full-length
        # ("United States"), folded like every other externally-sourced
        # string here. lat/lon are numeric -- no fold needed, but each is
        # validated as an actual number so a malformed/missing value
        # degrades to None rather than a guessed 0.0. ZERO new I/O: this
        # is the same per-callsign `_fetch_route()` call that already ran,
        # just keeping two more fields off the same real payload.
        "origin_country": paneltext.panel_text(origin.get("country_name")) or None,
        "dest_country": paneltext.panel_text(dest.get("country_name")) or None,
        "origin_lat": origin.get("latitude") if isinstance(origin.get("latitude"), (int, float)) else None,
        "origin_lon": origin.get("longitude") if isinstance(origin.get("longitude"), (int, float)) else None,
        "dest_lat": dest.get("latitude") if isinstance(dest.get("latitude"), (int, float)) else None,
        "dest_lon": dest.get("longitude") if isinstance(dest.get("longitude"), (int, float)) else None,
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


def _ac_key(ac):
    """The stable identity key for one real aircraft dict -- hex (ICAO24)
    preferred, ident as fallback. Mirrors engines.FlightEngine._sel_key()
    EXACTLY (hex-then-ident) -- one keying scheme for "the same real
    aircraft", not a second one invented on this side of the I/O boundary.
    Used by the window-takeover detector below and by the pending-detail
    hand-off, both of which need to agree with the engine on identity."""
    return ac.get("hex") or ac.get("ident")


class FlightFeed:
    """Background poller with a last-good cache -- same contract as
    MarketFeed/SatelliteFeed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._aircraft = []
        self._sky = []
        self._updated = 0.0
        self._last_try = 0.0
        self._last_read = 0.0
        self._last_config_check = 0.0
        self._thread = None
        self._err = None
        self._route_cache = {}          # callsign -> route dict or None (looked up, no route)
        self._home = satellite.FEED.get_location()   # (lat, lon, label)

        # ---- PLANE-IN-WINDOW TAKEOVER (2026-08-08) --------------------
        # Real detection of "an aircraft just entered the configured
        # window cone", built on the `in_window` flag `_fetch_positions()`
        # already stamps on every aircraft (see satellite.in_window() --
        # this does NOT re-derive that flag, only reacts to it changing).
        # Same one-shot adopt-then-diff idiom as `_seen_home_runs`/
        # `_seen_squawks`/every other detector in this project: None until
        # the first real refresh, which ADOPTS whatever is already in the
        # window without firing (a device that's been running with a
        # window aircraft parked at the edge must not take over the panel
        # the instant it starts), then only a genuinely NEW membership
        # counts as "just entered".
        self._seen_window = None            # set of ac keys in-window as of last refresh
        self._window_batch = []             # one-shot: newly-entered aircraft, closest-first/notable-secondary
        self._pending_detail = None         # one-shot: sel_key for FlightEngine to jump straight to on arrival

    def pop_window_takeover_batch(self):
        """One-shot: real aircraft that JUST entered the configured window
        cone since the last poll, or [] if nothing new happened. Consumed
        once -- calling this a second time before another real refresh
        returns [] -- same "pop, don't peek-forever" discipline as
        BigMomentSource.pop_big_moment(), so arcade_server's render-loop
        poll can't re-trigger the same takeover repeatedly.

        Sorted PRIMARY by real distance (dist_nm ascending -- closest
        first, the owner's explicit spec), SECONDARY by real notability
        rank descending (a tiebreak among aircraft at similar distance,
        same `_notable()` rank the main sort already uses elsewhere in
        this file). Full real aircraft dicts, not just keys -- the
        takeover screen needs registration/type/distance/altitude/
        in_window, all of which are already on these dicts."""
        with self._lock:
            batch, self._window_batch = self._window_batch, []
            return batch

    def push_pending_detail(self, key):
        """One-shot hand-off: PlaneWatchEngine calls this right before
        setting `.launch = 'flights'` so FlightEngine.reset() can jump
        straight to VIEW_DETAIL for THIS aircraft instead of landing on
        the plain scope -- set_mode() always constructs ENGINES[base]()
        with zero args, so this slot is the only way to pass "which
        aircraft" across a real mode switch. Same "consumed once" shape
        as pop_pending_detail() below, mirroring
        BigMomentSource.pop_big_moment()'s one-shot queue idiom."""
        with self._lock:
            self._pending_detail = key

    def pop_pending_detail(self):
        """Consume (not peek) the pending detail-selection key, or None."""
        with self._lock:
            key, self._pending_detail = self._pending_detail, None
            return key

    def get(self):
        """Returns {aircraft: [...], age, home_label, configured, err}.
        Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            aircraft = [dict(a) for a in self._aircraft]
            sky = [dict(a) for a in self._sky]
            updated, err = self._updated, self._err
            home_label = self._home[2]
        self._ensure_thread()
        age = (now - updated) if updated else None
        return {
            "aircraft": aircraft, "sky": sky, "age": age,
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
                self._sky = []
                self._updated = time.time()
                self._err = None
            return

        try:
            sky = _fetch_positions(lat, lon)
            aircraft = sky[:MAX_TRACKED]
        except Exception as e:                        # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"
            return

        # Enrich the whole sky, radar eight first. The board reads
        # every aircraft in range, not just the eight the scope shows
        # -- a MYR arrival that is 9th-nearest used to be invisible.
        lookups_left = MAX_LOOKUPS_PER_REFRESH
        for ac in sky:
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
            # PLAUSIBILITY CHECK (2026-08-11) -- see _route_plausible()'s
            # own docstring for the real incident that prompted this (a
            # real local aircraft showed an adsbdb-enriched route of
            # Osaka->Hawaii, a physical impossibility for an aircraft
            # ADS-B genuinely places within RADIUS_NM of home). A route
            # that fails this check is real ESPN^H^Hadsbdb data for SOME
            # real flight, just not credibly THIS one -- displaying it
            # confidently would be exactly the kind of "technically real
            # data, actually wrong for this aircraft" lie this project's
            # own "never invent" rule exists to prevent one level deeper
            # than usual. Dropped to None (an honest "route unknown")
            # rather than shown -- never downgraded to a guessed
            # alternative.
            if route and not _route_plausible(lat, lon, route.get("origin_lat"),
                                              route.get("origin_lon"), route.get("dest_lat"),
                                              route.get("dest_lon")):
                route = None
            self._route_cache[cs] = route
            ac["route"] = route

        # Cap cache growth over a long-running process -- a simple full
        # clear past a generous ceiling is fine here: it just means a
        # handful of callsigns re-enrich once, not a meaningful cost.
        if len(self._route_cache) > 2000:
            self._route_cache.clear()

        # THE HANGAR (hangar.py) -- record every real aircraft with a
        # real registration into the persistent collection, right here
        # on this same background poll thread. Pure composition of data
        # this cycle already fetched; no new network call, no new poll
        # cadence. Only aircraft that actually broadcast a registration
        # are recorded -- see hangar.py's own docstring on why a bare
        # hex isn't treated as a substitute tail number.
        # WELCOME BACK -- a favorited tail genuinely returning after a
        # real absence, direct owner ask. Only for `favorite_aircraft`
        # (a deliberate owner choice, same "the owner's own pick earns
        # this" reasoning FAVORITE_BOOST/FAVORITE_GLOW_FLOOR already use)
        # and only past a real threshold (WELCOME_BACK_MIN_S, 1 real day)
        # -- record_sighting()'s own VISIT_GAP_S (30min) is the right
        # threshold for "is this a new visit" but far too twitchy for an
        # event worth surfacing; a favorite circling the pattern for 35
        # minutes must not fire a "welcome back" every lap.
        for ac in sky:
            if not ac["reg"]:
                continue
            gap = hangar.LOG.record_sighting(
                ac["reg"], ac["type"], (ac.get("route") or {}).get("airline"))
            if (ac.get("is_favorite") and gap is not None
                    and gap >= WELCOME_BACK_MIN_S):
                label = _ident(ac)
                gap_txt = _fmt_gap_days(gap)
                summary = f"{label} IS BACK -- LAST SEEN {gap_txt}"
                events_log.LOG.record("plane", paneltext.panel_text(summary))

        # PLANE-IN-WINDOW TAKEOVER -- one-shot "newly entered the window"
        # detection, hooked into this SAME refresh cycle (zero new I/O,
        # zero new poll cadence). Reacts to the real `in_window` flag
        # `_fetch_positions()` already computed above via
        # satellite.in_window() -- this only diffs it against last cycle.
        now_in_window = {_ac_key(a) for a in sky
                          if a.get("in_window") and _ac_key(a)}
        if self._seen_window is None:
            self._seen_window = now_in_window   # first read: adopt, don't fire
        else:
            newly = now_in_window - self._seen_window
            if newly:
                entered = [a for a in sky if _ac_key(a) in newly]
                entered.sort(key=lambda a: (
                    a["dist_nm"] if isinstance(a.get("dist_nm"), (int, float)) else 1e9,
                    -(a["notable"][1] if a.get("notable") else 0)))
                with self._lock:
                    # Most recent batch wins if the previous one was never
                    # consumed -- same "only the most recent real event
                    # matters" shape as _set_big_moment()'s one-slot queue.
                    self._window_batch = entered
                # RECENT EVENTS LOG (events_log.py) -- one entry per real
                # aircraft that newly entered the window, same real batch
                # the takeover/detail hand-off consumes. Summary reuses
                # _ident()/_type_name(), the same fold-safe label-building
                # helpers every other real aircraft label in this module
                # already uses -- not reinvented here. Folded already (both
                # helpers fold at their own boundary), so this call site
                # passes an already-folded string, matching events_log.py's
                # own "caller folds" contract.
                for a in entered:
                    label = _ident(a)
                    tname = _type_name(a.get("type"))
                    summary = f"{label} ({tname})" if tname else label
                    events_log.LOG.record("plane", paneltext.panel_text(summary))
            self._seen_window = now_in_window

        with self._lock:
            self._sky = sky
            self._aircraft = aircraft
            self._updated = time.time()
            self._err = None


FEED = FlightFeed()


# ---- FOLLOW A SPECIFIC FLIGHT (2026-08-09) --------------------------------
# Owner ask, straight from competitive research on products like Mach 2:
# "track any specific flight by number, anywhere it's currently airborne,
# not just within the local radius." This is a genuinely separate feature
# from the local radar scope above -- global lookup by callsign vs. local
# radius by position -- so it gets its own config file, its own feed class,
# and its own engine, per the owner's own explicit instruction not to
# conflate the two.
FOLLOW_CONFIG_PATH = Path(__file__).parent / "follow_flight_config.json"

FOLLOW_REFRESH = 15.0   # flat interval, not adaptive like POSITION_REFRESH's
                        # neighbor concept -- this is a SINGLE flight lookup,
                        # not an up-to-8-aircraft scope, so there is no
                        # "sky full of traffic" volume concern to adapt
                        # against. 15-20s is plenty responsive for a wall
                        # panel glance and keeps this feed polite to
                        # api.adsb.lol at essentially zero real cost (one
                        # request every 15s while the mode is actually
                        # being viewed, same IDLE_STOP-gated lifecycle as
                        # every other feed here).


# ---- CURATED FAMOUS AIRCRAFT (2026-08-17) ----------------------------------
# Owner ask: "have a drop down of famous planes like air force one and stuff
# like that to follow." The hard rule (CLAUDE.md's own "never invent"): every
# entry here is a REAL, publicly documented aircraft identified by a REAL
# identifier the ADS-B networks this project already queries (adsb.lol /
# airplanes.live / adsb.fi) actually key on -- a registration (tail number)
# for the individual airframes, resolved via the reg-lookup fallback
# _fetch_follow() gained this same session. Nothing here is a guessed or
# aspirational identifier.
#
# `id` is what gets POSTed to the existing /api/flights/follow endpoint (it
# is normalized/uppercased by save_followed_flight() exactly like any typed
# callsign). `kind` is "reg" or "callsign", informational only -- the feed
# tries callsign then registration regardless, so a mislabeled entry still
# resolves; it exists so the picker can hint how a given plane is found.
#
# HONEST GAP, stated plainly and surfaced on the card: these are real
# airframes, but most are display/heritage aircraft that only fly on show
# days (the Goodyear blimps and NASA's Super Guppy fly far more often than
# the warbirds). Following one when it is parked shows the card's real
# "NOT CURRENTLY AIRBORNE" tri-state -- an honest "it isn't up right now",
# never a fabricated position. AIR FORCE ONE is included because the owner
# asked for it by name; its ADS-B is routinely filtered/blocked, so it will
# almost always read NOT AIRBORNE -- that honesty is the whole point of the
# tri-state, and the card/label says so rather than pretending otherwise.
FAMOUS_AIRCRAFT = (
    # (label, id, kind, note)
    # Real tail numbers, not a guessed callsign -- "AF1" is not a real
    # ADS-B callsign the VC-25 ever broadcasts (it flies under its own
    # Air Force tail number/callsign; "Air Force One" is a radio
    # callsign only while POTUS is aboard, which ADS-B doesn't reflect).
    # The two real VC-25A airframes are 82-8000 and 92-9000 (US Air
    # Force serials), which _fetch_follow()'s reg-lookup path expects as
    # N-less military tail numbers.
    ("AIR FORCE ONE (82-8000)", "82-8000", "reg",
     "Usually filtered on ADS-B -- expect NOT AIRBORNE"),
    ("AIR FORCE ONE (92-9000)", "92-9000", "reg",
     "Usually filtered on ADS-B -- expect NOT AIRBORNE"),
    ("GOODYEAR WINGFOOT ONE", "N1A", "reg", "Goodyear blimp (Zeppelin NT)"),
    ("GOODYEAR WINGFOOT TWO", "N2A", "reg", "Goodyear blimp (Zeppelin NT)"),
    ("GOODYEAR WINGFOOT THREE", "N3A", "reg", "Goodyear blimp (Zeppelin NT)"),
    ("NASA SUPER GUPPY", "N941NA", "reg", "NASA outsize-cargo turboprop"),
    ("B-29 FIFI", "N529B", "reg", "Commemorative Air Force B-29"),
    ("B-29 DOC", "N69972", "reg", "Doc's Friends B-29"),
    ("B-17 ALUMINUM OVERCAST", "N5017N", "reg", "EAA B-17 Flying Fortress"),
    ("B-17 SENTIMENTAL JOURNEY", "N9323Z", "reg", "CAF B-17 Flying Fortress"),
)


def famous_aircraft():
    """The curated famous-aircraft picker list as plain dicts, for the
    control panel / phone remote follow card. Single source of truth --
    the HTML never hardcodes tail numbers, it reads this."""
    return [{"label": l, "id": i, "kind": k, "note": n}
            for (l, i, k, n) in FAMOUS_AIRCRAFT]


def load_followed_flight():
    """The currently-followed callsign, or None if nothing is configured.

    Its own small file, not folded into location_config.json -- a
    followed callsign is NOT a location fact (that file's one unifying
    theme, see load_airport()/load_window() above); it is closer in kind
    to market.py's watchlist or sports.py's pinned player. Read-modify-
    write on save (see save_followed_flight() below) even though this
    file currently owns only one key -- CLAUDE.md's own 2026-08-09 lesson
    (market.py/news.py/blog.py/notify.py's save_config() bug) is to build
    that discipline in from the start rather than wait for a second key
    to make a fresh-dict write destructive.
    """
    path = FOLLOW_CONFIG_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text()) or {}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    cs = data.get("callsign")
    if not isinstance(cs, str) or not cs.strip():
        return None
    return cs.strip().upper()


def save_followed_flight(callsign):
    """Persist (or clear, with a falsy callsign) the followed callsign.

    Normalizes the same way _fetch_follow() below expects to query:
    stripped, uppercased, spaces removed (adsb.lol callsigns are the
    ICAO flight-number format -- airline ICAO code + number, no spaces,
    e.g. "UAL123" -- not the IATA format ("UA123") a traveler would
    recognize; see FollowFlightFeed's own docstring for why no IATA<->
    ICAO translation table is built here).
    """
    path = FOLLOW_CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text()) or {}
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            data = {}
    if callsign:
        norm = str(callsign).strip().upper().replace(" ", "")
        if norm:
            data["callsign"] = norm
        else:
            data.pop("callsign", None)
    else:
        data.pop("callsign", None)
    path.write_text(json.dumps(data, indent=2))
    return data.get("callsign")


def _fetch_follow(callsign):
    """One real aircraft dict (same shape _fetch_positions() emits, minus
    the home-relative fields that don't apply -- no dist_nm/dir_deg/
    in_window, this flight may be anywhere on Earth) or None if the
    callsign is not currently broadcasting a position.

    adsb.lol's global callsign endpoint returns the identical {"ac": [...]}
    envelope POSITION_URL does, confirmed live -- reuses the SAME per-
    aircraft field names _fetch_positions() already parses (_ident(),
    _type_name(), _phase(), the same alt_baro/gs/track/category/r/hex
    keys), not a second parsing scheme for what is the same real payload
    shape. An empty "ac" list is a REAL, honest "not currently airborne"
    result -- not treated as an error."""
    ac_list = _fetch_ac_list(FOLLOW_SOURCES, {"callsign": callsign})
    if not ac_list:
        # Fall back to a REGISTRATION lookup before concluding "not
        # airborne". Famous-aircraft picker entries (FAMOUS_AIRCRAFT) are
        # keyed by tail number, which the callsign endpoint never matches;
        # a real airline callsign ("UAL123") that IS up already returned
        # above, so this only runs when the callsign path found nothing.
        # A reg-source network failure is swallowed here (the callsign
        # path already succeeded-empty, so an honest "not airborne" beats
        # a NO SIGNAL error from the fallback tier).
        try:
            ac_list = _fetch_ac_list(FOLLOW_REG_SOURCES, {"reg": callsign})
        except Exception:                              # noqa: BLE001
            ac_list = []
    if not ac_list:
        return None
    # A callsign can (rarely) match more than one currently-squawking
    # aircraft in adsb.lol's own data (e.g. a stale/duplicate entry); the
    # one actually reporting a real altitude is the more useful pick, but
    # absent that, the first real entry adsb.lol returned is used as-is
    # rather than guessing which one is "more real."
    ac = next((a for a in ac_list if isinstance(a.get("alt_baro"), (int, float))),
              ac_list[0])
    alt = ac.get("alt_baro") if isinstance(ac.get("alt_baro"), (int, float)) else None
    phase, rate = _phase(ac, alt=alt)
    lat = ac.get("lat") if isinstance(ac.get("lat"), (int, float)) else None
    lon = ac.get("lon") if isinstance(ac.get("lon"), (int, float)) else None
    return {
        "ident": _ident(ac),
        "hex": (ac.get("hex") or "").strip().upper() or None,
        "reg": paneltext.panel_text((ac.get("r") or "").strip()) or None,
        "callsign": (ac.get("flight") or "").strip(),
        "category": str(ac.get("category") or ""),
        "type": (ac.get("t") or "").strip(),
        "alt_ft": alt,
        "gs_kt": ac.get("gs"),
        "track_deg": ac.get("track"),
        "lat": lat,
        "lon": lon,
        "phase": phase,
        "vrate_fpm": rate,
        "notable": _notable(ac, phase=phase),
    }


class FollowFlightFeed:
    """Background poller for a single owner-followed flight, anywhere it's
    currently airborne -- last-good cache, never blocks, same contract as
    every other FEED in this project.

    Deliberately its OWN class, not a second mode bolted onto FlightFeed
    above: FlightFeed's identity is "everything within RADIUS_NM of home,
    re-sorted by notability every refresh" -- a list. This is "one
    specific real-world flight, wherever it is" -- a single optional
    value, with a genuinely different honest-empty state ("not currently
    airborne" is not the same fact as "no local traffic right now").
    Conflating them would mean threading a special-case single-item
    exception through every list-shaped consumer of FlightFeed.get().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._callsign = load_followed_flight()
        self._aircraft = None       # last-good real aircraft dict, or None
        self._updated = 0.0
        self._last_try = 0.0
        self._last_read = 0.0
        self._last_config_check = 0.0
        self._thread = None
        self._err = None
        self._route_cache = {}      # callsign -> route dict or None

    def set_followed(self, callsign):
        """Owner sets (or clears, with a falsy callsign) which flight to
        follow. Persists immediately and resets the cached aircraft so a
        stale previous flight's position is never shown against a freshly
        chosen callsign."""
        norm = save_followed_flight(callsign)
        with self._lock:
            self._callsign = norm
            self._aircraft = None
            self._updated = 0.0
            self._last_try = 0.0
            self._err = None
        return norm

    def get(self):
        """Returns {configured, callsign, aircraft, route, age, airborne,
        err}. Never blocks.

        `airborne` is the explicit three-state signal the owner's own
        design spec called for: None (not configured -- nothing to look
        up), False (configured, real lookup ran, callsign genuinely is
        not currently broadcasting a position -- "NOT CURRENTLY AIRBORNE",
        never a guessed/stale position), True (a real current position is
        cached). `age` is seconds since that real position was last
        confirmed live, same "stale but honest" convention as every other
        feed here -- the engine can show it went stale without this feed
        ever inventing a fresher one."""
        now = time.time()
        with self._lock:
            self._last_read = now
            callsign = self._callsign
            aircraft = dict(self._aircraft) if self._aircraft else None
            updated, err = self._updated, self._err
        self._ensure_thread()
        age = (now - updated) if updated else None
        # airborne is a real tri-state. A dead replica / exception is
        # NOT "not airborne" -- that claim is only honest after every
        # replica that answered returned an empty list (err is None and
        # a fetch completed). Unknown (None) while configured means
        # looking, or the feed failed; the engine must not say GROUNDED.
        if callsign is None:
            airborne = None
        elif aircraft is not None:
            airborne = True
        elif err or not updated:
            airborne = None
        else:
            airborne = False
        return {
            "configured": callsign is not None,
            "callsign": callsign,
            "aircraft": aircraft,
            "route": (aircraft or {}).get("route") if aircraft else None,
            "age": age,
            "airborne": airborne,
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
            self._maybe_reload_config()
            self._refresh_once()
            time.sleep(2.0)

    def _maybe_reload_config(self):
        # Picks up a callsign set via /api/flights/follow on another
        # thread/process (e.g. the control panel), same periodic-reload
        # cost class as FlightFeed._maybe_reload_location() above --
        # cheap local file read, not new network I/O.
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        cs = load_followed_flight()
        with self._lock:
            if cs != self._callsign:
                self._callsign = cs
                self._aircraft = None
                self._updated = 0.0

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < FOLLOW_REFRESH:
                return
            self._last_try = now
            callsign = self._callsign

        if not callsign:
            with self._lock:
                self._aircraft = None
                self._updated = time.time()
                self._err = None
            return

        try:
            ac = _fetch_follow(callsign)
        except Exception as e:                        # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"
            return

        if ac is not None:
            cs = ac["callsign"] or callsign
            if cs in self._route_cache:
                ac["route"] = self._route_cache[cs]
            else:
                try:
                    route = _fetch_route(cs)
                except Exception:                      # noqa: BLE001
                    route = None
                self._route_cache[cs] = route
                ac["route"] = route

        with self._lock:
            self._aircraft = ac
            self._updated = time.time()
            self._err = None


FOLLOW_FEED = FollowFlightFeed()
