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
MARQUEE_OK = {"news", "ticker", "sports", "gameday", "ambient", "flights", "notify"}

# Modes worth auditing: the data modes and event modes, which are the ones
# rendering externally-sourced text. Games draw their own sprites and have
# no API strings to damage. `planewatch`/`notify` (2026-08-09) are
# force-triggered-only takeovers -- see drive_planewatch()/drive_notify()
# below for why they need dedicated drivers instead of the generic one.
TEXT_MODES = ["ticker", "satellite", "flights", "sports", "news", "weather",
              "clock", "blog", "ambient", "gameday", "planewatch", "notify"]

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


CUSTOM_DRIVERS["planewatch"] = drive_planewatch
CUSTOM_DRIVERS["notify"] = drive_notify


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
    for v in range(4):
        if hasattr(eng, "view"):
            eng.view = v
            snap("view %d" % v)
        if hasattr(eng, "panels") and eng.panels:
            eng.panel_i = v % len(eng.panels)
            snap("panel %d" % v)

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
