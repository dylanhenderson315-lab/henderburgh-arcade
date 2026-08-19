"""
racing.py -- real F1 (jolpi.ca) and NASCAR Cup (cf.nascar.com) data,
built the same session the owner asked "do f1 and try to find same for
nascar", following a dedicated research pass that confirmed both live,
free, keyless.

F1: api.jolpi.ca is a real, free, keyless continuation of the old
Ergast API (which shut down) -- confirmed live 2026-08-19: real season
schedule, real last-race results, real driver standings, real pit
stops. No documented hard rate limit; responses are CDN-cached ~10min
(`cache-control: max-age=600`), so polling faster than that is pointless
regardless -- `F1_REFRESH` is set well above that.

NASCAR: cf.nascar.com/cacher/{season}/race_list_basic.json is real,
free, keyless -- confirmed live: real Cup Series (series_id 1) schedule
and results, including a real prose recap (`race_comments`) with the
winner's name in its own first sentence. Xfinity/Truck series exist in
the same payload (series_id 2/3) but are deliberately NOT parsed here --
Cup is the series people mean by "NASCAR", and scope was kept to one
series per the same "don't build what wasn't asked for" discipline
`weather.py`'s own feature list follows.

HONEST GAP, stated plainly: cf.nascar.com/cacher/live/live-feed.json
looked like real in-race telemetry in research, but NASCAR was not
running a race at build time, so it could not be confirmed to actually
update during a green-flag race rather than just serving the last
race's final frozen state. NOT built here -- this module only ever
shows real SCHEDULE/RESULTS, the same "recent results, not live
telemetry" honest scope this project's own DepartureBoardEngine already
chose over an unconfirmed live claim. Revisit once a real race is
running and this can be checked live.

There is no reliable, cheap way to correlate ESPN's own racing
scoreboard events (which `sports.py` already surfaces through the
generic leaderboard renderer) to jolpi.ca's races by anything but
date -- confirmed by the research pass. This module does NOT attempt
that correlation; it is a fully separate, standalone real data source,
not tied to ESPN's racing coverage at all. `sports.py` is untouched.

Same shape as every other feed module here: a FEED singleton, get()
that never blocks, a background poll thread that self-limits via
IDLE_STOP, and never invents a number -- a field ESPN/jolpi.ca/NASCAR
didn't send is None, never guessed.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

import paneltext

JOLPI_BASE = "https://api.jolpi.ca/ergast/f1"
NASCAR_SCHEDULE_URL = "https://cf.nascar.com/cacher/{season}/race_list_basic.json"

F1_REFRESH = 1800.0      # well above jolpi.ca's own ~10min CDN cache window
NASCAR_REFRESH = 1800.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- F1 ----

def _parse_f1_schedule(data):
    """Real season race list -> [{round, name, date, time, circuit,
    locality, country}], or []. Confirmed live shape 2026-08-19:
    `MRData.RaceTable.Races[]`, each with `raceName`, `date` (real
    ISO date), optional `time` (UTC, "HH:MM:SSZ"), and `Circuit.
    {circuitName, Location.{locality, country}}`."""
    races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races")
    if not isinstance(races, list):
        return []
    out = []
    for r in races:
        if not isinstance(r, dict):
            continue
        circuit = r.get("Circuit") or {}
        loc = circuit.get("Location") or {}
        out.append({
            "round": r.get("round"),
            "name": paneltext.panel_text(r.get("raceName") or "") or None,
            "date": r.get("date"),
            "time": r.get("time"),
            "circuit": paneltext.panel_text(circuit.get("circuitName") or "") or None,
            "locality": paneltext.panel_text(loc.get("locality") or "") or None,
            "country": paneltext.panel_text(loc.get("country") or "") or None,
        })
    return out


def _parse_f1_last_result(data):
    """Real most-recent race's podium -> {name, round, date, podium:
    [{pos, code, family_name, constructor}]}, or None. `Driver.code`
    is a real 3-letter code ESPN-style displays already use elsewhere
    in this project (e.g. flights' registration-style short idents);
    `familyName` is the real surname."""
    races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races")
    if not isinstance(races, list) or not races:
        return None
    race = races[0]
    results = race.get("Results")
    if not isinstance(results, list):
        return None
    podium = []
    for res in results[:3]:
        drv = res.get("Driver") or {}
        con = res.get("Constructor") or {}
        podium.append({
            "pos": res.get("position"),
            "code": paneltext.panel_text(drv.get("code") or "") or None,
            "family_name": paneltext.panel_text(drv.get("familyName") or "") or None,
            "constructor": paneltext.panel_text(con.get("name") or "") or None,
        })
    return {
        "name": paneltext.panel_text(race.get("raceName") or "") or None,
        "round": race.get("round"),
        "date": race.get("date"),
        "podium": podium,
    }


def _parse_f1_standings(data):
    """Real current driver standings, top 5 -> [{pos, points, code,
    family_name, constructor}], or []."""
    lists = (((data or {}).get("MRData") or {}).get("StandingsTable") or {}).get("StandingsLists")
    if not isinstance(lists, list) or not lists:
        return []
    standings = lists[0].get("DriverStandings")
    if not isinstance(standings, list):
        return []
    out = []
    for s in standings[:5]:
        drv = s.get("Driver") or {}
        cons = s.get("Constructors") or [{}]
        out.append({
            "pos": s.get("position"),
            "points": s.get("points"),
            "code": paneltext.panel_text(drv.get("code") or "") or None,
            "family_name": paneltext.panel_text(drv.get("familyName") or "") or None,
            "constructor": paneltext.panel_text((cons[0] or {}).get("name") or "") or None,
        })
    return out


class F1Feed:
    def __init__(self):
        self._lock = threading.Lock()
        self._schedule = []
        self._last_result = None
        self._standings = []
        self._try = 0.0
        self._err = None
        self._last_read = 0.0
        self._thread = None

    def get(self):
        """{"schedule": [...], "next_race": {...}|None,
        "last_result": {...}|None, "standings": [...], "age", "err"}."""
        now = time.time()
        with self._lock:
            self._last_read = now
            schedule = list(self._schedule)
            last_result = dict(self._last_result) if self._last_result else None
            standings = list(self._standings)
            err = self._err
            age = (now - self._try) if self._try else None
        self._ensure_thread()
        next_race = next((r for r in schedule if r.get("date") and r["date"] > _today_iso()), None)
        return {
            "schedule": schedule, "next_race": next_race,
            "last_result": last_result, "standings": standings,
            "age": age, "err": err,
        }

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
            if now - self._try < F1_REFRESH:
                return
            self._try = now
        try:
            sched = _get_json(f"{JOLPI_BASE}/current.json")
            last = _get_json(f"{JOLPI_BASE}/current/last/results.json")
            stand = _get_json(f"{JOLPI_BASE}/current/driverstandings.json")
            with self._lock:
                self._schedule = _parse_f1_schedule(sched)
                self._last_result = _parse_f1_last_result(last)
                self._standings = _parse_f1_standings(stand)
                self._err = None
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError) as e:        # noqa: BLE001
            with self._lock:
                self._err = f"{type(e).__name__}"


def _today_iso():
    return time.strftime("%Y-%m-%d")


F1_FEED = F1Feed()


# ------------------------------------------------------------ NASCAR ----

_WON_RE = re.compile(r"^\s*(.+?)\s+won\s+the\s+", re.IGNORECASE)


def _parse_nascar_winner(comments):
    """Real winner name from the first sentence of NASCAR's own real
    prose recap ("Joey Logano won the Cook Out 400 at..."), confirmed
    live 2026-08-19 -- there is no separate structured winner-NAME field
    in this payload (only a numeric `winner_driver_id` with no matching
    roster lookup available here), so this is the one honest real source
    for the name. Returns None if the recap doesn't start with the
    expected real phrasing rather than guessing a name from elsewhere."""
    if not isinstance(comments, str) or not comments:
        return None
    m = _WON_RE.match(comments)
    if not m:
        return None
    return paneltext.panel_text(m.group(1)) or None


def _parse_nascar_cup(data):
    """Real Cup Series (series_id 1) schedule -> {last_result, next_race,
    schedule}. `data` is the real cacher race_list_basic.json dict."""
    races = data.get("series_1") if isinstance(data, dict) else None
    if not isinstance(races, list):
        return {"last_result": None, "next_race": None}
    today = _today_iso()
    past = sorted(
        (r for r in races if isinstance(r, dict) and r.get("actual_laps")
         and isinstance(r.get("race_date"), str) and r["race_date"][:10] <= today),
        key=lambda r: r["race_date"])
    fut = sorted(
        (r for r in races if isinstance(r, dict)
         and isinstance(r.get("race_date"), str) and r["race_date"][:10] > today),
        key=lambda r: r["race_date"])
    last_result = None
    if past:
        r = past[-1]
        last_result = {
            "name": paneltext.panel_text(r.get("race_name") or "") or None,
            "track": paneltext.panel_text(r.get("track_name") or "") or None,
            "date": r.get("race_date"),
            "winner": _parse_nascar_winner(r.get("race_comments")),
            "laps": r.get("actual_laps"),
            "cautions": r.get("number_of_cautions"),
        }
    next_race = None
    if fut:
        r = fut[0]
        next_race = {
            "name": paneltext.panel_text(r.get("race_name") or "") or None,
            "track": paneltext.panel_text(r.get("track_name") or "") or None,
            "date": r.get("race_date"),
        }
    return {"last_result": last_result, "next_race": next_race}


class NascarFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_result = None
        self._next_race = None
        self._try = 0.0
        self._err = None
        self._last_read = 0.0
        self._thread = None

    def get(self):
        now = time.time()
        with self._lock:
            self._last_read = now
            last_result = dict(self._last_result) if self._last_result else None
            next_race = dict(self._next_race) if self._next_race else None
            err = self._err
            age = (now - self._try) if self._try else None
        self._ensure_thread()
        return {"last_result": last_result, "next_race": next_race, "age": age, "err": err}

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
            if now - self._try < NASCAR_REFRESH:
                return
            self._try = now
        try:
            season = time.strftime("%Y")
            data = _get_json(NASCAR_SCHEDULE_URL.format(season=season))
            parsed = _parse_nascar_cup(data)
            with self._lock:
                self._last_result = parsed["last_result"]
                self._next_race = parsed["next_race"]
                self._err = None
        except (urllib.error.URLError, TimeoutError, ValueError,
                json.JSONDecodeError, OSError, KeyError) as e:        # noqa: BLE001
            with self._lock:
                self._err = f"{type(e).__name__}"


NASCAR_FEED = NascarFeed()
