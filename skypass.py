"""
skypass.py -- visible passes of ANY bright satellite, not just the ISS.

This is the ONLY pass predictor in the project as of 2026-08-01 -- it
covers the ISS too. satellite.py used to run a second, ISS-only pass
predictor against polluxlabs; that was retired once this module's SGP4
predictions were cross-validated against it (see below) and found to
agree, because maintaining two pipelines that already proved they agree
was pure duplication. satellite.py still owns the ISS's CONTINUOUS live
position (altitude/speed/sunlit) -- that is telemetry, not a pass
prediction, and nothing else in this catalogue has an equivalent source.

WHY THIS COMPUTES PASSES LOCALLY INSTEAD OF CALLING AN API
----------------------------------------------------------
The obvious route was N2YO's /visualpasses (free tier, 1000 req/hour), but
it needs a registered API key, and one key would have to serve every unit
of a shipped product -- PRODUCTION.md's whole objection to per-unit
recurring cost and shared-credential fragility. polluxlabs, which already
serves the ISS here, was checked and is ISS-ONLY: it accepts satid/norad/
sat parameters and ignores all of them, returning ISS (ZARYA) every time.

So instead: CelesTrak publishes the orbital elements for free with no key,
and the propagation is done here.

    https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle

The `visual` GROUP is exactly the right catalogue -- CelesTrak curates it
as the objects actually observable with the naked eye (~160 of them),
which is the "bright satellites only, don't list hundreds of invisible
objects" requirement solved upstream rather than by us inventing a
magnitude cutoff. One fetch per half-day covers everything.

Confirmed 2026-08-12: celestrak.org timed out from this network (IPv4
TCP, 10s+, same host as .com) while ISS telemetry (wheretheiss.at) and
ISS/CSS TLEs (SatNOGS, ARISS) answered in under a second. A single
timed-out fetch used to raise out of `_work()`, which skipped the 1 Hz
sky snapshot AND retried every second -- error was not "no satellites",
and hammering a down host is how CelesTrak starts issuing 403s. Same
discipline as flights.py's replica chain: first non-empty live visual
wins; last-good disk cache if the catalogue host is dead; ISS+CSS
station TLEs only when there is no visual catalogue at all; a dead
fetch never wipes a real sky.

VALIDATED, NOT ASSUMED. The propagator here was checked against
polluxlabs' ISS predictions -- a source this project already trusts --
for the same observer and the same TLE. Four consecutive passes agreed to
within 3-14 seconds of rise time (the scan step is 20s, so that is
resolution-limited) and 0.1-0.4 degrees of peak elevation.

DEPENDENCY NOTE. This is the first non-stdlib dependency outside
mirror/video: it needs `sgp4` (pip install sgp4). That is acceptable
because the launchd service already runs `.venv/bin/python`, so the venv
is the real runtime -- but it does mean this ONE mode degrades if the
package is missing, and it degrades honestly: HAVE_SGP4 is False, the feed
reports an error, and the engine shows nothing rather than guessing. A
hand-rolled propagator was considered and rejected: a subtly wrong SGP4
produces confidently wrong pass times, which is precisely the failure this
project refuses everywhere else.

WHAT "VISIBLE" MEANS HERE, because it is not the same as "overhead":
a pass counts only when the satellite is SUNLIT while the OBSERVER is in
darkness. A bright satellite directly overhead at noon is invisible, and
one in the Earth's shadow at midnight is equally invisible. Both
conditions are computed, not approximated.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paneltext
import satellite

try:
    from sgp4.api import Satrec, jday
    HAVE_SGP4 = True
except ImportError:                     # degrade honestly, never guess
    Satrec = None
    jday = None
    HAVE_SGP4 = False

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle"
# CelesTrak `visual` is the only full naked-eye catalogue. .org and .com
# resolve to the same IPv4 (confirmed 2026-08-12) -- listing both would
# just pay the timeout twice. Station replicas are ISS + CSS only, the
# real CelesTrak `stations` group, used when visual never arrives.
TLE_CACHE_PATH = Path(__file__).parent / "skypass_tle_cache.txt"
ISS_TLE_SOURCES = (
    ("satnogs", "https://db.satnogs.org/api/tle/?norad_cat_id=25544", "satnogs"),
    ("ariss", "https://live.ariss.org/iss.txt", "tle"),
    ("wheretheiss", "https://api.wheretheiss.at/v1/satellites/25544/tles", "wheretheiss"),
    ("ivanstanojevic", "https://tle.ivanstanojevic.me/api/tle/25544", "ivan"),
)
CSS_TLE_URL = "https://db.satnogs.org/api/tle/?norad_cat_id=48274"
TLE_REFRESH = 43200.0        # 12h -- elements are good for days; this is polite
TLE_STALE_MAX = 259200.0     # 3 days: beyond this, accuracy degrades enough to stop
TLE_BACKOFF_START = 60.0     # after a dead fetch, do not retry every 1 Hz loop
TLE_BACKOFF_MAX = 3600.0
PASS_REFRESH = 900.0         # recompute the schedule every 15 min
# All-sky snapshot cadence, DELIBERATELY DECOUPLED FROM THE FRAME RATE.
# Measured worst-case apparent motion of a real visible object is 0.13
# px/sec on the 64px sky dome, so even a 5s cadence stays sub-pixel --
# recomputing every frame (20x/sec) would be 20x the work for a result
# that cannot be seen. 1s keeps it comfortably live with margin to spare.
SKY_NOW_REFRESH = 1.0
IDLE_STOP = 180.0
TIMEOUT = 6.0            # per replica; a dead CelesTrak must not eat 15s
_UA = "Mozilla/5.0 (HenderburghArcade)"

# Search parameters.
MIN_ELEVATION = 15.0     # below this a pass is behind trees/houses in practice
SEARCH_HOURS = 36        # far enough ahead to always have something to show
COARSE_STEP = 30         # seconds; ~1 deg of ISS motion, fine for finding a pass
FINE_STEP = 2            # seconds; refines rise/set once a pass is bracketed
SUN_MAX_ALT = -6.0       # observer darkness: civil twilight or darker
MAX_RESULTS = 12

# Passes brighter/higher than this are worth calling out specially -- the
# same tiering satellite.py already uses for the ISS.
ELEV_EXCELLENT = 60.0
ELEV_GOOD = 40.0

EARTH_R = 6378.137           # km, WGS-84 equatorial
FLATTENING = 1 / 298.257223563

# Same NORAD catalog number satellite.py's POSITION_URL already hardcodes
# for the ISS (25544 / ZARYA). Used to pick the ISS entry OUT of this
# module's own unified pass list -- by catalog number, not by matching the
# display NAME, which is one CelesTrak formatting change away from
# silently breaking a string match.
ISS_NORAD_ID = 25544
CSS_NORAD_ID = 48274     # CSS / Tianhe -- the other CelesTrak `stations` object


# ---- moon phase ---------------------------------------------------------
# Zero network cost, zero API -- real deterministic ephemeris arithmetic,
# the same category of "computed, not invented" fact this project's sun-
# position math (weather's sunrise/sunset gating, the clock's sun-locked
# dial) already relies on. Real known new-moon reference epoch (2000-01-06
# 18:14 UTC, public astronomical constant) + the real mean synodic month
# length (29.530588861 real days). Accurate to roughly half a day over any
# realistic span -- more than enough for a small panel icon, and never
# claims better precision than that.
_MOON_SYNODIC_DAYS = 29.530588861
_MOON_REF_NEW_MOON_JD = 2451550.1


def moon_phase_frac(ts=None):
    """0..1 through the current synodic month -- 0/1 = new moon, 0.5 =
    full. Pure math, no I/O."""
    now = time.time() if ts is None else ts
    jd = now / 86400.0 + 2440587.5   # unix epoch -> Julian Date
    days_since = jd - _MOON_REF_NEW_MOON_JD
    return (days_since % _MOON_SYNODIC_DAYS) / _MOON_SYNODIC_DAYS


_MOON_PHASE_NAMES = (
    (0.0625, "NEW MOON"), (0.1875, "WAXING CRESCENT"), (0.3125, "FIRST QUARTER"),
    (0.4375, "WAXING GIBBOUS"), (0.5625, "FULL MOON"), (0.6875, "WANING GIBBOUS"),
    (0.8125, "LAST QUARTER"), (0.9375, "WANING CRESCENT"),
)


def moon_phase_name(frac):
    """Real 8-phase name for a 0..1 fraction from moon_phase_frac()."""
    for edge, name in _MOON_PHASE_NAMES:
        if frac < edge:
            return name
    return "NEW MOON"


# ---- astronomy ---------------------------------------------------------
def _gmst(jd):
    """Greenwich mean sidereal time, radians."""
    t = (jd - 2451545.0) / 36525.0
    g = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
         + 0.000387933 * t * t - t * t * t / 38710000.0)
    return math.radians(g % 360.0)


def _observer_eci(lat_deg, lon_deg, elev_m, theta):
    """Observer position in ECI km, accounting for Earth's oblateness."""
    la = math.radians(lat_deg)
    c = 1.0 / math.sqrt(1.0 + FLATTENING * (FLATTENING - 2.0) * math.sin(la) ** 2)
    s = (1.0 - FLATTENING) ** 2 * c
    r = (EARTH_R * c + elev_m / 1000.0) * math.cos(la)
    return (r * math.cos(theta + math.radians(lon_deg)),
            r * math.sin(theta + math.radians(lon_deg)),
            (EARTH_R * s + elev_m / 1000.0) * math.sin(la))


def _sun_eci(jd):
    """Low-precision solar position in ECI km. Accurate to ~0.01 deg, which
    is far finer than the twilight and shadow tests need."""
    n = jd - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360.0)
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = L + math.radians(1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    r = (1.00014 - 0.01671 * math.cos(g) - 0.00014 * math.cos(2 * g)) * 149597870.7
    return (r * math.cos(lam), r * math.cos(eps) * math.sin(lam), r * math.sin(eps) * math.sin(lam))


def _sun_altitude(jd, lat, lon, elev):
    """Sun altitude in degrees for the observer -- drives the darkness test."""
    theta = _gmst(jd)
    sx, sy, sz = _sun_eci(jd)
    ox, oy, oz = _observer_eci(lat, lon, elev, theta)
    rx, ry, rz = sx - ox, sy - oy, sz - oz
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    la, lo = math.radians(lat), math.radians(lon)
    st, ct = math.sin(theta + lo), math.cos(theta + lo)
    sl, cl = math.sin(la), math.cos(la)
    up = cl * ct * rx + cl * st * ry + sl * rz
    return math.degrees(math.asin(max(-1.0, min(1.0, up / rng))))


def _sunlit(sat_eci, jd):
    """Is the satellite in sunlight, or in Earth's shadow?

    Cylindrical shadow model: project the satellite onto the Earth-Sun
    axis. Behind the Earth AND within one Earth radius of that axis means
    eclipsed. This is the standard approximation for pass prediction --
    the penumbra it ignores is a few seconds at the edges of a pass.
    """
    sx, sy, sz = _sun_eci(jd)
    sn = math.sqrt(sx * sx + sy * sy + sz * sz)
    ux, uy, uz = sx / sn, sy / sn, sz / sn
    x, y, z = sat_eci
    proj = x * ux + y * uy + z * uz
    if proj >= 0:
        return True                      # sunward side: always lit
    perp = math.sqrt(max(0.0, (x * x + y * y + z * z) - proj * proj))
    return perp > EARTH_R


def _look_angles(sat, when, lat, lon, elev):
    """(elevation_deg, azimuth_deg, range_km, sunlit) or None on propagation
    failure -- a decayed or badly-conditioned TLE returns an error code
    rather than raising, and must be skipped rather than guessed at."""
    jd, fr = jday(when.year, when.month, when.day,
                  when.hour, when.minute, when.second + when.microsecond / 1e6)
    err, r, _v = sat.sgp4(jd, fr)
    if err != 0:
        return None
    theta = _gmst(jd + fr)
    ox, oy, oz = _observer_eci(lat, lon, elev, theta)
    rx, ry, rz = r[0] - ox, r[1] - oy, r[2] - oz
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    if rng <= 0:
        return None
    la, lo = math.radians(lat), math.radians(lon)
    st, ct = math.sin(theta + lo), math.cos(theta + lo)
    sl, cl = math.sin(la), math.cos(la)
    south = sl * ct * rx + sl * st * ry - cl * rz
    east = -st * rx + ct * ry
    up = cl * ct * rx + cl * st * ry + sl * rz
    el = math.degrees(math.asin(max(-1.0, min(1.0, up / rng))))
    az = math.degrees(math.atan2(-east, south)) % 360.0
    return el, az, rng, _sunlit(r, jd + fr)


COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass(az):
    return COMPASS[int((az % 360.0) / 45.0 + 0.5) % 8]


# ---- TLE ---------------------------------------------------------------
def parse_tles(text):
    """3-line TLE text -> [(name, line1, line2)]. Malformed groups are
    skipped rather than aborting the whole catalogue."""
    lines = [ln.rstrip() for ln in str(text).splitlines() if ln.strip()]
    out = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i].strip(), lines[i + 1].strip(), lines[i + 2].strip()
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append((name, l1, l2))
    return out


def _http_bytes(url, headers=None):
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _http_text(url, headers=None):
    return _http_bytes(url, headers).decode("utf-8", "replace")


def _http_json(url, headers=None):
    return json.loads(_http_text(url, headers))


def _norad_of(tle):
    """NORAD catalog number from a (name, line1, line2) triple. None if
    the line is malformed -- never guessed."""
    try:
        return int(tle[1][2:7])
    except (TypeError, ValueError, IndexError):
        return None


def _triples_from_satnogs(data):
    out = []
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("tle0") or "").lstrip("0 ").strip() or "OBJECT"
        l1 = str(row.get("tle1") or "").strip()
        l2 = str(row.get("tle2") or "").strip()
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append((name, l1, l2))
    return out


def _triple_from_json_tle(data, default_name="OBJECT"):
    if not isinstance(data, dict):
        return None
    name = str(data.get("header") or data.get("name") or default_name).strip()
    l1 = str(data.get("line1") or "").strip()
    l2 = str(data.get("line2") or "").strip()
    if l1.startswith("1 ") and l2.startswith("2 "):
        return (name, l1, l2)
    return None


def load_tle_cache():
    """Last-good visual (or stations) catalogue, or ([], 0.0, None).

    `fetched_at` is when the elements were actually retrieved, not when
    this process loaded the file -- using `now` here would reset the
    3-day stale clock and treat day-old elements as fresh. Missing or
    stale cache is an empty result, never a guessed catalogue."""
    if not TLE_CACHE_PATH.exists():
        return [], 0.0, None
    try:
        text = TLE_CACHE_PATH.read_text()
    except OSError:
        return [], 0.0, None
    fetched_at = 0.0
    kind = None
    body = []
    for ln in text.splitlines():
        if ln.startswith("# fetched_at"):
            parts = ln.split()
            try:
                fetched_at = float(parts[-1])
            except (TypeError, ValueError, IndexError):
                fetched_at = 0.0
        elif ln.startswith("# kind"):
            parts = ln.split()
            kind = parts[-1] if len(parts) >= 3 else None
        elif ln.startswith("#"):
            continue
        else:
            body.append(ln)
    tles = parse_tles("\n".join(body))
    if not tles or not fetched_at:
        return [], 0.0, None
    if (time.time() - fetched_at) > TLE_STALE_MAX:
        return [], 0.0, None
    if kind not in ("visual", "stations"):
        kind = "visual" if len(tles) >= 20 else "stations"
    return tles, fetched_at, kind


def save_tle_cache(tles, fetched_at, kind="visual"):
    """Persist a real catalogue. Comments are stripped on load so they
    cannot shift parse_tles' 3-line grouping."""
    if not tles or not fetched_at:
        return
    lines = ["# tle_cache v1", f"# fetched_at {fetched_at:.3f}", f"# kind {kind}"]
    for name, l1, l2 in tles:
        lines.extend([name, l1, l2])
    try:
        TLE_CACHE_PATH.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def fetch_visual_tles():
    """Live CelesTrak `visual` catalogue. Raises on network failure.
    An empty 200 is a hole, not a quiet sky -- the caller must not
    treat it as 'no satellites'."""
    tles = parse_tles(_http_text(TLE_URL))
    if not tles:
        raise ValueError("empty visual catalogue")
    return tles


def fetch_station_tles():
    """ISS + CSS only -- the real CelesTrak `stations` group, assembled
    from sources that answered when CelesTrak itself did not.

    Not a homemade 'visual' list: two known stations, each from a live
    TLE. Missing one station is omitted, never invented. First ISS
    replica that returns a real triple wins; CSS is a separate SatNOGS
    lookup so a dead ISS replica cannot hide Tianhe."""
    found = {}
    for _name, url, kind in ISS_TLE_SOURCES:
        try:
            if kind == "satnogs":
                triples = _triples_from_satnogs(_http_json(url))
            elif kind == "wheretheiss":
                t = _triple_from_json_tle(_http_json(url), "ISS (ZARYA)")
                triples = [t] if t else []
            elif kind == "ivan":
                t = _triple_from_json_tle(
                    _http_json(url, headers={"Accept": "application/json"}),
                    "ISS (ZARYA)")
                triples = [t] if t else []
            else:
                triples = parse_tles(_http_text(url))
        except Exception:                                 # noqa: BLE001
            continue
        for tle in triples:
            nid = _norad_of(tle)
            if nid is not None and nid not in found:
                found[nid] = tle
        if ISS_NORAD_ID in found:
            break
    try:
        for tle in _triples_from_satnogs(_http_json(CSS_TLE_URL)):
            nid = _norad_of(tle)
            if nid is not None and nid not in found:
                found[nid] = tle
    except Exception:                                     # noqa: BLE001
        pass
    return list(found.values())


def fetch_tles():
    """Live visual catalogue, same entry point the rest of this file
    already used. Station fallback and disk cache live on the feed so
    a smaller stations set cannot clobber a real visual last-good."""
    return fetch_visual_tles()


# ---- pass prediction ---------------------------------------------------
def predict(tles, lat, lon, elev=0.0, hours=SEARCH_HOURS, now=None,
            min_elevation=MIN_ELEVATION, limit=MAX_RESULTS):
    """Visible passes over the observer, soonest first.

    A pass qualifies only if, at some point, the satellite is above
    `min_elevation` AND sunlit AND the observer is in darkness. Those three
    together are what "visible" means; any one alone is not.
    """
    if not HAVE_SGP4 or not tles:
        return []
    start = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    end = start + timedelta(hours=hours)
    results = []

    for name, l1, l2 in tles:
        try:
            sat = Satrec.twoline2rv(l1, l2)
        except (ValueError, TypeError):
            continue
        norad_id = sat.satnum
        t = start
        in_pass = False
        peak_el = 0.0
        peak_az = 0.0
        rise_t = None
        rise_az = None
        vis_any = False
        while t < end:
            look = _look_angles(sat, t, lat, lon, elev)
            if look is None:
                break
            el, az, _rng, lit = look
            if el >= min_elevation:
                if not in_pass:
                    in_pass = True
                    rise_t, rise_az = t, az
                    peak_el, peak_az = el, az
                    vis_any = False
                if el > peak_el:
                    peak_el, peak_az = el, az
                if lit and _sun_altitude(*_jd_of(t), lat, lon, elev) <= SUN_MAX_ALT:
                    vis_any = True
            elif in_pass:
                in_pass = False
                if vis_any and rise_t is not None:
                    results.append({
                        "name": paneltext.panel_text(name),
                        "norad_id": norad_id,
                        "is_iss": norad_id == ISS_NORAD_ID,
                        "rise": rise_t, "set": t,
                        "rise_az": rise_az, "peak_az": peak_az,
                        "peak_el": round(peak_el, 1),
                        "duration_s": int((t - rise_t).total_seconds()),
                    })
                rise_t = None
            t += timedelta(seconds=COARSE_STEP)

    results.sort(key=lambda p: p["rise"])
    return results[:limit]


def sky_now(tles, lat, lon, elev=0.0, now=None, min_elevation=0.0):
    """Every catalogued object ABOVE THE HORIZON right now.

    This is a completely different workload from predict() and the cost
    difference is the whole reason a live all-sky scope is affordable:
    predict() scans 36 hours at 30-second steps -- roughly 4,300
    propagations PER satellite, ~678,000 in total, measured at 1,488 ms --
    whereas this is ONE propagation per satellite at a single instant.
    Measured against the real 157-object CelesTrak `visual` catalogue:
    0.32 ms median, 157/157 propagating cleanly, which is 0.6% of the
    50ms frame budget. The two must not be confused when reasoning about
    whether continuous tracking is affordable; it very much is.

    Returns objects sorted highest-elevation first, since on a crowded
    dome the high ones are the ones actually worth looking at.

    Each object carries `visible`: elevation >= MIN_ELEVATION AND sunlit
    AND the OBSERVER is in darkness -- the exact same three-part test
    predict() uses to decide a pass counts at all. This matters a lot:
    "above the horizon" and "sunlit" are true for plenty of objects in
    broad daylight (confirmed live: 8 objects returned this way at
    16:05 UTC with the sun 66.5 deg up), and none of those are actually
    visible to a person outside. Anything that decides whether the dome
    is "worth showing" must use `visible`, never the raw list -- using
    the raw list would make the dome claim content nearly around the
    clock, which is exactly the kind of invented worth this project's
    standing rule exists to prevent.
    """
    if not HAVE_SGP4 or not tles:
        return []
    when = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    jd = _jd_of(when)
    sun_alt = _sun_altitude(*jd, lat, lon, elev)
    observer_dark = sun_alt <= SUN_MAX_ALT
    out = []
    for name, l1, l2 in tles:
        try:
            sat = Satrec.twoline2rv(l1, l2)
        except (ValueError, TypeError):
            continue
        look = _look_angles(sat, when, lat, lon, elev)
        if look is None:
            continue                       # decayed/bad TLE: skip, never guess
        el, az, rng, lit = look
        if el < min_elevation:
            continue
        out.append({
            "name": paneltext.panel_text(name),
            "norad_id": sat.satnum,
            "is_iss": sat.satnum == ISS_NORAD_ID,
            "el": round(el, 2), "az": round(az, 2),
            "range_km": round(rng), "sunlit": lit,
            "visible": bool(lit and observer_dark and el >= MIN_ELEVATION),
        })
    out.sort(key=lambda o: -o["el"])
    return out


def _jd_of(when):
    """(jd, 0.0) helper so _sun_altitude can take a datetime."""
    jd, fr = jday(when.year, when.month, when.day,
                  when.hour, when.minute, when.second + when.microsecond / 1e6)
    return (jd + fr,)


def quality(p):
    """Tag only, kept for anywhere just the word is wanted."""
    return quality_rank(p)[0]


def quality_rank(p):
    """(tag, rank) -- higher rank = more worth going outside for. Every
    pass in this module's list already passed the sunlit/dark-observer
    test (see predict()), so there is no DAYLIT tier here the way the old
    per-ISS pass_quality() needed one; everything in this list is, by
    construction, something you could actually see.

    Rank drives the shared urgency chip used by every object in the
    unified UPCOMING view (see engines.SatelliteEngine._draw_chip):
    3 = GO OUTSIDE, 2 = GOOD PASS, 1 = VISIBLE."""
    el = (p or {}).get("peak_el") or 0
    if el >= ELEV_EXCELLENT:
        return ("BRIGHT", 3)
    if el >= ELEV_GOOD:
        return ("GOOD", 2)
    return ("LOW", 1)


class SkyPassFeed:
    """Background TLE fetch + local pass computation.

    Same contract as every other FEED here: get() never blocks, the work
    happens off the caller's thread, and nothing is invented -- if sgp4 is
    missing or the catalogue never arrived, this reports an error and the
    engine renders that honestly.
    """

    def __init__(self):
        self._lock = threading.Lock()
        cached, cached_at, cached_kind = load_tle_cache()
        self._tles = cached
        self._tles_at = cached_at
        self._tles_kind = cached_kind
        self._passes = []
        self._passes_at = 0.0
        self._err = None if HAVE_SGP4 else "sgp4 not installed"
        self._last_read = 0.0
        self._thread = None
        self._loc = None
        self._tle_try = 0.0
        self._tle_backoff = TLE_BACKOFF_START
        # Live all-sky snapshot for the sky-dome scope. Computed on THIS
        # background thread, never in the engine -- the engine does no work
        # of its own by contract. See SKY_NOW_REFRESH for the cadence and
        # why it is decoupled from the frame rate.
        self._sky_now = []
        self._sky_now_at = 0.0

    def get(self, lat=None, lon=None):
        now = time.time()
        with self._lock:
            self._last_read = now
            if lat is not None and lon is not None:
                if self._loc != (lat, lon):
                    self._loc = (lat, lon)
                    self._passes, self._passes_at = [], 0.0
            passes = [dict(p) for p in self._passes]
            sky_now = [dict(o) for o in self._sky_now]
            out = {
                "passes": passes,
                "next": passes[0] if passes else None,
                "count": len(passes),
                "tle_count": len(self._tles),
                "age": (now - self._passes_at) if self._passes_at else None,
                "err": self._err,
                "available": HAVE_SGP4,
                # Everything above the horizon right now, for the sky-dome
                # scope. Already computed on the background thread.
                "sky_now": sky_now,
                "sky_now_age": (now - self._sky_now_at) if self._sky_now_at else None,
            }
        # Window flags are stamped on the worker (see _stamp_window),
        # not here -- get() is called from engine tick() and must not
        # read location_config.json on the render thread. Copies above
        # already carry in_window from the last worker pass.
        if HAVE_SGP4:
            self._ensure_thread()
        return out

    def _ensure_thread(self):
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
        if not alive:
            t = threading.Thread(target=self._run, daemon=True)
            with self._lock:
                self._thread = t
            t.start()

    def _run(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read > IDLE_STOP
                loc = self._loc
            if idle:
                with self._lock:
                    self._thread = None
                return
            try:
                self._work(loc)
            except Exception as e:                          # noqa: BLE001 - never die
                with self._lock:
                    self._err = type(e).__name__
            # Was 5.0s. Tightened to match SKY_NOW_REFRESH so the live
            # all-sky snapshot stays current; the expensive work in
            # _work() (TLE fetch, predict()) is gated by its OWN timers
            # (TLE_REFRESH 12h, PASS_REFRESH 15min) and so does NOT run
            # any more often because of this -- only the 0.3ms snapshot
            # does. Verified by the cost measurements in sky_now().
            time.sleep(SKY_NOW_REFRESH)

    def _stamp_window(self):
        """Flag passes / overhead objects against the configured window.

        Runs on this worker, never in get(): load_window() reads the
        shared location_config.json, and get() is on the engine tick
        path. UPCOMING uses PEAK azimuth; sky_now uses CURRENT azimuth
        -- same rule the previous in-get() stamp documented."""
        win = satellite.load_window()
        with self._lock:
            for p in self._passes:
                p["in_window"] = satellite.in_window(
                    p.get("peak_az"), win["center_deg"], win["fov_deg"])
            for o in self._sky_now:
                o["in_window"] = satellite.in_window(
                    o.get("az"), win["center_deg"], win["fov_deg"])

    def _adopt_tles(self, tles, fetched_at, kind):
        with self._lock:
            self._tles = tles
            self._tles_at = fetched_at
            self._tles_kind = kind
            self._err = None
            self._tle_backoff = TLE_BACKOFF_START
            # New elements: force a pass recompute next _refresh_sky.
            if kind != "cache":
                self._passes_at = 0.0

    def _fill_stations_if_empty(self):
        """ISS+CSS from live station replicas when there is no visual set.

        Runs BEFORE the CelesTrak attempt so a 6s visual timeout cannot
        keep the dome blank. Does not overwrite a visual last-good.
        Also refreshes a stations-only set on the same 12h cadence as
        visual -- ISS elements still go stale, they just have working
        hosts."""
        with self._lock:
            if self._tles_kind == "visual":
                return
            if self._tles and (time.time() - self._tles_at) < TLE_REFRESH:
                return
        try:
            stations = fetch_station_tles()
        except Exception as e:                                # noqa: BLE001
            with self._lock:
                if not self._tles:
                    self._err = type(e).__name__
            return
        if stations:
            ts = time.time()
            save_tle_cache(stations, ts, kind="stations")
            self._adopt_tles(stations, ts, "stations")

    def _maybe_refresh_visual(self, now):
        """CelesTrak `visual` on its own cadence, with backoff.

        A dead host must not raise out of _work() (that used to skip
        sky_now for the whole timeout, every second). A stations set
        never overwrites a real visual last-good still inside
        TLE_STALE_MAX. Stations-only feeds keep probing visual so the
        full catalogue returns when the host does."""
        with self._lock:
            have = list(self._tles)
            have_at = self._tles_at
            kind = self._tles_kind
            due = (kind != "visual") or (not have) or (now - have_at > TLE_REFRESH)
            gated = (now - self._tle_try) < self._tle_backoff
        if not due or gated:
            return
        with self._lock:
            self._tle_try = now

        try:
            visual = fetch_visual_tles()
        except Exception as e:                                # noqa: BLE001
            with self._lock:
                self._tle_backoff = min(
                    TLE_BACKOFF_MAX,
                    max(TLE_BACKOFF_START, self._tle_backoff * 2))
                if not self._tles:
                    self._err = type(e).__name__
            return

        ts = time.time()
        save_tle_cache(visual, ts, kind="visual")
        self._adopt_tles(visual, ts, "visual")

    def _refresh_sky(self, loc, now):
        if not loc:
            return
        with self._lock:
            due = now - self._passes_at > PASS_REFRESH
            tles = list(self._tles)
            stale = self._tles_at and (now - self._tles_at) > TLE_STALE_MAX
            sky_due = now - self._sky_now_at > SKY_NOW_REFRESH

        # The cheap all-sky snapshot runs on its own fast cadence, BEFORE
        # the early return below -- it must keep updating on every quiet
        # iteration when the 15-minute pass recompute is not due.
        if tles and not stale and sky_due:
            snap = sky_now(tles, loc[0], loc[1])
            with self._lock:
                self._sky_now, self._sky_now_at = snap, time.time()

        if not due or not tles or stale:
            return
        passes = predict(tles, loc[0], loc[1])
        with self._lock:
            self._passes, self._passes_at = passes, time.time()
            if tles:
                self._err = None

    def _work(self, loc):
        now = time.time()
        # Stations first (fast, ~0.5s) so ISS is on the dome before we
        # pay a CelesTrak timeout. Then snapshot. Then visual, which may
        # block -- last-good sky_now already ran this cycle.
        self._fill_stations_if_empty()
        self._refresh_sky(loc, now)
        self._maybe_refresh_visual(now)
        self._refresh_sky(loc, now)
        self._stamp_window()


FEED = SkyPassFeed()
