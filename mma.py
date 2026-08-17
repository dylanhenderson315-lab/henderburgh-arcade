"""
mma.py -- UFC card data for GAME DAY mode.

Same shape as sports.py/flights.py/satellite.py: all I/O lives here so the
engine that draws it stays pure and testable without a network or a panel.

ONE endpoint, and it is unusually kind to us:
    site.web.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard
A single request returns the ENTIRE card -- every fight, its status, its
result. A 14-fight card therefore costs exactly one request per poll, not
fourteen. That matters here because CLAUDE.md's top standing risk is ESPN
request volume against an undocumented API.

STRUCTURE IS NOT LIKE THE TEAM SPORTS, and this is the thing to understand
before touching anything below. In sports.py one `event` is one game. Here:

    event          = the whole CARD  ("UFC Fight Night: Medic vs. Rodriguez")
    competitions[] = the individual FIGHTS on it, prelims first, MAIN EVENT LAST

Everything this module reports was verified against two real payloads on
2026-07-31 -- a completed card (UFC Fight Night: Ankalaev vs. Guskov,
2026-07-25, 12 fights, all final) and a scheduled one (Medic vs.
Rodriguez, 2026-08-01, 14 fights) -- not assumed from the field names in
the request. What is actually there:

  * weight class   -> competition.type.abbreviation   ("Bantamweight")
  * scheduled rnds -> competition.format.regulation.periods  (3, or 5 for
                      a main event -- this is how a main event is detected)
  * final round    -> competition.status.period
  * time in round  -> competition.status.displayClock
  * winner         -> competition.competitors[].winner (bool)
  * fighter record -> competition.competitors[].records[0].summary ("22-13-0"),
                      present BEFORE the fight too, so it is real pre-fight
                      stakes rather than a post-hoc stat
  * per-fight state-> competition.status.type.state (pre / in / post)

TWO FINDINGS THAT SHAPED THIS MODULE:

1. THERE IS NO `method` FIELD. The finish method is not a first-class
   value anywhere in the payload. It is only recoverable from the
   play-by-play `details` list, as an entry whose type text reads
   "Unofficial Winner <Method>". Across all 12 fights of the completed
   card there was exactly one such entry per fight and it always agreed
   with the `winner` boolean, so it is reliable -- but it is DERIVED, and
   `method` therefore comes back as None rather than a guess whenever the
   entry is missing. The engine must render a result without a method.

   The type IDs are stable and semantic, so we key on the ID and only
   fall back to the text:
       20 -> Submission     21 -> KO/TKO     22 -> Decision
   ESPN's own text for 21 is the mangled token "Unofficial Winner Kotko",
   which is exactly why matching on text alone would be fragile.

2. `displayClock` IS TIME ELAPSED IN THE FINAL ROUND, not time remaining.
   The payload proves this itself: every fight that went to a decision
   reads exactly "5:00" at its final scheduled round, and every finish
   reads a partial time. So "R2 3:06" means the finish came at 3:06 of
   round 2, which is how it is spoken and how it is displayed.

Also confirmed and worth not rediscovering:
  * There is NO working summary endpoint for MMA -- .../mma/ufc/summary
    returns 404. The scoreboard is the only source, which is fine because
    it already carries everything above.
  * `?dates=YYYYMMDD` works, but only for a date on which a card actually
    exists, and card dates are UTC (a Saturday-night US card is often the
    following UTC day). Guessing dates returns 0 events. The league
    `calendar` list is the authoritative schedule and is used for that.

FIGHT STATISTICS (sig. strikes, takedowns, control time -- the numbers a
broadcast graphic shows) are NOT in the scoreboard payload at all. They
live on ESPN's other, undocumented host, `sports.core.api.espn.com`, as a
per-fighter sub-resource:
    .../mma/leagues/ufc/events/{event_id}/competitions/{comp_id}
        /competitors/{athlete_id}/statistics?lang=en&region=us
Confirmed real on 2026-07-31 against THREE states, not just one: a
completed fight (real non-zero significant-strike/takedown/control-time
numbers, matching a real UFC broadcast graphic field-for-field), a
scheduled fight that has not started (every field comes back an honest
zero/"0:00", not missing and not stale), and the live scoreboard's own
event/competition ids (so no separate lookup is needed to build the URL).

This is a genuinely different cost shape from everything else in this
module: it is TWO calls (one per fighter) and there is no batched
"give me both corners" version. Fetched ONLY for the fight currently on
screen, and only while it is live or was the last one that just finished
-- never for the whole card -- same discipline as sports.py's win
probability (pinned team's live game only, not every game in the
ticker).

Same two standing rules as every other feed here:
  1. NEVER block the render loop -- background thread, last-good cache.
  2. NEVER invent a value -- a field ESPN does not provide comes back
     None and the engine renders around it honestly.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

import paneltext

SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
SCOREBOARD_DATED = SCOREBOARD_URL + "?dates={day}"

# One request returns the whole card, so the live tier is genuinely cheap:
# a fight ending is the single most time-critical event this project has
# (the result view is the payoff), and 20s is one call per 20s TOTAL, not
# per fight.
REFRESH_LIVE = 2.0         # clock + last action -- one cheap card call
REFRESH_CARD_TODAY = 120.0  # card exists today but nothing live yet
REFRESH_IDLE = 1800.0      # next card is days away
ERROR_BACKOFF_BASE = 30.0
ERROR_BACKOFF_MAX = 600.0
IDLE_STOP = 120.0          # stop polling once nothing has read for this long
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

# competition.details[].type.id -> method. IDs are stable and semantic;
# the accompanying text for 21 is ESPN's mangled "Kotko", so the ID is
# the primary key and the text only a fallback. Values are already
# panel-safe (uppercase, no glyphs outside the 3x5 font).
METHOD_BY_ID = {"20": "SUBMISSION", "21": "KO/TKO", "22": "DECISION"}
METHOD_BY_TEXT = {"SUBMISSION": "SUBMISSION", "KOTKO": "KO/TKO",
                  "KO/TKO": "KO/TKO", "TKO": "KO/TKO", "KO": "KO/TKO",
                  "DECISION": "DECISION", "DRAW": "DRAW",
                  "DISQUALIFICATION": "DQ", "NO CONTEST": "NO CONTEST"}

# Short forms, because the panel is 64px wide and "SUBMISSION" at scale 2
# does not fit. Kept next to the long forms so they cannot drift.
METHOD_SHORT = {"SUBMISSION": "SUB", "KO/TKO": "KO/TKO", "DECISION": "DEC",
                "DRAW": "DRAW", "DQ": "DQ", "NO CONTEST": "NC"}

# ---- panel-safe text ---------------------------------------------------
# Folding lives in paneltext.py now, NOT here. It used to be a private copy
# in this module, which is exactly how the same bug reappeared in sports.py
# later on (a live PGA leader, "Hojgaard", drew as "HJGAARD"). One shared
# fold, used by every feed at its I/O boundary. See paneltext.py's tally.
panel_text = paneltext.panel_text

STATS_URL = ("https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc"
            "/events/{event_id}/competitions/{comp_id}/competitors/{athlete_id}"
            "/statistics?lang=en&region=us")

# ESPN's raw stat names -> the compact broadcast-style fields this module
# exposes. Confirmed live on UFC 330 (Wells/Orolbai, 2026-08-16): the
# general split has 43 real numbers. The 64px glass gets the ones a
# broadcast graphic actually calls -- plus HEAD/BODY/LEG landed, which
# we SUM from the real location splits (never a guessed %).
_STAT_KEYS = {
    "sigStrikesLanded": "sig_landed",
    "sigStrikesAttempted": "sig_att",
    "totalStrikesLanded": "tot_landed",
    "totalStrikesAttempted": "tot_att",
    "takedownsLanded": "td_landed",
    "takedownsAttempted": "td_att",
    "knockDowns": "knockdowns",
    "timeInControl": "control_time",
    "submissions": "submissions",
    "reversals": "reversals",
    "advances": "advances",
    "takedownAccuracy": "td_acc",
    "takedownsSlams": "slams",
    "advanceToHalfGuard": "adv_half",
    "advanceToSide": "adv_side",
    "advanceToMount": "adv_mount",
    "advanceToBack": "adv_back",
}

# Location landed / attempted = distance + clinch + ground. All three
# must be present (0 is a real zero) or the sum stays None.
_LOC_HEAD = ("sigDistanceHeadStrikesLanded",
             "sigClinchHeadStrikesLanded",
             "sigGroundHeadStrikesLanded")
_LOC_BODY = ("sigDistanceBodyStrikesLanded",
             "sigClinchBodyStrikesLanded",
             "sigGroundBodyStrikesLanded")
_LOC_LEG = ("sigDistanceLegStrikesLanded",
            "sigClinchLegStrikesLanded",
            "sigGroundLegStrikesLanded")
_LOC_DIST = ("sigDistanceHeadStrikesLanded",
             "sigDistanceBodyStrikesLanded",
             "sigDistanceLegStrikesLanded")
_LOC_CLINCH = ("sigClinchHeadStrikesLanded",
               "sigClinchBodyStrikesLanded",
               "sigClinchLegStrikesLanded")
_LOC_GROUND = ("sigGroundHeadStrikesLanded",
               "sigGroundBodyStrikesLanded",
               "sigGroundLegStrikesLanded")
_LOC_HEAD_ATT = ("sigDistanceHeadStrikesAttempted",
                 "sigClinchHeadStrikesAttempted",
                 "sigGroundHeadStrikesAttempted")
_LOC_BODY_ATT = ("sigDistanceBodyStrikesAttempted",
                 "sigClinchBodyStrikesAttempted",
                 "sigGroundBodyStrikesAttempted")
_LOC_LEG_ATT = ("sigDistanceLegStrikesAttempted",
                "sigClinchLegStrikesAttempted",
                "sigGroundLegStrikesAttempted")
_LOC_DIST_ATT = ("sigDistanceHeadStrikesAttempted",
                 "sigDistanceBodyStrikesAttempted",
                 "sigDistanceLegStrikesAttempted")
_LOC_CLINCH_ATT = ("sigClinchHeadStrikesAttempted",
                   "sigClinchBodyStrikesAttempted",
                   "sigClinchLegStrikesAttempted")
_LOC_GROUND_ATT = ("sigGroundHeadStrikesAttempted",
                   "sigGroundBodyStrikesAttempted",
                   "sigGroundLegStrikesAttempted")

# Play-by-play on the SCOREBOARD (no extra I/O). IDs confirmed live
# on UFC 330. Round markers / "Results" / "Fight Over" are structure.
# Phase ids (walkout / tape / staredown) are status, not last-action.
_ACTION_SKIP = {"5", "18", "19", "23"}
_PHASE_BY_ID = {
    "1": "OPEN",
    "2": "WALKOUTS",
    "3": "TAPE",
    "4": "STAREDOWN",
}
_ACTION_BY_ID = {
    "12": "TD ATTEMPT",
    "13": "TAKEDOWN",
    "15": "REVERSAL",
    "16": "SUB ATTEMPT",
    "17": "KNOCKDOWN",
    "20": "SUBMISSION",
    "21": "KO/TKO",
    "22": "DECISION",
}

PLAYS_URL = ("https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc"
             "/events/{event_id}/competitions/{comp_id}/plays?limit=25")

BIOS_URL = ("https://site.web.api.espn.com/apis/common/v3/sports/mma"
            "/athletes/{athlete_id}")
BIOS_REFRESH = 600.0


def _sum_loc(raw, names):
    """Sum real landed splits. None if any piece is missing."""
    total = 0
    for name in names:
        if name not in raw:
            return None
        v = raw[name]
        if not isinstance(v, (int, float)):
            return None
        total += int(v)
    return total


def _parse_stats(payload):
    """One fighter's statistics resource -> the compact dict above.

    Every value defaults to None (not 0) if ESPN's response is missing
    the category entirely -- e.g. a fight that somehow has no `general`
    category -- so the engine can tell "genuinely zero" (a real 0-0 fight)
    apart from "field not provided" and render each honestly.
    """
    out = {v: None for v in _STAT_KEYS.values()}
    out["head_landed"] = None
    out["body_landed"] = None
    out["leg_landed"] = None
    cats = ((payload.get("splits") or {}).get("categories") or [])
    stats = cats[0].get("stats") if cats else []
    raw = {}
    for s in (stats or []):
        name = s.get("name")
        raw[name] = s.get("value")
        key = _STAT_KEYS.get(name)
        if key:
            out[key] = s.get("value") if key != "control_time" else panel_text(s.get("displayValue"))
    out["head_landed"] = _sum_loc(raw, _LOC_HEAD)
    out["body_landed"] = _sum_loc(raw, _LOC_BODY)
    out["leg_landed"] = _sum_loc(raw, _LOC_LEG)
    out["dist_landed"] = _sum_loc(raw, _LOC_DIST)
    out["clinch_landed"] = _sum_loc(raw, _LOC_CLINCH)
    out["ground_landed"] = _sum_loc(raw, _LOC_GROUND)
    out["head_att"] = _sum_loc(raw, _LOC_HEAD_ATT)
    out["body_att"] = _sum_loc(raw, _LOC_BODY_ATT)
    out["leg_att"] = _sum_loc(raw, _LOC_LEG_ATT)
    out["dist_att"] = _sum_loc(raw, _LOC_DIST_ATT)
    out["clinch_att"] = _sum_loc(raw, _LOC_CLINCH_ATT)
    out["ground_att"] = _sum_loc(raw, _LOC_GROUND_ATT)
    return out


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


# ---- parsing -----------------------------------------------------------
def _fighter(competitor):
    """One corner. `records` is present pre-fight as well as post, so the
    record is genuine stakes context rather than a result restated."""
    ath = competitor.get("athlete") or {}
    rec = None
    for r in (competitor.get("records") or []):
        if r.get("type") == "total" or r.get("summary"):
            rec = r.get("summary")
            break
    flag = ath.get("flag") or {}
    country = panel_text(flag.get("alt")) or None
    full = panel_text(ath.get("fullName") or ath.get("displayName"))
    last = None
    if full:
        parts = full.split()
        last = parts[-1] if parts else None
    return {
        "id": competitor.get("id"),          # athlete id -- needed to fetch
                                              # per-fighter statistics below
        "name": panel_text(ath.get("shortName") or ath.get("displayName")),
        "full": full,
        "last": last,
        "record": panel_text(rec) or None,
        "country": country,
        "winner": bool(competitor.get("winner")),
    }


def _clean_clock(raw):
    """Fold ESPN's clock. '-' / empty is 'no clock yet', not a time."""
    clk = panel_text(raw) or None
    if clk in ("-", "--", "0"):
        return None
    return clk


def _action_label(d):
    t = (d or {}).get("type") or {}
    tid = str(t.get("id") or "")
    if tid in _ACTION_SKIP or tid in _PHASE_BY_ID:
        return None
    text = str(t.get("text") or "")
    if text in ("Round Start", "Round End", "Fight Over", "Results"):
        return None
    mapped = _ACTION_BY_ID.get(tid)
    if mapped:
        return mapped
    folded = panel_text(text.replace("Unofficial Winner", "").strip())
    return folded or None


def _phase(comp):
    """Walkout / tape / staredown from the newest detail, else status."""
    period = ((comp.get("status") or {}).get("period") or 0)
    if isinstance(period, int) and period > 0:
        return None
    for d in (comp.get("details") or []):
        tid = str(((d.get("type") or {}).get("id") or ""))
        if tid in _PHASE_BY_ID:
            return _PHASE_BY_ID[tid]
        if tid not in _ACTION_SKIP and _action_label(d):
            return None
    st = ((comp.get("status") or {}).get("type") or {})
    if str(st.get("name") or "") == "STATUS_FIGHTERS_WALKING":
        return "WALKOUTS"
    sd = panel_text(st.get("shortDetail")) or ""
    if sd in ("IN PROGRESS", "FINAL", "SCHEDULED") or (sd and sd[0].isdigit()):
        return None
    return sd or None


def _last_actions(comp, limit=3):
    """Newest real fight actions, newest first. Scoreboard details[0] is newest."""
    out = []
    for d in (comp.get("details") or []):
        lab = _action_label(d)
        if not lab:
            continue
        out.append(lab)
        if len(out) >= limit:
            break
    return out


def _parse_plays(payload):
    """Core plays list -> phase + last actions with the clock ESPN stamped.

    Items arrive oldest-first. A play has no athlete, so we never guess
    who scored the takedown -- only the type, round, and clock.
    """
    items = payload.get("items") or []
    phase = None
    actions = []
    saw_round = False
    for p in items:
        rnd = ((p.get("period") or {}) or {}).get("number")
        if isinstance(rnd, int) and rnd > 0:
            saw_round = True
            break
    for p in reversed(items):
        t = (p or {}).get("type") or {}
        tid = str(t.get("id") or "")
        if not tid:
            continue
        if tid in _PHASE_BY_ID and phase is None and not saw_round:
            phase = _PHASE_BY_ID[tid]
            continue
        lab = _action_label(p)
        if not lab:
            continue
        rec = {"label": lab}
        rnd = ((p.get("period") or {}) or {}).get("number")
        if isinstance(rnd, int) and rnd > 0:
            rec["round"] = rnd
        clk = _clean_clock(((p.get("clock") or {}) or {}).get("displayValue"))
        if clk:
            rec["clock"] = clk
        actions.append(rec)
        if len(actions) >= 3:
            break
    return {"phase": phase, "actions": actions}


def _last_action(comp):
    acts = _last_actions(comp, limit=1)
    return acts[0] if acts else None


def _broadcast(comp):
    """Paramount+ / ESPN+ string ESPN actually sent, or None."""
    raw = comp.get("broadcast")
    if isinstance(raw, str) and raw.strip():
        return panel_text(raw)
    for b in (comp.get("broadcasts") or []):
        names = b.get("names") or []
        if names:
            return panel_text(names[0])
    for b in (comp.get("geoBroadcasts") or []):
        media = (b.get("media") or {}).get("shortName")
        if media:
            return panel_text(media)
    return None


def _parse_bios(payload):
    """Athlete profile -> tale-of-the-tape fields. Missing stays None."""
    ath = (payload or {}).get("athlete") or {}
    stance = ath.get("stance") or {}
    assoc = ath.get("association") or {}
    career_tko = career_sub = None
    for s in ((ath.get("statsSummary") or {}).get("statistics") or []):
        name = s.get("name")
        dv = panel_text(s.get("displayValue")) or None
        if name == "tkos-tkoLosses":
            career_tko = dv
        elif name == "submissions-submissionLosses":
            career_sub = dv
    height = panel_text(ath.get("displayHeight")) or None
    if height:
        height = height.replace("\"", "").replace("  ", " ").strip()
    reach = panel_text(ath.get("displayReach")) or None
    if reach:
        reach = reach.replace("\"", "").strip()
    age = ath.get("age")
    if not isinstance(age, int):
        age = None
    return {
        "height": height,
        "reach": reach,
        "stance": panel_text(stance.get("text")) or None,
        "age": age,
        "country": panel_text((ath.get("citizenshipCountry") or {}).get("abbreviation")
                              or ath.get("citizenship")) or None,
        "camp": panel_text(assoc.get("name")) or None,
        "style": panel_text(ath.get("displayFightingStyle")) or None,
        "weight": panel_text(ath.get("displayWeight")) or None,
        "nickname": panel_text(ath.get("nickname")) or None,
        "career_tko": career_tko,
        "career_sub": career_sub,
    }


def _method(comp):
    """Finish method, or None.

    Derived from the play-by-play `details` list because ESPN exposes no
    method field -- see the module docstring. Returns None rather than
    guessing when the entry is absent, which is the normal case for a
    fight that has not finished yet.
    """
    for d in (comp.get("details") or []):
        t = d.get("type") or {}
        text = str(t.get("text") or "")
        if "Winner" not in text:
            continue
        by_id = METHOD_BY_ID.get(str(t.get("id")))
        if by_id:
            return by_id
        token = panel_text(text.replace("Unofficial", "").replace("Winner", ""))
        return METHOD_BY_TEXT.get(token, token or None)
    return None


def _parse_fight(comp, index, total):
    status = comp.get("status") or {}
    stype = status.get("type") or {}
    state = stype.get("state") or "pre"
    fighters = [_fighter(c) for c in (comp.get("competitors") or [])]
    rounds = (((comp.get("format") or {}).get("regulation") or {})
              .get("periods"))
    winner = next((f for f in fighters if f["winner"]), None)
    return {
        "id": comp.get("id"),
        "index": index,                      # 0-based, prelims first
        "number": index + 1,                 # 1-based, for "FIGHT 4 OF 14"
        "weight": panel_text((comp.get("type") or {}).get("abbreviation")),
        "rounds": rounds,
        # A 5-round fight on a UFC card is the main event; everything else
        # is 3. This is how the payload distinguishes it -- there is no
        # "isMainEvent" flag -- and it held on both real cards checked.
        # Card order is prelims first, main last (verified). A 5-round
        # non-main (UFC 330 co-main) is still 5 rounds -- not MAIN EVENT.
        "main_event": index == total - 1,
        "co_main": index == total - 2,
        "five_round": rounds == 5,
        "state": state,                      # pre | in | post
        "completed": bool(stype.get("completed")),
        "fighters": fighters,
        "winner": winner["name"] if winner else None,
        "loser": next((f["name"] for f in fighters if not f["winner"]), None) if winner else None,
        "method": _method(comp) if state == "post" else None,
        # Time ELAPSED in the final round -- see module docstring.
        "final_round": status.get("period") or None,
        "final_time": _clean_clock(status.get("displayClock")),
        "period": status.get("period") or 0,
        "clock": _clean_clock(status.get("displayClock")),
        "detail": panel_text(stype.get("shortDetail") or stype.get("detail")) or None,
        "status_name": panel_text(stype.get("name")) or None,
        "phase": _phase(comp),
        "last_action": _last_action(comp),
        "last_actions": _last_actions(comp, limit=3),
        "broadcast": _broadcast(comp),
    }


def _parse_card(event):
    status = (event.get("status") or {}).get("type") or {}
    comps = event.get("competitions") or []
    fights = [_parse_fight(c, i, len(comps)) for i, c in enumerate(comps)]
    venue = {}
    if comps:
        venue = comps[0].get("venue") or {}
    addr = venue.get("address") or {}
    return {
        "id": event.get("id"),
        "name": panel_text(event.get("name")),
        "short": panel_text(event.get("shortName")),
        "date": event.get("date"),
        "state": status.get("state") or "pre",
        "completed": bool(status.get("completed")),
        "venue": panel_text(venue.get("fullName")) or None,
        "city": panel_text(addr.get("city")) or None,
        "state_abbr": panel_text(addr.get("state")) or None,
        "broadcast": next((f.get("broadcast") for f in fights if f.get("broadcast")), None),
        "fights": fights,
        "total": len(fights),
        "done": sum(1 for f in fights if f["state"] == "post"),
        "live": any(f["state"] == "in" for f in fights),
    }


def _next_card_date(payload):
    """Start date of the next card from the league calendar, or None.

    The calendar is the authoritative schedule; the `event.$ref` links in
    it point at sports.core.api.espn.pvt, a private host that does not
    resolve from here, so only the label/startDate are usable.
    """
    try:
        cal = (payload.get("leagues") or [{}])[0].get("calendar") or []
    except (AttributeError, IndexError, TypeError):
        return None
    now = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    upcoming = [c for c in cal if str(c.get("startDate") or "") > now]
    return upcoming[0] if upcoming else None


class UfcFeed:
    """Background poller with a last-good cache -- same contract as every
    other FEED here: never blocks the caller, never invents a value, stops
    polling once nothing has read from it for IDLE_STOP."""

    STATS_REFRESH_LIVE = 3.0    # two per-fighter calls + plays, live fight only
    STATS_REFRESH_DONE = 600.0  # a finished fight's stats don't change once final

    def __init__(self):
        self._lock = threading.Lock()
        self._card = None
        self._next_label = None
        self._next_date = None
        self._updated = 0.0
        self._last_try = 0.0
        self._interval = 0.0
        self._fails = 0
        self._last_read = 0.0
        self._thread = None
        self._err = None
        # Fight-statistics side channel. Deliberately separate polling from
        # the card itself: stats cost TWO extra calls (one per fighter) with
        # no batched form, so they are fetched for AT MOST one fight at a
        # time -- whichever one the engine is currently displaying -- never
        # the whole card. want_stats() below is how the engine declares
        # that interest each tick.
        self._stats_want = None      # (event_id, comp_id, [athlete_id, athlete_id], live)
        self._stats_last_read = 0.0  # own idle clock -- NOT shared with _last_read,
                                      # which only the card getter touches
        self._stats_cache = {}       # fight_id -> {athlete_id: parsed stats}
        self._stats_updated = {}     # fight_id -> epoch
        self._stats_try = 0.0
        self._stats_thread = None
        self._stats_err = None
        self._bios_want = None       # (fight_id, (athlete_id, ...))
        self._bios_cache = {}        # fight_id -> {athlete_id: parsed bios}
        self._bios_updated = {}
        self._bios_err = None
        self._plays_cache = {}       # fight_id -> {phase, actions}
        self._plays_err = None

    def get(self):
        """{card, next_label, next_date, age, err}. Never blocks."""
        now = time.time()
        with self._lock:
            card = json.loads(json.dumps(self._card)) if self._card else None
            out = {
                "card": card,
                "next_label": self._next_label,
                "next_date": self._next_date,
                "age": (now - self._updated) if self._updated else None,
                "err": self._err,
            }
            self._last_read = now
        self._ensure_thread()
        return out

    def want_stats(self, fight_id, event_id, comp_id, athlete_ids, live):
        """Declare which fight's statistics the engine wants right now.

        Called every tick with the fight actually on screen. Switching to
        a different fight_id drops interest in the old one (its cache
        entry is simply not refreshed further, and is evicted once it's
        not the current or most-recent fight) -- so a viewer browsing
        through a 14-fight card never triggers 26 fighter-stat calls, only
        ever the one or two fights actually looked at.
        """
        with self._lock:
            self._stats_want = (fight_id, event_id, comp_id, tuple(athlete_ids), live)
            self._stats_last_read = time.time()
        self._ensure_stats_thread()

    def get_stats(self, fight_id):
        """Cached stats for a fight, keyed by athlete id, or {} if nothing
        has been fetched for it (yet, or ever -- e.g. a fight several
        positions away from the one being watched). Never blocks."""
        with self._lock:
            self._stats_last_read = time.time()
            return dict(self._stats_cache.get(fight_id) or {})

    def want_bios(self, fight_id, athlete_ids):
        """Tale of the tape for the fight on screen. Height/reach/stance
        do not change mid-round -- fetched once and cached."""
        with self._lock:
            self._bios_want = (fight_id, tuple(athlete_ids or ()))
            self._stats_last_read = time.time()
        self._ensure_stats_thread()

    def get_bios(self, fight_id):
        """Cached bios keyed by athlete id, or {}. Never blocks."""
        with self._lock:
            self._stats_last_read = time.time()
            return dict(self._bios_cache.get(fight_id) or {})

    def get_plays(self, fight_id):
        """Last live actions with ESPN's own round/clock, or {}."""
        with self._lock:
            self._stats_last_read = time.time()
            rec = self._plays_cache.get(fight_id) or {}
            return dict(rec) if rec else {}

    def _ensure_stats_thread(self):
        with self._lock:
            alive = self._stats_thread is not None and self._stats_thread.is_alive()
        if not alive:
            t = threading.Thread(target=self._run_stats, daemon=True)
            with self._lock:
                self._stats_thread = t
            t.start()

    def _poll_stats(self):
        with self._lock:
            want = self._stats_want
        if not want:
            return
        fight_id, event_id, comp_id, athlete_ids, live = want
        fetched = {}
        for aid in athlete_ids:
            if not aid:
                continue
            url = STATS_URL.format(event_id=event_id, comp_id=comp_id, athlete_id=aid)
            fetched[aid] = _parse_stats(_get_json(url))
        with self._lock:
            self._stats_cache[fight_id] = fetched
            self._stats_updated[fight_id] = time.time()
            self._stats_err = None
            # Keep the cache small: only the fight just fetched and
            # whatever was fetched immediately before it (covers the
            # moment a result view is still holding the just-finished
            # fight while the engine has already moved its "current"
            # pointer on).
            if len(self._stats_cache) > 2:
                oldest = min(self._stats_updated, key=self._stats_updated.get)
                if oldest != fight_id:
                    self._stats_cache.pop(oldest, None)
                    self._stats_updated.pop(oldest, None)
                    self._plays_cache.pop(oldest, None)

    def _poll_plays(self):
        """One extra call, live fight only -- actions here carry round + clock."""
        with self._lock:
            want = self._stats_want
        if not want:
            return
        fight_id, event_id, comp_id, _aids, live = want
        if not live or not event_id or not comp_id:
            return
        parsed = _parse_plays(_get_json(
            PLAYS_URL.format(event_id=event_id, comp_id=comp_id)))
        with self._lock:
            self._plays_cache[fight_id] = parsed
            self._plays_err = None

    def _poll_bios(self):
        with self._lock:
            want = self._bios_want
        if not want:
            return
        fight_id, athlete_ids = want
        fetched = {}
        for aid in athlete_ids:
            if not aid:
                continue
            fetched[aid] = _parse_bios(_get_json(BIOS_URL.format(athlete_id=aid)))
        with self._lock:
            self._bios_cache[fight_id] = fetched
            self._bios_updated[fight_id] = time.time()
            self._bios_err = None
            if len(self._bios_cache) > 4:
                oldest = min(self._bios_updated, key=self._bios_updated.get)
                if oldest != fight_id:
                    self._bios_cache.pop(oldest, None)
                    self._bios_updated.pop(oldest, None)

    def _run_stats(self):
        while True:
            with self._lock:
                idle = time.time() - self._stats_last_read > IDLE_STOP
                want = self._stats_want
                if want:
                    fight_id, live = want[0], want[4]
                    last = self._stats_updated.get(fight_id, 0.0)
                    interval = self.STATS_REFRESH_LIVE if live else self.STATS_REFRESH_DONE
                    due = time.time() - last >= interval
                else:
                    due = False
                bios_want = self._bios_want
                if bios_want:
                    b_id = bios_want[0]
                    blast = self._bios_updated.get(b_id, 0.0)
                    bios_due = time.time() - blast >= BIOS_REFRESH
                else:
                    bios_due = False
            if idle:
                with self._lock:
                    self._stats_thread = None
                return
            if due:
                with self._lock:
                    self._stats_try = time.time()
                try:
                    self._poll_stats()
                except Exception as e:                     # noqa: BLE001 - never die
                    with self._lock:
                        self._stats_err = f"{type(e).__name__}"
                    time.sleep(ERROR_BACKOFF_BASE)
                else:
                    try:
                        self._poll_plays()
                    except Exception as e:                 # noqa: BLE001 - never die
                        with self._lock:
                            self._plays_err = f"{type(e).__name__}"
            if bios_due:
                try:
                    self._poll_bios()
                except Exception as e:                     # noqa: BLE001 - never die
                    with self._lock:
                        self._bios_err = f"{type(e).__name__}"
                    time.sleep(ERROR_BACKOFF_BASE)
            time.sleep(1.0)

    def _ensure_thread(self):
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
        if not alive:
            t = threading.Thread(target=self._run, daemon=True)
            with self._lock:
                self._thread = t
            t.start()

    def _next_interval(self, card):
        if card and card.get("live"):
            return REFRESH_LIVE
        if card and not card.get("completed"):
            return REFRESH_CARD_TODAY
        return REFRESH_IDLE

    def _poll(self):
        payload = _get_json(SCOREBOARD_URL)
        events = payload.get("events") or []
        card = _parse_card(events[0]) if events else None
        nxt = _next_card_date(payload)
        with self._lock:
            self._card = card
            self._next_label = panel_text(nxt.get("label")) if nxt else None
            self._next_date = (nxt or {}).get("startDate")
            self._updated = time.time()
            self._err = None
            self._fails = 0
            self._interval = self._next_interval(card)

    def _run(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read > IDLE_STOP
                due = time.time() - self._last_try >= self._interval
            if idle:
                with self._lock:
                    self._thread = None
                return
            if due:
                with self._lock:
                    self._last_try = time.time()
                try:
                    self._poll()
                except Exception as e:                      # noqa: BLE001 - never die
                    with self._lock:
                        self._fails += 1
                        self._err = f"{type(e).__name__}"
                        # Never hammer a throttled or broken endpoint --
                        # same discipline as sports.py.
                        self._interval = min(
                            ERROR_BACKOFF_MAX,
                            ERROR_BACKOFF_BASE * (2 ** (self._fails - 1)))
            time.sleep(1.0)


FEED = UfcFeed()
