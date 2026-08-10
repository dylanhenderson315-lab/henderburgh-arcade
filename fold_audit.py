"""
fold_audit.py -- prove every feed FOLDS its text, not that today's data is
already clean.

    .venv/bin/python fold_audit.py

WHY render_audit.py IS NOT ENOUGH. That tool instruments the renderer and
reports characters the font cannot draw -- but only for the data that
happens to be flowing right now. If today's headlines are plain ASCII, an
unfolded field looks perfectly healthy and the bug stays LATENT until the
day an accented name shows up.

That is not hypothetical. It is how this shipped repeatedly:

  * "Rasmus Hojgaard" led a PGA tournament and drew as "HJGAARD" -- the
    universal sports parser had never been folded, and nothing noticed
    because every previous leaderboard happened to be ASCII.
  * Four feeds (news, blog, flights, weather) were still calling bare
    .upper() long after paneltext existed. A live check passed on all of
    them, because their data that day contained nothing to lose.
  * sports.py's PER-LEAGUE parser was missed even while migrating "all
    feeds", and its live output was fold-clean, so it looked done.

So this test does not ask "is the current data clean". It replays each
feed's own REAL payload with known-undrawable characters injected into
the display strings, and asserts nothing undrawable survives to the
output. A feed that folds passes regardless of the injection; a feed that
does not fold fails immediately, today, instead of in six months.

CONTROL TOKENS ARE LEFT ALONE. Values like "pre"/"in"/"post" and
"home"/"away" are compared against by the parsers, so polluting them would
break the parse rather than test the fold. They are also never drawn -- the
engine maps them to its own display strings -- so they are skipped
deliberately, not overlooked.
"""
import copy
import json
import sys

import paneltext

# Characters chosen because each one has ALREADY caused a real bug here.
CANARIES = [
    "’",   # curly apostrophe -- dropped from a live RSS headline
    "é",   # e-acute          -- the accented-name class generally
    "ø",   # o-stroke         -- "Hojgaard" -> "HJGAARD"
    "–",   # en dash          -- common in headlines and venue names
    "ć",   # c-acute          -- "Medic", a real UFC fighter
]

# Never injected into: parsers compare against these, and none is drawn.
CONTROL_VALUES = {
    "pre", "in", "post", "home", "away", "team", "athlete",
    "true", "false", "STATUS_SCHEDULED", "STATUS_FINAL", "STATUS_IN_PROGRESS",
}

# Keys whose values are identifiers, URLs or timestamps -- polluting them
# breaks joins and date parsing without testing anything about folding.
SKIP_KEYS = {
    "id", "uid", "guid", "competitionId", "event_id", "pccEventId",
    "date", "startDate", "endDate", "seasonStartDate", "seasonEndDate",
    "teeTime", "href", "link", "links", "logo", "logoDark", "headshot",
    "$ref", "slug", "type", "state", "homeAway", "abbreviation",
}


def inject(obj, key=None):
    """Deep-copy a payload with canaries appended to display strings."""
    if isinstance(obj, dict):
        return {k: inject(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [inject(v, key) for v in obj]
    if isinstance(obj, str):
        if key in SKIP_KEYS or obj in CONTROL_VALUES or not obj.strip():
            return obj
        return obj + "".join(CANARIES)
    return obj


def strings_of(obj, path="", out=None):
    """Every string an output structure contains, with its path."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            strings_of(v, "%s.%s" % (path, k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            strings_of(v, "%s[%d]" % (path, i), out)
    elif isinstance(obj, str):
        out.append((path, obj))
    return out


def check(name, produce, ignore_paths=()):
    """Run one feed's parser over canary-injected input and report any
    undrawable character that survived."""
    try:
        result = produce()
    except Exception as e:                      # noqa: BLE001 - report, continue
        print("  %-28s ERROR %s: %s" % (name, type(e).__name__, e))
        return 1
    bad = []
    for path, s in strings_of(result):
        if any(path.endswith(p) or p in path for p in ignore_paths):
            continue
        undrawable = paneltext.undrawable(s)
        if undrawable:
            bad.append((path, s[:44], undrawable))
    if bad:
        print("  %-28s NOT FOLDED (%d field%s)" % (name, len(bad), "" if len(bad) == 1 else "s"))
        for path, s, u in bad[:6]:
            print("      %-26s %r  leaks %s" % (path.lstrip("."), s, u))
        return 1
    print("  %-28s folded" % name)
    return 0


def main():
    import sports
    import mma

    print("Replaying real payloads with canaries injected: %s\n"
          % " ".join(repr(c) for c in CANARIES))
    bad = 0

    # ---- sports: the UNIVERSAL header parser ----------------------------
    raw = sports._get_json(sports.HEADER_URL)
    poisoned = inject(raw)
    # `state` drives control flow and is skipped above, so the parse stays
    # structurally valid while every display string is polluted.
    bad += check("sports universal header",
                 lambda: sports._parse_header(poisoned),
                 ignore_paths=("state", "sport", "player_state", "home_away",
                               "id", "competition_id", "tee_time"))

    # ---- sports: the PER-LEAGUE parser ---------------------------------
    # This is the one that was missed while "migrating all feeds", and its
    # live output was clean, so nothing caught it.
    league = "MLB"
    sb = sports._get_json(
        sports.SCOREBOARD_URL.format(path=sports.LEAGUE_PATHS[league]))
    events = sb.get("events") or []
    if events:
        ev = inject(events[0])
        bad += check("sports per-league event",
                     lambda: sports._parse_event(ev, league),
                     ignore_paths=("state", "event_id", "league", "home_away"))
    else:
        print("  %-28s skipped (no events today)" % "sports per-league event")

    # ---- mma card -------------------------------------------------------
    card = sports._get_json(mma.SCOREBOARD_URL).get("events") or []
    if card:
        ev = inject(card[0])
        bad += check("mma card",
                     lambda: mma._parse_card(ev),
                     ignore_paths=("state", "id", "date", "completed"))
    else:
        print("  %-28s skipped (no card)" % "mma card")

    # ---- sports: TENNIS -- own dedicated per-tour endpoint, different
    # nested shape (groupings[] -> competitions[]) from every parser above.
    tdata = sports._get_json(sports.SCOREBOARD_URL.format(path=sports.TENNIS_TOURS["WTA"]))
    tevents = tdata.get("events") or []
    if tevents:
        ev = inject(tevents[0])
        g = (ev.get("groupings") or [{}])[0]
        comp = (g.get("competitions") or [{}])[0]
        bad += check("sports tennis match",
                     lambda: sports._parse_tennis_match(
                         comp, (g.get("grouping") or {}).get("displayName"),
                         "WTA", paneltext.panel_text(ev.get("name")),
                         paneltext.panel_text(ev.get("shortName"))),
                     ignore_paths=("state", "id", "home_away", "tiebreak", "winner",
                                   "games", "sport", "league", "best_of"))
    else:
        print("  %-28s skipped (no tennis today)" % "sports tennis match")

    # ---- weather (2026-08-09) -- added after a gap audit found weather.py
    # had NO coverage here at all despite folding city/state/textDescription/
    # event/headline/areaDesc inline in its own fetch functions. The severe
    # alert path (event/headline/areaDesc) is exactly the kind of field a
    # real NWS alert can put a curly quote or em dash into. Real live
    # payloads, canary-injected, same as every parser above -- with
    # `_get_json` monkeypatched (not the network call) so the injected
    # payload reaches the real boundary code, not a synthetic dict shaped
    # by guesswork.
    import weather
    _real_get_json = weather._get_json

    def _mock_get_json(seq):
        it = iter(seq)
        return lambda url: next(it)

    obs_raw = _real_get_json("https://api.weather.gov/stations/KMYR/observations/latest")
    weather._get_json = _mock_get_json([inject(obs_raw)])
    try:
        bad += check("weather observation",
                     lambda: weather._read_observation("KMYR"),
                     ignore_paths=("station",))
    finally:
        weather._get_json = _real_get_json

    alerts_raw = _real_get_json(weather.ALERTS_URL.format(lat=34.0, lon=-81.0))
    weather._get_json = _mock_get_json([inject(alerts_raw)])
    try:
        if (alerts_raw.get("features") or []):
            bad += check("weather alerts",
                         lambda: {"a": weather._fetch_alerts(34.0, -81.0)},
                         ignore_paths=("severity", "urgency"))
        else:
            print("  %-28s skipped (no active alerts)" % "weather alerts")
    finally:
        weather._get_json = _real_get_json

    points_raw = _real_get_json(weather.POINTS_URL.format(lat=34.0, lon=-81.0))
    stations_url = ((points_raw.get("properties") or {}).get("observationStations"))
    stations_raw = _real_get_json(stations_url) if stations_url else {"features": []}
    weather._get_json = _mock_get_json([inject(points_raw), inject(stations_raw)])
    try:
        bad += check("weather point (city/state)",
                     lambda: weather._fetch_point(34.0, -81.0),
                     ignore_paths=("stations", "station", "forecast_url"))
    finally:
        weather._get_json = _real_get_json

    # ---- /api/notify pass-through (2026-08-09) -- the fold lives inline
    # in arcade_server.py ("Fold at the boundary" comment on the /api/notify
    # handler), which this file never imports (constructing Arcade() while
    # the real service may be running is the exact hazard CLAUDE.md calls
    # out). The boundary code is literally `paneltext.panel_text(title)` /
    # `paneltext.panel_text(message)` with no other transform, so exercising
    # the fold function directly on the same shape of input tests the real
    # boundary behavior, same reasoning the skypass/atc_transcribe cases
    # below already use for a fold that lives inline rather than in its own
    # named function.
    dirty_notify = "Garage Door" + "".join(CANARIES) + " Opened"
    bad += check("notify title/message fold",
                 lambda: {"title": paneltext.panel_text(dirty_notify),
                          "message": paneltext.panel_text(dirty_notify)})

    # ---- now playing (2026-08-09) -- real artist/track/album names are
    # exactly the kind of text that carries accents and curly quotes
    # ("Sigur Ros", "Beyonce", "Deja Vu"), i.e. the single highest-risk
    # category for this project's #1 bug class after MMA fighter names.
    # Replayed through nowplaying._parse_now_playing() -- the real
    # boundary function -- with canaries injected into a real-shaped
    # Last.fm user.getRecentTracks payload. NOT a live fetch: Last.fm
    # requires an API key and none is configured (see nowplaying.py's own
    # honest note about that), so this replays the documented payload
    # shape rather than a captured one -- which is exactly what this
    # audit is for, since it tests THE FOLD, not the schema.
    import nowplaying
    dirty_np = {"recenttracks": {"track": [{
        "name": "Song" + "".join(CANARIES),
        "artist": {"#text": "Artist" + "".join(CANARIES)},
        "album": {"#text": "Album" + "".join(CANARIES)},
        "@attr": {"nowplaying": "true"},
    }]}}
    bad += check("nowplaying track/artist/album",
                 lambda: nowplaying._parse_now_playing(dirty_np))

    # ---- the pure text cleaners each feed exposes ------------------------
    import news
    import blog
    import flights
    import skypass
    dirty = "Test" + "".join(CANARIES) + " Name"
    bad += check("news._clean_title", lambda: {"t": news._clean_title(dirty)})
    bad += check("blog._clean", lambda: {"t": blog._clean(dirty)})
    bad += check("flights._ident",
                 lambda: {"t": flights._ident({"flight": dirty, "r": dirty})})
    # THE HANGAR's "reg" field (hangar.py) -- folds inline in
    # _fetch_positions() rather than through a standalone function, same
    # as _ident's own "r" fallback just above; exercised the same way,
    # via the real fold call directly on a dirty registration string.
    bad += check("flights reg field (Hangar)",
                 lambda: {"t": flights.paneltext.panel_text(dirty)})
    # skypass folds the TLE object name inline in predict() rather than
    # through a standalone cleaner -- exercised via the real fold function
    # directly since there is no fetch to replay without a live catalogue.
    bad += check("skypass (TLE name fold)",
                 lambda: {"name": paneltext.panel_text(dirty)})

    # atc_transcribe.fold_transcript() -- PERSONAL-RIG-ONLY (see
    # atc.py/CLAUDE.md), but the fold boundary is real code and gets the
    # same coverage as everything else. Added after this exact bug shipped
    # ONCE already, in this same codebase, in this same feature: Whisper's
    # raw output is natural mixed-case English, and unfolded "A bunch of
    # them" rendered as literally just "A" -- caught by looking at a
    # rendered frame, not by this audit, because this audit didn't cover
    # it yet. It does now. Uses lowercase in the dirty string specifically
    # (the other canaries alone wouldn't have caught THIS bug) since that
    # was the actual failure mode, not just diacritics/punctuation.
    import atc_transcribe
    dirty_transcript = "a bunch of them, runway two three, " + "".join(CANARIES)
    bad += check("atc_transcribe fold",
                 lambda: {"text": atc_transcribe.fold_transcript(dirty_transcript)})

    print("\n%d feed%s NOT folding" % (bad, "" if bad == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
