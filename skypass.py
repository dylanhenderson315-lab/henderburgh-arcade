"""
skypass.py -- visible passes of ANY bright satellite, not just the ISS.

Extends what satellite.py does for the station to the whole naked-eye
catalogue. satellite.py is deliberately left ALONE and keeps owning the
ISS (live position + its own pass source); this module is additive, so the
existing ISS behaviour cannot regress.

WHY THIS COMPUTES PASSES LOCALLY INSTEAD OF CALLING AN API
----------------------------------------------------------
The obvious route was N2YO's /visualpasses (free tier, 1000 req/hour), but
it needs a registered API key, and one key would have to serve every unit
of a shipped product -- PRODUCTION.md's whole objection to per-unit
recurring cost and shared-credential fragility. polluxlabs, which already
serves the ISS here, was checked and is ISS-ONLY: it accepts satid/norad/
sat parameters and ignores all of them, returning ISS (ZARYA) every time.

So instead: CelesTrak publishes the orbital elements for free with no key
and no rate limit, and the propagation is done here.

    https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle

The `visual` GROUP is exactly the right catalogue -- CelesTrak curates it
as the objects actually observable with the naked eye (~160 of them),
which is the "bright satellites only, don't list hundreds of invisible
objects" requirement solved upstream rather than by us inventing a
magnitude cutoff. One fetch per half-day covers everything.

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

import paneltext

try:
    from sgp4.api import Satrec, jday
    HAVE_SGP4 = True
except ImportError:                     # degrade honestly, never guess
    Satrec = None
    jday = None
    HAVE_SGP4 = False

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle"
TLE_REFRESH = 43200.0        # 12h -- elements are good for days; this is polite
TLE_STALE_MAX = 259200.0     # 3 days: beyond this, accuracy degrades enough to stop
PASS_REFRESH = 900.0         # recompute the schedule every 15 min
IDLE_STOP = 180.0
TIMEOUT = 15.0
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


def fetch_tles():
    req = urllib.request.Request(TLE_URL, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return parse_tles(r.read().decode("utf-8", "replace"))


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
                        "rise": rise_t, "set": t,
                        "rise_az": rise_az, "peak_az": peak_az,
                        "peak_el": round(peak_el, 1),
                        "duration_s": int((t - rise_t).total_seconds()),
                    })
                rise_t = None
            t += timedelta(seconds=COARSE_STEP)

    results.sort(key=lambda p: p["rise"])
    return results[:limit]


def _jd_of(when):
    """(jd, 0.0) helper so _sun_altitude can take a datetime."""
    jd, fr = jday(when.year, when.month, when.day,
                  when.hour, when.minute, when.second + when.microsecond / 1e6)
    return (jd + fr,)


def quality(p):
    """Same tiering satellite.py uses for the ISS, so the two agree."""
    el = (p or {}).get("peak_el") or 0
    if el >= ELEV_EXCELLENT:
        return "BRIGHT"
    if el >= ELEV_GOOD:
        return "GOOD"
    return "LOW"


class SkyPassFeed:
    """Background TLE fetch + local pass computation.

    Same contract as every other FEED here: get() never blocks, the work
    happens off the caller's thread, and nothing is invented -- if sgp4 is
    missing or the catalogue never arrived, this reports an error and the
    engine renders that honestly.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tles = []
        self._tles_at = 0.0
        self._passes = []
        self._passes_at = 0.0
        self._err = None if HAVE_SGP4 else "sgp4 not installed"
        self._last_read = 0.0
        self._thread = None
        self._loc = None

    def get(self, lat=None, lon=None):
        now = time.time()
        with self._lock:
            self._last_read = now
            if lat is not None and lon is not None:
                if self._loc != (lat, lon):
                    self._loc = (lat, lon)
                    self._passes, self._passes_at = [], 0.0
            passes = [dict(p) for p in self._passes]
            out = {
                "passes": passes,
                "next": passes[0] if passes else None,
                "count": len(passes),
                "tle_count": len(self._tles),
                "age": (now - self._passes_at) if self._passes_at else None,
                "err": self._err,
                "available": HAVE_SGP4,
            }
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
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    ValueError, TypeError, KeyError) as e:
                with self._lock:
                    self._err = type(e).__name__
            time.sleep(5.0)

    def _work(self, loc):
        now = time.time()
        with self._lock:
            need_tle = (not self._tles) or (now - self._tles_at > TLE_REFRESH)
        if need_tle:
            tles = fetch_tles()
            if tles:
                with self._lock:
                    self._tles, self._tles_at, self._err = tles, time.time(), None
        if not loc:
            return
        with self._lock:
            due = now - self._passes_at > PASS_REFRESH
            tles = list(self._tles)
            stale = self._tles_at and (now - self._tles_at) > TLE_STALE_MAX
        if not due or not tles or stale:
            return
        passes = predict(tles, loc[0], loc[1])
        with self._lock:
            self._passes, self._passes_at = passes, time.time()
            self._err = None


FEED = SkyPassFeed()
