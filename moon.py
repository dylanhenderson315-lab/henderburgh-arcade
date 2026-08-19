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
"""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import paneltext
import satellite

USNO_URL = ("https://aa.usno.navy.mil/api/rstt/oneday"
            "?date={date}&coords={lat},{lon}&tz={tz}")
LL2_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=5&mode=list"

USNO_REFRESH = 3600.0 * 6   # real rise/set/illumination is a once-a-day fact; 6h keeps it current across a long-running day
LAUNCH_REFRESH = 3600.0     # respect LL2's real ~15/hr soft limit by a wide margin
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


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


class MoonFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._usno = {}
        self._usno_try = 0.0
        self._usno_err = None
        self._launch = None
        self._launch_try = 0.0
        self._launch_err = None
        self._last_read = 0.0
        self._thread = None
        self._home = satellite.FEED.get_location()

    def get(self):
        """{"curphase", "illum_pct", "closest_phase", "moonrise",
        "moonset", "launch": {...}|None, "configured", "age", "err"}.
        Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            usno = dict(self._usno)
            launch = dict(self._launch) if self._launch else None
            err = self._usno_err or self._launch_err
            age = (now - self._usno_try) if self._usno_try else None
        self._ensure_thread()
        out = {
            "configured": satellite.FEED.configured,
            "age": age, "err": err, "launch": launch,
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
            time.sleep(5.0)

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
