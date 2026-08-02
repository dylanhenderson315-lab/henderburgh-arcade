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

import mma
import paneltext
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

# The subset of LEAGUE_PATHS that is soccer -- derived, not hardcoded to
# "EPL", so a future soccer league added to LEAGUE_PATHS is picked up
# automatically by anything (like the big-moment goal detector) that
# needs to know "is the pinned favorite's league soccer".
SOCCER_LEAGUES = {code for code, path in LEAGUE_PATHS.items()
                  if path.startswith("soccer/")}

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


def load_golf_player():
    """Pinned golfer, or None. Stored in the same sports_config.json as the
    favorite team -- one config file for one mode -- but read separately
    because load_config()'s two-value contract is used in several places
    and widening it would touch all of them for no benefit.

    Matched by NAME rather than ESPN athlete id: the id is stable but a
    person setting this from a phone types a name, and the leaderboard
    carries "R. HOJGAARD" style abbreviations that we can match against
    without making them look up a numeric id.
    """
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return None
    p = data.get("golf_player")
    if isinstance(p, str) and p.strip():
        return paneltext.panel_text(p)
    return None


def save_golf_player(name):
    """Persist (or clear, with a falsy name) the pinned golfer."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    cleaned = paneltext.panel_text(name) if name else None
    if cleaned:
        data["golf_player"] = cleaned
    else:
        data.pop("golf_player", None)
    data.setdefault("leagues", list(DEFAULT_LEAGUES))
    data.setdefault("favorite", None)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return cleaned


def save_config(leagues, favorite):
    """Preserves any key this function does not own (golf_player), so
    setting a favorite team can never silently wipe the pinned golfer."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    data["leagues"] = list(leagues)
    data["favorite"] = favorite
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


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
        "abbr": paneltext.panel_text(team.get("abbreviation")),
        "name": paneltext.panel_text(team.get("shortDisplayName") or team.get("name")),
        # A numeric score stays an int; anything else is DISPLAY TEXT and
        # must be folded (soccer aggregate strings, tennis set scores).
        "score": (int(score) if isinstance(score, str) and score.isdigit()
                  else (paneltext.panel_text(score) if isinstance(score, str) else score)),
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
            return paneltext.panel_text(rec["summary"])
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
        "down_distance": (paneltext.panel_text(sit.get("downDistanceText"))
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
        "short_name": paneltext.panel_text(event.get("shortName")),
        "state": stype.get("state"),          # "pre" | "in" | "post"
        "completed": bool(stype.get("completed")),
        # ESPN returns this mixed-case ("Final", "Top 5th", "Halftime").
        # The panel's 3x5 font is uppercase-only and silently drops any
        # glyph it doesn't have -- flights.py's airline-name field hit
        # this exact bug already ("United Airlines" rendered as just "U"
        # "A"). Every other display string in this codebase is uppercased
        # at the source for that reason; this is that same fix applied here.
        "detail": paneltext.panel_text(stype.get("shortDetail") or stype.get("detail")),
        "period": status.get("period"),
        "situation": _situation(comp, stype.get("state")),
        "display_clock": paneltext.panel_text(status.get("displayClock")) or None,
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



def _fetch_home_run_plays(league, event_id):
    """MLB home-run plays from a game's play-by-play, or [] for anything
    else. Returns a list of {"id": str, "text": str}, text already
    paneltext.panel_text()-folded (this is the I/O boundary -- see
    paneltext.py's docstring on why the fold belongs here, not in the
    caller).

    Confirmed live against a real finished game before writing this: each
    scoring play carries `scoringPlay: bool` and `alternativeType.text`,
    which is the literal string "Home Run" (plus distance) for a home run
    -- other scoring plays seen the same day were "Single", "Double",
    "Sacrifice Fly", none of which should trigger a celebration. Same
    per-game summary endpoint _fetch_win_prob() already uses -- deliberately
    NOT a new endpoint, so the request-volume discipline (only the pinned
    favorite's own LIVE game, see the caller in engines.py) is the only
    thing standing between this and the per-league-poll volume risk
    CLAUDE.md already flags twice.
    """
    if league != "MLB":
        return []
    path = LEAGUE_PATHS[league]
    data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    plays = data.get("plays")
    if not isinstance(plays, list):
        return []
    out = []
    for p in plays:
        if not p.get("scoringPlay"):
            continue
        alt_type = p.get("alternativeType") or {}
        if alt_type.get("text") != "Home Run":
            continue
        pid = p.get("id") or p.get("playId")
        if pid is None:
            continue
        out.append({
            "id": str(pid),
            "text": paneltext.panel_text(p.get("text") or ""),
        })
    return out


def _fetch_key_events(league, event_id):
    """One soccer match's play-by-play event log (goals, cards, subs,
    kickoff/halftime/regulation-end markers...) from the summary
    endpoint. Confirmed live 2026-08-02 against a real MLS match (CF
    Montreal vs New England Revolution, event 761697): each entry has a
    `type.text`, a `scoringPlay: bool`, free-text `text`, and -- unlike
    MLB's `plays`, which has no stable id at all -- a genuine string
    `id` field, so a caller can dedupe on it directly rather than
    falling back to a (type, text) tuple."""
    path = LEAGUE_PATHS[league]
    data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    return data.get("keyEvents") or []


def fetch_new_soccer_goals(league, event_id, seen):
    """Newly-seen goals for ONE soccer match's keyEvents. `seen` is a
    set the CALLER owns and this mutates in place -- same per-game
    "seen" idiom GameDayEngine._seen_done uses for MMA finishes: adopt
    whatever ids are already present on the first call for a given
    `seen` set rather than replaying history (the caller is responsible
    for handing in a fresh empty set when the game being watched
    changes), then report only what's genuinely new after that.

    A goal is any keyEvent with `scoringPlay: True`. Verified against a
    real 2026-08-02 MLS/NWSL slate -- 30+ real goals across 8 matches,
    zero false positives among scoringPlay=True entries. ESPN's own
    `type.text` varies with HOW it was scored; real values actually
    seen: "Goal", "Goal - Header", "Goal - Volley", "Goal - Free-kick",
    "Penalty - Scored", "Own Goal". `scoringPlay` is therefore the
    reliable signal to filter on, not a type.text whitelist that would
    need updating for every finishing variant ESPN might add.

    Returns a list of {"type", "text"} dicts, both already
    `paneltext.panel_text()`-folded here at the I/O boundary -- callers
    must not re-fold or pull raw text out of the original event.
    """
    out = []
    for ev in _fetch_key_events(league, event_id):
        if not ev.get("scoringPlay"):
            continue
        key = ev.get("id")
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append({
            "type": paneltext.panel_text((ev.get("type") or {}).get("text")),
            "text": paneltext.panel_text(ev.get("text")),
        })
    return out



def _fetch_mma_finish_method(league_slug, event_id):
    """Best-effort finish-method lookup for an MMA/PFL event surfaced
    through the UNIVERSAL header feed (sports.FEED.get_universal()) --
    NOT mma.FEED, the separate dedicated UFC-card feed GAME DAY uses,
    confirmed non-interchangeable in an earlier session.

    Reuses the ALREADY-VERIFIED type-ID logic from mma.py (20=submission,
    21=KO/TKO, 22=decision -- see mma.METHOD_BY_ID/mma.METHOD_BY_TEXT)
    rather than re-deriving it, on the theory that IF a `details` play-by-
    play list is reachable for a universal-feed event id, it has the same
    shape mma.py's own SCOREBOARD_URL already proved out.

    TWO GENUINE, DOCUMENTED-NOT-GUESSED-PAST UNKNOWNS, because there is no
    live/recent MMA event in either feed as of this build to test against:

    1. Whether this endpoint even EXISTS for a universal-feed event id.
       mma.py's own docstring already established that the bare
       `.../mma/ufc/summary` (no event id) 404s. This calls the per-event
       form instead -- the same URL SHAPE sports.py's own SUMMARY_URL uses
       for every team sport's win-probability/home-run/goal lookups
       (`.../mma/{slug}/summary?event=ID`) -- on the theory that a summary
       endpoint commonly 404s with no event id and behaves differently
       with one, the way ESPN's other sports do. That theory is UNTESTED.
    2. `league_slug` is a guess. The universal header event only exposes
       the ALREADY-UPPERCASED, panel_text()-folded league display name
       ("UFC", "PFL") -- see sports._header_event()'s `league` field --
       not ESPN's raw path slug, so this lowercases the display name
       rather than using the real slug mma.py's own SCOREBOARD_URL is
       built on ("ufc"). For UFC these likely happen to match; PFL is
       unconfirmed either way.

    Returns None on absolutely anything unexpected -- wrong shape, no
    `details`/`plays` list, 404, timeout, malformed json -- never a
    guessed method. Never call this more than once per finish: it is one
    request per fight ending, the same narrow per-event scope every other
    big-moment detector in this file uses (mlb_hr, soccer goals), not a
    new source of continuous polling.
    """
    try:
        path = f"mma/{(league_slug or '').lower()}"
        data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    except Exception:                    # noqa: BLE001 - never invent, never raise
        return None
    if not isinstance(data, dict):
        return None
    # mma.py's own `_method()` reads a flat `details` list off the
    # competition; this endpoint's shape (if it exists at all) is
    # unverified, so also try the `plays` key soccer/MLB's summary
    # payloads use, on the chance MMA's differs -- either way, nothing is
    # invented if neither is present.
    details = data.get("details")
    if not isinstance(details, list):
        details = data.get("plays")
    if not isinstance(details, list):
        return None
    for d in details:
        if not isinstance(d, dict):
            continue
        t = d.get("type") or {}
        text = str(t.get("text") or "")
        if "Winner" not in text:
            continue
        by_id = mma.METHOD_BY_ID.get(str(t.get("id")))
        if by_id:
            return by_id
        token = paneltext.panel_text(text.replace("Unofficial", "").replace("Winner", ""))
        return mma.METHOD_BY_TEXT.get(token, token or None)
    return None


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
        # Pinned golfer: the config name, plus the last (place, par) seen so
        # a NOTABLE move can be detected between polls. Cached in the FEED
        # rather than the engine because the engine is recreated on every
        # mode switch and would lose the baseline -- and then flash on
        # arrival, which is exactly what Pulse's first-value rule forbids.
        self._golf_player = load_golf_player()
        self._golf_prev = None
        self._golf_move = None
        self._golf_move_at = 0.0
        self._golf_field = None       # (meta, [competitors]) full-field fallback
        self._golf_field_try = 0.0
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
        # Notable-move detection for the pinned golfer, done HERE rather
        # than in the engine: this is the only place that sees consecutive
        # polls, which is what a "move" is defined against.
        with self._lock:
            pinned = self._golf_player
        if pinned:
            _, pc = find_pinned_golfer(events, pinned)
            if pc is None:
                # Not in the header's top 25 -- go to the full field. Only
                # reached when someone IS pinned and was NOT already found,
                # so it costs nothing in the common case.
                pc = self._pinned_from_field(events, pinned)
            cur = (pc.get("place"), _par_value(pc.get("score"))) if pc else None
            with self._lock:
                mv = golfer_move(self._golf_prev, cur)
                if mv:
                    self._golf_move, self._golf_move_at = mv, time.time()
                self._golf_prev = cur
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
            golf_player = self._golf_player
            golf_move = (self._golf_move
                         if self._golf_move and (now - self._golf_move_at) < GOLF_MOVE_TTL
                         else None)
        self._ensure_thread()
        # Live first, then upcoming, then finished -- within that, keep
        # ESPN's own ordering, which already groups by sport sensibly.
        rank = {"in": 0, "pre": 1, "post": 2}
        events.sort(key=lambda e: rank.get(e["state"], 3))
        gev, gc = find_pinned_golfer(events, golf_player)
        if golf_player and gc is None:
            gc = self._pinned_from_field(events, golf_player)
            if gc is not None:
                gev = self.golf_field_event()
        return {"events": events, "age": age,
                "leagues": sorted({(e["sport"], e["league"]) for e in events}),
                "golf_player": golf_player, "golf_event": gev, "golf_pinned": gc,
                # Reported for a short window then lapses on its own, so the
                # engine never has to acknowledge or clear it.
                "golf_move": golf_move}

    def _maybe_reload_config(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        leagues, favorite = load_config()
        gp = load_golf_player()
        with self._lock:
            if leagues != self.leagues or favorite != self.favorite:
                self.leagues, self.favorite = leagues, favorite
                self._win_prob_try = 0.0
            if gp != self._golf_player:
                # Changed under us (edited file / other process): drop the
                # baseline so the new player cannot flash on arrival.
                self._golf_player, self._golf_prev, self._golf_move = gp, None, None

    def _pinned_from_field(self, events, pinned):
        """Pinned golfer from the whole field, or None.

        Only tours that actually have a live leaderboard in the header are
        queried, so this never fires out of season, and it is rate-limited
        independently of the header poll."""
        tours = sorted({e["league"].lower() for e in events
                        if e.get("leaderboard") and e.get("state") in ("in", "pre")})
        if not tours:
            return None
        now = time.time()
        with self._lock:
            if now - self._golf_field_try < GOLF_FIELD_REFRESH and self._golf_field is not None:
                cached = self._golf_field
                return self._match_in_field(cached, pinned)
            self._golf_field_try = now
        found = None
        for tour in tours:
            try:
                meta, field = _fetch_golf_field(tour)
            except Exception:                    # noqa: BLE001 - never die
                continue
            if not field:
                continue
            with self._lock:
                self._golf_field = (meta, field)
            hit = self._match_in_field((meta, field), pinned)
            if hit:
                found = hit
                break
        return found

    @staticmethod
    def _match_in_field(cached, pinned):
        if not cached:
            return None
        meta, field = cached
        fake = dict(meta)
        fake["competitors"] = field
        _, c = find_pinned_golfer([fake], pinned)
        return c

    def golf_field_event(self):
        """Tournament meta for a pinned player found only in the full
        field -- the header event object does not describe them."""
        with self._lock:
            return dict(self._golf_field[0]) if self._golf_field else None

    def set_golf_player(self, name):
        cleaned = save_golf_player(name)
        with self._lock:
            self._golf_player = cleaned
            self._golf_prev = None
            self._golf_move = None
        return cleaned

    def get_golf_player(self):
        with self._lock:
            return self._golf_player

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
GOLF_MOVE_TTL = 20.0           # how long a notable move stays reportable

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
        "abbr": paneltext.panel_text(name),
        "full": paneltext.panel_text(c.get("displayName") or name),
        "score": _num_or_none(score) if is_team else (
            paneltext.panel_text(score) if score is not None else None),
        "home_away": c.get("homeAway"),
        "winner": bool(c.get("winner")),
        "color": _hex_to_rgb(c.get("color")) if c.get("color") else None,
        "alt_color": _hex_to_rgb(c.get("alternateColor")) if c.get("alternateColor") else None,
        # `record` is a plain string on this endpoint ("7-2-0"), unlike the
        # per-league API's records[] list.
        "record": (paneltext.panel_text(c["record"])
                   if isinstance(c.get("record"), str) and c.get("record") else None),
        "seed": _num_or_none(c.get("tournamentSeed")),
        "rank": _num_or_none(c.get("rank")),          # tennis world ranking
        "form": paneltext.panel_text(c.get("form")) or None,   # soccer WDLDL
        "movement": _num_or_none(c.get("movement")),  # golf position change
        "shootout": _num_or_none(c.get("shootoutScore")),  # soccer penalties
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
        "league": paneltext.panel_text(league_slug),
        "league_name": paneltext.panel_text(league_name or league_slug),
        "name": paneltext.panel_text(e.get("name")),
        "short_name": paneltext.panel_text(e.get("shortName")),
        "state": state,                       # pre | in | post
        "live": state == "in",
        # Display text ESPN already formatted for this sport: "Final", "FT",
        # "Round 3 - In Progress", "Bot 9th". Uppercased at the boundary.
        "detail": paneltext.panel_text(e.get("summary")),
        "period": _num_or_none(e.get("period")),
        "clock": (paneltext.panel_text(e.get("clock"))
                  if e.get("clock") not in (None, "") else None),
        # Fields we already pay for and were dropping. Deliberately NOT
        # including `odds` (betting stays off by default per PRODUCTION.md)
        # or logos (IP).
        "broadcast": paneltext.panel_text(
            (e.get("broadcast") or "")
            or ((e.get("broadcasts") or [{}])[0].get("shortName") if e.get("broadcasts") else "")
        ) or None,
        "week": paneltext.panel_text(e.get("weekText")) or None,
        "playoff": bool(e.get("playoff")),
        "neutral": bool(e.get("neutralSite")),
        "venue": paneltext.panel_text(e.get("location")) or None,
        "series": paneltext.panel_text(e.get("seriesSummary")) or None,
        "note": paneltext.panel_text(e.get("note")) or None,
        # MMA weight class / tennis draw both live in competitionType.
        "class_label": paneltext.panel_text((e.get("competitionType") or {}).get("text")) or None,
        "match_number": _num_or_none(e.get("matchNumber")),
        "card_segment": paneltext.panel_text(e.get("cardSegment")) or None,
        "round": paneltext.panel_text(e.get("round")) or None,
        "leaderboard": leaderboard,
        "competitors": comps,
        # Tennis ships completed-match summaries as free text.
        "notes": [paneltext.panel_text(n.get("text"))
                  for n in (e.get("notes") or []) if n.get("text")][:4],
        "bases": bases if any(bases) else None,
        "outs": outs,
        "runners_text": paneltext.panel_text(e.get("baseRunnersText")) or None,
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


# ---- pinned golfer -----------------------------------------------------
# Golf is the one sport here where the interesting question is not "what is
# the score" but "where is MY player, and did they just do something".
# Everything below is derived from fields verified against two LIVE
# tournaments (Rocket Classic and the AIG Women's Open, both mid-round on
# 2026-08-01): place, score to par as a string ("-11"), and status.thru.


def _par_value(score):
    """'-11' -> -11, 'E' -> 0, '+3' -> 3. None if unparseable.

    Golf scores are STRINGS and 'E' (even par) is not a number -- treating
    it as one is how a leaderboard ends up sorting or comparing wrongly.
    """
    if score is None:
        return None
    t = str(score).strip().upper()
    if t in ("E", "EVEN", "0"):
        return 0
    try:
        return int(t.replace("+", ""))
    except ValueError:
        return None


def find_pinned_golfer(events, pinned):
    """Locate the pinned player on any live leaderboard.

    Returns (event, competitor) or (None, None). Matching is deliberately
    forgiving because the leaderboard shows "R. HOJGAARD" while someone
    would type "Rasmus Hojgaard" or just "Hojgaard": a match is any of an
    exact hit, a surname hit, or one containing the other.
    """
    if not pinned:
        return None, None
    want = paneltext.panel_text(pinned)
    if not want:
        return None, None
    want_last = want.split()[-1]
    for ev in events:
        if not ev.get("leaderboard"):
            continue
        for c in ev.get("competitors") or []:
            name = c.get("full") or c.get("abbr") or ""
            if not name:
                continue
            last = name.split()[-1] if name.split() else ""
            if (name == want or last == want_last
                    or want in name or name in want):
                return ev, c
    return None, None


def golfer_move(prev, cur):
    """What NOTABLE thing just happened to the pinned player, or None.

    Only genuinely notable events, because a flash that fires on every
    routine par is a flash you learn to ignore -- the same reasoning as
    Pulse never firing on a first value.

      LEAD      -- moved into a share of the lead (place 1)
      LOST LEAD -- was leading, no longer is
      EAGLE     -- score to par improved by 2+ since the last look, which
                   on one poll interval is an eagle or better
      BIRDIE    -- improved by exactly 1
      BOGEY     -- worsened by 1 or more

    `prev` and `cur` are the (place, par) tuples this module caches. Any
    missing value returns None rather than guessing a move happened.
    """
    if not prev or not cur:
        return None
    p_place, p_par = prev
    c_place, c_par = cur
    if p_par is not None and c_par is not None and c_par != p_par:
        delta = c_par - p_par
        if delta <= -2:
            return "EAGLE"
        if delta == -1:
            return "BIRDIE"
        if delta >= 1:
            return "BOGEY"
    if p_place is not None and c_place is not None and p_place != c_place:
        if c_place == 1:
            return "LEAD"
        if p_place == 1:
            return "LOST LEAD"
    return None


# ---- full-field golf lookup -------------------------------------------
# The header endpoint carries only the TOP 25 of a leaderboard. That is
# fine for showing the leaders, but it silently breaks the pinned player
# the moment they are 26th or worse -- which is most players most of the
# time, and exactly when you most want to know where they are.
#
# So when a pinned golfer is not in the top 25, fall back to the per-tour
# scoreboard, which returns the ENTIRE field (147 players at the Rocket
# Classic, 144 at the AIG Women's Open, both verified live).
#
# Cost discipline, same as everywhere else here: this runs ONLY when a
# golfer is pinned, ONLY when they were not already found in the header,
# and ONLY for tours that currently have a leaderboard -- never
# speculatively, and never for the whole field when the header already
# answered the question.
GOLF_FIELD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/{tour}/scoreboard"
GOLF_FIELD_REFRESH = 60.0


def _golf_thru(competitor, period):
    """Holes completed in the CURRENT round, or None.

    The full-field payload has no `thru` (the header does), but it does
    carry `linescores` as one entry per round, each with a nested per-hole
    list -- so counting the holes in the current round recovers it rather
    than leaving the most useful number blank.
    """
    rounds = competitor.get("linescores") or []
    if not rounds:
        return None

    def played(r):
        return sum(1 for h in (r.get("linescores") or []) if h.get("value") is not None)

    # The event's own `period` is None on this endpoint (unlike the
    # header), and the rounds list always contains ALL FOUR rounds --
    # including future ones with zero holes. So the current round is the
    # LAST one with any holes actually played; taking rounds[-1] always
    # picked an empty round 4 and reported None for everybody.
    cur = None
    if period:
        cur = next((r for r in rounds if r.get("period") == period and played(r)), None)
    if cur is None:
        for r in reversed(rounds):
            if played(r):
                cur = r
                break
    if cur is None:
        return None
    n = played(cur)
    # 18 means that round is complete, which is "F" on a real leaderboard
    # rather than "thru 18"; the caller renders it as finished.
    return n or None


def _fetch_golf_field(tour):
    """Whole field for a tour, normalised like a header competitor."""
    d = _get_json(GOLF_FIELD_URL.format(tour=tour))
    events = d.get("events") or []
    if not events:
        return None, []
    ev = events[0]
    comp = (ev.get("competitions") or [{}])[0]
    period = (ev.get("status") or {}).get("period")
    out = []
    for c in comp.get("competitors") or []:
        ath = c.get("athlete") or {}
        full = paneltext.panel_text(ath.get("displayName") or ath.get("fullName"))
        if not full:
            continue
        out.append({
            "id": c.get("id"),
            "is_team": False,
            "abbr": paneltext.panel_text(ath.get("shortName") or full),
            "full": full,
            "score": paneltext.panel_text(c.get("score")),
            "place": _num_or_none(c.get("order")),
            "thru": _golf_thru(c, period),
            "hole": None, "tee_time": None, "player_state": None,
            "winner": False, "home_away": None, "color": None,
            "alt_color": None, "record": None, "seed": None,
        })
    meta = {
        "league": paneltext.panel_text(tour),
        "league_name": paneltext.panel_text(tour),
        "name": paneltext.panel_text(ev.get("name")),
        "detail": paneltext.panel_text(((ev.get("status") or {}).get("type") or {}).get("description")),
        "leaderboard": True,
        "state": ((ev.get("status") or {}).get("type") or {}).get("state") or "in",
    }
    return meta, out
