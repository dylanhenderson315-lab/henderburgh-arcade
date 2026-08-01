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
import re
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

# ESPN's site API is undocumented and unofficial: no published rate
# limit, no terms covering this use, no support channel. `ambient` mode
# is designed to be left running for hours, which made a flat 20s poll
# per league roughly 17k requests/day (see CLAUDE.md). These tiers keep
# the fast poll exactly where scores actually change and stop spending
# requests to be told nothing happened.
SCOREBOARD_REFRESH_LIVE = 20.0    # a game is IN PROGRESS -- scores change, stay fast
SCOREBOARD_REFRESH_IDLE = 300.0   # games today but none live yet / all final
SCOREBOARD_REFRESH_EMPTY = 1800.0 # no games at all today (off-season)
# Honest tradeoff of the IDLE tier: a game kicking off is noticed up to 5
# minutes late, after which the league flips to the LIVE tier and updates
# every 20s. A scoreboard being 5 minutes late to say "0-0, 1st" is worth
# roughly a 15x cut in requests against an API that could withdraw
# access at any time.
ERROR_BACKOFF_BASE = 30.0         # first retry delay after a failure
ERROR_BACKOFF_MAX = 600.0         # never hammer a throttled/broken endpoint
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
        # Kept so _disambiguate_colors() has somewhere to go when the two
        # teams in a game would otherwise render the same accent bar.
        "alt_color": _hex_to_rgb(team.get("alternateColor")),
        "record": _team_record(competitor),
        "rank": _team_rank(competitor),
    }


def _team_record(competitor):
    """Overall W-L from ESPN's records[type=total].summary.

    Verified present in ALL SEVEN leagues. Format differs by sport and is
    passed through VERBATIM rather than reformatted, because each is the
    correct convention for its sport: MLB/NFL/NBA "55-54" (W-L), NHL
    "39-26-17" (W-L-OTL), EPL "19-11-7" (W-D-L). Reformatting these into
    one shape would make three of them wrong.
    """
    for rec in competitor.get("records") or []:
        if rec.get("type") == "total" and rec.get("summary"):
            return str(rec["summary"]).strip()
    return None


def _team_rank(competitor):
    """AP/coaches poll rank, or None.

    ESPN uses 99 as its UNRANKED sentinel, not a real 99th place -- NHL
    returns 99 for every team. Verified: real ranks come back for NCAAF
    (1, 9) and NCAAB (1, 2); NFL/NBA/EPL return None. Anything outside a
    plausible poll range is treated as "no rank" rather than displayed.
    """
    try:
        rank = int((competitor.get("curatedRank") or {}).get("current"))
    except (TypeError, ValueError):
        return None
    return rank if 1 <= rank <= 25 else None


MIN_TEAM_COLOR_DISTANCE = 60.0     # RGB euclidean; below this the two bars read as one colour


def _color_distance(a, b):
    if not a or not b:
        return 999.0
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _disambiguate_colors(away, home):
    """Make the two accent bars in a game visually distinct.

    Real, measured problem rather than a hypothetical: across 19 live
    games spanning all seven leagues, 5 (26%) had primary team colours
    close enough to be indistinguishable at this size -- SEA vs NE in the
    NFL were byte-identical, both being dark navies that the brightness
    floor in _hex_to_rgb() lifts to the same value. Two identical bars
    defeat the entire point of colour-coding the teams.

    Fix uses ESPN's own alternateColor rather than inventing a colour:
    try each team's alternate, keep whichever pairing separates best. If
    nothing separates (both teams genuinely ship the same two colours),
    leave the primaries alone -- the abbreviations still disambiguate, and
    a made-up colour would be worse than an honest collision.
    """
    best = (_color_distance(away.get("color"), home.get("color")),
            away.get("color"), home.get("color"))
    if best[0] >= MIN_TEAM_COLOR_DISTANCE:
        return
    for a_col, h_col in ((away.get("alt_color"), home.get("color")),
                         (away.get("color"), home.get("alt_color")),
                         (away.get("alt_color"), home.get("alt_color"))):
        if not a_col or not h_col:
            continue
        d = _color_distance(a_col, h_col)
        if d > best[0]:
            best = (d, a_col, h_col)
    if best[0] >= MIN_TEAM_COLOR_DISTANCE:
        away["color"], home["color"] = best[1], best[2]


def _situation(comp, state):
    """Live in-game state, or None.

    ESPN only includes `situation` while a game is ACTUALLY IN PROGRESS --
    verified absent on completed games in all seven leagues -- so this is
    None for anything pre/post and the engine simply shows less.

    VERIFICATION STATUS, because it differs per sport and matters:
      * MLB  -- VERIFIED live: balls, strikes, outs, and onFirst/onSecond/
        onThird as booleans. This is the whole "bottom 9th, 2 outs, runner
        on third" payload and it is real.
      * NFL  -- `downDistanceText` is read defensively but is NOT verified:
        it is July, the NFL is out of season, and `situation` does not
        persist on completed games, so there was no live game anywhere to
        check against. If ESPN provides it, it renders; if the key never
        appears, nothing is shown. Deliberately NOT given an invented
        layout or fabricated fallback.
      * NHL power-play state -- NOT built. No field name for it could be
        confirmed against real data, and guessing one would be inventing
        the feature.
    """
    if state != "in":
        return None
    sit = comp.get("situation")
    if not isinstance(sit, dict):
        return None
    bases = [bool(sit.get("onFirst")), bool(sit.get("onSecond")), bool(sit.get("onThird"))]
    out = {
        "outs": sit.get("outs") if isinstance(sit.get("outs"), int) else None,
        "balls": sit.get("balls") if isinstance(sit.get("balls"), int) else None,
        "strikes": sit.get("strikes") if isinstance(sit.get("strikes"), int) else None,
        "bases": bases if any(bases) else None,
        # Display-ready string when ESPN supplies one (NFL). Uppercased at
        # the I/O boundary like every other externally-sourced string.
        "down_distance": (str(sit.get("downDistanceText")).upper().strip()
                          if sit.get("downDistanceText") else None),
    }
    return out if any(v is not None for v in out.values()) else None


def _parse_event(event, league):
    comp = event["competitions"][0]
    status = comp["status"]
    stype = status["type"]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    away_row, home_row = _team_row(away), _team_row(home)
    _disambiguate_colors(away_row, home_row)
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
        "situation": _situation(comp, stype.get("state")),
        "display_clock": status.get("displayClock"),
        "home": home_row,
        "away": away_row,
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
        self._interval = {}        # league -> current poll interval (adaptive)
        self._fails = {}           # league -> consecutive failure count
        self._win_prob = None      # 0..1 or None
        self._win_prob_updated = 0.0
        self._win_prob_try = 0.0
        self._last_config_check = 0.0
        self._last_read = 0.0
        # Universal multi-sport scoreboard (the header endpoint). Separate
        # from the per-league cache above: one request covers every sport
        # ESPN is currently featuring, and a league is present ONLY when it
        # has something on -- so "nothing happening" needs no filtering.
        self._universal = []
        self._universal_updated = 0.0
        self._universal_try = 0.0
        self._universal_interval = 0.0
        self._universal_fails = 0
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
                self._interval[lg] = 0.0
                self._fails[lg] = 0
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
            self._refresh_universal()
            self._refresh_scoreboards()
            self._refresh_win_prob()
            time.sleep(2.0)

    def _refresh_universal(self):
        """Poll the one endpoint that covers every sport.

        Cheap by construction: ONE request regardless of how many leagues
        are live, versus one per configured league for the per-league
        path. Backs off the same way everything else here does."""
        now = time.time()
        with self._lock:
            if now - self._universal_try < self._universal_interval:
                return
            self._universal_try = now
        try:
            events = _parse_header(_get_json(HEADER_URL))
        except Exception as e:                       # noqa: BLE001 - never die
            with self._lock:
                self._universal_fails += 1
                self._universal_interval = min(
                    ERROR_BACKOFF_MAX,
                    ERROR_BACKOFF_BASE * (2 ** (self._universal_fails - 1)))
            return
        with self._lock:
            self._universal = events
            self._universal_updated = time.time()
            self._universal_fails = 0
            if any(e["live"] for e in events):
                self._universal_interval = HEADER_REFRESH_LIVE
            elif events:
                self._universal_interval = HEADER_REFRESH_IDLE
            else:
                self._universal_interval = HEADER_REFRESH_EMPTY

    def get_universal(self):
        """Every currently-relevant event across every sport, live first.

        Never blocks. A league with nothing on is simply not in here -- it
        is not represented as an empty entry, because ESPN omits it
        upstream and we deliberately do not add it back."""
        now = time.time()
        with self._lock:
            self._last_read = now
            events = [dict(e) for e in self._universal]
            age = (now - self._universal_updated) if self._universal_updated else None
        self._ensure_thread()
        # Live first, then upcoming, then finished -- within that, keep
        # ESPN's own ordering, which already groups by sport sensibly.
        rank = {"in": 0, "pre": 1, "post": 2}
        events.sort(key=lambda e: rank.get(e["state"], 3))
        return {"events": events, "age": age,
                "leagues": sorted({(e["sport"], e["league"]) for e in events})}

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

    @staticmethod
    def _interval_for(games):
        """How long to wait before polling this league again, from what it
        just returned. Anything in progress keeps the fast poll; a league
        with nothing on today is provably not worth asking again soon."""
        if any(g.get("state") == "in" for g in games):
            return SCOREBOARD_REFRESH_LIVE
        if games:
            return SCOREBOARD_REFRESH_IDLE
        return SCOREBOARD_REFRESH_EMPTY

    def _refresh_scoreboards(self):
        now = time.time()
        with self._lock:
            leagues = list(self.leagues)
        for lg in leagues:
            with self._lock:
                # Default to the fast interval so a league that has never
                # been fetched is polled immediately on first use.
                wait = self._interval.get(lg, 0.0)
                if now - self._last_try.get(lg, 0.0) < wait:
                    continue
                self._last_try[lg] = now
            try:
                games = _fetch_scoreboard(lg)
                with self._lock:
                    self._games[lg] = games
                    self._updated[lg] = time.time()
                    self._interval[lg] = self._interval_for(games)
                    self._fails[lg] = 0
                    self._err = None
            except Exception as e:                     # noqa: BLE001 - never die
                with self._lock:
                    # Exponential backoff on failure. A throttled or broken
                    # endpoint must not be retried every 20s forever -- that
                    # is exactly the behaviour that would earn a block from
                    # an API with no rate limit published and no way to ask.
                    self._fails[lg] = self._fails.get(lg, 0) + 1
                    self._interval[lg] = min(
                        ERROR_BACKOFF_MAX,
                        ERROR_BACKOFF_BASE * (2 ** (self._fails[lg] - 1)))
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


# =============================================================================
# UNIVERSAL SCOREBOARD -- every sport ESPN is currently featuring, in ONE call.
#
# WHY THIS ENDPOINT. The per-league scoreboard above needs one request per
# league, and ESPN publishes 338 sport/league slugs (enumerated from
# sports.core.api.espn.com/v2/sports on 2026-08-01). Polling even a fraction
# of that against an undocumented API with no published rate limit is exactly
# the risk CLAUDE.md flags as the project's top standing concern.
#
# This is the endpoint that drives espn.com's own top scoreboard bar:
#     site.api.espn.com/apis/v2/scoreboard/header
# One request returned 43 live/relevant events across 11 leagues in 7 sports
# when this was written -- golf (PGA + LPGA), MLB, WNBA, three soccer
# competitions, PFL, PLL lacrosse, ATP and WTA tennis.
#
# ABSENCE IS FREE, AND THAT IS THE POINT. ESPN only includes a league here
# when it has something on. There is no "no games today" state to filter out
# because a quiet league simply is not in the response. That is the required
# behaviour ("leagues with nothing happening are simply ABSENT") implemented
# by the source rather than by us guessing what counts as relevant.
#
# SHAPES DIFFER BY SPORT AND THE DIFFERENCES ARE REAL. Verified against a
# live payload, not assumed -- MMA already proved these are not uniform:
#   * MLB puts baserunners at the EVENT top level (onFirst/onSecond/onThird,
#     outsText, baseRunnersText) -- NOT nested in `situation` the way the
#     per-league scoreboard does it.
#   * `status` here is a plain STRING ("pre"/"in"/"post") and `summary` is
#     the display text ("Final", "FT", "Round 3 - In Progress"). The
#     per-league API instead nests these under status.type.*.
#   * Golf/tennis competitors are ATHLETES with `place`/`score`("-10")/
#     `status.thru`, not teams with numeric scores.
#   * Tennis carries `linescores`, `tournamentSeed` and a `notes[]` list of
#     completed-match text; soccer carries `form` and `addedClock`; MMA
#     carries `cardSegment`, `matchNumber` and a weight class in
#     `competitionType`.
# Everything below normalises those into one shape WITHOUT inventing fields:
# anything a given sport does not provide stays None.
# =============================================================================
HEADER_URL = "https://site.api.espn.com/apis/v2/scoreboard/header"

HEADER_REFRESH_LIVE = 25.0     # something is actually in progress
HEADER_REFRESH_IDLE = 180.0    # events exist but none live
HEADER_REFRESH_EMPTY = 900.0   # nothing at all anywhere (rare on this endpoint)

# Sports whose "event" is a multi-competitor standing rather than a head-to-
# head fixture. Determined by the competitor `type` being athlete AND there
# being more than two of them, not by a hardcoded sport list -- but these are
# the ones observed doing it, kept for readable labelling.
LEADERBOARD_SPORTS = {"golf"}


def _num_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _header_competitor(c, sport):
    """One competitor, team or athlete, normalised."""
    is_team = c.get("type") == "team"
    name = (c.get("abbreviation") or c.get("shortName")
            or c.get("name") or c.get("displayName") or "")
    st = c.get("status") if isinstance(c.get("status"), dict) else {}
    score = c.get("score")
    return {
        "id": c.get("id"),
        "is_team": is_team,
        # Teams get their abbreviation; athletes get "C. Young".
        "abbr": str(name).upper().strip(),
        "full": str(c.get("displayName") or name).upper().strip(),
        "score": _num_or_none(score) if is_team else (
            str(score).upper().strip() if score is not None else None),
        "home_away": c.get("homeAway"),
        "winner": bool(c.get("winner")),
        "color": _hex_to_rgb(c.get("color")) if c.get("color") else None,
        "alt_color": _hex_to_rgb(c.get("alternateColor")) if c.get("alternateColor") else None,
        # `record` is a plain string on this endpoint ("7-2-0"), unlike the
        # per-league API's records[] list.
        "record": (str(c["record"]).upper().strip()
                   if isinstance(c.get("record"), str) and c.get("record") else None),
        "seed": _num_or_none(c.get("tournamentSeed")),
        # Leaderboard fields -- golf. `thru` is holes completed; when a
        # player has not teed off it is 0 and teeTime is what matters.
        "place": _num_or_none(c.get("place")),
        "thru": _num_or_none(st.get("thru")),
        "hole": _num_or_none(st.get("hole")),
        "tee_time": st.get("teeTime"),
        "player_state": st.get("state"),
    }


def _header_event(e, sport, league_slug, league_name):
    """One event from the header endpoint, normalised across sports."""
    comps = [_header_competitor(c, sport) for c in (e.get("competitors") or [])]
    athletes = [c for c in comps if not c["is_team"]]
    leaderboard = len(athletes) > 2

    # MLB puts live baserunner state at the EVENT level here.
    bases = [bool(e.get("onFirst")), bool(e.get("onSecond")), bool(e.get("onThird"))]
    outs = None
    if isinstance(e.get("outsText"), str):
        m = re.match(r"\s*(\d+)", e["outsText"])
        if m:
            outs = int(m.group(1))

    state = str(e.get("status") or "").lower() or "pre"
    return {
        "id": str(e.get("id") or ""),
        "competition_id": str(e.get("competitionId") or e.get("id") or ""),
        "sport": sport,
        "league": (league_slug or "").upper(),
        "league_name": (league_name or league_slug or "").upper(),
        "name": str(e.get("name") or "").upper(),
        "short_name": str(e.get("shortName") or "").upper(),
        "state": state,                       # pre | in | post
        "live": state == "in",
        # Display text ESPN already formatted for this sport: "Final", "FT",
        # "Round 3 - In Progress", "Bot 9th". Uppercased at the boundary.
        "detail": str(e.get("summary") or "").upper().strip(),
        "period": _num_or_none(e.get("period")),
        "clock": (str(e.get("clock")).upper().strip()
                  if e.get("clock") not in (None, "") else None),
        "venue": str(e.get("location") or "").upper().strip() or None,
        "series": str(e.get("seriesSummary") or "").upper().strip() or None,
        "note": str(e.get("note") or "").upper().strip() or None,
        # MMA weight class / tennis draw both live in competitionType.
        "class_label": str((e.get("competitionType") or {}).get("text") or "").upper().strip() or None,
        "match_number": _num_or_none(e.get("matchNumber")),
        "card_segment": str(e.get("cardSegment") or "").upper().strip() or None,
        "round": str(e.get("round") or "").upper().strip() or None,
        "leaderboard": leaderboard,
        "competitors": comps,
        # Tennis ships completed-match summaries as free text.
        "notes": [str(n.get("text") or "").upper().strip()
                  for n in (e.get("notes") or []) if n.get("text")][:4],
        "bases": bases if any(bases) else None,
        "outs": outs,
        "runners_text": str(e.get("baseRunnersText") or "").upper().strip() or None,
    }


def _parse_header(payload):
    """Whole header payload -> flat list of normalised events."""
    out = []
    for s in (payload.get("sports") or []):
        sport = s.get("slug") or ""
        for lg in (s.get("leagues") or []):
            slug = lg.get("slug") or lg.get("abbreviation") or ""
            name = lg.get("shortName") or lg.get("abbreviation") or lg.get("name") or slug
            for e in (lg.get("events") or []):
                try:
                    out.append(_header_event(e, sport, slug, name))
                except (AttributeError, TypeError, ValueError):
                    continue          # one malformed event must not lose the rest
    return out
