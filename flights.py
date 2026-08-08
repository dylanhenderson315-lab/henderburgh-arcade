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

import hangar
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
    url = POSITION_URL.format(lat=lat, lon=lon, radius_nm=RADIUS_NM)
    data = _get_json(url)
    window = load_window()   # cheap local config read, same cost class as
                              # every other per-refresh config check here
                              # (e.g. satellite's own CONFIG_CHECK reload)
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
        dir_deg = ac.get("dir")
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
            "reg": paneltext.panel_text((ac.get("r") or "").strip()) or None,
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
            "dist_nm": ac.get("dst"),
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
                          and ac.get("dst") is not None
                          and ac["dst"] <= window["max_nm"]),
        })
    # Most notable first, then nearest -- ADDITIVELY adjusted by the
    # window boost (WINDOW_BOOST above), never replacing this ranking.
    # Sorting purely by distance means the mode almost always leads with
    # a routine regional jet, because "closest" and "interesting" are
    # rarely the same aircraft -- a wide-body or a helicopter a few miles
    # further out is the one worth looking up for; a window aircraft is
    # the same idea applied to "worth looking up for because it's
    # literally visible out the window right now".
    def _rank(a):
        base = a["notable"][1] if a["notable"] else 0
        return base + (WINDOW_BOOST if a["in_window"] else 0.0)
    out.sort(key=lambda a: (-_rank(a),
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

        # THE HANGAR (hangar.py) -- record every real aircraft with a
        # real registration into the persistent collection, right here
        # on this same background poll thread. Pure composition of data
        # this cycle already fetched; no new network call, no new poll
        # cadence. Only aircraft that actually broadcast a registration
        # are recorded -- see hangar.py's own docstring on why a bare
        # hex isn't treated as a substitute tail number.
        for ac in aircraft:
            if ac["reg"]:
                hangar.LOG.record_sighting(
                    ac["reg"], ac["type"], (ac.get("route") or {}).get("airline"))

        # PLANE-IN-WINDOW TAKEOVER -- one-shot "newly entered the window"
        # detection, hooked into this SAME refresh cycle (zero new I/O,
        # zero new poll cadence). Reacts to the real `in_window` flag
        # `_fetch_positions()` already computed above via
        # satellite.in_window() -- this only diffs it against last cycle.
        now_in_window = {_ac_key(a) for a in aircraft
                          if a.get("in_window") and _ac_key(a)}
        if self._seen_window is None:
            self._seen_window = now_in_window   # first read: adopt, don't fire
        else:
            newly = now_in_window - self._seen_window
            if newly:
                entered = [a for a in aircraft if _ac_key(a) in newly]
                entered.sort(key=lambda a: (
                    a["dist_nm"] if isinstance(a.get("dist_nm"), (int, float)) else 1e9,
                    -(a["notable"][1] if a.get("notable") else 0)))
                with self._lock:
                    # Most recent batch wins if the previous one was never
                    # consumed -- same "only the most recent real event
                    # matters" shape as _set_big_moment()'s one-slot queue.
                    self._window_batch = entered
            self._seen_window = now_in_window

        with self._lock:
            self._aircraft = aircraft
            self._updated = time.time()
            self._err = None


FEED = FlightFeed()
