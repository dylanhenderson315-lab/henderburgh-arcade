"""
render_audit.py -- catch silent text damage before it reaches the panel.

    .venv/bin/python render_audit.py            # every mode, real feed data
    .venv/bin/python render_audit.py sports     # one mode
    .venv/bin/python render_audit.py --strict   # exit 1 on truncation too

WHY THIS EXISTS. The single most persistent bug class in this project is
text that is SILENTLY damaged on its way to the panel. It has shipped at
least nine times, and twice more in one afternoon while building the
per-sport renderers. It is invisible to a code read, and easy to miss on
the panel, because the output always looks like *a* plausible string:

    lowercase symbols vanishing        -> a shorter, still-real ticker
    "United Airlines"                  -> "U A"
    "AWAY 3 @ HOME 5"                  -> "AWAY 3  HOME 5"  (lost @)
    "3RD & 7"                          -> "3RD  7"          (lost &)
    "Rasmus Hojgaard"                  -> "HJGAARD"         (lost stroked O)
    "7-6(7-5)"                         -> "7-67-5"          (a DIFFERENT score)
    "T. POSTARNAKOVA"                  -> "T."              (lost the identity)
    a golf footer at y=60              -> bottom row clipped off-panel

Every one of those was found by instrumenting the renderer and driving it
with real data. This makes that instrumentation permanent and repeatable
instead of an ad-hoc script rewritten per session.

FOUR CLASSES OF DAMAGE ARE CHECKED:

  DROPPED   a character the 3x5 font has no glyph for. draw_text3x5 skips
            it silently -- no error, no substitute.
  OVERFLOW  a draw whose box leaves the 64x64 panel. Partially-drawn
            glyphs at the edge, or a row below y=59 clipping.
  TRUNCATED fit_text/fit_person shortened a string. Not always a bug -- a
            long headline SHOULD abbreviate -- so it is reported
            separately and only fails the run under --strict.
  COLLISION two text draws sharing pixels. Fixed layouts collide the
            moment content varies (a longer record, a team with a longer
            name), which is exactly how the MMA name/record and the
            tennis venue/record overlaps happened.

SELECT-TO-EXPAND IS NOW DRIVEN, NOT JUST STEPPED (2026-08-02). The
per-step loop used to only step left/right and snap whatever was
already on screen -- it never called input("rotate")/input("drop"), so
any mode's expanded/detail view got ZERO automated coverage unless a
human happened to drive it by hand. That is exactly how a real
collision (a flights detail-card layout bug: an inline aircraft-type
string centred across the wrong span, only visible for certain
left/right stat widths) passed a clean render_audit run and was only
found by manually pressing rotate against every real aircraft. Proven
closed, not just assumed: reintroducing that exact bug and re-running
this tool with no manual driving reproduces the failure automatically
(`COLLISION step 2 rotate: '37MI NW' overlaps 'C414'`). `rotate` is
this hardware's real select button (same convention SportsEngine's own
select-to-expand documents), so this now runs unconditionally on every
`_step`-based mode in the sweep -- see drive()'s per-step loop.

Marquees legitimately draw partially off-panel at both edges to loop
seamlessly, so draws flagged by a renderer known to marquee are reported
but not failed -- see MARQUEE_OK.

NEVER import arcade_server here. Constructing a second Arcade alongside
the launchd service puts two DDP senders on the panel and locks it hard
enough to need a physical power cycle (see CLAUDE.md). This imports
`engines` only, which needs no panel and no render loop.
"""
import sys
import time

import engines
import notify

# Modes that scroll a marquee, which draws glyphs deliberately off-edge.
# `ambient` inherits this: it composes real instances of the other
# modes and delegates frame(), so a news marquee inside ambient is
# the same marquee. `notify` joins this set for the same reason -- its
# own message row falls back to draw_marquee() when the wrapped text
# doesn't fit three lines (see NotifyEngine.frame()).
MARQUEE_OK = {"news", "ticker", "sports", "gameday", "ambient", "flights", "notify",
              "nowplaying", "weather"}

# followflight's route line is fit_text()-truncated, never marqueed --
# it has no continuous scroll of its own, so it does NOT join this set.

# Modes worth auditing: the data modes and event modes, which are the ones
# rendering externally-sourced text. Games draw their own sprites and have
# no API strings to damage. `planewatch`/`notify` (2026-08-09) are
# force-triggered-only takeovers -- see drive_planewatch()/drive_notify()
# below for why they need dedicated drivers instead of the generic one.
TEXT_MODES = ["ticker", "satellite", "flights", "followflight", "departures", "nowplaying",
              "sports", "news", "weather", "clock", "blog", "events", "ambient", "gameday",
              "planewatch", "notify"]

# Modes with a dedicated driver instead of the generic zero-arg
# construct-and-tick loop in drive() below.
CUSTOM_DRIVERS = {}


class Audit:
    """Wraps the renderer's text primitives and records what they did."""

    def __init__(self):
        self.draws = []
        self.dropped = []
        self.overflow = []
        self.truncated = []
        self.clipped = []
        self._orig_text = None
        self._orig_fit = None
        self._orig_person = None
        self._orig_putpx = None

    def install(self):
        self._orig_text = engines.draw_text3x5
        self._orig_fit = engines.fit_text
        self._orig_person = engines.fit_person
        self._orig_putpx = engines.put_px

        def put_px(buf, x, y, color):
            # put_px is BOUNDS-CHECKED AND SILENT -- an out-of-range write
            # is simply dropped, no error, no visible sign anything is
            # wrong. Every graphical primitive that isn't text (diamonds,
            # outs pips, trend arrows, arcs, event-frame borders) goes
            # through this, so a layout bug here is invisible to both the
            # DROPPED/OVERFLOW checks (which only watch draw_text3x5) and
            # to a spot check, exactly the same way a silently truncated
            # string is. Found by hand-deriving a real overflow in the
            # baseball detail view's y-budget (53+12=65 > 64) that had NO
            # signal anywhere else -- this closes that blind spot.
            if not (0 <= x < engines.WIDTH and 0 <= y < engines.HEIGHT):
                self.clipped.append((x, y))
            return self._orig_putpx(buf, x, y, color)

        engines.put_px = put_px

        def draw_text3x5(buf, x, y, text, color, scale=1):
            s = str(text)
            for ch in s:
                if ch not in engines._FONT3x5:
                    self.dropped.append((s, ch))
            w = engines.text_w(s, scale)
            h = 5 * scale
            if s.strip():
                if x < 0 or x + w > engines.WIDTH or y < 0 or y + h > engines.HEIGHT:
                    self.overflow.append((s, x, y, w, scale))
                self.draws.append((x, y, w, h, s))
            return self._orig_text(buf, x, y, text, color, scale)

        def fit_text(s, max_px, scale=1):
            out = self._orig_fit(s, max_px, scale)
            if s and out != str(s):
                self.truncated.append(("fit_text", str(s), out))
            return out

        def fit_person(name, max_px, scale=1):
            out = self._orig_person(name, max_px, scale)
            if name and out != str(name).strip():
                self.truncated.append(("fit_person", str(name), out))
            return out

        engines.draw_text3x5 = draw_text3x5
        engines.fit_text = fit_text
        engines.fit_person = fit_person

    def remove(self):
        engines.draw_text3x5 = self._orig_text
        engines.fit_text = self._orig_fit
        engines.fit_person = self._orig_person
        engines.put_px = self._orig_putpx

    def reset_frame(self):
        self.draws = []

    def collisions(self):
        """Text draws that share pixels within a single frame.

        Compares axis-aligned boxes: cheap, and the false-positive rate is
        low because glyph boxes are tight. Two draws that legitimately
        interleave (none currently do) would need an exception here."""
        hits = []
        for i in range(len(self.draws)):
            xa, ya, wa, ha, sa = self.draws[i]
            for j in range(i + 1, len(self.draws)):
                xb, yb, wb, hb, sb = self.draws[j]
                if (xa < xb + wb and xb < xa + wa
                        and ya < yb + hb and yb < ya + ha):
                    hits.append((sa, sb))
        return hits


def _snap(audit, eng, label, frames_box, collisions):
    """Shared single-frame probe: reset the audit's per-frame state, render
    once, count it, and record any collision under `label`. Same shape as
    drive()'s own inner snap(), pulled out so the dedicated drivers below
    can reuse it without duplicating the try/except-and-report contract."""
    audit.reset_frame()
    try:
        eng.frame()
    except Exception as e:                        # noqa: BLE001 - report, don't abort
        print("    !! %s raised %s: %s" % (label, type(e).__name__, e))
        return
    frames_box[0] += 1
    for a, b in audit.collisions():
        collisions.append((label, a, b))


def drive_planewatch(audit):
    """PlaneWatchEngine (2026-08-08's plane-in-window takeover) is
    force-triggered only: reset() pops flights.FEED's real one-shot
    window-entry batch, which requires an actual aircraft crossing the
    configured window cone RIGHT NOW. The generic drive() below would
    construct it, see an empty batch, and render nothing but the blank
    fallback -- passing clean while covering none of the real content.
    This is exactly the gap the 2026-08-09 full-project gap audit flagged:
    every change to this engine had to be remembered and manually
    force-verified by hand, with no permanent regression net.

    Bypasses the FEED pop (there is no public "push a batch" API, by
    design -- the real one-shot slot only exists to be filled by a real
    detector, see flights.FEED.pop_window_takeover_batch()'s own
    docstring) the same way this session's own ad hoc verification
    scripts already did: construct via __new__ and set .batch/.idx/.ticks
    directly, mirroring exactly what reset() would have set had a real
    aircraft been popped. Three synthetic variants exercise the real
    branches: a single notable aircraft (single-hold path, header tag
    stacking), a two-aircraft batch (the "N/M" position counter + idx
    cycling), and a minimal aircraft with no reg/type/notable data at all
    (the honest-gap fallback text: "UNKNOWN" ident, no type row)."""
    frames = [0]
    collisions = []
    variants = [
        [{"hex": "a1b2c3", "ident": "UAL2847", "reg": "N182UA", "type": "B739",
          "alt_ft": 3800, "dist_nm": 2.1, "notable": ["HEAVY"]}],
        [{"hex": "d4e5f6", "ident": "N911PD", "reg": "N911PD", "type": "EC35",
          "alt_ft": 900, "dist_nm": 0.8, "notable": ["LAW ENFORCEMENT HELICOPTER"]},
         {"hex": "b7c8d9", "ident": "SWA415", "reg": "N8620E", "type": "B38M",
          "alt_ft": 4500, "dist_nm": 3.4, "notable": None}],
        [{"hex": "000000", "ident": None, "reg": None, "type": None,
          "alt_ft": None, "dist_nm": None, "notable": None}],
    ]
    for i, batch in enumerate(variants):
        eng = engines.PlaneWatchEngine.__new__(engines.PlaneWatchEngine)
        eng.batch = batch
        eng.idx = 0
        eng.ticks = 20
        eng.launch = None
        _snap(audit, eng, "planewatch variant %d" % i, frames, collisions)
        if len(batch) > 1:
            eng.idx = 1
            _snap(audit, eng, "planewatch variant %d idx 1" % i, frames, collisions)

    # HANGAR "STORY FACT" branch (2026-08-10) -- none of the three
    # variants above have a registration that matches a real Hangar
    # entry, so the "FIRST SIGHTING"/"SEEN NX" text this screen now
    # leads with (replacing the old raw dist+alt row) had ZERO real
    # coverage from the sweep above: every variant fell through to the
    # distance-fallback branch instead. Monkeypatches engines.hangar.LOG.get()
    # (never the real hangar_log.jsonl file) to exercise both real
    # sub-branches directly.
    _real_hangar_get = engines.hangar.LOG.get
    try:
        engines.hangar.LOG.get = lambda: [{"reg": "N182UA", "times_seen": 1}]
        eng = engines.PlaneWatchEngine.__new__(engines.PlaneWatchEngine)
        eng.batch = variants[0]
        eng.idx = 0
        eng.ticks = 20
        eng.launch = None
        _snap(audit, eng, "planewatch first-sighting story", frames, collisions)

        engines.hangar.LOG.get = lambda: [{"reg": "N182UA", "times_seen": 47}]
        _snap(audit, eng, "planewatch seen-47x story", frames, collisions)
    finally:
        engines.hangar.LOG.get = _real_hangar_get
    return frames[0], collisions


def drive_notify(audit):
    """NotifyEngine (2026-08-08's HA notification takeover) is also
    force-triggered only -- reset() pops notify's own one-shot pending
    slot, populated for real only by a genuine /api/notify POST. Unlike
    PlaneWatchEngine there IS a real public API for this
    (notify.push_pending()), so this drives it through the actual
    reset()/pop path rather than poking internals -- push a real payload,
    then construct engines.NotifyEngine() normally and let its own
    reset() consume it. Three variants: a short title/message (the
    common case), a long message that forces the wrap-to-3-lines-or-
    marquee-overflow branch, and an empty payload (reset()'s own
    `str(payload.get(...) or "")` honest-empty fallback, not a crash).

    Payloads are pre-folded through paneltext.panel_text() before being
    pushed, matching arcade_server.py's own /api/notify handler EXACTLY
    ("Fold at the boundary -- the one place external text enters this
    endpoint") -- NotifyEngine.frame() draws self.title/self.message raw,
    with no fold of its own, by design: this project's fold discipline is
    ONE boundary per feed, not defense-in-depth at every draw site (see
    CLAUDE.md/fold_audit.py's docstring). Pushing raw mixed-case here
    would report a false DROPPED-lowercase failure that doesn't exist in
    real production traffic, where /api/notify always folds first."""
    import paneltext
    frames = [0]
    collisions = []
    variants = [
        {"title": "Garage Door", "message": "Left open 20 minutes", "color": None},
        {"title": "Front Door Camera Motion Detected",
         "message": ("A much longer message body than any single HA "
                      "automation should realistically send, long enough "
                      "to force either the multi-line wrap budget or the "
                      "marquee overflow fallback, whichever this message "
                      "actually needs."),
         "color": None},
        {"title": "", "message": "", "color": None},
    ]
    for i, payload in enumerate(variants):
        folded = dict(payload)
        folded["title"] = paneltext.panel_text(payload["title"])
        folded["message"] = paneltext.panel_text(payload["message"])
        notify.push_pending(folded)
        eng = engines.NotifyEngine()
        eng.ticks = 20
        _snap(audit, eng, "notify variant %d" % i, frames, collisions)
    return frames[0], collisions


def drive_events(audit):
    """EventsLogEngine (2026-08-09's recent-events log) reads
    events_log.LOG.get(), which on a real fresh device is honestly empty
    -- the generic drive() below would cover only the "NO EVENTS YET"
    fallback and never exercise a populated page. A dedicated driver
    injects real-shaped synthetic entries directly onto the engine's own
    `.entries` list (bypassing events_log.LOG entirely, same "__new__ +
    set attributes directly" technique drive_planewatch() already uses)
    -- this NEVER touches the real events_log.jsonl file, matching the
    task's own constraint. Three variants: a full page of all three real
    kinds (plane/sports/notify) to check row spacing/collision across
    every kind color, a single very-long summary (fit_text() truncation
    path), and an aircraft-shaped entry with a long age (multi-day, the
    DAYS-aware branch of FlightEngine._fmt_age_long)."""
    frames = [0]
    collisions = []
    now = time.time()
    variants = [
        [
            {"ts": now - 300, "kind": "plane", "summary": "N182UA (737-900)"},
            {"ts": now - 1800, "kind": "sports", "summary": "HOME RUN: NYY 4, BOS 2"},
            {"ts": now - 7200, "kind": "notify", "summary": "GARAGE DOOR: LEFT OPEN 20 MIN"},
        ],
        [{"ts": now - 60,
          "kind": "notify",
          "summary": "FRONT DOOR CAMERA MOTION DETECTED AT THE MAIN ENTRANCE"}],
        [{"ts": now - 400000, "kind": "plane", "summary": "N911PD (EC135)"}],
    ]
    for i, entries in enumerate(variants):
        eng = engines.EventsLogEngine.__new__(engines.EventsLogEngine)
        eng.entries = entries
        eng.page = 0
        _snap(audit, eng, "events variant %d" % i, frames, collisions)
    return frames[0], collisions


def drive_followflight(audit):
    """FollowFlightEngine (2026-08-09's follow-a-specific-flight mode)
    reads flights.FOLLOW_FEED, an ordinary background-poll feed -- but
    driving it through the generic drive() loop with no callsign
    configured would only ever exercise the NO FLIGHT SET screen, since
    this session's real environment has nothing pre-configured and a
    real live callsign can't be relied on to be airborne at audit time.
    Sets `.data` directly (the same dict shape flights.FOLLOW_FEED.get()
    returns, per its own docstring), bypassing the feed's I/O entirely --
    same "poke internals with a real-shaped payload" approach
    drive_planewatch() already established for a different force-fed
    engine. Three variants cover the tri-state design: not configured,
    configured-but-not-airborne (the honest adsb.lol empty-result case),
    and configured-and-airborne with a full real-shaped aircraft+route."""
    frames = [0]
    collisions = []
    variants = [
        {"configured": False, "callsign": None, "aircraft": None,
         "route": None, "age": None, "airborne": None, "err": None},
        {"configured": True, "callsign": "UAL123", "aircraft": None,
         "route": None, "age": 12.0, "airborne": False, "err": None},
        {"configured": True, "callsign": "UAL123", "aircraft": None,
         "route": None, "age": None, "airborne": None, "err": "HTTPError"},
        {"configured": True, "callsign": "DAL1362", "aircraft": {
            "ident": "DAL1362", "hex": "A1B2C3", "reg": "N182DN", "type": "B739",
            "callsign": "DAL1362", "category": "A3", "alt_ft": 35000,
            "gs_kt": 480, "track_deg": 270, "lat": 39.0, "lon": -95.0,
            "phase": None, "vrate_fpm": 0,
            "route": {"origin": "RDU", "dest": "LGA",
                      "origin_city": "RALEIGH/DURHAM", "dest_city": "NEW YORK",
                      "airline": "DELTA AIR LINES"}},
         "route": {"origin": "RDU", "dest": "LGA",
                   "origin_city": "RALEIGH/DURHAM", "dest_city": "NEW YORK",
                   "airline": "DELTA AIR LINES"},
         "age": 3.0, "airborne": True, "err": None},
    ]
    for i, data in enumerate(variants):
        eng = engines.FollowFlightEngine()
        eng.data = data
        _snap(audit, eng, "followflight variant %d" % i, frames, collisions)
    return frames[0], collisions


def drive_departures(audit):
    """DepartureBoardEngine (2026-08-09's departure board) reads real
    aircraft/airport state via flights.FEED/flights.load_airport() --
    with no real home airport configured (or no qualifying traffic at
    audit time), the generic drive() loop would only ever cover the two
    honest-empty states. Sets `.airport`/`.rows` directly, same "poke
    internals with a real-shaped payload" technique every other custom
    driver here uses. Three variants: no airport configured, an airport
    configured with zero qualifying traffic, and a populated multi-page
    board (2 DEPARTING + 1 ARRIVING, real-shaped route data) to exercise
    row layout/paging/collision across both status colors."""
    frames = [0]
    collisions = []
    airport = {"code": "MYR", "lat": 33.68, "lon": -78.93}
    variants = [
        (None, []),
        (airport, []),
        (airport, [
            {"status": "DEPARTING", "ac": {"reg": "N182UA", "ident": "UAL2847",
             "dist_nm": 5.0, "route": {"dest": "LGA", "dest_city": "NEW YORK"}}},
            {"status": "DEPARTING", "ac": {"reg": "N8620E", "ident": "SWA415",
             "dist_nm": 12.0, "route": {"dest": None, "dest_city": None}}},
            {"status": "ARRIVING", "ac": {"reg": "N911PD", "ident": "N911PD",
             "dist_nm": 3.0, "route": {"origin": "RDU", "origin_city": "RALEIGH/DURHAM"}}},
        ]),
    ]
    for i, (ap, rows) in enumerate(variants):
        eng = engines.DepartureBoardEngine.__new__(engines.DepartureBoardEngine)
        eng.airport = ap
        eng.rows = rows
        eng.page = 0
        _snap(audit, eng, "departures variant %d" % i, frames, collisions)
        if len(rows) > 3:
            eng.page = 1
            _snap(audit, eng, "departures variant %d page 1" % i, frames, collisions)
    return frames[0], collisions


def drive_nowplaying(audit):
    """NowPlayingEngine (2026-08-09) reads nowplaying.FEED, which is
    honestly unconfigured in this environment (no real Last.fm key) --
    the generic drive() loop would only ever cover the NO LAST.FM
    ACCOUNT SET state. Sets `.data` directly, same technique every other
    custom driver here uses. Six variants: not configured, configured
    but honestly nothing playing (with a real-shaped API error message,
    to exercise that text row too), a short track/artist/album, a
    deliberately long track name to force the marquee-vs-centered branch
    (text_w(name) > WIDTH-4), and two mic-only-equalizer variants (the
    real audio_sync.FEED is whatever this environment's actual mic state
    is -- typically stale here with no real WLED packets arriving -- so
    both mic_only+configured and mic_only+unconfigured are covered to
    exercise the header/mode dispatch regardless of which audio branch
    happens to fire)."""
    frames = [0]
    collisions = []
    variants = [
        {"configured": False, "mic_only": False, "playing": None, "track": None, "age": None, "err": None},
        {"configured": True, "mic_only": False, "playing": False, "track": None, "age": 5.0,
         "err": "INVALID API KEY - YOU MUST BE GRANTED A VALID KEY"},
        {"configured": True, "mic_only": False, "playing": True, "age": 2.0, "err": None,
         "track": {"track": "HOPPIPOLLA", "artist": "SIGUR ROS", "album": "TAKK..."}},
        {"configured": True, "mic_only": False, "playing": True, "age": 2.0, "err": None,
         "track": {"track": "A REALLY VERY EXTREMELY LONG SONG TITLE THAT WONT FIT",
                   "artist": "SOME ARTIST WITH A LONG NAME TOO", "album": None}},
        {"configured": False, "mic_only": True, "playing": None, "track": None, "age": None, "err": None},
        {"configured": True, "mic_only": True, "playing": True, "age": 2.0, "err": None,
         "track": {"track": "HOPPIPOLLA", "artist": "SIGUR ROS", "album": "TAKK..."}},
    ]
    for i, data in enumerate(variants):
        eng = engines.NowPlayingEngine.__new__(engines.NowPlayingEngine)
        eng.data = data
        eng.pulse = engines.Pulse()
        eng.scroll = 0.0
        _snap(audit, eng, "nowplaying variant %d" % i, frames, collisions)
    return frames[0], collisions


CUSTOM_DRIVERS["planewatch"] = drive_planewatch
CUSTOM_DRIVERS["notify"] = drive_notify
CUSTOM_DRIVERS["events"] = drive_events
CUSTOM_DRIVERS["followflight"] = drive_followflight
CUSTOM_DRIVERS["departures"] = drive_departures
CUSTOM_DRIVERS["nowplaying"] = drive_nowplaying


def drive(mode, audit, ticks=60, settle=0.25):
    """Tick a mode until its feed is warm, then render every state it can
    reach: each browse position, and each internal view."""
    eng = engines.ENGINES[mode]()
    frames = 0
    collisions = []

    for _ in range(ticks):
        eng.tick()
        if getattr(eng, "universal", None) or getattr(eng, "data", None):
            pass
        time.sleep(settle if _ < 12 else 0)

    def snap(label):
        nonlocal frames
        audit.reset_frame()
        try:
            eng.frame()
        except Exception as e:                    # noqa: BLE001 - report, don't abort
            print("    !! %s raised %s: %s" % (label, type(e).__name__, e))
            return
        frames += 1
        for a, b in audit.collisions():
            collisions.append((label, a, b))

    snap("default")

    # Walk every browsable position on both axes.
    n = len(getattr(eng, "universal", None) or [])
    if n:
        has_detail = hasattr(eng, "detail")
        for i in range(min(n, 60)):
            eng.ucur = i
            snap("event %d" % i)
            if has_detail:
                # select-to-expand: this state was previously UNEXERCISED
                # by this tool entirely (no snap ever set .detail), which
                # is exactly the kind of gap this tool exists to close --
                # a per-sport expanded-detail renderer could ship with a
                # real bug and a clean audit run would say nothing.
                eng.detail = eng.universal[i]["id"]
                snap("event %d detail" % i)
                eng.detail = None
        if getattr(eng, "browse_v", None) is not None:
            for i in range(len(eng._league_order())):
                eng._step_v(1)
                snap("league %d" % i)
    else:
        for i in range(8):
            if hasattr(eng, "_step"):
                try:
                    eng._step(1)
                except Exception:                 # noqa: BLE001
                    break
            eng.tick()
            snap("step %d" % i)
            # SELECT-TO-EXPAND, driven through the REAL input() dispatch --
            # not a poke at whatever internal attribute happens to hold the
            # view, at EVERY step position, not just wherever the cursor
            # happened to land last. This is the gap that let a real bug
            # through clean: the old "internal view cycles" loop below sets
            # eng.view directly, which only ever exercised the render for
            # the LAST position this loop left the cursor at -- so a
            # flights layout bug that only showed up for one specific
            # aircraft (P46T, mid-list) rendered clean here right up until
            # someone manually drove input('rotate') across every real
            # aircraft by hand. `rotate` is the select button on this
            # hardware (same convention documented in SportsEngine's
            # select-to-expand and now FlightEngine's); calling it on a
            # mode with no such concept is harmless (it becomes a no-op or
            # a cycling-pause toggle, exercised safely inside try/except
            # like every other probe here) so this runs unconditionally
            # rather than needing a per-mode allowlist.
            if hasattr(eng, "input"):
                try:
                    eng.input("rotate")
                    snap("step %d rotate" % i)
                    eng.input("drop")
                    snap("step %d rotate-back" % i)
                except Exception as e:             # noqa: BLE001 - report, don't abort
                    print("    !! step %d rotate/drop raised %s: %s"
                          % (i, type(e).__name__, e))

    # Internal view cycles (satellite/weather/sports pinned, etc).
    #
    # The count is DERIVED from the engine's own VIEW_* constants, not a
    # hardcoded 4. It was 4 for a long time, which silently stopped
    # covering any engine that grew a fifth view -- exactly the "one
    # system doesn't know about a state another just entered" bug class
    # CLAUDE.md names repeatedly, here in the audit tool itself. Falls
    # back to 4 for an engine with no VIEW_* constants at all, so nothing
    # that passed before loses coverage.
    # `VIEW_TICKS` is a DURATION, not a view id (240 on FlightEngine) --
    # excluded by name, or this would drive 240 nonexistent views.
    view_ids = [getattr(type(eng), a) for a in dir(type(eng))
                if a.startswith("VIEW_") and not a.endswith("_TICKS")
                and isinstance(getattr(type(eng), a), int)]
    n_views = (max(view_ids) + 1) if view_ids else 4
    for v in range(max(4, n_views)):
        if hasattr(eng, "view"):
            eng.view = v
            snap("view %d" % v)
        if hasattr(eng, "panels") and eng.panels:
            eng.panel_i = v % len(eng.panels)
            snap("panel %d" % v)

    # STRING-VALUED view lists (WeatherEngine's VIEWS = ["main", "hourly",
    # "radar"], 2026-08-10) -- a SECOND view-id convention this tool did
    # not know about until now, added after the int-sweep above already
    # missed it: `eng.view = v` (an int, from the loop right above) was
    # silently a no-op for a string-compared `self.view`, so the new
    # hourly/radar views got ZERO real coverage from a "clean" audit run.
    # Same class of gap this tool's own history already names (VIEW_*
    # count used to be a hardcoded 4) -- generalized here instead of
    # special-cased to weather, so any future string-VIEWS engine is
    # covered automatically too.
    views = getattr(type(eng), "VIEWS", None)
    if isinstance(views, (list, tuple)):
        for v in views:
            eng.view = v
            snap("view %r" % (v,))

    return frames, collisions


def main(argv):
    strict = "--strict" in argv
    wanted = [a for a in argv[1:] if not a.startswith("-")] or TEXT_MODES

    audit = Audit()
    audit.install()
    bad = 0
    try:
        for mode in wanted:
            if mode not in engines.ENGINES:
                print("skip %s (not a mode)" % mode)
                continue
            audit.dropped = []
            audit.overflow = []
            audit.truncated = []
            audit.clipped = []
            if mode in CUSTOM_DRIVERS:
                frames, collisions = CUSTOM_DRIVERS[mode](audit)
            else:
                frames, collisions = drive(mode, audit)

            dropped = sorted(set(audit.dropped))
            overflow = sorted({o for o in audit.overflow})
            trunc = sorted(set(audit.truncated))
            clipped = sorted(set(audit.clipped))
            marquee = mode in MARQUEE_OK

            status = "ok"
            if dropped or collisions or (overflow and not marquee) or (clipped and not marquee):
                status = "FAIL"
                bad += 1
            elif trunc and strict:
                status = "FAIL"
                bad += 1
            elif trunc or overflow or clipped:
                status = "warn"

            print("%-10s %-5s %3d frames" % (mode, status, frames))
            for s, ch in dropped[:8]:
                print("    DROPPED   %r missing %r" % (s, ch))
            if overflow:
                note = "  (marquee, expected)" if marquee else ""
                for o in overflow[:6]:
                    print("    OVERFLOW  %r at x=%s y=%s w=%s scale=%s%s"
                          % (o[0], o[1], o[2], o[3], o[4], note))
            if clipped:
                # Non-text graphics (diamonds, outs, trend arrows, arcs,
                # event-frame borders) silently losing pixels off-panel --
                # put_px drops out-of-range writes with no error. Capped
                # at 6 since a genuine layout bug clips many pixels at
                # once; the count matters more than the full list.
                note = "  (marquee, expected)" if marquee else ""
                for x, y in clipped[:6]:
                    print("    CLIPPED   pixel at x=%s y=%s (%d total)%s"
                          % (x, y, len(clipped), note))
            for label, a, b in collisions[:6]:
                print("    COLLISION %s: %r overlaps %r" % (label, a, b))
            for kind, src, out in trunc[:6]:
                print("    TRUNCATED %s: %r -> %r" % (kind, src, out))
    finally:
        audit.remove()

    print("\n%d mode(s) failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
