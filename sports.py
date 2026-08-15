"""
sports.py -- free ESPN scoreboard data for the sports ticker mode.

Same shape as market.py/satellite.py/flights.py, deliberately: all I/O lives
here so the mode that draws it stays pure.

One keyless source, ESPN's public (undocumented) site API:
  * site.web.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
    -- today's games across a whole league in one call. Used for the
    rotating ticker AND for the pinned team's score/clock -- cheap, one
    call per league per refresh.
  * site.web.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event=ID
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

HONEST POSTURE. ESPN's site API is undocumented and unofficial: no
published rate limit, no terms covering commercial use, no support.
This house / personal Mac rig may poll it (`espn_use=personal`, the
default). A sellable device must set `espn_use=off` until a licensed
source exists -- we do not have a commercial license, and there is no
working "commercial" mode to invent. Off is the only honest production
lock. The worker then makes no new ESPN HTTP calls and keeps last-good
cache; it never invents scores.
"""
import calendar
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

# site.api.espn.com started returning a blanket Akamai 403 ("Access Denied",
# AkamaiGHost) on EVERY path from this network -- not a UA/header block,
# confirmed by replaying the same request with full browser headers and a
# referer and still getting denied. site.web.api.espn.com serves the exact
# same JSON shape for every endpoint below (scoreboard, summary, the golf
# field, the universal header) -- checked live for all four before
# switching -- so this is a hostname swap, not a new integration.
SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
SUMMARY_URL = "https://site.web.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"
STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/{path}/standings"
STANDINGS_REFRESH = 900.0     # 15 min -- standings are not live scores
STANDINGS_LEAGUES = ("MLB", "NHL", "NBA")

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


# ---- real team lists, for the control panel's dropdowns (2026-08-09) -----
# TEAMS_URL uses the SAME site.web.api.espn.com host every other endpoint
# here already switched to (site.api.espn.com blanket-403s from this
# network, see the module note above `SCOREBOARD_URL`) -- confirmed live
# that /teams serves the identical real shape on that host before using
# it, same "checked live for every endpoint before switching" discipline
# this file already followed for scoreboard/summary/the golf field/the
# universal header.
TEAMS_URL = "https://site.web.api.espn.com/apis/site/v2/sports/{path}/teams"
# Same host/path family as TEAMS_URL. team_id comes from the teams list
# (team.id), never from a guessed mapping or list index.
SCHEDULE_URL = "https://site.web.api.espn.com/apis/site/v2/sports/{path}/teams/{team_id}/schedule"
SCHEDULE_REFRESH = 1800.0  # 30 min -- a future first-pitch does not move often

TEAMS_REFRESH = 86400.0   # 24h -- real team rosters/abbreviations change
                          # maybe once a year (relocations/rebrands), not
                          # worth polling on any faster cadence than this

_teams_cache = {}          # league -> [{abbr, location, name}, ...]
_teams_cache_ts = {}       # league -> epoch seconds of last real fetch


def fetch_teams(league):
    """Real team list for one real league -- [{"abbr", "location", "name"}],
    sorted by location. Cached per-process for TEAMS_REFRESH; a real
    fetch failure falls back to whatever was last cached (even if stale)
    rather than an empty list, same "stale but honest beats a blank
    screen" rule every other feed here follows -- and if nothing has
    EVER been cached, returns [] honestly rather than inventing a team
    list. `league` must be a real LEAGUE_PATHS key; an unknown league
    returns [] without a network call."""
    path = LEAGUE_PATHS.get(league)
    if not path:
        return []
    # Production lock: no new ESPN HTTP, even for the teams dropdown.
    # Last-good cache if this process already fetched; else honest [].
    if load_espn_use() == ESPN_USE_OFF:
        return _teams_cache.get(league) or []
    now = time.time()
    cached = _teams_cache.get(league)
    if (cached is not None
            and now - _teams_cache_ts.get(league, 0) < TEAMS_REFRESH
            and (not cached or all("id" in t for t in cached))):
        return cached
    try:
        data = _get_json(TEAMS_URL.format(path=path))
        raw = data["sports"][0]["leagues"][0]["teams"]
        teams = sorted(
            ({"abbr": paneltext.panel_text(t["team"].get("abbreviation")),
              "location": paneltext.panel_text(t["team"].get("location")),
              "name": paneltext.panel_text(t["team"].get("name")),
              # ESPN team.id (confirmed live: ARI -> "29"). Needed to
              # build SCHEDULE_URL; missing id is stored as None rather
              # than guessed.
              "id": (str(t["team"]["id"]) if t["team"].get("id") is not None
                     else None)}
             for t in raw if t.get("team", {}).get("abbreviation")),
            key=lambda t: t["location"] or "")
    except Exception:                                  # noqa: BLE001 - never die
        return _teams_cache.get(league, [])
    _teams_cache[league] = teams
    _teams_cache_ts[league] = now
    return teams


def fetch_all_teams():
    """{league: [team, ...]} for every real LEAGUE_PATHS entry -- one call
    per league, each independently cached/degraded via fetch_teams()
    above, so one league's real fetch failure never blanks out the
    others."""
    return {league: fetch_teams(league) for league in LEAGUE_PATHS}


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


# ---- ESPN poll posture (espn_use) ----------------------------------------
# Isolated key, same discipline as golf_player: own load/save, do NOT
# widen load_config()'s two-value contract, do NOT wipe leagues /
# favorite / golf_player / favorite_teams. "personal" (default) is this
# house Mac. "off" is the only honest production lock. There is no
# licensed commercial source, so "commercial" is not a mode -- invalid
# or missing values fall back to personal.
ESPN_USE_PERSONAL = "personal"
ESPN_USE_OFF = "off"
ESPN_OFF_ERR = paneltext.panel_text("ESPN OFF")


def load_espn_use():
    """ESPN poll posture, or "personal". Stored in the same
    sports_config.json as the favorite team -- one config file for one
    mode -- but read separately because load_config()'s two-value
    contract is used in several places and widening it would touch all
    of them for no benefit.

    "personal" (this house Mac) may poll ESPN's unofficial site API.
    "off" is the only honest production lock -- we do not have a
    commercial license. Invalid or missing values default to personal.
    """
    if not CONFIG_PATH.exists():
        return ESPN_USE_PERSONAL
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return ESPN_USE_PERSONAL
    v = data.get("espn_use")
    if isinstance(v, str) and v.strip().lower() == ESPN_USE_OFF:
        return ESPN_USE_OFF
    return ESPN_USE_PERSONAL


def save_espn_use(value):
    """Persist the ESPN poll posture. Preserves every other key -- same
    discipline as save_golf_player(), because this file also carries
    leagues/favorite/golf_player/favorite_teams and a naive rewrite
    would wipe them. Only "off" is off; everything else becomes
    personal. "commercial" is not a mode.
    """
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    cleaned = (ESPN_USE_OFF
               if isinstance(value, str) and value.strip().lower() == ESPN_USE_OFF
               else ESPN_USE_PERSONAL)
    data["espn_use"] = cleaned
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


# ---- favorite-teams ticker filter (2026-08-08) --------------------------
# The existing `favorite` field (load_config()/save_config() above) is ONE
# team, and it already has a dedicated full-screen PINNED view -- it isn't
# what this is. This is a SEPARATE, multi-team list that filters the
# UNIVERSAL ticker (every league's rotation) down to only games involving
# a team on the list, across sports -- "is anything I actually care about
# on right now", not "show me my one favorite team's own game full
# screen". Deliberately stored under its own key (`favorite_teams`), not
# folded into `favorite`, so pinning one team for the full PINNED view and
# building a cross-sport watchlist stay two independent choices -- setting
# one was never supposed to require or imply the other.


def load_favorite_teams():
    """Returns (teams, filter_enabled). `teams` is a list of
    {"league", "team_abbr"} dicts (empty list if none set). `filter_enabled`
    is a bool -- having a list saved does not by itself turn the filter on,
    so toggling it off and back on doesn't require re-entering every team."""
    if not CONFIG_PATH.exists():
        return [], False
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return [], False
    raw = data.get("favorite_teams")
    teams = []
    if isinstance(raw, list):
        for t in raw:
            if (isinstance(t, dict) and t.get("league") in LEAGUE_PATHS
                    and t.get("team_abbr")):
                teams.append({"league": str(t["league"]).upper(),
                              "team_abbr": str(t["team_abbr"]).upper()})
    enabled = bool(data.get("favorite_teams_filter"))
    return teams, enabled


def save_favorite_teams(teams, filter_enabled):
    """Persist the favorite-teams list + filter toggle. PRESERVES every
    key this function does not own (leagues/favorite/golf_player/
    tennis_player) -- the identical lesson every other save_* in this
    file already learned about this same config file."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    cleaned = []
    for t in (teams or []):
        if (isinstance(t, dict) and t.get("league") in LEAGUE_PATHS
                and t.get("team_abbr")):
            cleaned.append({"league": str(t["league"]).upper(),
                            "team_abbr": str(t["team_abbr"]).upper()})
    data["favorite_teams"] = cleaned
    data["favorite_teams_filter"] = bool(filter_enabled)
    data.setdefault("leagues", list(DEFAULT_LEAGUES))
    data.setdefault("favorite", None)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return cleaned, bool(filter_enabled)


def event_matches_favorite_teams(ev, teams):
    """True if any REAL competitor on this universal-feed event matches a
    (league, team_abbr) pair in `teams`. Compares against the SAME `abbr`
    field the ticker already displays (`_header_competitor`'s `abbr`), not
    a second name representation -- one source of truth for "what is this
    team called" instead of two that could quietly drift apart. Only
    checks TEAM competitors (`is_team`), never an individual athlete
    (golf/tennis/MMA have no "team" to match here -- they already have
    their own dedicated pinned-player mechanism, this filter is for team
    sports)."""
    if not teams:
        return False
    league = ev.get("league")
    wanted = {t["team_abbr"] for t in teams if t["league"] == league}
    if not wanted:
        return False
    return any(c.get("is_team") and c.get("abbr") in wanted
              for c in (ev.get("competitors") or []))


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


MIN_PRIMARY_VIVIDNESS = 40.0   # below this, a team's own "primary" reads as near-black/gray
MIN_ALT_VIVIDNESS = 60.0       # the alternate must itself be genuinely vivid to be worth it


def _color_vividness(hex_str):
    """How much a real ESPN team-color hex actually reads as A COLOR
    (vs. near-black/near-white/gray) -- chroma weighted by brightness, so
    a dark-but-saturated color still scores low (it won't read well on
    this panel's black background either) while a bright saturated one
    scores high. Returns -1.0 for a missing/malformed hex so it never
    wins a comparison against a real one."""
    if not isinstance(hex_str, str) or len(hex_str) != 6:
        return -1.0
    try:
        r, g, b = (int(hex_str[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return -1.0
    mx, mn = max(r, g, b), min(r, g, b)
    return (mx - mn) * (mx / 255.0)


def _pick_team_hex(primary_hex, alt_hex):
    """Choose which of a team's two REAL ESPN colors to actually lead
    with as its display color.

    Direct owner ask (2026-08-11), with a real example: "pgh teams
    should be the pgh colors, or gold. not just black... but also easy
    to read." Real teams frequently ship a near-black/near-white/gray
    PRIMARY with a far more recognizable and legible secondary --
    confirmed live: Pirates primary=000000 (black) alt=fdb827 (gold);
    Guardians primary=002b5c (navy) alt=e31937 (red); Tigers
    primary=0a2240 (navy) alt=ff4713 (orange); Braves primary=0c2340
    (navy) alt=ba0c2f (red); Astros primary=002d62 (navy)
    alt=eb6e1f (orange).

    This NEVER invents a color -- both candidates are real ESPN-supplied
    hex values for this exact team; it only picks which of the two real
    values to lead with. Swaps to the alternate only when the primary is
    genuinely dull (< MIN_PRIMARY_VIVIDNESS) AND the alternate is itself
    genuinely vivid (>= MIN_ALT_VIVIDNESS) -- confirmed against a real
    26-team sample this threshold pair correctly swaps the five cases
    above while correctly leaving already-vivid or no-better-alternative
    teams alone (Rockies purple, Orioles orange, Reds red, Yankees navy
    -- whose real alternate, pale silver, isn't any more vivid, so it's
    not worth swapping to)."""
    pv = _color_vividness(primary_hex)
    if not alt_hex:
        return primary_hex
    av = _color_vividness(alt_hex)
    if pv < MIN_PRIMARY_VIVIDNESS and av >= MIN_ALT_VIVIDNESS and av > pv:
        return alt_hex
    return primary_hex


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
        "color": _hex_to_rgb(_pick_team_hex(team.get("color"), team.get("alternateColor"))),
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
      * MLB pitch count -- added 2026-08-11, direct owner ask ("score
        bugs even feature that"). Read defensively from two real,
        publicly-documented ESPN shapes (`situation.pitcher.pitchCount`
        and a flatter `situation.pitchCount`, since this project has no
        live-reachable network this session to confirm which one -- or
        whether either -- this specific summary endpoint actually
        returns) -- same honest-degrade contract as `downDistanceText`
        just above: if neither key is present, `pitch_count` is simply
        None and nothing is shown, never a guessed or carried-over
        number. Verify the real key name the first session this sandbox
        can reach ESPN with a live MLB game in progress.
      * MLB batter/on-deck/pitcher -- added 2026-08-11, same owner ask
        ("how is on base or deck... tons of stuff we can add"). Read
        defensively from `situation.batter`/`situation.onDeck`/
        `situation.pitcher` (real, publicly-documented ESPN athlete
        sub-objects on this same summary endpoint -- `pitcher` is the
        same dict `pitchCount` above already reads from), each reduced
        to a short display name. Same honest
        gap as pitch_count -- unverified against a live payload this
        session, absent means absent, never guessed. Deliberately did
        NOT attempt batting average or other box-score stats here: this
        summary endpoint has no confirmed field for a batter's season
        average, on-base%, or similar -- that lives on a real, different
        ESPN endpoint (a boxscore/statistics resource) this project has
        never fetched, and guessing a stat number would be exactly the
        kind of invented data this project's own standing rule forbids.
        A real boxscore endpoint is a genuine, separate follow-up, not a
        one-line addition here.
    """
    if state != "in":
        return None
    sit = comp.get("situation")
    if not isinstance(sit, dict):
        return None
    bases = [bool(sit.get("onFirst")), bool(sit.get("onSecond")), bool(sit.get("onThird"))]
    pitcher = sit.get("pitcher") if isinstance(sit.get("pitcher"), dict) else {}
    pitch_count = pitcher.get("pitchCount")
    if not isinstance(pitch_count, int):
        pitch_count = sit.get("pitchCount") if isinstance(sit.get("pitchCount"), int) else None

    def _athlete_name(a):
        """REAL BUG FIXED 2026-08-11: this used to read `shortName`
        directly off the pitcher/batter dict, but the real scoreboard
        payload nests it one level deeper, under `.athlete` -- confirmed
        live (situation.batter.athlete.shortName == "M. CLARK"). The
        wrong path meant these silently returned None on every single
        real live game, so the panel rendered nothing and looked like
        the feature was never built. Falls back to the flat shape too,
        since the SUMMARY endpoint's own situation is shaped differently
        from the SCOREBOARD's (also confirmed live)."""
        if not isinstance(a, dict):
            return None
        ath = a.get("athlete") if isinstance(a.get("athlete"), dict) else a
        name = ath.get("shortName") or ath.get("displayName")
        return paneltext.panel_text(name) if name else None

    # Pitcher name -- same real athlete sub-object shape as batter/onDeck,
    # just nested one level under `situation.pitcher` (the same dict
    # `pitchCount` above already reads from) rather than a sibling key.
    pitcher_name = _athlete_name(pitcher)

    out = {
        "outs": sit.get("outs") if isinstance(sit.get("outs"), int) else None,
        "balls": sit.get("balls") if isinstance(sit.get("balls"), int) else None,
        "strikes": sit.get("strikes") if isinstance(sit.get("strikes"), int) else None,
        "bases": bases if any(bases) else None,
        "pitch_count": pitch_count,
        "pitcher": pitcher_name,
        "batter": _athlete_name(sit.get("batter")),
        "on_deck": _athlete_name(sit.get("onDeck")),
        # Display-ready string when ESPN supplies one (NFL). Uppercased at
        # the I/O boundary like every other externally-sourced string.
        "down_distance": (paneltext.panel_text(sit.get("downDistanceText"))
                          if sit.get("downDistanceText") else None),
    }
    # FOOTBALL / HOCKEY / BASKETBALL extras -- same honest-degrade
    # contract as pitch_count: read real ESPN keys when they are the
    # documented type, otherwise omit. Never derived into a fake
    # redzone/PP/bonus from score+clock. A missing key is a missing
    # glyph, not a guessed one.
    yl = sit.get("yardLine")
    if isinstance(yl, (int, float)) and 0 <= float(yl) <= 100:
        out["yard_line"] = int(round(float(yl)))
    if isinstance(sit.get("isRedZone"), bool):
        out["is_redzone"] = sit["isRedZone"]
    poss = sit.get("possession")
    if isinstance(poss, str) and poss.strip():
        out["possession"] = paneltext.panel_text(poss)
    elif isinstance(poss, dict):
        # Some payloads nest the possessing team under id/abbreviation.
        tag = poss.get("abbreviation") or poss.get("id")
        if tag:
            out["possession"] = paneltext.panel_text(str(tag))
    down, dist = sit.get("down"), sit.get("distance")
    if isinstance(down, int) and 1 <= down <= 4:
        out["down"] = down
    if isinstance(dist, int) and dist >= 0:
        out["distance"] = dist
    for src, dest in (("homeTimeouts", "home_timeouts"),
                      ("awayTimeouts", "away_timeouts")):
        v = sit.get(src)
        if isinstance(v, int) and 0 <= v <= 8:
            out[dest] = v
    last = sit.get("lastPlay")
    if isinstance(last, dict) and last.get("text"):
        out["last_play"] = paneltext.panel_text(last["text"])
    elif isinstance(last, str) and last.strip():
        out["last_play"] = paneltext.panel_text(last)
    if isinstance(sit.get("isPowerPlay"), bool):
        out["power_play"] = sit["isPowerPlay"]
    # Basketball bonus -- same honest bool as isRedZone. ESPN's
    # documented key names vary; only a real bool is kept. Never
    # inferred from foul count or score.
    for key in ("isBonus", "bonus", "inBonus"):
        if isinstance(sit.get(key), bool):
            out["bonus"] = sit[key]
            break
    strength = sit.get("strength")
    if isinstance(strength, str) and strength.strip():
        out["strength"] = paneltext.panel_text(strength)
    elif isinstance(strength, dict):
        desc = strength.get("description") or strength.get("text")
        if desc:
            out["strength"] = paneltext.panel_text(str(desc))
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


def _iso_to_epoch(iso):
    """ESPN ISO date (e.g. 2026-08-14T23:15Z) -> UTC epoch seconds.

    Returns None if the string is missing or unparseable. Never invents a
    date. Z is UTC; a numeric offset is applied when ESPN actually sends
    one. Display conversion to local is the caller's job (time.localtime
    on this epoch).
    """
    if not isinstance(iso, str) or not iso.strip():
        return None
    s = iso.strip()
    off = 0
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1]
    else:
        m = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            off = sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60)
            s = s[:m.start()]
    if "." in s:
        s = s.split(".", 1)[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s, fmt)) - off
        except ValueError:
            continue
    return None


def _format_next_when(iso):
    """UTC ISO -> local 'FRI 7:15P', or None if the date cannot be parsed.

    time.localtime on the parsed UTC epoch -- the machine's timezone, not
    a guessed venue offset.
    """
    epoch = _iso_to_epoch(iso)
    if epoch is None:
        return None
    t = time.localtime(epoch)
    hour = t.tm_hour % 12 or 12
    ap = "A" if t.tm_hour < 12 else "P"
    return f"{time.strftime('%a', t).upper()} {hour}:{t.tm_min:02d}{ap}"


def _looks_like_abbr(tok):
    t = paneltext.panel_text(tok)
    return 2 <= len(t) <= 5 and all(ch.isalnum() or ch == "&" for ch in t)


def _split_short_name_abbrs(short_name):
    """ESPN shortName 'ARI @ ATL' -> (away, home) if both tokens are abbrs.

    Only splits ESPN's own shortName. Does not invent scores or a matchup
    that the payload did not already spell out.
    """
    if not isinstance(short_name, str) or " @ " not in short_name:
        return None, None
    left, right = short_name.split(" @ ", 1)
    left, right = left.strip(), right.strip()
    if _looks_like_abbr(left) and _looks_like_abbr(right):
        return left, right
    return None, None


def _stub_team_row(abbr, home_away):
    return {
        "abbr": paneltext.panel_text(abbr),
        "name": paneltext.panel_text(abbr),
        "score": None,
        "home_away": home_away,
        "winner": False,
        "color": None,
        "alt_color": None,
        "record": None,
        "rank": None,
    }


def _pick_next_schedule_event(events, now=None):
    """Earliest future `pre` event, or None.

    Compares each event's ISO date to `now` (epoch seconds). Past games
    and non-pre states are skipped. An unparseable date is skipped, never
    guessed. Returns the raw event dict.
    """
    now = time.time() if now is None else now
    best, best_ts = None, None
    for ev in events or []:
        try:
            state = ev["competitions"][0]["status"]["type"]["state"]
        except (KeyError, IndexError, TypeError):
            continue
        if state != "pre":
            continue
        ts = _iso_to_epoch(ev.get("date"))
        if ts is None or ts <= now:
            continue
        if best_ts is None or ts < best_ts:
            best, best_ts = ev, ts
    return best


def _parse_schedule_event(event, league):
    """Schedule event -> game dict, or None.

    Prefers `_parse_event` when the event has the scoreboard competitor
    shape (confirmed present on the 2026 MLB team schedule). If
    competitors are missing, falls back to ESPN's own shortName
    'ARI @ ATL' for display abbreviations only. Adds `date` (raw ISO)
    and `when` (folded local 'FRI 7:15P') -- never invents either.
    """
    parsed = None
    try:
        parsed = _parse_event(event, league)
    except (KeyError, IndexError, TypeError, ValueError):
        parsed = None
    if parsed is None:
        away_abbr, home_abbr = _split_short_name_abbrs(event.get("shortName"))
        if not away_abbr or not home_abbr:
            return None
        try:
            stype = event["competitions"][0]["status"]["type"]
        except (KeyError, IndexError, TypeError):
            stype = {}
        parsed = {
            "event_id": event.get("id"),
            "league": league,
            "short_name": paneltext.panel_text(event.get("shortName")),
            "state": stype.get("state"),
            "completed": bool(stype.get("completed")),
            "detail": paneltext.panel_text(
                stype.get("shortDetail") or stype.get("detail")),
            "period": None,
            "situation": None,
            "display_clock": None,
            "home": _stub_team_row(home_abbr, "home"),
            "away": _stub_team_row(away_abbr, "away"),
        }
    iso = event.get("date") if isinstance(event.get("date"), str) else None
    parsed["date"] = iso
    when = _format_next_when(iso)
    parsed["when"] = paneltext.panel_text(when) if when else None
    return parsed


def _team_id_for(league, team_abbr):
    """Resolve ESPN team.id from the existing teams list. None if unknown.

    Identity is league + abbreviation, never a list index. Reuses
    fetch_teams() (24h cache) so this is not a second teams poll.
    """
    want = paneltext.panel_text(team_abbr)
    for t in fetch_teams(league):
        if t.get("abbr") == want and t.get("id"):
            return str(t["id"])
    return None


def _fetch_favorite_next(league, team_abbr):
    """Next future pre game for this favorite, or None if there is none.

    Raises on transport / teams-list failure so the worker can keep
    last-good. A successful empty schedule is None, not an exception.
    """
    path = LEAGUE_PATHS.get(league)
    if not path:
        return None
    teams = fetch_teams(league)
    team_id = _team_id_for(league, team_abbr)
    if not team_id:
        if not teams:
            raise RuntimeError("teams list unavailable")
        return None
    data = _get_json(SCHEDULE_URL.format(path=path, team_id=team_id))
    ev = _pick_next_schedule_event(data.get("events") or [])
    if not ev:
        return None
    return _parse_schedule_event(ev, league)


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


# ---- standings (MLB / NHL / NBA) ---------------------------------------
# site.web.api.espn.com/apis/v2/sports/{path}/standings -- confirmed live
# 2026-08-12: children[] are conferences (AL/NL, East/West). Each child's
# standings.entries[] is already in table order. Stats are a named list,
# NEVER a positional array -- zip by name or abbreviation. Team color
# hexes are often absent on this endpoint; missing means omit, never
# invent. If a child has its own children (divisions) with entries,
# those deeper tables win over the conference rollup.

def _zip_standings_stats(stats):
    """Map lowercased name AND abbreviation -> the stat object.

    ESPN sends a bag of {name, abbreviation, displayValue, value} --
    column order is not a contract. Later keys overwrite earlier ones
    only when they share a token; name and abbr for the same object
    point at the same dict, so that is not a collision."""
    out = {}
    for s in stats or []:
        if not isinstance(s, dict):
            continue
        for key in (s.get("name"), s.get("abbreviation")):
            if isinstance(key, str) and key.strip():
                out[key.strip().lower()] = s
    return out


def _stat_display_or_int(stat):
    """displayValue if ESPN sent one, else a whole-number value as a
    string. None when neither is a real number we can show -- never
    guessed, never formatted from a sibling stat."""
    if not isinstance(stat, dict):
        return None
    dv = stat.get("displayValue")
    if isinstance(dv, str) and dv.strip() != "":
        return paneltext.panel_text(dv)
    if isinstance(dv, int) and not isinstance(dv, bool):
        return str(dv)
    val = stat.get("value")
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return None


def _stat_by_keys(smap, *keys):
    for k in keys:
        if k and k.lower() in smap:
            return smap[k.lower()]
    return None


def _stat_seed(smap):
    """playoffSeed as an int, or None. ESPN sends value=1.0; only a
    whole number counts as a seed."""
    stat = _stat_by_keys(smap, "playoffseed", "seed")
    if not stat:
        return None
    val = stat.get("value")
    if isinstance(val, bool) or val is None:
        dv = stat.get("displayValue")
        if isinstance(dv, int) and not isinstance(dv, bool):
            return dv
        if isinstance(dv, str) and dv.strip().lstrip("-").isdigit():
            return int(dv.strip())
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and val == int(val):
        return int(val)
    return None


def _parse_standings_row(entry, league):
    if not isinstance(entry, dict):
        return None
    team = entry.get("team") or {}
    if not isinstance(team, dict):
        return None
    abbr = paneltext.panel_text(team.get("abbreviation"))
    if not abbr:
        return None
    row = {
        "abbr": abbr,
        "name": paneltext.panel_text(
            team.get("shortDisplayName") or team.get("name")),
    }
    color = _hex_to_rgb(_pick_team_hex(team.get("color"),
                                       team.get("alternateColor")))
    if color:
        row["color"] = color
    smap = _zip_standings_stats(entry.get("stats"))
    wins = _stat_display_or_int(_stat_by_keys(smap, "wins", "w"))
    if wins is not None:
        row["wins"] = wins
    losses = _stat_display_or_int(_stat_by_keys(smap, "losses", "l"))
    if losses is not None:
        row["losses"] = losses
    # OTL is the NHL convention. ESPN also ships OTLosses on MLB
    # extra-inning games; those are not an NHL-style third column, so
    # they stay off the row unless this is actually NHL and ESPN sent
    # the field.
    if league == "NHL":
        otl = _stat_display_or_int(
            _stat_by_keys(smap, "otlosses", "otl", "overtimelosses"))
        if otl is not None:
            row["otl"] = otl
    gb_stat = _stat_by_keys(smap, "gamesbehind", "gb")
    if gb_stat:
        gb = gb_stat.get("displayValue")
        if isinstance(gb, str) and gb.strip() and gb.strip() != "-":
            folded = paneltext.panel_text(gb)
            if folded and folded != "-":
                row["gb"] = folded
    pct = _stat_display_or_int(_stat_by_keys(smap, "winpercent", "pct"))
    if pct is not None:
        row["pct"] = pct
    # Standings points are abbreviation PTS (NHL). MLB's `points` stat
    # is GBP -- games-behind points -- and is not a points column.
    pts_stat = None
    for key in ("pts", "points"):
        cand = smap.get(key)
        if cand and str(cand.get("abbreviation") or "").upper() == "PTS":
            pts_stat = cand
            break
    pts = _stat_display_or_int(pts_stat) if pts_stat else None
    if pts is not None:
        row["pts"] = pts
    seed = _stat_seed(smap)
    if seed is not None:
        row["seed"] = seed
    return row


def _group_label(node):
    raw = (node.get("abbreviation") or node.get("shortName")
           or node.get("name"))
    return paneltext.panel_text(raw) if raw else ""


def _standings_groups(node, league):
    """Deepest groups that actually have entries.

    A conference with division children that have rows yields those
    divisions. A conference whose children are empty (or missing)
    yields its own standings.entries, if any. ESPN's own order is
    kept -- this does not sort."""
    if not isinstance(node, dict):
        return []
    children = node.get("children")
    deeper = []
    if isinstance(children, list):
        for child in children:
            deeper.extend(_standings_groups(child, league))
    if deeper:
        return deeper
    entries = ((node.get("standings") or {}).get("entries")
               if isinstance(node.get("standings"), dict) else None) or []
    rows = []
    for entry in entries:
        row = _parse_standings_row(entry, league)
        if row:
            rows.append(row)
    if not rows:
        return []
    return [{"name": _group_label(node), "rows": rows}]


def parse_standings(league, payload):
    """Normalize one ESPN standings payload into
    {league, groups:[{name, rows:[...]}]}.

    Missing ESPN fields are omitted, never invented. Rows stay in
    ESPN's standings order. `league` is the LEAGUE_PATHS key (MLB /
    NHL / NBA)."""
    code = str(league or "").upper()
    folded = paneltext.panel_text(code) or code
    if not isinstance(payload, dict):
        return {"league": folded, "groups": []}
    return {"league": folded, "groups": _standings_groups(payload, code)}


def _copy_standings(standings):
    out = {}
    for lg, parsed in (standings or {}).items():
        groups = []
        for g in (parsed.get("groups") or []):
            groups.append({
                "name": g.get("name"),
                "rows": [dict(r) for r in (g.get("rows") or [])],
            })
        out[lg] = {"league": parsed.get("league", lg), "groups": groups}
    return out


def _boxscore_stats(data):
    """Real per-athlete boxscore stats for a baseball game, keyed by the
    athlete's real ESPN id (a string).

    CONFIRMED LIVE 2026-08-11 against a real in-progress MLB game
    (CLE @ DET, event 401816481) -- this is NOT the defensive
    read-and-hope shape the rest of this file's MLB extras shipped as.
    The summary endpoint's `boxscore.players[]` carries two real stat
    blocks per team, each with its own `labels` list naming the columns
    and a parallel `stats` list per athlete:

      * BATTING labels: H-AB, AB, R, H, RBI, HR, BB, K, #P, AVG, OBP, SLG
        -- real example: S. Kwan -> ['1-3','3','1','1','0','0','1','0',
        '19','.266','.365','.329']. `AVG` is the real season batting
        average the owner asked for; `H-AB` is today's real line.
      * PITCHING labels: IP, H, R, ER, BB, K, HR, PC-ST, ERA, PC
        -- real example: T. Bibee -> ['6.0','5','4','4','0','4','0',
        '81-56','3.94','81']. `PC` is the real PITCH COUNT.

    This is why the earlier `situation.pitcher.pitchCount` guess never
    rendered anything: that key does not exist on this endpoint at all.
    The only `pitchCount` present is per-play (`plays[].pitchCount` =
    {balls, strikes}, the count within one at-bat), NOT a game total.

    Zipping `labels` to `stats` by POSITION rather than hardcoding
    indices, so a column ESPN adds or reorders can't silently shift
    every value one slot over -- that would produce confidently wrong
    numbers, the exact failure this project's "never invent" rule
    exists to prevent."""
    out = {}
    for team in (data.get("boxscore") or {}).get("players") or []:
        for block in team.get("statistics") or []:
            labels = block.get("labels")
            if not isinstance(labels, list):
                continue
            for a in block.get("athletes") or []:
                stats = a.get("stats")
                ath = a.get("athlete") or {}
                aid = ath.get("id")
                if not aid or not isinstance(stats, list):
                    continue
                row = out.setdefault(str(aid), {})
                row.update(dict(zip(labels, stats)))
                name = ath.get("shortName") or ath.get("displayName")
                if name:
                    row["_name"] = paneltext.panel_text(name)
    return out


def fetch_baseball_matchup(league, event_id, data=None):
    """The real current pitcher-vs-batter matchup for a live MLB game,
    with the real stats a broadcast scorebug actually shows.

    Direct owner ask, twice ("pitch count, batting average... whos on or
    next up, pitcher and pitch count"). Everything here is CONFIRMED
    against a real live payload (see `_boxscore_stats()` for the exact
    real rows) -- no defensive guessing at key names, which is what made
    the first attempt at this render nothing at all.

    Returns None if this isn't a real live baseball game with a real
    current pitcher/batter, or {} keys omitted individually when a
    specific real stat is genuinely absent. Never a guessed number.

    ON-DECK IS DELIBERATELY NOT INCLUDED, and that is a real finding,
    not an oversight: the string "onDeck" appears ZERO times anywhere in
    this endpoint's entire real response (checked directly). ESPN simply
    does not publish the on-deck batter here. Inventing one from the
    lineup's bat order would be guessing at a real fact -- a pinch
    hitter or a mid-inning substitution makes batting-order arithmetic
    wrong exactly when it matters. If a real field for it ever turns up
    on another endpoint, that's a genuine follow-up.

    Same narrow per-game scope every other summary-endpoint fetch in
    this file uses (win probability, the big-moment detectors): ONE
    game, the one actually on screen, never every game in a league."""
    path = LEAGUE_PATHS.get(league)
    if not path:
        return None
    if data is None:
        data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    sit = data.get("situation")
    if not isinstance(sit, dict):
        return None
    stats = _boxscore_stats(data)

    def _side(key, fields):
        who = sit.get(key)
        if not isinstance(who, dict):
            return None
        # The SUMMARY endpoint's situation carries only {"playerId": N};
        # the SCOREBOARD endpoint's carries a full nested `athlete`.
        # Both are real and both are handled -- confirmed live, this
        # asymmetry between two ESPN endpoints is genuine.
        pid = who.get("playerId")
        if pid is None:
            pid = (who.get("athlete") or {}).get("id")
        if pid is None:
            return None
        row = stats.get(str(pid))
        if not row:
            return None
        out = {"name": row.get("_name")}
        for label, dest in fields:
            v = row.get(label)
            if v not in (None, "", "-"):
                out[dest] = v
        return out if out.get("name") else None

    pitcher = _side("pitcher", [("PC", "pitch_count"), ("ERA", "era"),
                                ("IP", "ip"), ("K", "k"), ("ER", "er")])
    batter = _side("batter", [("AVG", "avg"), ("H-AB", "today"),
                              ("HR", "hr"), ("RBI", "rbi")])
    if not pitcher and not batter:
        return None
    return {"pitcher": pitcher, "batter": batter}


# ESPN MLB summary `plays[].pitchCoordinate` canvas. Confirmed live
# 2026-08-12 on BAL@MIN (401816500) and PHI@STL: x ~32..187, y ~102..262,
# y increases downward (a high pitch has a SMALLER y). There is no named
# strike-zone rectangle in the payload. The inner box below is the
# called-strike envelope from those two games (98% of Strike Looking
# inside, 98% of Balls outside) -- a plot frame, not Statcast rulebook
# feet. A missing coordinate is a missing dot.
ESPN_PITCH_X0, ESPN_PITCH_X1 = 40.0, 180.0
ESPN_PITCH_Y0, ESPN_PITCH_Y1 = 110.0, 250.0
ESPN_ZONE_X0, ESPN_ZONE_X1 = 89.0, 150.0
ESPN_ZONE_Y0, ESPN_ZONE_Y1 = 142.0, 193.0

_PITCH_SKIP = {"END BATTER/PITCHER", "PLAY RESULT"}


def _mlb_pitch_kind(type_text):
    t = (type_text or "").upper()
    if not t or t in _PITCH_SKIP:
        return None
    if t.startswith("BALL"):
        return "ball"
    if "FOUL" in t:
        return "foul"
    if "LOOKING" in t:
        return "looking"
    if "SWINGING" in t:
        return "swinging"
    if t.startswith("STRIKE"):
        return "strike"
    return "inplay"


def _mlb_pitches_from_payload(data):
    """Every real pitch in a summary `plays` list. ZERO I/O."""
    plays = data.get("plays") if isinstance(data, dict) else None
    if not isinstance(plays, list):
        return []
    out = []
    for p in plays:
        if not isinstance(p, dict):
            continue
        kind = _mlb_pitch_kind((p.get("type") or {}).get("text"))
        if not kind:
            continue
        coord = p.get("pitchCoordinate")
        if not isinstance(coord, dict):
            continue
        x, y = coord.get("x"), coord.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        pt = p.get("pitchType") if isinstance(p.get("pitchType"), dict) else {}
        vel = p.get("pitchVelocity")
        out.append({
            "id": str(p.get("id") or ""),
            "atbat": str(p.get("atBatId") or ""),
            "n": p.get("atBatPitchNumber") if isinstance(p.get("atBatPitchNumber"), int) else None,
            "x": float(x),
            "y": float(y),
            "kind": kind,
            "type": paneltext.panel_text(pt.get("abbreviation") or pt.get("text")) or None,
            "vel": vel if isinstance(vel, int) else None,
            "text": paneltext.panel_text(p.get("text")) or None,
        })
    return out


def last_atbat_pitches(pitches):
    """Pitches from the current (or just-finished) plate appearance."""
    if not isinstance(pitches, list) or not pitches:
        return []
    ab = pitches[-1].get("atbat")
    if not ab:
        return pitches[-8:]
    return [p for p in pitches if p.get("atbat") == ab]


def _int_if_sent(v):
    """Parse a real ESPN number, or None. Never invents 0 from a miss."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _mlb_inning_runs(display_value):
    """One linescore displayValue -> int runs, or None.

    Digits (including ESPN's own "0") become that int. "X" -- home
    did not bat -- is None, never a fake 0. Any other token is also
    None rather than guessed.
    """
    if isinstance(display_value, bool):
        return None
    if isinstance(display_value, int):
        return display_value
    if display_value is None:
        return None
    s = str(display_value).strip()
    if s.isdigit():
        return int(s)
    return None


def _mlb_line_side(competitor):
    """One header competitor -> {abbr, runs, hits, errors, innings}."""
    team = competitor.get("team") if isinstance(competitor.get("team"), dict) else {}
    abbr = team.get("abbreviation") or competitor.get("abbreviation")
    innings = []
    lines = competitor.get("linescores")
    if isinstance(lines, list):
        for ls in lines:
            if not isinstance(ls, dict):
                continue
            innings.append(_mlb_inning_runs(ls.get("displayValue")))
    return {
        "abbr": paneltext.panel_text(abbr) if abbr else "",
        "runs": _int_if_sent(competitor.get("score")),
        "hits": _int_if_sent(competitor.get("hits")),
        "errors": _int_if_sent(competitor.get("errors")),
        "innings": innings,
    }


def _mlb_line_score_from_payload(data):
    """header.competitions[0].competitors -> {away, home} or None.

    Confirmed live 2026-08-12, BAL @ MIN event 401816500 (state=post):
    each competitor carries hits/errors as ints and linescores[] with
    one {displayValue} per inning actually played. Home (MIN) had 8
    entries -- they did not bat in the 9th -- and that omitted inning
    stays omitted here, never padded with a fake 0. displayValue "X"
    becomes None, not 0. Returns None when neither side has linescores.
    """
    if not isinstance(data, dict):
        return None
    header = data.get("header") if isinstance(data.get("header"), dict) else data
    comps = header.get("competitions") if isinstance(header, dict) else None
    if not isinstance(comps, list) or not comps:
        comps = data.get("competitions")
    if not isinstance(comps, list) or not comps:
        return None
    comp = comps[0] if isinstance(comps[0], dict) else None
    if not comp:
        return None
    competitors = comp.get("competitors")
    if not isinstance(competitors, list):
        return None
    away = next((c for c in competitors
                 if isinstance(c, dict) and c.get("homeAway") == "away"), None)
    home = next((c for c in competitors
                 if isinstance(c, dict) and c.get("homeAway") == "home"), None)
    if not away or not home:
        return None
    away_side, home_side = _mlb_line_side(away), _mlb_line_side(home)
    if not away_side["innings"] and not home_side["innings"]:
        return None
    return {"away": away_side, "home": home_side}


def _win_prob_from_payload(data):
    wp = data.get("winprobability")
    if not isinstance(wp, list) or not wp:
        return None
    last = wp[-1]
    pct = last.get("homeWinPercentage")
    return float(pct) if isinstance(pct, (int, float)) else None


def summarize_payload(league, event_id, data):
    """One summary JSON -> every per-game fact the engine reads.

    Called only from the feed worker. The engine never sees the raw
    payload and never hits SUMMARY_URL itself. Missing pieces stay
    missing -- a quiet key is not filled in from another sport.
    """
    if not isinstance(data, dict):
        return {}
    soccer_goals = _soccer_goals_from_payload(data)
    mma_cur, mma_total = _mma_rounds_from_payload(data)
    return {
        "event_id": event_id,
        "league": league,
        "win_prob": _win_prob_from_payload(data),
        "matchup": (fetch_baseball_matchup(league, event_id, data=data)
                    if league == "MLB" else None),
        "home_runs": _fetch_home_run_plays(league, event_id, data=data),
        "touchdowns": _fetch_touchdown_plays(league, event_id, data=data),
        "goals": _fetch_goal_plays(league, event_id, data=data),
        "clutch": _fetch_clutch_plays(league, event_id, data=data),
        "soccer_goals": soccer_goals,
        "last_goal": last_goal_from_list(soccer_goals),
        "mma_method": _mma_finish_from_payload(data),
        "current_round": mma_cur,
        "total_rounds": mma_total,
        "atbat_pitches": last_atbat_pitches(_mlb_pitches_from_payload(data)),
        "line_score": _mlb_line_score_from_payload(data),
    }


def summary_path(league, sport=None):
    """ESPN summary path for a league code, a header slug, or a
    universal-header display name (UFC/PFL). None if this project
    has no honest path -- caller must not invent one.

    Soccer on the universal header ships as `eng.1` / `usa.1`, not
    `EPL`. Those slugs are the same tokens LEAGUE_PATHS already
    uses after the sport prefix, so `soccer/{slug}` is a real path
    rather than a guess. MMA's per-event summary 404s (see mma.py);
    UFC/PFL still resolve here so a future working endpoint is
    one line, but the engine should not poll them for live state.
    """
    if league in LEAGUE_PATHS:
        return LEAGUE_PATHS[league]
    raw = (league or "").strip()
    if not raw:
        return None
    if raw.upper() in ("UFC", "PFL"):
        return f"mma/{raw.lower()}"
    if (sport or "").lower() == "soccer":
        return f"soccer/{raw.lower()}"
    return None


def _soccer_goals_from_payload(data):
    """Parse keyEvents scoring plays. ZERO I/O. Same facts
    fetch_new_soccer_goals() used, just without a network call."""
    events = data.get("keyEvents") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    out = []
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("scoringPlay"):
            continue
        key = ev.get("id")
        if key is None:
            continue
        out.append({
            "id": str(key),
            "type": paneltext.panel_text((ev.get("type") or {}).get("text")),
            "text": paneltext.panel_text(ev.get("text")),
            "scorer": _play_scorer(ev),
        })
    return out


def _mma_finish_from_payload(data):
    """Same method extraction as _fetch_mma_finish_method, no I/O."""
    if not isinstance(data, dict):
        return None
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


def _goal_is_own_or_shootout(goal):
    """True for own-goals and shootout conversions. Regular-time
    penalties (`Penalty - Scored`) stay -- those are real goals."""
    blob = " ".join(str(x) for x in (goal.get("type"), goal.get("text")) if x)
    u = blob.upper()
    return "OWN GOAL" in u or "SHOOTOUT" in u


def last_goal_from_list(goals):
    """Newest non-own-goal, non-shootout scoring play, or None.

    Walks newest-first so a later own-goal does not erase the last
    real scorer. Missing list / empty list / every play skipped
    returns None -- never a guessed name.
    """
    if not isinstance(goals, list):
        return None
    for g in reversed(goals):
        if isinstance(g, dict) and not _goal_is_own_or_shootout(g):
            return g
    return None


def _mma_rounds_from_payload(data):
    """(current_round, scheduled_rounds) from a summary payload.

    Same `format.regulation.periods` field mma.py already verified
    on the UFC scoreboard. Read defensively -- MMA's per-event
    summary 404s today, so both values stay None until a real
    payload actually carries them.
    """
    if not isinstance(data, dict):
        return None, None
    header = data.get("header") if isinstance(data.get("header"), dict) else data
    comps = header.get("competitions") if isinstance(header, dict) else None
    if not isinstance(comps, list) or not comps:
        comps = data.get("competitions")
    if not isinstance(comps, list) or not comps:
        return None, None
    comp = comps[0] if isinstance(comps[0], dict) else None
    if not comp:
        return None, None
    periods = ((comp.get("format") or {}).get("regulation") or {}).get("periods")
    total = periods if isinstance(periods, int) and 1 <= periods <= 5 else None
    st = comp.get("status") if isinstance(comp.get("status"), dict) else {}
    cur = st.get("period")
    current = cur if isinstance(cur, int) and cur >= 1 else None
    return current, total


def _athlete_scorer(ath):
    if not isinstance(ath, dict):
        return None
    jersey = ath.get("jersey")
    pos = ath.get("position")
    pos_abbr = pos.get("abbreviation") if isinstance(pos, dict) else None
    name = ath.get("shortName") or ath.get("displayName")
    if not (jersey or pos_abbr or name):
        return None
    return {
        "jersey": paneltext.panel_text(str(jersey)) if jersey else None,
        "position": paneltext.panel_text(str(pos_abbr)) if pos_abbr else None,
        "name": paneltext.panel_text(str(name)) if name else None,
    }


def _play_scorer(p):
    """Real jersey number/position/name for whoever's credited on a real
    scoring play, or None. 2026-08-10, direct owner ask ("player
    highlight after key plays").

    HONEST GAP, stated per this project's own standing precedent: ESPN's
    real per-play `participants` list (commonly `[{"athlete": {...},
    "type": "scorer"}, ...]` on other ESPN site-API endpoints this
    project has NOT independently confirmed for THIS summary endpoint)
    is read defensively here, the same way `_situation()`'s own
    down/distance numeric subfields are documented as "read defensively
    but NOT verified" -- this sandbox's network to ESPN is unreachable
    this session (same class of block already documented elsewhere in
    this file), so this has not been checked against a real live
    payload. Returns None on ANY unexpected shape (missing key, wrong
    type, empty list) rather than guessing -- a missing highlight is
    honest; a wrong player's jersey number is not. Verify against a real
    live scoring play the first session this sandbox can reach ESPN.
    """
    participants = p.get("participants")
    if isinstance(participants, list) and participants:
        scorer = next((x for x in participants
                       if isinstance(x, dict) and x.get("type") == "scorer"), participants[0])
        ath = scorer.get("athlete") if isinstance(scorer, dict) else None
        got = _athlete_scorer(ath)
        if got:
            return got
    # Soccer keyEvents often credit the scorer on athletesInvolved
    # instead of (or as well as) participants. Same defensive read:
    # missing / wrong shape -> None, never a guessed name.
    involved = p.get("athletesInvolved")
    if isinstance(involved, list) and involved:
        first = involved[0] if isinstance(involved[0], dict) else None
        nested = first.get("athlete") if isinstance(first, dict) else None
        got = _athlete_scorer(nested if isinstance(nested, dict) else first)
        if got:
            return got
    return None


def _fetch_home_run_plays(league, event_id, data=None):
    """MLB home-run plays from a game's play-by-play, or [] for anything
    else. Returns a list of {"id": str, "text": str}, text already
    paneltext.panel_text()-folded (this is the I/O boundary -- see
    paneltext.py's docstring on why the fold belongs here, not in the
    caller).

    `data` is an already-fetched summary payload (the off-thread
    worker). When present, this function does ZERO I/O -- it only
    parses. The engine must never call this without `data` on the
    render tick.

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
    if data is None:
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
            "scorer": _play_scorer(p),
        })
    return out


def _fetch_touchdown_plays(league, event_id, data=None):
    """NFL/NCAAF touchdown plays from a game's `scoringPlays` array, or []
    for anything else. Returns a list of {"id": str, "text": str,
    "awayScore": int, "homeScore": int}, text already
    paneltext.panel_text()-folded (this is the I/O boundary, same
    discipline as _fetch_home_run_plays()).

    Confirmed live (event 401873271, Panthers @ Cardinals, finished NFL
    game): the top-level key that actually carries scoring plays for NFL
    is `scoringPlays`, NOT `plays` (MLB's key) and NOT `keyEvents`
    (soccer's key, empty for this NFL event) -- checked all three on the
    real payload before picking this one. Each entry has a `type.text`
    like "Rushing Touchdown" or "Passing Touchdown"; a touchdown is any
    entry whose `type.text` contains the substring "Touchdown"
    (case-sensitive, matching the real observed casing), deliberately not
    an exhaustive enum -- ESPN has other real variants not seen in this
    one sample (e.g. "Return Touchdown") and a substring match is generic
    to all of them without needing a future update every time ESPN adds a
    new touchdown flavor. Field goals, safeties, and extra points are
    real scoring plays that must NOT fire this -- confirmed their
    `type.text` values do not contain "Touchdown".

    Widened to NCAAF 2026-08-08: NCAAF is the same "football" sport
    family on ESPN's site API, using the identical `SUMMARY_URL` template
    and the identical `scoringPlays` shape -- confirmed live, not
    assumed, against a real finished NCAAF game (event 401769072, Alabama
    @ Indiana, 2026 CFP semifinal): the same top-level `scoringPlays` key,
    the same `type.text` values ("Passing Touchdown", "Rushing
    Touchdown", "Field Goal Good"), same substring-match logic holding
    unchanged. Both leagues share one summary-endpoint shape, so one
    function covers both rather than forking a near-duplicate.

    Same per-game summary endpoint _fetch_win_prob() already uses --
    deliberately NOT a new endpoint, so the request-volume discipline
    (only the pinned favorite's own LIVE game, see the caller in
    engines.py) is the only thing standing between this and the
    per-league-poll volume risk CLAUDE.md already flags twice.
    """
    if league not in ("NFL", "NCAAF"):
        return []
    if data is None:
        path = LEAGUE_PATHS[league]
        data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    plays = data.get("scoringPlays")
    if not isinstance(plays, list):
        return []
    out = []
    for p in plays:
        ptype = p.get("type") or {}
        if "Touchdown" not in (ptype.get("text") or ""):
            continue
        pid = p.get("id")
        if pid is None:
            continue
        out.append({
            "id": str(pid),
            "text": paneltext.panel_text(p.get("text") or ""),
            "awayScore": p.get("awayScore"),
            "homeScore": p.get("homeScore"),
            "scorer": _play_scorer(p),
        })
    return out


def _fetch_goal_plays(league, event_id, data=None):
    """NHL goal plays from a game's `scoringPlays` array, or [] for
    anything else. Returns a list of {"id": str, "text": str, "awayScore":
    int, "homeScore": int}, text already paneltext.panel_text()-folded --
    same I/O-boundary discipline as _fetch_home_run_plays() /
    _fetch_touchdown_plays().

    UNVERIFIED against real live data -- every NHL event in today's
    scoreboard is `state == "pre"` (off-season), so no live NHL
    `scoringPlays` payload has been observed this session. Built on the
    same `scoringPlays` shape confirmed live for both NFL (see
    _fetch_touchdown_plays()) and MLB's sibling `plays` key, since ESPN's
    site API uses one unified shape across these team sports. The
    assumption made here, NOT confirmed against a real payload: a goal is
    a scoring play whose `type.text` == "Goal" (exact match, not a
    substring -- a "Penalty Shot Goal" or similar more-specific variant,
    if ESPN uses one, would NOT match this and would need its own
    handling once a real payload is seen). Documented honestly as
    unconfirmed, same precedent as mma.py's finish-method reconstruction
    shipping unverified when no live MMA event existed.

    Same per-game summary endpoint _fetch_win_prob() already uses --
    deliberately NOT a new endpoint, matching the request-volume
    discipline of the other big-moment fetchers.
    """
    if league != "NHL":
        return []
    if data is None:
        path = LEAGUE_PATHS[league]
        data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    plays = data.get("scoringPlays")
    if not isinstance(plays, list):
        return []
    out = []
    for p in plays:
        ptype = p.get("type") or {}
        if (ptype.get("text") or "") != "Goal":
            continue
        pid = p.get("id")
        if pid is None:
            continue
        out.append({
            "id": str(pid),
            "text": paneltext.panel_text(p.get("text") or ""),
            "awayScore": p.get("awayScore"),
            "homeScore": p.get("homeScore"),
            "scorer": _play_scorer(p),
        })
    return out


# How many seconds of real clock remain in the period for a scoring play
# to even be CONSIDERED clutch. A judgment call, not a measured fact --
# there is no ESPN field that says "this moment was clutch", so this is
# reasoned the same way WINDOW_MAX_NM_DEFAULT (flights.py) was reasoned
# about: the final 2 minutes of the final period/half is the common
# broadcast/fan notion of "clutch time" in basketball (it's the point
# announcers start saying it out loud), and it is short enough that it
# stays rare relative to the ~90 real scoring plays a full game produces
# (see _fetch_clutch_plays()'s own docstring for the real count observed
# this session). Config-driven would be reasonable future work but this
# project's other per-sport thresholds (golf's EAGLE/BIRDIE-only firing)
# are also plain constants, not configurable, so this matches precedent.
CLUTCH_WINDOW_SECONDS = 120.0


def _clock_seconds_remaining(clock):
    """Parses a basketball play's `clock.displayValue` into real seconds
    remaining in the period, or None if unparseable.

    CONFIRMED LIVE 2026-08-08 against a real finished WNBA game (event
    401857125, LV @ MIN) that the format is NOT uniform, contrary to a
    naive read of a single sample: for most of a period `displayValue` is
    "M:SS" ("9:43", "10:00"), but once the real clock drops under one
    minute remaining, ESPN switches to a bare decimal-seconds string
    ("43.5", "18.1", "0.0") with no colon at all -- checked programmatically
    across all 382 real plays in that game (336 colon-form, 46 bare-decimal
    form, and every bare-decimal one was inside the final minute of its
    period). Both forms are handled here rather than assuming one; a
    string matching neither (missing clock, unexpected shape) returns
    None so a caller can skip it rather than mis-parse a play as
    clutch-eligible.
    """
    dv = (clock or {}).get("displayValue")
    if not dv:
        return None
    try:
        if ":" in dv:
            m, s = dv.split(":", 1)
            return float(m) * 60.0 + float(s)
        return float(dv)
    except (TypeError, ValueError):
        return None


# NBA is 4 real quarters; NCAAB is 2 real halves -- CONFIRMED live
# 2026-08-08 against a real finished NCAAB game (event 401825568): every
# play's `period.number` tops out at 2 with `period.displayValue` reading
# "1st Half"/"2nd Half", never "Quarter". So "final period" is NOT the
# same literal number across the two leagues -- NBA's final regulation
# period is 4, NCAAB's is 2. Overtime periods (5+/3+) are naturally
# covered by the same ">=" comparison without a separate branch.
FINAL_PERIOD_BY_LEAGUE = {"NBA": 4, "NCAAB": 2}


def _play_shot_xy(p):
    """Real shot x/y for a basketball play, or None. 2026-08-10, direct
    owner ask ("NBA mini shot-location court").

    Deliberately reuses THIS project's own already-fetched
    `_fetch_clutch_plays()` summary payload (`play.coordinate.x/y`, a
    real ESPN field documented across multiple independent public ESPN-
    API tools) rather than adding stats.nba.com as a brand-new external
    data source -- a different domain with its own header/rate-limit
    quirks this project has no existing relationship with, and zero new
    I/O beyond what the clutch-shot detector already fetches.

    HONEST GAP: this sandbox's network to ESPN is unreachable this
    session (same documented block as every other new field added this
    pass), so the real coordinate SYSTEM (which corner is origin, full-
    court vs half-court feet) is not independently confirmed against a
    live payload. Returns None on any missing/non-numeric field rather
    than guessing -- an absent shot dot is honest; a wrong one is not.
    Verify against a real live clutch shot the first session this
    sandbox can reach ESPN."""
    coord = p.get("coordinate")
    if not isinstance(coord, dict):
        return None
    x, y = coord.get("x"), coord.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return {"x": float(x), "y": float(y)}


def _fetch_clutch_plays(league, event_id, data=None):
    """Basketball "clutch shot" plays from a game's `plays` play-by-play
    list, or [] for anything else. Returns a list of {"id": str, "text":
    str, "awayScore": int, "homeScore": int}, text already
    paneltext.panel_text()-folded (I/O boundary, same discipline as every
    other _fetch_*_plays() in this module).

    Basketball's summary payload has NO `scoringPlays` key at all --
    confirmed live 2026-08-08 against a real finished WNBA game (event
    401857125, LV @ MIN, used only as a real schema reference; WNBA is
    NOT a supported league here, see the league check below) -- unlike
    NFL/NCAAF/NHL. The real key is `plays` (MLB's sibling key, but a full
    play-by-play list here, not just scoring plays -- 382 real entries in
    that one game), and a scoring entry is marked `scoringPlay: true`
    (93 of the 382 were real scoring plays in that game).

    Unlike every other _fetch_*_plays() here, "any scoring play" is
    deliberately NOT the trigger -- basketball scores far too often for
    that to mean anything (93 real scoring plays in one real game is
    roughly one every 30 real seconds of game clock). The real, derivable
    signal built instead is a CLUTCH SHOT: a real scoring play that is
    ALL of --
      (a) in the final period or later for that league's real period
          convention (see FINAL_PERIOD_BY_LEAGUE -- NBA quarters vs
          NCAAB halves, confirmed live, not guessed),
      (b) inside the real clock threshold (CLUTCH_WINDOW_SECONDS, a
          judgment call -- see that constant's own docstring), and
      (c) a real TIE or LEAD CHANGE -- this play's resulting
          away-score-minus-home-score margin has a different sign (or is
          zero) than the margin immediately BEFORE this play, found by
          walking backward through the same real `plays` list to the
          previous play that carried a score. This is a real
          before/after comparison of real ESPN scores, the same
          "place comparison" category of derived-but-real signal
          `golfer_move()`'s LEAD/LOST LEAD detection already uses for
          golf -- never an invented "confidence" number.

    Same per-game summary endpoint _fetch_win_prob() already uses --
    deliberately NOT a new endpoint, so the request-volume discipline
    (only the pinned favorite's own LIVE game, see the caller in
    engines.py) is the only thing standing between this and the
    per-league-poll volume risk CLAUDE.md already flags twice.
    """
    if league not in ("NBA", "NCAAB"):
        return []
    final_period = FINAL_PERIOD_BY_LEAGUE[league]
    if data is None:
        path = LEAGUE_PATHS[league]
        data = _get_json(SUMMARY_URL.format(path=path, event_id=event_id))
    plays = data.get("plays")
    if not isinstance(plays, list):
        return []
    out = []
    for i, p in enumerate(plays):
        if not p.get("scoringPlay"):
            continue
        period = p.get("period") or {}
        if not isinstance(period.get("number"), int) or period["number"] < final_period:
            continue
        secs = _clock_seconds_remaining(p.get("clock"))
        if secs is None or secs > CLUTCH_WINDOW_SECONDS:
            continue
        away_score, home_score = p.get("awayScore"), p.get("homeScore")
        if not isinstance(away_score, (int, float)) or not isinstance(home_score, (int, float)):
            continue
        after_margin = away_score - home_score
        before_margin = 0.0   # honest default: nothing scored yet this game
        for j in range(i - 1, -1, -1):
            prev = plays[j]
            pa, ph = prev.get("awayScore"), prev.get("homeScore")
            if isinstance(pa, (int, float)) and isinstance(ph, (int, float)):
                before_margin = pa - ph
                break
        # Tie or lead change: the margin's sign flipped, or either side
        # of the comparison is a genuine 0-0 tie -- a real before/after
        # score comparison, not a threshold on anything invented.
        tie_or_change = (after_margin == 0) or (before_margin == 0) or \
                         ((before_margin > 0) != (after_margin > 0))
        if not tie_or_change:
            continue
        pid = p.get("id")
        if pid is None:
            continue
        out.append({
            "id": str(pid),
            "text": paneltext.panel_text(p.get("text") or ""),
            "awayScore": int(away_score),
            "homeScore": int(home_score),
            "scorer": _play_scorer(p),
            "shot_xy": _play_shot_xy(p),
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
            "scorer": _play_scorer(ev),
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


# =============================================================================
# TENNIS -- task #19, the last per-sport MAIN renderer. Blocked since
# 2026-08-01 on "no live match to verify against"; unblocked this session
# against real live/finished WTA National Bank Open + Warsaw Polish Open
# matches (site.web.api.espn.com/apis/site/v2/sports/tennis/{atp,wta}
# /scoreboard, checked live).
#
# WHY A DEDICATED FETCH, NOT THE UNIVERSAL HEADER. The header endpoint
# (HEADER_URL, what get_universal() already surfaces) DOES include tennis
# events -- confirmed live, 6 real WTA matches today -- but a `pre`-state
# match has literally no score/linescores fields on its competitors. That
# is confirmed empty-until-live, not a missing field: this endpoint is the
# real source of truth for tennis scores, the same SCOREBOARD_URL template
# every team sport already uses, just pointed at a tennis path.
#
# THE SHAPE IS COMPLETELY DIFFERENT FROM EVERY OTHER SPORT THIS MODULE
# PARSES -- confirmed live, not assumed. One `event` here is a whole
# TOURNAMENT (e.g. "National Bank Open"), closer to mma.py's "one event =
# the whole CARD" shape than to _parse_event()'s flat team-sport shape.
# `groupings[]` adds one more nesting level MMA doesn't have -- the DRAW
# category (Men's Singles / Women's Singles / Women's Doubles) -- and each
# grouping's `competitions[]` are the individual MATCHES. mma.py's
# `_parse_card()` is the structural precedent this follows, not
# `_parse_event()`.
#
# `linescores` entries are `{value, tiebreak (optional), winner}` -- NOT
# the `{value, displayValue, period, winner}` shape an earlier speculative
# note in this project once described before anyone had actually pulled a
# live payload. `value` is games won in that set (a float, e.g. 7.0),
# `tiebreak` is only present when that set went to a breaker (the loser's
# breaker points, e.g. 7 meaning the set finished 7-5 in a 7-5 tiebreak --
# confirmed against a real completed match, Mejia d. Trungelliti
# 7-6(7-5) 3-6 6-1), `winner` is whether THIS competitor won that set.
#
# DOUBLES uses a DIFFERENT competitor shape than SINGLES -- also confirmed
# live, not assumed. Singles carries `athlete` (one player). Doubles
# carries `roster` (a two-player team: `roster.athletes[]`,
# `roster.displayName`/`shortDisplayName` for the pair). Both are handled
# below; an unrecognised shape degrades to an empty name rather than a
# guess.
#
# NO LIVE (in-progress) MATCH WAS AVAILABLE TO VERIFY LIVE in-game point
# score fields against this session -- every real match checked was `pre`
# or `post`. Built correctly against the real PRE/POST shapes actually
# seen; the `in` state renders only the real set-by-set score so far, with
# no live point score, because no field for one was ever observed to
# confirm a name for.
TENNIS_TOURS = {"ATP": "tennis/atp", "WTA": "tennis/wta"}
# Each tour's scoreboard dumps BOTH genders AND doubles (confirmed
# 2026-08-12: ATP and WTA returned the identical 608-match Rogers +
# Cincinnati blob). We take Men's Singles from ATP, Women's Singles
# from WTA, nothing else.
TENNIS_DRAW = {"ATP": "MEN'S SINGLES", "WTA": "WOMEN'S SINGLES"}
# Upcoming: seeds 1-16 (a real seeded section). Finished: seed vs
# seed with a top-8 involved -- late-round results, not every R64
# a seed already played. Live: any match with a ranked player.
TENNIS_PRE_SEED = 16
TENNIS_POST_SEED = 8

TENNIS_REFRESH_LIVE = 20.0     # a match is actually in progress
TENNIS_REFRESH_IDLE = 300.0    # matches exist today but none live
TENNIS_REFRESH_EMPTY = 1800.0  # no tennis at all (real off-season condition)


def _tennis_competitor_name(c):
    """Singles carries `athlete`, doubles carries `roster` -- two
    genuinely different real shapes on the same endpoint, confirmed live
    against a real doubles match (Brooks/Haverlag). Returns
    (abbr, full) already panel_text()-folded, or ("", "") if neither
    shape is present rather than a guessed name."""
    ath = c.get("athlete")
    if isinstance(ath, dict) and ath:
        full = ath.get("fullName") or ath.get("displayName") or ""
        abbr = ath.get("shortName") or full
        return paneltext.panel_text(abbr), paneltext.panel_text(full)
    roster = c.get("roster")
    if isinstance(roster, dict) and roster:
        full = roster.get("displayName") or ""
        abbr = roster.get("shortDisplayName") or full
        return paneltext.panel_text(abbr), paneltext.panel_text(full)
    return "", ""


def _parse_tennis_sets(competitor):
    """`linescores` -> [{"games": int, "tiebreak": int|None, "winner": bool}].
    `value` is always a float on the real payload (7.0, not 7) -- cast to
    int since a tennis set score is never fractional. A set with no
    `value` at all (should not happen, but never invent one) is skipped."""
    out = []
    for s in (competitor.get("linescores") or []):
        v = s.get("value")
        if v is None:
            continue
        out.append({
            "games": int(v),
            "tiebreak": s.get("tiebreak"),
            "winner": bool(s.get("winner")),
        })
    return out


def _tennis_seed_and_rank(c):
    """Tournament seed and world rank from a raw ESPN competitor.

    HONEST GAP, checked live against the ATP/WTA scoreboard (Rogers +
    Cincinnati, 608 matches): this endpoint has NO ATP/WTA world-ranking
    field. `curatedRank.current` is the tournament seed (real values
    1..33 on that slate). Header tennis sometimes also sends `rank` /
    `tournamentSeed`; those are read when present. A missing number is
    unseeded/unranked, never a guessed ranking.
    """
    seed = None
    cr = c.get("curatedRank")
    if isinstance(cr, dict):
        seed = _num_or_none(cr.get("current"))
    if seed is None:
        seed = _num_or_none(c.get("tournamentSeed"))
    rank = _num_or_none(c.get("rank"))
    return rank, seed


def _tennis_player_ranked(c):
    """True when ESPN published a real seed or rank number for them."""
    return _tennis_player_mark(c) is not None


def _tennis_player_mark(c):
    """Best real seed or world rank on this competitor, or None."""
    if not isinstance(c, dict):
        return None
    best = None
    for key in ("rank", "seed"):
        v = c.get(key)
        if isinstance(v, int) and v >= 1:
            best = v if best is None else min(best, v)
    return best


def _tennis_best_rank(match):
    """Lowest real seed/rank on either side. None if both are unmarked."""
    best = None
    for c in (match or {}).get("competitors") or []:
        m = _tennis_player_mark(c)
        if m is not None:
            best = m if best is None else min(best, m)
    return best


def _tennis_seeded_count(match):
    return sum(1 for c in (match or {}).get("competitors") or []
               if _tennis_player_mark(c) is not None)


def _tennis_draw_ok(tour_code, grouping_name):
    """ATP = men's singles, WTA = women's singles. Doubles and
    qualifying are not a 64px slate."""
    n = (grouping_name or "").upper().replace("-", " ").replace("'", "")
    if "DOUBLE" in n or "QUALIF" in n:
        return False
    if tour_code == "ATP":
        return "MEN" in n and "SINGLE" in n and "WOMEN" not in n
    if tour_code == "WTA":
        return "WOMEN" in n and "SINGLE" in n
    return False


def _tennis_has_real_name(match):
    """TBD vs Zverev is a real next match. TBD vs TBD is not."""
    names = []
    for c in (match or {}).get("competitors") or []:
        n = (c.get("full") or c.get("abbr") or "").strip()
        if n and n.upper() != "TBD":
            names.append(n)
    return bool(names)


def _tennis_name_matches(c, want):
    name = (c.get("full") or c.get("abbr") or "") if isinstance(c, dict) else ""
    if not name or not want:
        return False
    last = name.split()[-1] if name.split() else ""
    want_last = want.split()[-1] if want.split() else want
    return name == want or last == want_last or want in name or name in want


def _tennis_match_followed(match, pinned=None):
    """Keep a match the wall can actually use.

    Unranked vs unranked is out. Unranked vs a ranked player stays --
    that is the only way an unranked name appears. A pinned player
    always keeps their match.

    The 200-card dump was every seed's entire tournament history plus
    doubles. Finished early-round beatdowns (seed 1 vs qualifier, four
    times) are not a slate. Upcoming is seeds 1-16. Live is any match
    with a ranked player. Post is seed-vs-seed with a top-8 involved.
    """
    comps = match.get("competitors") or []
    if pinned:
        want = paneltext.panel_text(pinned)
        if want and any(_tennis_name_matches(c, want) for c in comps):
            return True
    if not _tennis_has_real_name(match):
        return False
    best = _tennis_best_rank(match)
    if best is None:
        return False
    state = match.get("state")
    if state == "in":
        return True
    if state == "pre":
        return best <= TENNIS_PRE_SEED
    if state == "post":
        return best <= TENNIS_POST_SEED and _tennis_seeded_count(match) >= 2
    return False


def _tennis_collapse_post(matches):
    """One finished result per top-8 player -- their latest card.

    The scoreboard keeps every prior round. Without this, seed 5 is
    four extra screens of opponents they already beat."""
    live_pre = [m for m in matches if m.get("state") != "post"]
    latest = {}
    for m in matches:
        if m.get("state") != "post":
            continue
        cid = str(m.get("id") or "")
        for c in m.get("competitors") or []:
            mark = _tennis_player_mark(c)
            if mark is None or mark > TENNIS_POST_SEED:
                continue
            pid = c.get("id") or c.get("full")
            if not pid:
                continue
            prev = latest.get(pid)
            if prev is None or str(prev.get("id") or "") <= cid:
                latest[pid] = m
    seen = set()
    posts = []
    for m in latest.values():
        eid = m.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        posts.append(m)
    return live_pre + posts


def _tennis_sort_key(match, pinned=None):
    """Live, then upcoming, then results -- and inside that, #1 first.

    A pinned player leads their state so you do not walk 16 seeds to
    find them. Unmarked ranks sort last, never as 0."""
    state = {"in": 0, "pre": 1, "post": 2}.get((match or {}).get("state"), 3)
    pin = 1
    if pinned:
        want = paneltext.panel_text(pinned)
        comps = (match or {}).get("competitors") or []
        if want and any(_tennis_name_matches(c, want) for c in comps):
            pin = 0
    best = _tennis_best_rank(match)
    return (state, pin, best if best is not None else 999,
            (match or {}).get("name") or "", (match or {}).get("id") or "")


def _dedupe_tennis_matches(matches):
    """ATP and WTA scoreboards can return the SAME combined event
    (verified live: Rogers/Cincinnati, 608/608 ids identical). First
    copy of each competition id wins."""
    seen = set()
    out = []
    for m in matches:
        eid = m.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append(m)
    return out


def _parse_tennis_competitor(c):
    abbr, full = _tennis_competitor_name(c)
    rank, seed = _tennis_seed_and_rank(c)
    return {
        "id": c.get("id"),
        "is_team": False,
        "abbr": abbr,
        "full": full,
        "winner": bool(c.get("winner")),
        "seed": seed,
        "sets": _parse_tennis_sets(c),
        "score": None,       # tennis has no single numeric score -- see "sets"
        "color": None, "alt_color": None, "record": None, "rank": rank,
        "form": None, "movement": None, "shootout": None,
        "place": None, "thru": None, "hole": None,
        "tee_time": None, "player_state": None, "home_away": c.get("homeAway"),
    }


def _parse_tennis_match(comp, grouping_name, tour_code, event_name, event_short):
    """One match (a `competitions[]` entry inside a `groupings[]` entry)
    -> the same normalised shape sports.py's other parsers produce
    (`_header_event`'s keys), so this slots into the existing
    universal-events list and SPORT_RENDERERS/SPORT_DETAIL_RENDERERS
    dispatch without a third rendering path."""
    status = comp.get("status") or {}
    stype = status.get("type") or {}
    state = stype.get("state") or "pre"
    comps = [_parse_tennis_competitor(c) for c in (comp.get("competitors") or [])]
    venue = comp.get("venue") or {}
    venue_bits = [x for x in (venue.get("fullName"), venue.get("court")) if x]
    fmt = ((comp.get("format") or {}).get("regulation") or {}).get("periods")
    notes = [paneltext.panel_text(n.get("text"))
             for n in (comp.get("notes") or []) if n.get("text")][:2]
    return {
        "id": str(comp.get("id") or ""),
        "competition_id": str(comp.get("id") or ""),
        "sport": "tennis",
        "league": tour_code,                 # ATP / WTA
        "league_name": tour_code,
        "name": event_name,                  # tournament name, already folded
        "short_name": paneltext.panel_text(grouping_name) or event_short,
        "state": state,
        "live": state == "in",
        "detail": paneltext.panel_text(stype.get("shortDetail") or stype.get("description")),
        "period": _num_or_none(status.get("period")),
        "clock": None,   # no live in-game clock field observed -- see module docstring
        "broadcast": None, "week": None, "playoff": False, "neutral": False,
        "venue": paneltext.panel_text(" - ".join(venue_bits)) or None,
        "series": None,
        "note": notes[0] if notes else None,
        "notes": notes,
        "class_label": paneltext.panel_text(grouping_name) or None,   # the DRAW, e.g. "Men's Singles"
        "match_number": None,
        "card_segment": None,
        "round": None,           # not present on this endpoint -- never guessed
        "best_of": fmt,          # 3 or 5 -- real scheduled-format field
        "leaderboard": False,
        "competitors": comps,
        "bases": None, "outs": None, "runners_text": None,
    }


def _fetch_tennis_matches(tour_code):
    """Every real match across every real tournament this tour's
    scoreboard currently carries -- confirmed live returning MULTIPLE
    real tournaments in one call (WTA: National Bank Open AND the Warsaw
    T-Mobile Polish Open, same request)."""
    path = TENNIS_TOURS[tour_code]
    data = _get_json(SCOREBOARD_URL.format(path=path))
    out = []
    for ev in data.get("events") or []:
        name = paneltext.panel_text(ev.get("name"))
        short = paneltext.panel_text(ev.get("shortName"))
        for g in (ev.get("groupings") or []):
            gr = g.get("grouping") or {}
            gname = gr.get("displayName") or gr.get("slug") or ""
            if not _tennis_draw_ok(tour_code, gname):
                continue
            for comp in (g.get("competitions") or []):
                try:
                    out.append(_parse_tennis_match(comp, gname, tour_code, name, short))
                except (KeyError, IndexError, TypeError, ValueError):
                    continue      # one malformed match must not lose the rest
    return out


def load_tennis_player():
    """Pinned tennis player, or None. Same file/shape as
    load_golf_player() -- one config file for one mode, matched by name
    rather than ESPN athlete id for the same reason golf's is: someone
    types "Coco Gauff", the scoreboard shows "C. GAUFF"."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return None
    p = data.get("tennis_player")
    if isinstance(p, str) and p.strip():
        return paneltext.panel_text(p)
    return None


def save_tennis_player(name):
    """Persist (or clear) the pinned tennis player. Preserves every other
    key -- same discipline as save_golf_player(), because this file also
    carries leagues/favorite/golf_player and a naive rewrite would wipe
    them."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    cleaned = paneltext.panel_text(name) if name else None
    if cleaned:
        data["tennis_player"] = cleaned
    else:
        data.pop("tennis_player", None)
    data.setdefault("leagues", list(DEFAULT_LEAGUES))
    data.setdefault("favorite", None)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return cleaned


def find_pinned_tennis_player(matches, pinned):
    """Locate the pinned player in any real tennis match, forgiving on
    name form exactly like find_pinned_golfer(): exact, surname, or
    substring. Prefers a LIVE match over a finished/upcoming one when the
    player appears in more than one real match on the board."""
    if not pinned:
        return None, None
    want = paneltext.panel_text(pinned)
    if not want:
        return None, None
    hits = []
    for m in matches:
        for c in m.get("competitors") or []:
            if _tennis_name_matches(c, want):
                hits.append((m, c))
                break
    if not hits:
        return None, None
    live = [h for h in hits if h[0]["state"] == "in"]
    if live:
        return live[0]
    pre = [h for h in hits if h[0]["state"] == "pre"]
    if pre:
        return pre[0]
    return hits[0]


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
        # Tennis -- own dedicated poll, separate from the universal header
        # (see the TENNIS section docstring for why: the header's tennis
        # entries carry no score until live, this is the real source).
        self._tennis = []             # real matches, every tour, every tournament
        self._tennis_updated = 0.0
        self._tennis_try = 0.0
        self._tennis_interval = 0.0
        self._tennis_fails = 0
        self._tennis_player = load_tennis_player()
        # Favorite-teams ticker filter (2026-08-08) -- see
        # event_matches_favorite_teams()'s own docstring for why this is a
        # SEPARATE mechanism from `favorite` (one full-screen pinned team)
        # rather than reusing it. Cached in the FEED like every other
        # config value here, re-read on the same _maybe_reload_config()
        # timer as leagues/favorite/golf_player/tennis_player.
        self._favorite_teams, self._favorite_teams_filter = load_favorite_teams()
        # ESPN poll posture -- re-read on the same _maybe_reload_config()
        # timer as leagues/favorite/golf_player so a control-panel toggle
        # applies live without a restart. "off" is the only honest
        # production lock (see HONEST POSTURE in the module docstring).
        self._espn_use = load_espn_use()
        self._thread = None
        self._err = ESPN_OFF_ERR if self._espn_use == ESPN_USE_OFF else None
        # ONE-GAME summary cache -- the off-thread worker that replaced
        # SportsEngine's tick-thread SUMMARY_URL hits (matchup + every
        # big-moment detector). Engine calls want_summary() each tick
        # for the games it actually has on screen; this thread fetches
        # at WINPROB_REFRESH. get_summary() never blocks.
        self._summary_wanted = {}     # event_id -> (league, last_want_ts, sport)
        self._summaries = {}          # event_id -> bundle
        self._summary_try = {}        # event_id -> last fetch ts
        # Standings (MLB/NHL/NBA). 15-minute poll -- these are tables, not
        # live scores. Worker only runs while someone is reading (_last_read
        # / IDLE_STOP), same as every other poll here.
        self._standings = {}          # league -> parsed {league, groups}
        self._standings_try = 0.0
        self._standings_updated = 0.0
        # Next future game for the pinned favorite -- only fetched when
        # they have no game TODAY (scoreboard is dates=TODAY). Never
        # written into favorite_game: that slot is today-only.
        self._favorite_next = None
        self._next_try = 0.0
        self._next_interval = 0.0
        self._next_fails = 0

    # ---- reading -------------------------------------------------------
    def get(self):
        """Returns {games: [...], favorite, favorite_game, favorite_next,
        win_prob, age, err, standings}. Never blocks.

        favorite_game is TODAY only (today's scoreboard). favorite_next is
        the next future pre game when there is no today game, or when
        today's game is already FINAL -- last-final hands off to next.
        """
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
            raw_next = dict(self._favorite_next) if self._favorite_next else None
            standings = _copy_standings(self._standings)
        self._ensure_thread()

        games.sort(key=lambda g: (g["state"] != "in", g["state"] == "post"))

        favorite_game = None
        if favorite:
            favorite_game = next(
                (g for g in games if g["league"] == favorite["league"] and
                 favorite["team_abbr"] in (g["home"]["abbr"], g["away"]["abbr"])),
                None)

        favorite_next = None
        today_blocking = favorite_game and favorite_game.get("state") in ("in", "pre")
        if favorite and raw_next and not today_blocking:
            # Identity is league + abbr, never a list index. Drop a
            # cached next-game that no longer belongs to the pinned team.
            if (raw_next.get("league") == favorite["league"] and
                    favorite["team_abbr"] in (
                        (raw_next.get("home") or {}).get("abbr"),
                        (raw_next.get("away") or {}).get("abbr"))):
                favorite_next = raw_next

        age = (now - min(updated_times)) if updated_times else None
        return {
            "games": games, "favorite": favorite, "favorite_game": favorite_game,
            "favorite_next": favorite_next,
            "win_prob": win_prob if favorite_game and favorite_game["state"] == "in" else None,
            "age": age, "err": err, "standings": standings,
        }

    def get_standings(self):
        """Last-good standings tables, or {}. Never blocks. Touches
        _last_read so the worker keeps polling while a reader is here."""
        now = time.time()
        with self._lock:
            self._last_read = now
            standings = _copy_standings(self._standings)
        self._ensure_thread()
        return standings

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
            self._favorite_next = None
            self._next_try = 0.0
            self._next_interval = 0.0
            self._next_fails = 0
            leagues = list(self.leagues)
        save_config(leagues, favorite)
        return favorite

    def clear_favorite(self):
        with self._lock:
            self.favorite = None
            self._win_prob = None
            self._favorite_next = None
            leagues = list(self.leagues)
        save_config(leagues, None)

    # ---- polling ---------------------------------------------------------
    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _espn_http_blocked(self):
        """True when the worker must not open a new ESPN request."""
        with self._lock:
            return self._espn_use == ESPN_USE_OFF

    def _poke_espn_refresh(self):
        """Force the next worker pass to hit ESPN immediately. Caller
        holds `_lock`. Used when posture flips back to personal so a
        live toggle does not wait out a leftover IDLE/EMPTY interval."""
        self._last_try = {k: 0.0 for k in self._last_try}
        self._interval = {k: 0.0 for k in self._interval}
        self._universal_try = 0.0
        self._universal_interval = 0.0
        self._tennis_try = 0.0
        self._tennis_interval = 0.0
        self._standings_try = 0.0
        self._win_prob_try = 0.0
        self._next_try = 0.0
        self._next_interval = 0.0
        self._golf_field_try = 0.0
        self._summary_try = {}

    def _apply_espn_use(self, value):
        """Install a posture already persisted by save_espn_use().
        Caller holds `_lock`."""
        prev = self._espn_use
        self._espn_use = value
        if value == ESPN_USE_OFF:
            self._err = ESPN_OFF_ERR
        elif prev == ESPN_USE_OFF:
            if self._err == ESPN_OFF_ERR:
                self._err = None
            self._poke_espn_refresh()

    def set_espn_use(self, value):
        cleaned = save_espn_use(value)
        with self._lock:
            self._apply_espn_use(cleaned)
        return cleaned

    def get_espn_use(self):
        with self._lock:
            return self._espn_use

    def _loop(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read
            if idle > IDLE_STOP:
                return
            self._maybe_reload_config()
            # OFF: no new ESPN HTTP. Keep last-good cache. Never invent
            # scores. LIVE/IDLE/EMPTY intervals are not touched -- those
            # stay correct for personal use; we just do not spend them.
            if self._espn_http_blocked():
                with self._lock:
                    self._err = ESPN_OFF_ERR
                time.sleep(2.0)
                continue
            self._refresh_universal()
            self._refresh_tennis()
            self._refresh_scoreboards()
            self._refresh_standings()
            self._refresh_favorite_next()
            self._refresh_win_prob()
            self._refresh_summaries()
            time.sleep(2.0)

    def _refresh_tennis(self):
        """Both tours, real matches, own adaptive interval -- same tiered
        backoff shape as _refresh_scoreboards()/_refresh_universal(), just
        against the dedicated tennis endpoint instead of the header."""
        if self._espn_http_blocked():
            return
        now = time.time()
        with self._lock:
            if now - self._tennis_try < self._tennis_interval:
                return
            self._tennis_try = now
        matches = []
        failed = 0
        for tour in TENNIS_TOURS:
            try:
                matches.extend(_fetch_tennis_matches(tour))
            except Exception:                     # noqa: BLE001 - never die
                failed += 1
        with self._lock:
            pinned = self._tennis_player
        matches = [m for m in _dedupe_tennis_matches(matches)
                   if _tennis_match_followed(m, pinned)]
        matches = _tennis_collapse_post(matches)
        matches.sort(key=lambda m: _tennis_sort_key(m, pinned))
        with self._lock:
            if failed == len(TENNIS_TOURS) and self._tennis:
                # Both tours failed and we already have real cached data --
                # back off rather than replace good data with nothing.
                self._tennis_fails += 1
                self._tennis_interval = min(
                    ERROR_BACKOFF_MAX,
                    ERROR_BACKOFF_BASE * (2 ** (self._tennis_fails - 1)))
                return
            self._tennis = matches
            self._tennis_updated = time.time()
            self._tennis_fails = 0
            if any(m["live"] for m in matches):
                self._tennis_interval = TENNIS_REFRESH_LIVE
            elif matches:
                self._tennis_interval = TENNIS_REFRESH_IDLE
            else:
                self._tennis_interval = TENNIS_REFRESH_EMPTY

    def set_tennis_player(self, name):
        cleaned = save_tennis_player(name)
        with self._lock:
            self._tennis_player = cleaned
        return cleaned

    def get_tennis_player(self):
        with self._lock:
            return self._tennis_player

    def _refresh_universal(self):
        """Poll the one endpoint that covers every sport.

        Cheap by construction: ONE request regardless of how many leagues
        are live, versus one per configured league for the per-league
        path. Backs off the same way everything else here does."""
        if self._espn_http_blocked():
            return
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
            # Tennis is DROPPED from the header list and REPLACED with the
            # dedicated per-tour fetch -- the header's tennis entries are
            # confirmed empty-until-live (no score/linescores while `pre`),
            # so keeping both would either duplicate a match or show the
            # useless header copy alongside the real one. See the TENNIS
            # section docstring above _fetch_tennis_matches().
            events = [dict(e) for e in self._universal if e.get("sport") != "tennis"]
            events.extend(dict(m) for m in self._tennis)
            age = (now - self._universal_updated) if self._universal_updated else None
            tennis_age = (now - self._tennis_updated) if self._tennis_updated else None
            if tennis_age is not None:
                age = tennis_age if age is None else min(age, tennis_age)
            golf_player = self._golf_player
            golf_move = (self._golf_move
                         if self._golf_move and (now - self._golf_move_at) < GOLF_MOVE_TTL
                         else None)
            tennis_player = self._tennis_player
            favorite_teams = list(self._favorite_teams)
            favorite_teams_filter = self._favorite_teams_filter
        self._ensure_thread()
        # FAVORITE-TEAMS FILTER (2026-08-08): applied here, once, before
        # every downstream consumer (league grouping, the ticker index,
        # has_content()) ever sees the list -- same "filter at the source,
        # not at each call site" reasoning WINDOW FILTER's in_window flag
        # already follows. Never applied to golf/tennis: those already
        # have their own dedicated pinned-player mechanism and are
        # individual-athlete sports with no "team" for this filter to
        # match against in the first place (event_matches_favorite_teams()
        # only ever checks `is_team` competitors).
        if favorite_teams_filter and favorite_teams:
            events = [e for e in events
                     if e.get("sport") in ("golf", "tennis")
                     or event_matches_favorite_teams(e, favorite_teams)]
        # Live first, then upcoming, then finished -- within that, keep
        # ESPN's own ordering, which already groups by sport sensibly.
        rank = {"in": 0, "pre": 1, "post": 2}
        events.sort(key=lambda e: rank.get(e["state"], 3))
        gev, gc = find_pinned_golfer(events, golf_player)
        if golf_player and gc is None:
            gc = self._pinned_from_field(events, golf_player)
            if gc is not None:
                gev = self.golf_field_event()
        tev, tc = find_pinned_tennis_player(events, tennis_player)
        return {"events": events, "age": age,
                "leagues": sorted({(e["sport"], e["league"]) for e in events}),
                "golf_player": golf_player, "golf_event": gev, "golf_pinned": gc,
                # Reported for a short window then lapses on its own, so the
                # engine never has to acknowledge or clear it.
                "golf_move": golf_move,
                "tennis_player": tennis_player, "tennis_event": tev, "tennis_pinned": tc}

    def _maybe_reload_config(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        leagues, favorite = load_config()
        gp = load_golf_player()
        tp = load_tennis_player()
        ft, ftf = load_favorite_teams()
        eu = load_espn_use()
        with self._lock:
            if eu != self._espn_use:
                self._apply_espn_use(eu)
            if leagues != self.leagues or favorite != self.favorite:
                if favorite != self.favorite:
                    self._favorite_next = None
                    self._next_try = 0.0
                    self._next_interval = 0.0
                    self._next_fails = 0
                self.leagues, self.favorite = leagues, favorite
                self._win_prob_try = 0.0
            if gp != self._golf_player:
                # Changed under us (edited file / other process): drop the
                # baseline so the new player cannot flash on arrival.
                self._golf_player, self._golf_prev, self._golf_move = gp, None, None
            if tp != self._tennis_player:
                self._tennis_player = tp
            self._favorite_teams, self._favorite_teams_filter = ft, ftf

    def _pinned_from_field(self, events, pinned):
        """Pinned golfer from the whole field, or None.

        Only tours that actually have a live leaderboard in the header are
        queried, so this never fires out of season, and it is rate-limited
        independently of the header poll."""
        with self._lock:
            espn_off = self._espn_use == ESPN_USE_OFF
            cached = self._golf_field
        if espn_off:
            # No new ESPN HTTP. Last-good full-field cache only.
            return self._match_in_field(cached, pinned)
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

    def set_favorite_teams(self, teams, filter_enabled):
        cleaned, enabled = save_favorite_teams(teams, filter_enabled)
        with self._lock:
            self._favorite_teams, self._favorite_teams_filter = cleaned, enabled
        return cleaned, enabled

    def get_favorite_teams(self):
        with self._lock:
            return list(self._favorite_teams), self._favorite_teams_filter

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
        if self._espn_http_blocked():
            return
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

    def _refresh_standings(self):
        """MLB / NHL / NBA conference tables. 15-minute cadence.

        Only runs while the worker is alive, which is only while
        someone has touched _last_read recently (get / get_standings /
        get_universal). A failed league keeps its last-good table;
        a successful empty parse drops that league so has_content
        stays honest. Zero I/O on the render thread."""
        if self._espn_http_blocked():
            return
        now = time.time()
        with self._lock:
            if now - self._standings_try < STANDINGS_REFRESH:
                return
            self._standings_try = now
        for lg in STANDINGS_LEAGUES:
            path = LEAGUE_PATHS.get(lg)
            if not path:
                continue
            try:
                parsed = parse_standings(
                    lg, _get_json(STANDINGS_URL.format(path=path)))
            except Exception:                               # noqa: BLE001
                continue          # keep last-good; never wipe on a blip
            with self._lock:
                if parsed.get("groups"):
                    self._standings[lg] = parsed
                    self._standings_updated = time.time()
                else:
                    self._standings.pop(lg, None)

    def _refresh_favorite_next(self):
        """Schedule poll for the pinned favorite's next future pre game.

        Only spends a request when a favorite is set AND that team has no
        live or upcoming game today. A FINAL today still fetches next.
        """
        if self._espn_http_blocked():
            return
        now = time.time()
        with self._lock:
            favorite = dict(self.favorite) if self.favorite else None
            games = (list(self._games.get(favorite["league"], []))
                     if favorite else [])
            wait = self._next_interval
            last_try = self._next_try
        if not favorite:
            with self._lock:
                self._favorite_next = None
            return
        # Same identity match get() uses: league + team_abbr, not index.
        today = next(
            (g for g in games if g["league"] == favorite["league"] and
             favorite["team_abbr"] in (g["home"]["abbr"], g["away"]["abbr"])),
            None)
        if today and today.get("state") in ("in", "pre"):
            with self._lock:
                self._favorite_next = None
            return
        if now - last_try < wait:
            return
        snap = (favorite["league"], favorite["team_abbr"])
        with self._lock:
            self._next_try = now
        try:
            nxt = _fetch_favorite_next(favorite["league"], favorite["team_abbr"])
        except Exception:                               # noqa: BLE001 - never die
            with self._lock:
                self._next_fails += 1
                self._next_interval = min(
                    ERROR_BACKOFF_MAX,
                    ERROR_BACKOFF_BASE * (2 ** (self._next_fails - 1)))
            return
        with self._lock:
            cur = ((self.favorite["league"], self.favorite["team_abbr"])
                   if self.favorite else None)
            if cur != snap:
                return
            self._favorite_next = nxt
            self._next_fails = 0
            self._next_interval = SCHEDULE_REFRESH

    def _refresh_win_prob(self):
        if self._espn_http_blocked():
            return
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

    def want_summary(self, league, event_id, sport=None):
        """Ask the worker to keep this one game's summary warm.

        Cheap, non-blocking, safe to call every tick. Wants older than
        8s are dropped so leaving a detail view stops the poll.
        MMA summaries 404 (mma.py); skip them rather than poll a
        known-dead URL every 20s.
        """
        if not league or not event_id:
            return
        if (sport or "").lower() == "mma" or str(league).strip().upper() in ("UFC", "PFL"):
            return
        if not summary_path(league, sport):
            return
        with self._lock:
            self._last_read = time.time()
            self._summary_wanted[str(event_id)] = (league, time.time(), sport)

    def get_summary(self, event_id):
        """Last-good bundle for this event, or {}. Never blocks."""
        with self._lock:
            rec = self._summaries.get(str(event_id))
            return dict(rec) if rec else {}

    def _refresh_summaries(self):
        if self._espn_http_blocked():
            return
        now = time.time()
        with self._lock:
            wanted = {}
            keep = {}
            for eid, rec in self._summary_wanted.items():
                lg, t = rec[0], rec[1]
                sp = rec[2] if len(rec) > 2 else None
                if now - t < 8.0:
                    wanted[eid] = (lg, sp)
                    keep[eid] = rec
            self._summary_wanted = keep
            # Drop caches for games nobody is watching.
            for eid in list(self._summaries):
                if eid not in wanted:
                    self._summaries.pop(eid, None)
                    self._summary_try.pop(eid, None)
        for eid, (lg, sp) in wanted.items():
            with self._lock:
                if now - self._summary_try.get(eid, 0.0) < WINPROB_REFRESH:
                    continue
                self._summary_try[eid] = now
            try:
                path = summary_path(lg, sp)
                if not path:
                    continue
                data = _get_json(SUMMARY_URL.format(path=path, event_id=eid))
                bundle = summarize_payload(lg, eid, data)
            except Exception:                               # noqa: BLE001
                continue          # keep last-good; never wipe on a blip
            with self._lock:
                self._summaries[eid] = bundle

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
#     site.web.api.espn.com/apis/v2/scoreboard/header
# One request returned 43 live/relevant events across 11 leagues in 7 sports
# when this was written -- golf (PGA + LPGA), MLB, women's basketball, three
# soccer competitions, PFL, PLL lacrosse, ATP and WTA tennis.
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
HEADER_URL = "https://site.web.api.espn.com/apis/v2/scoreboard/header"

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
        "color": (_hex_to_rgb(_pick_team_hex(c.get("color"), c.get("alternateColor")))
                 if c.get("color") else None),
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


def _parse_odds(raw):
    """Real ESPN odds -> {"spread": str|None, "over_under": float|None},
    or None. REVERSED 2026-08-10 -- this field was previously deliberately
    dropped ("betting stays off by default", see this function's own call
    site) as a project policy; the owner explicitly asked to reverse that
    this session. This is data ESPN already sends on the same header
    payload every other field here comes from -- zero new I/O, zero new
    (and zero PAID) data source, unlike the paid odds-API options this
    project's competitive-research pass separately flagged and rejected.

    `raw` is ESPN's real `odds` list (one entry per provider, when
    present). `details` is a pre-formatted string ESPN itself produces
    ("NYM -1.5"); `overUnder` is a plain float. Only the FIRST provider
    entry is used -- ESPN's own site widgets do the same, and this
    project has no reason to prefer a second provider's line over the
    first ESPN already surfaces as primary.

    HONEST GAP, same as every other field this project ships defensively:
    this project's sandbox cannot reach ESPN's odds-bearing endpoint this
    session (network-blocked, same `site.api.espn.com` 403 already
    documented elsewhere in this file), so the real payload SHAPE here is
    from ESPN's own publicly documented format, not confirmed against a
    live response the way most fields in this file are. Guards
    defensively against every field being missing/malformed rather than
    assuming the shape; verify against a real live payload the first
    session this sandbox can reach ESPN again, per this project's own
    "ship correct, flag honestly" precedent (NHL goal detector, MMA
    finish detector)."""
    if not isinstance(raw, list) or not raw:
        return None
    entry = raw[0]
    if not isinstance(entry, dict):
        return None
    details = entry.get("details")
    spread = paneltext.panel_text(details) if isinstance(details, str) and details else None
    ou = entry.get("overUnder")
    over_under = float(ou) if isinstance(ou, (int, float)) else None
    if spread is None and over_under is None:
        return None
    return {"spread": spread, "over_under": over_under}


def _scheduled_periods(payload):
    """`format.regulation.periods` when it is a real 1..6 int, else None.

    Same field mma.py already verified (3 or 5). Golf stroke-play cards
    use it for scheduled rounds when ESPN sends it. Never a default 4.
    """
    periods = ((payload.get("format") or {}).get("regulation") or {}).get("periods")
    return periods if isinstance(periods, int) and 1 <= periods <= 6 else None


def _header_event(e, sport, league_slug, league_name):
    """One event from the header endpoint, normalised across sports."""
    comps = [_header_competitor(c, sport) for c in (e.get("competitors") or [])]
    athletes = [c for c in comps if not c["is_team"]]
    leaderboard = len(athletes) > 2

    # REAL BUG FIXED 2026-08-11: _disambiguate_colors() was only ever
    # called from _parse_event() (the per-league path), never from here
    # -- but THIS is the path the universal ticker and every expanded
    # detail view actually render from. Result: a real live CLE @ DET
    # game drew two solid team-colour bars in visually identical navy
    # (both teams genuinely ship a dark navy primary), defeating the
    # entire point of colour-coding the two sides. The measurement that
    # justified this function in the first place (5 of 19 real games too
    # close to tell apart) applied here just as much; it simply was not
    # wired in. Falls back to ESPN's own real alternateColor, never an
    # invented colour -- same contract as the per-league call site.
    if len(comps) == 2 and all(c["is_team"] for c in comps):
        _disambiguate_colors(comps[0], comps[1])

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
        # Fields we already pay for and were dropping. `odds` (betting)
        # was dropped here until 2026-08-10 -- the owner explicitly asked
        # to reverse that default; see _parse_odds()'s own docstring for
        # the full reasoning and the one honest gap (unverified live
        # payload shape). Logos still deliberately excluded (IP).
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
        "odds": _parse_odds(e.get("odds")),
        "total_rounds": _scheduled_periods(e),
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
GOLF_FIELD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/golf/{tour}/scoreboard"
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
