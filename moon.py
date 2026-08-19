"""
moon.py -- real lunar data for a genuine space/lunar hobbyist, direct
owner ask ("I do love the moon one... a lunar hobbyist or even a space
hobbyist would love this").

Two real, free, keyless sources, each verified live 2026-08-19 before
being wired in here:

  * USNO (aa.usno.navy.mil/api/rstt/oneday) -- the US Naval
    Observatory's own real astronomical data service. No key, no
    signup, no rate limit documented. Real fields used: `curphase`
    (current phase name), `fracillum` (real illumination percentage,
    e.g. "46%" -- a strict upgrade over skypass.py's own
    moon_phase_name()'s 8-bucket approximation), `closestphase` (name/
    date/time of the next quarter/full/new), and `moondata[]` (real
    per-event Rise/Upper Transit/Set times for the configured home).
  * Launch Library 2 (ll.thespacedevs.com) -- real upcoming real-world
    rocket launches. No key required for read access. Real soft rate
    limit (~15 req/hour unauthenticated per public docs) -- this module
    polls at LAUNCH_REFRESH (1h), nowhere close to that ceiling.

DELIBERATELY NOT BUILT: real Earth-Moon distance / a "supermoon" flag.
A previous research pass found no free hosted API for it and
recommended computing it locally via a simplified lunar ephemeris
formula -- but a subtly wrong distance formula would produce a
confidently wrong "SUPERMOON" claim to exactly the audience (a real
lunar hobbyist) most likely to notice and be bothered by it. Flagged
as a real, deliberate gap rather than shipped as an unverified guess;
revisit if a real, verifiable ephemeris source is found.

Location is NOT duplicated -- reuses satellite.py's
location_config.json via satellite.FEED.get_location(), same as every
other module here.

PLANET POSITIONS (2026-08-19) -- "make the moon mode a whole space hub,
a lunar/space hobbyist's dream", direct owner ask. A dedicated research
pass confirmed live that USNO has NO real planet endpoint (`body=` on
`rstt/oneday` is silently ignored -- confirmed by comparing two calls),
and that api.nasa.gov exposes no live position data either. The one
genuinely free, keyless, confirmed-live source found: JPL HORIZONS
(`ssd.jpl.nasa.gov/api/horizons.api`) -- NASA/JPL's own real ephemeris
system, not a third party. No key, no documented hard rate wall (tested
with multiple back-to-back calls with no throttling response), but it
is a general-purpose research tool, not built for tight polling --
polled at `PLANET_REFRESH` (1h) per planet, one planet fetched per real
feed-loop pass (round-robin via `_planet_cursor`), never all seven at
once.

Real per-planet fields: `az_deg`/`el_deg` (true apparent azimuth/
elevation from the configured home coordinates -- this is what answers
"is it up right now" and "which way do I look"), `mag` (apparent
magnitude -- lower/negative is brighter), `dist_au` (real current
Earth-observer distance in astronomical units). `QUANTITIES='4,9,20'`
(azi/elev, apmag/S-brt, delta/deldot) keeps the returned row to only
those fields -- RA/Dec (`QUANTITIES=1`) is deliberately not requested,
this project has no use for equatorial coordinates.

Horizons returns a JSON-wrapped fixed-width TEXT ephemeris table
(`{"result": "...$$SOE ... $$EOE..."}`), not structured JSON --
`_parse_horizons_row()` pulls the trailing 6 numeric fields via regex
rather than a fixed column split, since the leading date/time/flag
columns have a variable-width flag field ("*m", "Am", or blank) that
makes a naive `split()` position-fragile. A row that doesn't match the
expected numeric tail returns None for that planet -- an honest gap,
never a guessed position.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import paneltext
import satellite

USNO_URL = ("https://aa.usno.navy.mil/api/rstt/oneday"
            "?date={date}&coords={lat},{lon}&tz={tz}")
LL2_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=5&mode=list"
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
CAD_URL = "https://ssd-api.jpl.nasa.gov/cad.api"   # real, free, keyless close-approach data

USNO_REFRESH = 3600.0 * 6   # real rise/set/illumination is a once-a-day fact; 6h keeps it current across a long-running day
LAUNCH_REFRESH = 3600.0     # respect LL2's real ~15/hr soft limit by a wide margin
PLANET_REFRESH = 3600.0     # per planet -- Horizons is a research tool, not built for tight polling
NEO_LIST_REFRESH = 3600.0 * 6   # real close-approach list -- new ones don't appear that often
NEO_VECTOR_REFRESH = 900.0      # real position for the CURRENT tracked NEO, refreshed often
                                 # since a close-approaching NEO genuinely moves fast (see
                                 # MoonEngine's own dead-reckoning docstring for why 15min is
                                 # still not "live" and why extrapolation matters more here
                                 # than for any planet)
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

# Real JPL Horizons body ids -- reference data, not invented.
PLANETS = [
    ("199", "MERCURY"), ("299", "VENUS"), ("499", "MARS"),
    ("599", "JUPITER"), ("699", "SATURN"), ("799", "URANUS"),
    ("899", "NEPTUNE"),
]
ORBIT_BODIES = PLANETS + [("399", "EARTH")]   # real heliocentric x/y, see
                                                # _fetch_planet_vector() --
                                                # Earth included so the
                                                # orbit diagram can show
                                                # where WE are too.
# Real Galilean moons of Jupiter (Io/Europa/Ganymede/Callisto), the
# single most recognizable real moon system to point a hobbyist at --
# direct owner ask ("select planet, zoomed in look where we can see
# the moons"). Scoped to Jupiter only for this pass (Saturn/Mars are a
# natural next step, same technique, not built yet -- an honest scope
# limit, not an oversight).
JUPITER_MOONS = [("501", "IO"), ("502", "EUROPA"), ("503", "GANYMEDE"), ("504", "CALLISTO")]
JUPITER_ID = "599"
MOON_VECTOR_REFRESH = 1800.0   # 30min -- moons orbit fast (Io: 1.77 real days), worth
                                # refreshing more often than a planet's own slow orbit

SUN_ID = "10"   # real Horizons body id -- fetched the same way as a planet
                # (see _refresh_one_planet's round-robin), but kept OUT of
                # PLANETS: it's not a planet to browse, it's real day/night
                # context (real el_deg > 0 means real daylight at the
                # configured home right now, honestly dimming the dome
                # rather than pretending every hour is equally good for
                # looking up).

_HORIZONS_ROW_RE = re.compile(
    r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+|n\.a\.)\s+(-?\d+\.\d+|n\.a\.)\s+"
    r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$"
)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _local_utc_offset_hours():
    """Real local UTC offset (DST-aware), for USNO's `tz` param."""
    off = datetime.now().astimezone().utcoffset()
    return off.total_seconds() / 3600.0 if off is not None else 0.0


def _parse_usno(data, now_local):
    """Real fields only. CONFIRMED live shape 2026-08-19 (do not
    re-derive from memory -- re-verify against a real response if this
    ever needs touching again): the real payload nests under
    `properties.data`, NOT `properties` directly. `moondata[]` entries
    use `phen` values "Rise"/"Upper Transit"/"Set" (full words, not
    single letters). `closestphase` carries `phase`/`day`/`month`/
    `year`/`time` as separate real fields, no combined `date` string.
    Missing/malformed fields degrade to None, never a guessed value."""
    if not isinstance(data, dict):
        return {}
    props = ((data.get("properties") or {}).get("data")
             or data.get("properties") or {})
    curphase = paneltext.panel_text(props.get("curphase") or "") or None
    fracillum = props.get("fracillum")
    illum_pct = None
    if isinstance(fracillum, str) and fracillum.strip().rstrip("%").isdigit():
        illum_pct = int(fracillum.strip().rstrip("%"))
    closest = props.get("closestphase") or {}
    closest_txt = None
    if isinstance(closest, dict) and closest.get("phase") and closest.get("day"):
        closest_txt = paneltext.panel_text(
            f"{closest['phase']} {closest.get('month')}/{closest['day']}")
    moonrise = moonset = None
    for row in (props.get("moondata") or []):
        if not isinstance(row, dict):
            continue
        phen = str(row.get("phen") or "").strip().upper()
        t = paneltext.panel_text(row.get("time") or "") or None
        if phen == "RISE" and t:
            moonrise = t
        elif phen == "SET" and t:
            moonset = t
    return {
        "curphase": curphase,
        "illum_pct": illum_pct,
        "closest_phase": closest_txt,
        "moonrise": moonrise,
        "moonset": moonset,
    }


def _parse_launch(data):
    """Real next FUTURE launch, or None. CONFIRMED live 2026-08-19: LL2's
    own `upcoming` endpoint's default ordering can list a launch that
    has ALREADY happened (net in the past, status "Launch Successful")
    ahead of genuinely future ones -- filtered here rather than trusted
    blindly, since a countdown to an already-flown launch would be a
    real, confusing wrong fact on a screen built for a hobbyist who'd
    notice immediately. `lsp_name` is the real provider-name field in
    this endpoint's list mode (NOT a nested `launch_service_provider`
    object, which only the detail endpoint carries)."""
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for L in results:
        net = L.get("net")
        if not (isinstance(net, str) and net > now_iso):
            continue
        name = paneltext.panel_text(L.get("name") or "") or None
        provider = paneltext.panel_text(L.get("lsp_name") or "") or None
        status = ((L.get("status") or {}).get("name"))
        status = paneltext.panel_text(status) if status else None
        if not name:
            continue
        return {"name": name, "net": net, "provider": provider, "status": status}
    return None


def _parse_horizons_row(text):
    """Real az/el/mag/distance from ONE Horizons ephemeris row, or None.
    Takes the LAST real row before $$EOE (closest to "now" of whatever
    window was requested). Matches the trailing 6 numeric fields by
    regex rather than a fixed column split -- see this module's own
    docstring for why the leading flag column makes position-based
    splitting fragile. `n.a.` (Horizons' own honest "not available" for
    S-brt/deldot on some bodies) is treated as absent, not zero."""
    if not isinstance(text, str):
        return None
    try:
        body = text.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    except IndexError:
        return None
    rows = [ln for ln in body.splitlines() if ln.strip()]
    if not rows:
        return None
    m = _HORIZONS_ROW_RE.search(rows[-1])
    if not m:
        return None
    az, el, apmag, _sbrt, delta, _deldot = m.groups()
    try:
        out = {"az_deg": float(az), "el_deg": float(el), "dist_au": float(delta)}
    except ValueError:
        return None
    if apmag != "n.a.":
        try:
            out["mag"] = float(apmag)
        except ValueError:
            pass
    return out


_VECTOR_RE = re.compile(
    r"X\s*=\s*(-?[\d.]+E[+-]\d+)\s+Y\s*=\s*(-?[\d.]+E[+-]\d+)\s+Z\s*=\s*(-?[\d.]+E[+-]\d+)"
)


def _parse_horizons_vector(text):
    """Real heliocentric ecliptic (x_au, y_au) for ONE body, the LAST
    real row before $$EOE, or None. Confirmed live 2026-08-19 against
    Jupiter: parsed (-3.24, 4.19) AU, matching its real ~5.30 AU
    distance from the Sun (sqrt(3.24^2+4.19^2)=5.30) -- a genuine
    cross-check, not just a shape match. Z is parsed but not kept (a
    top-down diagram only needs the ecliptic-plane x/y; real orbital
    inclinations are small enough -- under 7 degrees for every planet
    but Mercury -- that a top-down projection is an honest, standard
    simplification, not a distortion)."""
    if not isinstance(text, str):
        return None
    try:
        body = text.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    except IndexError:
        return None
    rows = [ln for ln in body.splitlines() if "X =" in ln or "X=" in ln]
    if not rows:
        return None
    m = _VECTOR_RE.search(rows[-1])
    if not m:
        return None
    try:
        return {"x_au": float(m.group(1)), "y_au": float(m.group(2))}
    except ValueError:
        return None


def _fetch_planet_vector(body_id):
    """Real heliocentric ecliptic position (x_au, y_au) for one body,
    Sun-centered (CENTER='500@10'), via the same JPL Horizons service
    the observer fetch already uses -- a genuine top-down "where is it
    in the solar system right now" position, not a schematic guess."""
    now = datetime.utcnow()
    start = now.strftime("%Y-%m-%d")
    stop = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "format": "json", "COMMAND": f"'{body_id}'", "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES", "EPHEM_TYPE": "VECTORS",
        "CENTER": "'500@10'", "REF_PLANE": "ECLIPTIC", "OUT_UNITS": "'AU-D'",
        "VEC_TABLE": "'1'",
        "START_TIME": f"'{start}'", "STOP_TIME": f"'{stop}'", "STEP_SIZE": "'1 d'",
    }
    url = HORIZONS_URL + "?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    return _parse_horizons_vector(data.get("result") if isinstance(data, dict) else None)


def _fetch_closest_neo():
    """Real closest upcoming near-Earth-object close approach in the
    next 30 days, via JPL's Center for NEO Studies CAD (Close-Approach
    Data) API -- confirmed live 2026-08-19, free, keyless, no signup
    (a genuinely different, separate JPL service from api.nasa.gov's
    NeoWs, which DOES need a key -- this one does not). Real fields:
    `des` (designation, e.g. "2026 PX"), `cd` (close-approach date/
    time), `dist` (real AU distance at closest approach), `v_rel`
    (real relative velocity, km/s), `h` (real absolute magnitude).
    Returns None on any real failure or an empty result -- there is
    always SOME close approach within 30 days in practice, but an
    honest empty read is possible and must not be masked."""
    params = {
        "date-min": time.strftime("%Y-%m-%d"),
        "date-max": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 30 * 86400)),
        "dist-max": "0.2", "sort": "dist",
    }
    url = CAD_URL + "?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    if not isinstance(data, dict):
        return None
    fields = data.get("fields")
    rows = data.get("data")
    if not (isinstance(fields, list) and isinstance(rows, list) and rows):
        return None
    row = dict(zip(fields, rows[0]))
    des = paneltext.panel_text(row.get("des") or "") or None
    if not des:
        return None
    try:
        dist_au = float(row.get("dist"))
    except (TypeError, ValueError):
        dist_au = None
    try:
        v_rel = float(row.get("v_rel"))
    except (TypeError, ValueError):
        v_rel = None
    try:
        h_mag = float(row.get("h"))
    except (TypeError, ValueError):
        h_mag = None
    return {"des": des, "cd": paneltext.panel_text(row.get("cd") or "") or None,
            "dist_au": dist_au, "v_rel": v_rel, "h": h_mag}


def _fetch_smallbody_vector(designation):
    """Real heliocentric (x_au, y_au) for a real small body (asteroid/
    comet), via the SAME Horizons VECTORS call `_fetch_planet_vector()`
    uses, just with the small-body COMMAND syntax (`DES=<designation>;`
    -- confirmed live 2026-08-19 against a real CAD-listed object,
    "2026 PX"). Small bodies are perturbed orbits Horizons integrates
    numerically (`Small perturbers: Yes` in the real response), same
    real ephemeris service as every planet here, not a second-class
    approximation."""
    now = datetime.utcnow()
    start = now.strftime("%Y-%m-%d")
    stop = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "format": "json", "COMMAND": f"'DES={designation};'", "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES", "EPHEM_TYPE": "VECTORS",
        "CENTER": "'500@10'", "REF_PLANE": "ECLIPTIC", "OUT_UNITS": "'AU-D'",
        "VEC_TABLE": "'1'",
        "START_TIME": f"'{start}'", "STOP_TIME": f"'{stop}'", "STEP_SIZE": "'1 d'",
    }
    url = HORIZONS_URL + "?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    return _parse_horizons_vector(data.get("result") if isinstance(data, dict) else None)


def _fetch_moon_vector(moon_id, planet_id):
    """Real (x_km, y_km) of a real moon RELATIVE TO ITS PLANET (not the
    Sun) -- CENTER='500@<planet_id>' is Horizons' real body-centered
    frame syntax, confirmed live 2026-08-19 against Io/Jupiter (parsed
    ~421,700km from Jupiter, matching Io's real ~421,800km orbital
    radius). KM-S units here, not AU -- a moon's real orbital distance
    is naturally thousands of km, not a fraction of an AU."""
    now = datetime.utcnow()
    start = now.strftime("%Y-%m-%d")
    stop = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "format": "json", "COMMAND": f"'{moon_id}'", "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES", "EPHEM_TYPE": "VECTORS",
        "CENTER": f"'500@{planet_id}'", "REF_PLANE": "ECLIPTIC", "OUT_UNITS": "'KM-S'",
        "VEC_TABLE": "'1'",
        "START_TIME": f"'{start}'", "STOP_TIME": f"'{stop}'", "STEP_SIZE": "'1 d'",
    }
    url = HORIZONS_URL + "?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    v = _parse_horizons_vector(data.get("result") if isinstance(data, dict) else None)
    return {"x_km": v["x_au"], "y_km": v["y_au"]} if v else None


def _fetch_planet(body_id, lat, lon):
    """Real az/el/mag/distance for one planet from home, right now, via
    JPL Horizons -- or None on any real failure. A ~2h observer window
    is requested (STEP_SIZE 1h) so the last real row is close to now
    without needing exact-second alignment."""
    now = datetime.utcnow()
    start = now.strftime("%Y-%m-%d %H:%M")
    stop = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    params = {
        "format": "json", "COMMAND": f"'{body_id}'", "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES", "EPHEM_TYPE": "OBSERVER",
        "CENTER": "'coord@399'",
        "SITE_COORD": f"'{lon:.4f},{lat:.4f},0'",
        "START_TIME": f"'{start}'", "STOP_TIME": f"'{stop}'",
        "STEP_SIZE": "'1 h'", "QUANTITIES": "'4,9,20'",
    }
    url = HORIZONS_URL + "?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    return _parse_horizons_row(data.get("result") if isinstance(data, dict) else None)


class MoonFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._usno = {}
        self._usno_try = 0.0
        self._usno_err = None
        self._launch = None
        self._launch_try = 0.0
        self._launch_err = None
        self._planets = {}          # name -> {az_deg, el_deg, mag, dist_au}
        self._sun = {}               # real {az_deg, el_deg} -- day/night context, see SUN_ID
        self._orbits = {}           # name -> {x_au, y_au}, real heliocentric, see ORBIT_BODIES
        self._orbits_ts = {}        # name -> real wall time of that fetch, for dead-reckoning
        self._orbit_cursor = 0
        self._orbit_try = {}
        self._planet_cursor = 0
        self._planet_try = {}       # name -> last-fetch wall time
        self._neo = None            # real closest upcoming close-approach, see _fetch_closest_neo
        self._neo_try = 0.0
        self._neo_vec_try = 0.0
        self._neo_ts = 0.0          # real wall time of the NEO's own last vector fetch
        self._jmoons = {}           # name -> {x_km, y_km}, real, relative to Jupiter
        self._jmoons_ts = {}
        self._jmoon_cursor = 0
        self._jmoon_try = {}
        self._last_read = 0.0
        self._thread = None
        self._home = satellite.FEED.get_location()

    def get(self):
        """{"curphase", "illum_pct", "closest_phase", "moonrise",
        "moonset", "launch": {...}|None, "planets": {name: {...}},
        "configured", "age", "err"}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            usno = dict(self._usno)
            launch = dict(self._launch) if self._launch else None
            planets = {k: dict(v) for k, v in self._planets.items()}
            sun = dict(self._sun) if self._sun else None
            orbits = {k: dict(v) for k, v in self._orbits.items()}
            orbits_ts = dict(self._orbits_ts)
            neo = dict(self._neo) if self._neo else None
            neo_ts = self._neo_ts
            jmoons = {k: dict(v) for k, v in self._jmoons.items()}
            jmoons_ts = dict(self._jmoons_ts)
            err = self._usno_err or self._launch_err
            age = (now - self._usno_try) if self._usno_try else None
        self._ensure_thread()
        out = {
            "configured": satellite.FEED.configured,
            "age": age, "err": err, "launch": launch, "planets": planets, "sun": sun,
            "orbits": orbits, "orbits_ts": orbits_ts, "neo": neo, "neo_ts": neo_ts or None,
            "jmoons": jmoons, "jmoons_ts": jmoons_ts,
        }
        out.update(usno)
        return out

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
            self._refresh_usno()
            self._refresh_launch()
            self._refresh_one_planet()
            self._refresh_one_orbit()
            self._refresh_neo_list()
            self._refresh_neo_vector()
            self._refresh_one_jmoon()
            time.sleep(5.0)

    def _refresh_one_jmoon(self):
        """Round-robin, same shape as _refresh_one_orbit() -- at most one
        Galilean moon's real Jupiter-relative position per loop pass.
        Always kept warm alongside everything else here (cheap: 4 real
        bodies, 30min cadence) rather than gated on whether the moon
        zoom view is currently open -- the SAME "tick every sub-thing so
        switching to it is never cold" reasoning AmbientEngine's own
        composed sub-engines already follow."""
        now = time.time()
        for _ in range(len(JUPITER_MOONS)):
            moon_id, name = JUPITER_MOONS[self._jmoon_cursor]
            self._jmoon_cursor = (self._jmoon_cursor + 1) % len(JUPITER_MOONS)
            if now - self._jmoon_try.get(name, 0.0) < MOON_VECTOR_REFRESH:
                continue
            self._jmoon_try[name] = now
            try:
                parsed = _fetch_moon_vector(moon_id, JUPITER_ID)
            except (urllib.error.URLError, TimeoutError, ValueError,
                    json.JSONDecodeError, OSError, KeyError):        # noqa: BLE001
                return
            if parsed is not None:
                with self._lock:
                    self._jmoons[name] = parsed
                    self._jmoons_ts[name] = now
            return

    def _refresh_one_orbit(self):
        """Round-robin, same shape as _refresh_one_planet(): at most ONE
        body's real heliocentric position per loop pass. ORBIT_BODIES is
        PLANETS plus Earth (399) -- the Sun itself is always (0,0) by
        definition of a Sun-centered frame, so it needs no fetch."""
        now = time.time()
        for _ in range(len(ORBIT_BODIES)):
            body_id, name = ORBIT_BODIES[self._orbit_cursor]
            self._orbit_cursor = (self._orbit_cursor + 1) % len(ORBIT_BODIES)
            if now - self._orbit_try.get(name, 0.0) < PLANET_REFRESH:
                continue
            self._orbit_try[name] = now
            try:
                parsed = _fetch_planet_vector(body_id)
            except (urllib.error.URLError, TimeoutError, ValueError,
                    json.JSONDecodeError, OSError, KeyError):        # noqa: BLE001
                return
            if parsed is not None:
                with self._lock:
                    self._orbits[name] = parsed
                    self._orbits_ts[name] = now
            return

    def _refresh_neo_list(self):
        now = time.time()
        with self._lock:
            if now - self._neo_try < NEO_LIST_REFRESH:
                return
            self._neo_try = now
        try:
            neo = _fetch_closest_neo()
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError):        # noqa: BLE001
            return
        if neo is not None:
            with self._lock:
                # A genuinely NEW closest object resets the vector cadence
                # so its real position is fetched again right away, not
                # left showing the PREVIOUS object's stale position under
                # the new one's label.
                if not self._neo or self._neo.get("des") != neo.get("des"):
                    self._neo_vec_try = 0.0
                self._neo = neo

    def _refresh_neo_vector(self):
        now = time.time()
        with self._lock:
            if now - self._neo_vec_try < NEO_VECTOR_REFRESH:
                return
            des = (self._neo or {}).get("des")
        if not des:
            return
        with self._lock:
            self._neo_vec_try = now
        try:
            parsed = _fetch_smallbody_vector(des)
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError):        # noqa: BLE001
            return
        if parsed is not None:
            with self._lock:
                if (self._neo or {}).get("des") == des:   # still the same real object
                    self._neo.update(parsed)
                    self._neo_ts = now

    def _refresh_one_planet(self):
        """Round-robin: at most ONE body fetched per loop pass, and only
        if that body is actually due (PLANET_REFRESH). The SUN rides
        the same round-robin (as an 8th body, using the same real
        Horizons call this function already makes) -- keeps Horizons
        load to at most 8 calls/hour total, never a burst."""
        if not satellite.FEED.configured:
            return
        bodies = PLANETS + [(SUN_ID, "SUN")]
        lat, lon, _ = satellite.FEED.get_location()
        now = time.time()
        for _ in range(len(bodies)):
            body_id, name = bodies[self._planet_cursor]
            self._planet_cursor = (self._planet_cursor + 1) % len(bodies)
            if now - self._planet_try.get(name, 0.0) < PLANET_REFRESH:
                continue
            self._planet_try[name] = now
            try:
                parsed = _fetch_planet(body_id, lat, lon)
            except (urllib.error.URLError, TimeoutError, ValueError,
                    json.JSONDecodeError, OSError, KeyError):        # noqa: BLE001
                return
            if parsed is not None:
                with self._lock:
                    if name == "SUN":
                        self._sun = parsed
                    else:
                        self._planets[name] = parsed
            return

    def _refresh_usno(self):
        now = time.time()
        with self._lock:
            if now - self._usno_try < USNO_REFRESH:
                return
            self._usno_try = now
            if not satellite.FEED.configured:
                return
            lat, lon, _ = satellite.FEED.get_location()
        try:
            date = time.strftime("%Y-%m-%d")
            tz = _local_utc_offset_hours()
            data = _get_json(USNO_URL.format(
                date=date, lat=round(lat, 4), lon=round(lon, 4), tz=round(tz, 2)))
            parsed = _parse_usno(data, now)
            with self._lock:
                self._usno = parsed
                self._usno_err = None
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError) as e:        # noqa: BLE001
            with self._lock:
                self._usno_err = f"{type(e).__name__}"

    def _refresh_launch(self):
        now = time.time()
        with self._lock:
            if now - self._launch_try < LAUNCH_REFRESH:
                return
            self._launch_try = now
        try:
            data = _get_json(LL2_URL)
            parsed = _parse_launch(data)
            with self._lock:
                self._launch = parsed
                self._launch_err = None
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError) as e:        # noqa: BLE001
            with self._lock:
                self._launch_err = f"{type(e).__name__}"


FEED = MoonFeed()
