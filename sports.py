"""
sports.py -- free ESPN scoreboard data for the sports ticker mode.

Same shape as market.py/satellite.py/flights.py, deliberately: all I/O lives
here so the mode that draws it stays pure.

One keyless source, ESPN's public (undocumented) site API:
  * site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
    -- today's games across a whole league in one call. Used for the
    rotating ticker AND for the pinned team's score/clock -- cheap, one
    call per league per refresh.
  * site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event=ID
    -- per-game detail. This is the ONLY place win probability lives; it
    is NOT in the scoreboard payload for any of the four leagues (checked
    live against real games before writing this). Only fetched for the
    pinned team's own game, and only while that game is actually live, so
    a full 16-game MLB slate in the ticker never costs 16 extra requests.

Confirmed by pulling real scoreboard/summary payloads before writing this
module (see commit message / PR notes for exact games checked), not
assumed from docs:
  * NFL, NBA, MLB all return a "winprobability" list in the summary
    endpoint that fills in with real data once the game has live plays.
  * NHL's summary endpoint does not have a "winprobability" key AT ALL --
    checked against a genuinely completed real NHL game, not just an
    unplayed one. This is a real gap in ESPN's public NHL data, not a
    bug here. The engine must simply not show a win% for NHL rather than
    inventing one.

Same two rules as every other feed in this project:
  1. NEVER block the render loop -- background thread, last-good cache.
  2. NEVER invent numbers -- if a field ESPN doesn't provide (win prob
     for NHL, or any field ESPN omits for a specific game), it comes back
     as None/missing and the engine must render around that honestly.

Favorite team is config-driven (sports_config.json), same pattern as
market_config.json's watchlist and location_config.json's home
coordinates -- set from the control panel/phone, not hand-edited.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "sports_config.json"

# sport/league path ESPN expects, keyed by the short league code an owner
# would actually type/select. EPL/NCAAF/NCAAB confirmed live before adding
# (same shape scoreboard/summary response as the original 4 -- checked
# real events, not assumed): EPL's summary endpoint has no
# "winprobability" key at all (same real gap as NHL), NCAAF/NCAAB both
# have it and it populates once a game goes live.
LEAGUE_PATHS = {
    "NFL": "football/nfl",
    "NBA": "basketball/nba",
    "MLB": "baseball/mlb",
    "NHL": "hockey/nhl",
    "EPL": "soccer/eng.1",
    "NCAAF": "football/college-football",
    "NCAAB": "basketball/mens-college-basketball",
}
DEFAULT_LEAGUES = ["NFL", "NBA", "MLB", "NHL"]

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"

SCOREBOARD_REFRESH = 20.0     # 15-30s per the spec; games don't need faster
WINPROB_REFRESH = 20.0        # only polled for the pinned team's own live game
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def load_config():
    """Read sports_config.json, seeding sane defaults on first run -- same
    contract as market.load_config(). favorite is {league, team_abbr} or
    None (unset: the mode still works, it just has nothing to pin)."""
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            leagues = [str(s).upper() for s in data.get("leagues", DEFAULT_LEAGUES)
                       if str(s).upper() in LEAGUE_PATHS]
            fav = data.get("favorite")
            favorite = None
            if isinstance(fav, dict) and fav.get("league") in LEAGUE_PATHS and fav.get("team_abbr"):
                favorite = {"league": fav["league"], "team_abbr": str(fav["team_abbr"]).upper()}
            return (leagues or list(DEFAULT_LEAGUES)), favorite
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            pass
    save_config(DEFAULT_LEAGUES, None)
    return list(DEFAULT_LEAGUES), None


def save_config(leagues, favorite):
    CONFIG_PATH.write_text(json.dumps(
        {"leagues": list(leagues), "favorite": favorite}, indent=2))


def _hex_to_rgb(hex_str, min_brightness=90):
    """ESPN gives team colors as a bare 6-hex-digit string, no '#', and
    real teams really do ship near-black or near-white as a primary color
    (several soccer clubs use pure black). On this panel's pure-black
    background that would render as invisible, which is a real rendering
    constraint, not a reason to invent a different color -- the actual
    hue is kept, only lifted to a floor brightness so it's visible.
    Returns None if the field is missing/malformed so the caller can fall
    back to a neutral color rather than guess."""
    if not isinstance(hex_str, str) or len(hex_str) != 6:
        return None
    try:
        r, g, b = (int(hex_str[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None
    if max(r, g, b) < min_brightness:
        scale = min_brightness / max(1, max(r, g, b))
        r, g, b = min(255, int(r * scale)), min(255, int(g * scale)), min(255, int(b * scale))
        if max(r, g, b) < min_brightness:      # started at (0,0,0) -- scale can't lift that
            r = g = b = min_brightness
    return (r, g, b)


def _team_row(competitor):
    team = competitor.get("team") or {}
    score = competitor.get("score")
    return {
        "abbr": (team.get("abbreviation") or "").upper(),
        "name": (team.get("shortDisplayName") or team.get("name") or "").upper(),
        "score": int(score) if isinstance(score, str) and score.isdigit() else score,
        "home_away": competitor.get("homeAway"),
        "winner": bool(competitor.get("winner")),
        "color": _hex_to_rgb(team.get("color")),
    }


def _parse_event(event, league):
    comp = event["competitions"][0]
    status = comp["status"]
    stype = status["type"]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    return {
        "event_id": event["id"],
        "league": league,
        "short_name": (event.get("shortName") or "").upper(),
        "state": stype.get("state"),          # "pre" | "in" | "post"
        "completed": bool(stype.get("completed")),
        # ESPN returns this mixed-case ("Final", "Top 5th", "Halftime").
        # The panel's 3x5 font is uppercase-only and silently drops any
        # glyph it doesn't have -- flights.py's airline-name field hit
        # this exact bug already ("United Airlines" rendered as just "U"
        # "A"). Every other display string in this codebase is uppercased
        # at the source for that reason; this is that same fix applied here.
        "detail": (stype.get("shortDetail") or stype.get("detail") or "").upper(),
        "period": status.get("period"),
        "display_clock": status.get("displayClock"),
        "home": _team_row(home),
        "away": _team_row(away),
    }


def _fetch_scoreboard(league):
    path = LEAGUE_PATHS[league]
    # ESPN's scoreboard with NO dates param doesn't mean "today" during a
    # dead period -- it jumps forward to the next scheduled game, which can
    # be months away (an NFL preseason opener shown in July, an NHL game
    # from September). Passing today's date explicitly is what actually
    # means "today": confirmed live against a real off-season league,
    # which correctly returns zero events instead of a future game. A
    # league with nothing today should show as empty, not misleadingly
    # show something that isn't happening for months.
    today = time.strftime("%Y%m%d")
    data = _get_json(f"{SCOREBOARD_URL.format(path=path)}?dates={today}")
    out = []
    for ev in data.get("events") or []:
        try:
            parsed = _parse_event(ev, league)
        except (KeyError, IndexError, TypeError, ValueError):
            continue          # one malformed event must not lose the whole slate
        if parsed:
            out.append(parsed)
    return out


def _fetch_win_prob(league, event_id):
    """Returns the home team's current win percentage (0..1) or None if
    ESPN doesn't have one for this game/sport. NHL's summary endpoint has
    no "winprobability" key at all -- confirmed against real completed
    NHL games, not assumed -- so this correctly returns None for every
    NHL game, always. For NFL/NBA/MLB it returns None only until the game
    has actually started producing plays (pregame games have an empty
    list, not missing data)."""
    path = LEAGUE_PATHS[league]
    data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    wp = data.get("winprobability")
    if not isinstance(wp, list) or not wp:
        return None
    last = wp[-1]
    pct = last.get("homeWinPercentage")
    return float(pct) if isinstance(pct, (int, float)) else None


class SportsFeed:
    """Background poller with a last-good cache -- same contract as every
    other FEED in this project: never blocks the caller, never invents a
    number, stops polling once nothing has read from it for a while."""

    def __init__(self):
        self._lock = threading.Lock()
        self.leagues, self.favorite = load_config()
        self._games = {}           # league -> [game dicts]
        self._updated = {}         # league -> epoch seconds
        self._last_try = {}        # league -> epoch seconds
        self._win_prob = None      # 0..1 or None
        self._win_prob_updated = 0.0
        self._win_prob_try = 0.0
        self._last_config_check = 0.0
        self._last_read = 0.0
        self._thread = None
        self._err = None

    # ---- reading -------------------------------------------------------
    def get(self):
        """Returns {games: [...], favorite, favorite_game, win_prob,
        age, err}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            games = []
            for lg in self.leagues:
                games.extend(dict(g) for g in self._games.get(lg, []))
            updated_times = [self._updated[lg] for lg in self.leagues if lg in self._updated]
            favorite = dict(self.favorite) if self.favorite else None
            win_prob = self._win_prob
            err = self._err
        self._ensure_thread()

        games.sort(key=lambda g: (g["state"] != "in", g["state"] == "post"))

        favorite_game = None
        if favorite:
            favorite_game = next(
                (g for g in games if g["league"] == favorite["league"] and
                 favorite["team_abbr"] in (g["home"]["abbr"], g["away"]["abbr"])),
                None)

        age = (now - min(updated_times)) if updated_times else None
        return {
            "games": games, "favorite": favorite, "favorite_game": favorite_game,
            "win_prob": win_prob if favorite_game and favorite_game["state"] == "in" else None,
            "age": age, "err": err,
        }

    def get_config(self):
        with self._lock:
            return list(self.leagues), (dict(self.favorite) if self.favorite else None)

    def set_leagues(self, leagues):
        leagues = [str(s).strip().upper() for s in leagues if str(s).strip()]
        accepted = [s for s in leagues if s in LEAGUE_PATHS]
        rejected = [s for s in leagues if s not in LEAGUE_PATHS]
        with self._lock:
            self.leagues = accepted or list(DEFAULT_LEAGUES)
            for lg in self.leagues:
                self._last_try[lg] = 0.0     # refresh immediately
            favorite = self.favorite
        save_config(self.leagues, favorite)
        return list(self.leagues), rejected

    def set_favorite(self, league, team_abbr):
        league = str(league).strip().upper()
        team_abbr = str(team_abbr).strip().upper()
        if league not in LEAGUE_PATHS or not team_abbr:
            return None
        favorite = {"league": league, "team_abbr": team_abbr}
        with self._lock:
            self.favorite = favorite
            if league not in self.leagues:
                self.leagues.append(league)
            self._last_try[league] = 0.0
            self._win_prob_try = 0.0
            self._win_prob = None
            leagues = list(self.leagues)
        save_config(leagues, favorite)
        return favorite

    def clear_favorite(self):
        with self._lock:
            self.favorite = None
            self._win_prob = None
            leagues = list(self.leagues)
        save_config(leagues, None)

    # ---- polling ---------------------------------------------------------
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
            self._refresh_scoreboards()
            self._refresh_win_prob()
            time.sleep(2.0)

    def _maybe_reload_config(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        leagues, favorite = load_config()
        with self._lock:
            if leagues != self.leagues or favorite != self.favorite:
                self.leagues, self.favorite = leagues, favorite
                self._win_prob_try = 0.0

    def _refresh_scoreboards(self):
        now = time.time()
        with self._lock:
            leagues = list(self.leagues)
        for lg in leagues:
            with self._lock:
                if now - self._last_try.get(lg, 0.0) < SCOREBOARD_REFRESH:
                    continue
                self._last_try[lg] = now
            try:
                games = _fetch_scoreboard(lg)
                with self._lock:
                    self._games[lg] = games
                    self._updated[lg] = time.time()
                    self._err = None
            except Exception as e:                     # noqa: BLE001 - never die
                with self._lock:
                    self._err = f"{type(e).__name__}"

    def _refresh_win_prob(self):
        now = time.time()
        with self._lock:
            if now - self._win_prob_try < WINPROB_REFRESH:
                return
            favorite = self.favorite
        if not favorite:
            return
        with self._lock:
            games = list(self._games.get(favorite["league"], []))
        game = next((g for g in games if favorite["team_abbr"] in
                     (g["home"]["abbr"], g["away"]["abbr"])), None)
        if not game or game["state"] != "in":
            with self._lock:
                self._win_prob_try = now
                self._win_prob = None
            return
        with self._lock:
            self._win_prob_try = now
        try:
            pct = _fetch_win_prob(favorite["league"], game["event_id"])
            with self._lock:
                self._win_prob = pct
        except Exception:                               # noqa: BLE001
            with self._lock:
                self._win_prob = None


FEED = SportsFeed()
