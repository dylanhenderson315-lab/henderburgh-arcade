"""
mma.py -- UFC card data for GAME DAY mode.

Same shape as sports.py/flights.py/satellite.py: all I/O lives here so the
engine that draws it stays pure and testable without a network or a panel.

ONE endpoint, and it is unusually kind to us:
    site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard
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
import unicodedata
import urllib.error
import urllib.request

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
SCOREBOARD_DATED = SCOREBOARD_URL + "?dates={day}"

# One request returns the whole card, so the live tier is genuinely cheap:
# a fight ending is the single most time-critical event this project has
# (the result view is the payoff), and 20s is one call per 20s TOTAL, not
# per fight.
REFRESH_LIVE = 20.0        # a fight is in progress right now
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
# THE RECURRING BUG IN THIS PROJECT, and MMA is its worst case yet.
#
# engines._FONT3x5 has 52 glyphs -- A-Z, 0-9, space and !$%&'+,-./:<>?@
# -- and draw_text3x5 SILENTLY SKIPS anything else. It does
# not raise, it does not substitute; the character just is not there. That
# has now produced a visible bug six separate times in this codebase.
#
# UFC is the highest-risk feed we have for it: the roster is heavily
# international and a single real card (2026-08-01) contained Medic,
# Spasic, Milosevic, Rebecki, Cepo and Todorovic with accented characters
# in every one of those names. Untreated, "MEDIC" would have rendered as
# "MEDI" -- a wrong name, silently, with nothing in the output to hint at
# it.
#
# So folding happens HERE, at the I/O boundary, exactly once, and the
# engine can trust that every string this module emits is renderable.
# NFKD decomposition handles the accented Latin letters; the map below
# covers the ones that do not decompose (a stroke through a letter is not
# a combining mark).
_TRANSLIT = {
    "Ø": "O", "ø": "O",      # O with stroke
    "Ł": "L", "ł": "L",      # L with stroke (Blachowicz, Rebecki)
    "Æ": "AE", "æ": "AE",
    "Đ": "D", "đ": "D",      # D with stroke
    "ß": "SS",
    "Ħ": "H", "ħ": "H",
    "Þ": "TH", "þ": "TH",
    "Ĳ": "IJ", "ĳ": "IJ",
    "’": "'", "‘": "'",      # curly quotes -> the straight one we have
    "–": "-", "—": "-", "−": "-",
    "×": "X",
}
_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !$%&'+,-./:<>?@")


def panel_text(s):
    """Fold any external string to something the 3x5 font can actually
    draw, uppercased.

    Order matters: explicit map first (those characters do not decompose),
    then NFKD to strip combining accents, then a hard filter so ANY
    remaining unsupported character becomes a space rather than vanishing.
    A space is deliberate -- it preserves the shape of the name and is
    visible as "something was here", whereas silent removal is the exact
    failure mode this function exists to prevent.
    """
    if s is None:
        return ""
    s = str(s)
    for bad, good in _TRANSLIT.items():
        s = s.replace(bad, good)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = "".join(c if c in _ALLOWED else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()


STATS_URL = ("https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc"
            "/events/{event_id}/competitions/{comp_id}/competitors/{athlete_id}"
            "/statistics?lang=en&region=us")

# ESPN's raw stat names -> the compact broadcast-style fields this module
# exposes. Only the ones an on-air UFC stats graphic actually shows (see
# the module docstring) -- ESPN exposes 40+ granular strike-location
# splits (head/body/leg x distance/clinch/ground) that are real but not
# what belongs on a 64px panel.
_STAT_KEYS = {
    "sigStrikesLanded": "sig_landed",
    "sigStrikesAttempted": "sig_att",
    "takedownsLanded": "td_landed",
    "takedownsAttempted": "td_att",
    "knockDowns": "knockdowns",
    "timeInControl": "control_time",
}


def _parse_stats(payload):
    """One fighter's statistics resource -> the compact dict above.

    Every value defaults to None (not 0) if ESPN's response is missing
    the category entirely -- e.g. a fight that somehow has no `general`
    category -- so the engine can tell "genuinely zero" (a real 0-0 fight)
    apart from "field not provided" and render each honestly.
    """
    out = {v: None for v in _STAT_KEYS.values()}
    cats = ((payload.get("splits") or {}).get("categories") or [])
    stats = cats[0].get("stats") if cats else []
    for s in (stats or []):
        key = _STAT_KEYS.get(s.get("name"))
        if key:
            out[key] = s.get("value") if key != "control_time" else panel_text(s.get("displayValue"))
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
    return {
        "id": competitor.get("id"),          # athlete id -- needed to fetch
                                              # per-fighter statistics below
        "name": panel_text(ath.get("shortName") or ath.get("displayName")),
        "full": panel_text(ath.get("fullName") or ath.get("displayName")),
        "record": panel_text(rec) or None,
        "winner": bool(competitor.get("winner")),
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
        "main_event": rounds == 5,
        "co_main": index == total - 2,
        "state": state,                      # pre | in | post
        "completed": bool(stype.get("completed")),
        "fighters": fighters,
        "winner": winner["name"] if winner else None,
        "loser": next((f["name"] for f in fighters if not f["winner"]), None) if winner else None,
        "method": _method(comp) if state == "post" else None,
        # Time ELAPSED in the final round -- see module docstring.
        "final_round": status.get("period") or None,
        "final_time": panel_text(status.get("displayClock")) or None,
        "period": status.get("period") or 0,
        "clock": status.get("displayClock") or None,
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

    STATS_REFRESH_LIVE = 15.0   # a fight in progress -- numbers move fast
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
            if idle:
                with self._lock:
                    self._stats_thread = None
                return
            if due:
                with self._lock:
                    self._stats_try = time.time()
                try:
                    self._poll_stats()
                except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                        ValueError, KeyError, TypeError) as e:
                    with self._lock:
                        self._stats_err = f"{type(e).__name__}"
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
                except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                        ValueError, KeyError, TypeError) as e:
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
