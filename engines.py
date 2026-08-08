"""
engines.py — headless game engines for the 64x64 Apollo M-1 arcade.

Each engine shares one interface so the arcade server can drive any of them:

    e = SnakeEngine()
    e.input("left")            # up/down/left/right/rotate/drop
    e.auto()                   # demo mode: engine steers itself
    e.tick()                   # advance one step (self-resets on game over)
    rgb = e.frame()            # WIDTH*HEIGHT*3 bytes, row-major, top-left origin
    e.tick_rate                # seconds between ticks (may change mid-game)
    e.score

Pure standard library. Coordinates: (x, y), origin top-left, y grows downward.
Polished for LED readability: pure black bg, saturated colours, simple silhouettes.
"""
import json
import math
import random
import time
from collections import deque
from pathlib import Path

import market
import satellite
import skypass
import flights
import atc
import hangar
import sports
import news
import weather
import blog
import mma
import gameday
import notify
import tic80_core
import transitions

WIDTH = 64
HEIGHT = 64

CARTS_DIR = Path(__file__).parent / "carts" / "tic80"

UP, DOWN, LEFT, RIGHT = (0, -1), (0, 1), (-1, 0), (1, 0)
DIRS = (UP, DOWN, LEFT, RIGHT)


def blank():
    return bytearray(WIDTH * HEIGHT * 3)


def put_px(buf, x, y, color):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        i = (y * WIDTH + x) * 3
        buf[i], buf[i + 1], buf[i + 2] = color


def put_cell(buf, gx, gy, cell, color, x_off=0, y_off=0):
    for dy in range(cell):
        for dx in range(cell):
            put_px(buf, x_off + gx * cell + dx, y_off + gy * cell + dy, color)


def fill(buf, color):
    r, g, b = color
    for i in range(0, len(buf), 3):
        buf[i], buf[i + 1], buf[i + 2] = r, g, b


def lerp_color(a, b, t):
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _add3(a, b, c):
    """Named function beats a lambda inside map() in the Life inner loop."""
    return a + b + c


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# Unit conversion, done here at the render layer on purpose -- feed
# modules (satellite.py, weather.py) keep reporting whatever unit their
# real upstream API actually returns (verified live, not assumed) so the
# I/O layer stays a faithful mirror of the data source. Converting for
# DISPLAY is a rendering concern, same as picking a color or a font
# scale, so it lives with every other rendering decision instead of
# leaking a "display preference" into the data layer.
def km_to_mi(km):
    return km * 0.621371


def kmh_to_mph(kmh):
    return kmh * 0.621371


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def kt_to_mph(kt):
    return kt * 1.15078


def nm_to_mi(nm):
    """Nautical miles -> statute miles. ADS-B reports distance in nautical
    miles, which IS an imperial-family unit but not the one someone means
    by 'how far away is that plane' -- a statute mile is."""
    return nm * 1.15078


def rim(color, k=0.35):
    """Darker rim of a neon colour — outlines that survive busy backgrounds."""
    return (max(0, int(color[0] * k)), max(0, int(color[1] * k)),
            max(0, int(color[2] * k)))


def put_blob(buf, x, y, color, outline=True):
    """1px core + optional dark rim for readability on dynamic FX."""
    if outline:
        o = rim(color, 0.25)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            put_px(buf, x + dx, y + dy, o)
    put_px(buf, x, y, color)


def draw_line(buf, x0, y0, x1, y1, color):
    """Integer Bresenham line -- used for procedurally-rotated icons (e.g.
    the flight tracker's heading-oriented plane glyph) where a fixed sprite
    table would need one entry per angle."""
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        put_px(buf, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


# Neon palette notes for ambient self-play over WLED backgrounds:
#  * Game BG must stay pure black so backgrounds composite through.
#  * Sprites use high-sat complementary neons (not muddy mid-greens).
#  * Avoid dim greys that disappear into Fire / Matrix / Plasma.


# 3×5 digit glyphs for 2048 (and any future score labels). 1 = on.
_FONT3x5 = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "K": ("101", "110", "100", "110", "101"),  # 1024+ shorthand optional
    # A-Z: added for the system menu (game names, control hints). The set
    # above only ever needed digits + "K", so any word came out as near-blank
    # -- draw_text3x5 silently skips unmapped characters and just advances
    # the cursor, which is correct for a deliberate space but was hiding this
    # gap for every letter.
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "111", "100", "111"),
    "F": ("111", "100", "111", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "101", "101", "101"),
    "N": ("101", "110", "101", "101", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "111", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "010"),
    "V": ("101", "101", "101", "010", "010"),
    "W": ("101", "101", "101", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    " ": ("000", "000", "000", "000", "000"),
    # Punctuation. draw_text3x5 silently SKIPS any glyph it does not know, so
    # a missing "." did not error -- it quietly dropped the decimal point and
    # rendered 75.84 as "75 84", which reads as 7584. Numeric modes need these.
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    ":": ("000", "010", "000", "010", "000"),
    "+": ("000", "010", "111", "010", "000"),
    "-": ("000", "000", "111", "000", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "%": ("101", "001", "010", "100", "101"),
    # Ampersand: ESPN's NFL down-and-distance text is literally "3RD & 7",
    # and a missing glyph is SILENTLY DROPPED by draw_text3x5, so without
    # this the sports mode renders "3RD  7" -- the seventh time this font's
    # silent-drop behaviour has produced a real visible bug here. Adding
    # the glyph is the right fix: the data is correct, the font was short.
    "&": ("110", "110", "011", "101", "011"),
    # Parentheses: tennis set scores carry tiebreaks as "7-6(7-5)". Without
    # these the brackets are silently dropped and it renders "7-67-5" -- a
    # DIFFERENT, wrong-looking score. Eighth instance of this font's
    # silent-drop behaviour causing a real bug.
    "(": ("010", "100", "100", "100", "010"),
    ")": ("010", "001", "001", "001", "010"),
    "$": ("011", "110", "010", "011", "110"),
    "!": ("010", "010", "010", "000", "010"),
    "?": ("110", "001", "010", "000", "010"),
    "'": ("010", "010", "000", "000", "000"),
    ">": ("100", "010", "001", "010", "100"),
    "<": ("001", "010", "100", "010", "001"),
    # "@" is load-bearing, not decoration: the sports tape renders
    # "AWAY 3 @ HOME 5", and without a glyph draw_text3x5 silently drops
    # it, leaving "AWAY 3  HOME 5" -- which loses the only thing telling
    # you which team is home. Found by instrumenting draw_text3x5 to log
    # every character the font lacks, rather than by reading the string.
    "@": ("111", "101", "111", "100", "011"),
}


def draw_text3x5(buf, x, y, text, color, scale=1):
    """Draw left-to-right 3×5 text. Returns width used."""
    cx = x
    for ch in text:
        g = _FONT3x5.get(ch)
        if not g:
            cx += 2 * scale
            continue
        for row, bits in enumerate(g):
            for col, bit in enumerate(bits):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            put_px(buf, cx + col * scale + sx, y + row * scale + sy, color)
        cx += (3 + 1) * scale  # glyph + 1px gap
    return cx - x


# =============================================================================
# Shared visual vocabulary for the DATA modes (ticker/ISS/flights/sports/news
# /weather). These exist so the six modes read as one designed product rather
# than six independently-styled text dumps -- the header treatment, the value
# hierarchy and the chrome are identical everywhere, and only the accent
# colour changes per mode. Games deliberately do NOT use these; they have
# their own full-bleed visual language.
# =============================================================================
def text_w(s, scale=1):
    """Rendered pixel width of a string -- the ONE place that knows the
    glyph pitch. Hand-inlined `4*len(s)-1` math was duplicated ~40 times
    and is exactly how a scale=2 string silently overflowed before."""
    return (4 * len(s) - 1) * scale


def draw_text_centered(buf, y, s, color, scale=1, x_min=2):
    """Horizontally-centred text, clamped so it can never start off-screen."""
    return draw_text3x5(buf, max(x_min, (WIDTH - text_w(s, scale)) // 2), y, s, color, scale=scale)


def fit_text(s, max_px, scale=1):
    """Trim to fit real pixels, dropping whole trailing words first so a
    cut string reads as abbreviated rather than broken."""
    while s and text_w(s, scale) > max_px and " " in s:
        s = s.rsplit(" ", 1)[0]
    while s and text_w(s, scale) > max_px:
        s = s[:-1]
    return s


def fit_person(name, max_px, scale=1):
    """Fit a PERSON's name, preferring the surname over the initial.

    fit_text drops whole trailing words first, which is right for a
    headline and exactly wrong for a name: "T. POSTARNAKOVA" became "T.",
    throwing away the identity and keeping the least informative part.
    Here, if the full name will not fit, the surname alone is tried before
    anything is cut -- "POSTARNAKOVA" is still unambiguous, "T." is not.
    """
    name = str(name or "").strip()
    if not name or text_w(name, scale) <= max_px:
        return name
    parts = name.split()
    if len(parts) > 1:
        surname = parts[-1]
        if text_w(surname, scale) <= max_px:
            return surname
        name = surname
    # Still too wide: cut characters, never whole words.
    while name and text_w(name, scale) > max_px:
        name = name[:-1]
    return name


def wrap_text(text, max_px, max_lines=None, scale=1):
    """Word-wrap into lines that each fit max_px, dropping whole trailing
    words at the end if max_lines truncates -- same "abbreviate at a word
    boundary, never mid-glyph" discipline as fit_text(), just across
    multiple lines instead of one.

    Extracted from draw_alert_frame()'s original inline wrap (which used
    this exact loop for severe-weather event names) so the ATC log view
    can reuse it rather than re-deriving the same logic a second time."""
    words = str(text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if text_w(trial, scale) <= max_px:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if max_lines is not None:
        lines = lines[:max_lines]
    return lines


def draw_header(buf, title, accent, right_tag=None, stale=False, icon=None):
    """Standard top chrome for every data mode: a full-width accent rule,
    the mode/source title on it, and an optional right-aligned tag.

    A solid coloured rule (rather than only coloured text) is what makes
    each mode identifiable from across a room before any text is legible
    -- at 64px the title itself is only readable up close, but a band of
    colour reads instantly.

    `icon` is an optional draw_icon_*(buf, x, y, color) callable (see
    SPORT_ICONS) -- when present it draws a small 5px glyph to the left
    of the title and the title's own budget shrinks to make room, so a
    sport is identifiable by shape as well as by its accent-colour rule,
    the same "which of these am I looking at" role the flight mode's
    aircraft-type icon plays there."""
    for x in range(WIDTH):
        put_px(buf, x, 0, accent)
        put_px(buf, x, 1, rim(accent, 0.45))
    # Right edge is laid out FIRST -- stale pip, then tag inside it --
    # and the title gets whatever room is genuinely left. A fixed title
    # budget (WIDTH-22) collided with any tag wider than a "1/9" counter
    # (e.g. weather's "FL 86F"), and the pip and tag both claimed the same
    # far-right corner, so both reserves have to be measured, not assumed.
    right = WIDTH - 2
    if stale:
        for dx in range(2):
            for dy in range(2):
                put_px(buf, right - 2 + dx, 3 + dy, (255, 170, 40))
        right -= 5
    tag = fit_text(right_tag, 30) if right_tag else ""
    if tag:
        draw_text3x5(buf, right - text_w(tag), 3, tag, (110, 118, 140))
        right -= text_w(tag) + 3
    left = 2
    if icon is not None:
        icon(buf, left, 3, color_on_dark(accent))
        left += 6
    draw_text3x5(buf, left, 3, fit_text(title, right - left), color_on_dark(accent))


def color_on_dark(c, floor=140):
    """Lift a colour to stay legible as text on the near-black body."""
    m = max(c)
    if m >= floor:
        return c
    k = floor / max(1, m)
    return (min(255, int(c[0] * k)), min(255, int(c[1] * k)), min(255, int(c[2] * k)))


def draw_divider(buf, y, color=(26, 30, 40), inset=2):
    for x in range(inset, WIDTH - inset):
        put_px(buf, x, y, color)


class Scroller:
    """Shared browse control for ANY mode with a sequence of items.

    Every competitor in this category is push-only: you watch what it
    decides to show, on its timing. This is the piece that lets someone
    actually browse, and it is deliberately ONE control scheme shared by
    every mode rather than a different gesture per mode.

    The interaction is the standard key-repeat model everybody already
    knows from holding an arrow key in a text field, so it needs no
    instructions:

        tap left/right   -> step exactly one item, immediately
        hold left/right  -> step once, brief delay, then repeat with
                            ACCELERATION so a long hold skims fast
        release          -> stop, then auto-advance stays paused for a
                            few seconds so the timer doesn't immediately
                            yank the screen away from where you browsed to

    Wiring: the phone remote and control page already send
    press/release (see bindHold in remote.html), so this needs no new
    hardware and no new gesture -- it maps onto the controls that exist.

    IMPORTANT interaction with the existing client: bindHold ALSO fires a
    repeating input() every ~110ms while held. If that were honoured on
    top of this class's own repeat, a hold would double-step. So input()
    is ignored while a press is active (see `tap`), and the server-side
    tick clock is the single source of repeat timing -- which also makes
    the acceleration curve independent of client repeat rate.
    """

    __slots__ = ("dir", "held", "hold_t", "since_repeat", "pause_t",
                 "delay", "fast_after", "slow_every", "fast_every", "pause")

    def __init__(self, tick_rate=0.05):
        per_s = max(1, int(round(1.0 / max(0.001, tick_rate))))
        self.delay = max(2, int(per_s * 0.35))        # ~0.35s before repeat starts
        self.fast_after = max(1, int(per_s * 1.2))    # ~1.2s held -> full speed
        self.slow_every = max(1, int(per_s * 0.28))   # initial repeat ~3.5/s
        self.fast_every = 1                            # accelerated: one per tick
        self.pause = max(1, int(per_s * 4.0))         # ~4s of manual control after input
        self.dir = 1
        self.held = False
        self.hold_t = 0
        self.since_repeat = 0
        self.pause_t = 0

    # ---- input ---------------------------------------------------------
    def press(self, direction):
        """Held down. Returns steps to apply right now (always 1: the
        first step must feel instant, not wait for the repeat delay)."""
        self.dir = direction
        self.held = True
        self.hold_t = 0
        self.since_repeat = 0
        self.pause_t = self.pause
        return 1

    def release(self):
        self.held = False
        self.hold_t = 0
        self.pause_t = self.pause      # restart the pause from the moment of release

    def tap(self, direction):
        """A plain input() with no press/release around it. Ignored while
        a press is active so the client's own repeat can't double-step."""
        if self.held:
            return 0
        self.dir = direction
        self.pause_t = self.pause
        return 1

    # ---- per-tick ------------------------------------------------------
    def tick(self):
        """Steps to apply this tick from an ongoing hold. Call once per
        engine tick, before the auto-advance check."""
        if self.pause_t > 0:
            self.pause_t -= 1
        if not self.held:
            return 0
        self.hold_t += 1
        if self.hold_t < self.delay:
            return 0
        # Accelerate: ease the repeat interval from slow to fast over the
        # hold, so a short hold nudges and a long hold genuinely skims.
        k = min(1.0, (self.hold_t - self.delay) / float(max(1, self.fast_after)))
        every = max(self.fast_every,
                    int(round(self.slow_every + (self.fast_every - self.slow_every) * k)))
        self.since_repeat += 1
        if self.since_repeat >= every:
            self.since_repeat = 0
            return 1
        return 0

    @property
    def auto_ok(self):
        """Whether the mode's own auto-advance timer may run. False while
        held and for `pause` ticks after the last manual input."""
        return not self.held and self.pause_t <= 0


class Browsable:
    """Mixin that gives an engine the shared browse control.

    An engine opts in by calling _init_scroll() in reset(), implementing
    _step(direction), calling _scroll_tick() at the top of tick(), and
    gating its own auto-advance on `self.browse.auto_ok`. That is the
    whole contract -- any future sequence mode should follow it so the
    control scheme stays identical system-wide.
    """

    # Set True by an engine that also wants UP/DOWN as a second browse
    # axis (see SportsEngine: left/right walks games, up/down walks
    # leagues). Both axes get the identical tap/hold/accelerate feel from
    # the same Scroller class, so the control scheme stays one system
    # rather than two.
    VERTICAL_BROWSE = False

    def _init_scroll(self):
        # NOT named `scroll`: NewsEngine/TickerEngine already use
        # self.scroll as a marquee pixel offset (a float). Colliding
        # with that made press() call .press on a float.
        self.browse = Scroller(getattr(self, "tick_rate", 0.05))
        self.browse_v = Scroller(getattr(self, "tick_rate", 0.05)) \
            if self.VERTICAL_BROWSE else None

    def _step(self, direction):                      # pragma: no cover - overridden
        raise NotImplementedError

    def _step_v(self, direction):                    # pragma: no cover - overridden
        raise NotImplementedError

    def _axis(self, cmd):
        """(scroller, step_fn, direction) for a d-pad command, or None."""
        if cmd in ("left", "right"):
            return self.browse, self._step, (-1 if cmd == "left" else 1)
        if self.browse_v is not None and cmd in ("up", "down"):
            return self.browse_v, self._step_v, (-1 if cmd == "up" else 1)
        return None

    # Held-button entry points. arcade_server routes press/release here
    # when an engine defines them (see send_press/send_release).
    def press(self, cmd):
        ax = self._axis(cmd)
        if ax is None:
            return self.input(cmd)
        scroller, step, d = ax
        for _ in range(scroller.press(d)):
            step(d)

    def release(self, cmd):
        ax = self._axis(cmd)
        if ax is not None:
            ax[0].release()

    def _browse_input(self, cmd):
        """Handle a browse command from a plain input(). Returns True if it
        was one and has been dealt with."""
        ax = self._axis(cmd)
        if ax is None:
            return False
        scroller, step, d = ax
        for _ in range(scroller.tap(d)):
            step(d)
        return True

    def _scroll_tick(self):
        for _ in range(self.browse.tick()):
            self._step(self.browse.dir)
        if self.browse_v is not None:
            for _ in range(self.browse_v.tick()):
                self._step_v(self.browse_v.dir)

    @property
    def _browse_auto_ok(self):
        """Auto-advance may run only when NEITHER axis is being driven."""
        return self.browse.auto_ok and (
            self.browse_v is None or self.browse_v.auto_ok)


# Where GAME DAY hands the panel back to when its event ends. Assigned
# from arcade_server.DEFAULT_MODE at import so the two can never drift.
RESTING_MODE = "clock"

GAMEDAY_ACCENT = (235, 45, 65)      # fight-night crimson
GAMEDAY_GOLD = (255, 200, 70)       # the "occasion" second colour

BASE_ON = (255, 226, 60)
BASE_OFF = (46, 50, 62)
OUT_ON = (255, 90, 80)
OUT_OFF = (46, 50, 62)

# Regulation periods per league. Real structural fact about each sport,
# not data ESPN provides -- the scoreboard payload carries the CURRENT
# period but never how many there are. Used to judge how late a game is.
REGULATION_PERIODS = {"NFL": 4, "NBA": 4, "MLB": 9, "NHL": 3,
                      "EPL": 2, "NCAAF": 4, "NCAAB": 2}


def draw_diamond(buf, x, y, bases, on_col=BASE_ON, off_col=BASE_OFF):
    """Baseball diamond with occupied bases filled -- the single most
    information-dense glyph available here. Four pixels say what "runner
    on first and third" needs a whole sentence to say, and it reads
    instantly to anyone who has watched a game.

    Base order from ESPN is [onFirst, onSecond, onThird]; drawn in the
    real diamond orientation (1st right, 2nd top, 3rd left) rather than
    left-to-right, because a diamond drawn wrong is worse than none."""
    on = bases or [False, False, False]
    pts = ((x + 3, y + 3), (x + 3, y), (x, y + 3))     # 1st, 2nd, 3rd
    for i, (px, py) in enumerate(pts):
        c = on_col if on[i] else off_col
        put_px(buf, px, py, c)
        put_px(buf, px + 1, py, c)
        put_px(buf, px, py + 1, c)
        put_px(buf, px + 1, py + 1, c)


def draw_outs(buf, x, y, outs, on_col=OUT_ON, off_col=OUT_OFF):
    """Outs as filled/hollow pips, the way a real scoreboard shows them."""
    for i in range(3):
        c = on_col if (outs or 0) > i else off_col
        put_px(buf, x + i * 3, y, c)
        put_px(buf, x + i * 3 + 1, y, c)


# =============================================================================
# PER-SPORT IDENTITY ICONS -- baseball already had draw_diamond()/draw_outs()
# above; this is the same idea (a few put_px, a fixed relative offset table,
# no general-purpose shape algorithm) extended to every other sport this
# project covers. ONE shape per sport, reused in TWO contexts (a tiny header
# glyph and a bigger celebration accent), exactly the "one shape language,
# multiple scaled contexts" pattern the flight icons already established.
#
# HARD RULE: every offset table below is simple original geometry (line
# segments / dot clusters describing the SPORT generically -- a ball, a
# flag, a puck), never a real league's actual mark. If a shape reads as a
# specific real trademark, it needs to be simplified further, not shipped.
#
# Each function draws into a small box anchored at (x, y) and takes a
# `scale` (drawn by multiplying offsets, matching the "reuse the same
# geometric definition, scaled differently" precedent from the aircraft
# sprites) -- no interpolation, just sparser ink at larger scale, which is
# fine for an accent glyph, not a hero image.
def _draw_offsets(buf, x, y, offsets, color, scale=1):
    for dx, dy in offsets:
        px, py = x + dx * scale, y + dy * scale
        put_px(buf, px, py, color)
        if scale > 1:
            # Fill the scale x scale cell so a bigger icon isn't just the
            # same sparse dots spread further apart.
            for fx in range(scale):
                for fy in range(scale):
                    put_px(buf, px + fx, py + fy, color)


def draw_icon_football(buf, x, y, color, scale=1):
    """Oval/lentil + a center seam line -- generic football silhouette,
    not any team's ball art."""
    outline = ((1, 0), (2, 0), (3, 0), (0, 1), (4, 1), (0, 2), (4, 2),
               (1, 3), (2, 3), (3, 3))
    _draw_offsets(buf, x, y, outline, color, scale)
    _draw_offsets(buf, x, y, ((2, 1), (2, 2)), color, scale)


def draw_icon_basketball(buf, x, y, color, scale=1):
    """Circle outline + a cross seam -- a generic ball, not a real league
    mark."""
    outline = ((1, 0), (2, 0), (3, 0), (0, 1), (4, 1), (0, 2), (4, 2),
               (0, 3), (4, 3), (1, 4), (2, 4), (3, 4))
    _draw_offsets(buf, x, y, outline, color, scale)
    _draw_offsets(buf, x, y, ((2, 1), (2, 2), (2, 3)), color, scale)


def draw_icon_hockey(buf, x, y, color, scale=1):
    """A flat puck (2px-tall rectangle) plus a short angled stick line --
    pure geometric silhouette."""
    puck = ((0, 2), (1, 2), (2, 2), (3, 2), (0, 3), (1, 3), (2, 3), (3, 3))
    stick = ((4, 0), (3, 1))
    _draw_offsets(buf, x, y, puck, color, scale)
    _draw_offsets(buf, x, y, stick, color, scale)


def draw_icon_soccer(buf, x, y, color, scale=1):
    """Ball outline with an internal dot suggesting a panel seam --
    deliberately simpler than the basketball glyph so the two don't read
    the same at a glance."""
    outline = ((1, 0), (2, 0), (3, 0), (0, 1), (4, 1), (0, 2), (4, 2),
               (0, 3), (4, 3), (1, 4), (2, 4), (3, 4))
    _draw_offsets(buf, x, y, outline, color, scale)
    _draw_offsets(buf, x, y, ((2, 2),), color, scale)


def draw_icon_golf(buf, x, y, color, scale=1):
    """A flag on a pin -- vertical line plus a small triangular flag at
    the top."""
    pin = ((0, 0), (0, 1), (0, 2), (0, 3), (0, 4))
    flag = ((1, 0), (2, 0), (1, 1))
    _draw_offsets(buf, x, y, pin, color, scale)
    _draw_offsets(buf, x, y, flag, color, scale)


def draw_icon_mma(buf, x, y, color, scale=1):
    """An abstract rounded-mitt silhouette -- a glove shape reduced to its
    simplest geometric blob, not a detailed rendering."""
    mitt = ((1, 0), (2, 0), (0, 1), (1, 1), (2, 1), (3, 1),
            (0, 2), (1, 2), (2, 2), (3, 2), (1, 3), (2, 3))
    _draw_offsets(buf, x, y, mitt, color, scale)


def draw_icon_tennis(buf, x, y, color, scale=1):
    """A ball outline with a curved seam suggested by two offset dots --
    deliberately distinct from the soccer ball glyph's single centred
    dot/straight seam, so the two don't read the same at a glance."""
    outline = ((1, 0), (2, 0), (3, 0), (0, 1), (4, 1), (0, 2), (4, 2),
               (0, 3), (4, 3), (1, 4), (2, 4), (3, 4))
    _draw_offsets(buf, x, y, outline, color, scale)
    _draw_offsets(buf, x, y, ((1, 1), (3, 3)), color, scale)


# Dispatch by SportsEngine's own normalized `sport` key (see SPORT_ACCENT).
# Baseball is deliberately absent -- draw_diamond()/draw_outs() are its
# existing, already-shipped glyph and are reused as-is (see
# _backdrop_sports below), not redefined here. Tennis was skipped here
# until task #19 (the tennis MAIN renderer) shipped -- now that a real
# renderer exists to attach an icon to, it is included.
SPORT_ICONS = {
    "football": draw_icon_football,
    "basketball": draw_icon_basketball,
    "hockey": draw_icon_hockey,
    "soccer": draw_icon_soccer,
    "golf": draw_icon_golf,
    "mma": draw_icon_mma,
    "tennis": draw_icon_tennis,
}

# Same icons, keyed instead by the OLDER per-league LEAGUE_PATHS code
# (sports.LEAGUE_PATHS: NFL/NBA/MLB/NHL/EPL/NCAAF/NCAAB) -- the pinned-
# favorite and per-league-ticker views predate the universal `sport` key
# and still key off the raw league string. MLB is deliberately absent:
# that view already carries baseball's own diamond/outs glyph in the
# body, so a second identity mark in the header would be redundant.
LEAGUE_ICON = {
    "NFL": draw_icon_football, "NCAAF": draw_icon_football,
    "NBA": draw_icon_basketball, "NCAAB": draw_icon_basketball,
    "NHL": draw_icon_hockey,
    "EPL": draw_icon_soccer,
}


def draw_trend_arrow(buf, x, y, up, color):
    """A tiny triangle: up or down. The font has no arrow glyph, and
    spelling out a word costs far more than 3px of a 64px row buys back.
    First built for baseball's half-inning indicator; flights reuses it
    for climb/descend so the two look like one system's convention rather
    than two modes inventing their own arrows."""
    if up:
        rows = ((2, 1), (1, 3), (0, 5))
    else:
        rows = ((0, 5), (1, 3), (2, 1))
    for dy, (dx, w) in enumerate(rows):
        for i in range(w):
            put_px(buf, x + dx + i, y + dy, color)


def situation_line(g):
    """Compact live-state string, or "" -- NOT a fabricated one.

    Prefers ESPN's own down-and-distance text when present (NFL);
    otherwise falls back to the count, which is verified real for MLB.
    Bases and outs are drawn as glyphs instead of words because at 64px a
    diamond costs 16px and the phrase does not fit at all."""
    sit = g.get("situation") or {}
    if sit.get("down_distance"):
        return sit["down_distance"]
    b, k = sit.get("balls"), sit.get("strikes")
    if isinstance(b, int) and isinstance(k, int):
        return f"{b}-{k}"
    return ""


class Pulse:
    """Shared "something just changed" flash.

    Sports had a bespoke scoring flash first and it set the quality bar;
    this generalises exactly that behaviour so every mode marks new
    content the same way instead of silently swapping a value. One
    definition means the flash rate and feel cannot drift between modes.

    Usage: call note() every tick with a value that identifies the current
    content (a score, a headline, a post id, a rounded temperature). The
    FIRST value seen never flashes -- otherwise every mode would flash on
    arrival, which trains you to ignore it.
    """

    __slots__ = ("ticks", "t", "_key")

    def __init__(self, ticks=14):
        self.ticks = ticks
        self.t = 0
        self._key = None

    def note(self, key):
        if self._key is not None and key != self._key and key is not None:
            self.t = self.ticks
        self._key = key
        if self.t > 0:
            self.t -= 1

    @property
    def on(self):
        """Blink rather than a solid hold -- a static highlight reads as a
        colour choice, a blinking one reads as an event."""
        return self.t > 0 and (self.t // 2) % 2 == 0

    def mix(self, color, flash=(255, 255, 255)):
        return flash if self.on else color


def draw_dots(buf, y, n, cur, on=(150, 160, 185), off=(38, 42, 54), cap=10):
    """Position indicator shared by every rotating mode."""
    n = min(n, cap)
    if n <= 1:
        return
    w = n * 3 - 1
    x0 = (WIDTH - w) // 2
    for i in range(n):
        put_px(buf, x0 + i * 3, y, on if i == (cur % n) else off)


# NWS severity -> alert styling. Module level because the SAME styling is
# used two ways: inside the weather mode's own rotation, and as a global
# takeover over any other mode (see draw_alert_frame). One definition, so
# the two can't drift apart visually.
ALERT_SEVERITY_COLOR = {
    "Extreme": (255, 45, 45), "Severe": (255, 80, 40),
    "Moderate": (255, 170, 40), "Minor": (240, 200, 70),
    "Unknown": (200, 200, 200),
}

# Only these preempt other modes globally. A routine coastal-flood
# advisory interrupting a game would train someone to ignore the panel
# exactly when it finally matters -- the whole point of the takeover is
# that it's rare enough to still mean something. Weather mode itself
# still shows every alert regardless of severity.
GLOBAL_ALERT_SEVERITIES = ("Extreme", "Severe")


def draw_alert_frame(alert, ticks, place="", n_alerts=1, cur_alert=0):
    """Full-screen severe-weather alert. Used by WeatherEngine and by the
    global takeover in arcade_server, so both look identical."""
    buf = blank()
    fill(buf, (0, 0, 0))
    col = ALERT_SEVERITY_COLOR.get(alert.get("severity"), ALERT_SEVERITY_COLOR["Unknown"])

    # Pulse: a wall panel has to earn attention from someone who isn't
    # already looking at it, and motion does that where a static red
    # screen does not.
    k = 0.55 + 0.45 * abs(math.sin(ticks * 0.09))
    pulse = tuple(min(255, int(c * k)) for c in col)

    for x in range(WIDTH):
        for y in (0, 1, 2):
            put_px(buf, x, y, pulse)
        for y in (HEIGHT - 3, HEIGHT - 2, HEIGHT - 1):
            put_px(buf, x, y, pulse)
    for y in range(HEIGHT):
        for x in (0, 1):
            put_px(buf, x, y, pulse)
        for x in (WIDTH - 2, WIDTH - 1):
            put_px(buf, x, y, pulse)

    draw_text_centered(buf, 6, fit_text(str(alert.get("severity", "")).upper(), WIDTH - 10), pulse)

    # The event name is the thing that matters ("TORNADO WARNING").
    # Wrapped across up to three lines rather than truncated -- this is
    # the one view where cutting the message off could actually matter.
    lines = wrap_text(alert.get("event", ""), WIDTH - 10, max_lines=3)
    # Vertically centre the wrapped block between the severity label and
    # the footer, so a 1-line event doesn't sit in the top third with dead
    # space under it and a 3-line one still fits.
    band_top, band_bot = 15, HEIGHT - 12
    block_h = len(lines) * 8 - 2
    y0 = band_top + max(0, ((band_bot - band_top) - block_h) // 2)
    for i, ln in enumerate(lines):
        draw_text_centered(buf, y0 + i * 8, ln, (255, 255, 255))

    if n_alerts > 1:
        draw_dots(buf, HEIGHT - 6, n_alerts, cur_alert, on=pulse)
    elif place:
        draw_text_centered(buf, HEIGHT - 9, fit_text(place, WIDTH - 10), rim(col, 0.8))
    return bytes(buf)


# =============================================================================
# HOME ASSISTANT NOTIFY -- normal-priority banner (task #8, 2026-08-08).
#
# Deliberately modeled on _severe_alert_frame()'s COMPOSITE-not-mode-swap
# pattern (drawn over whatever the current mode already rendered, applied
# post-render, never touches self.mode or input) but visually the OPPOSITE
# of severe weather's full-bleed 4-edge pulse: a single quiet band pinned
# to the bottom third, everything above it untouched. Severe weather has
# to be unmissable because it can be life-safety; a garage-left-open
# notice is an FYI and should read as one -- reusing the pulse treatment
# for both would train someone to tune out the panel exactly like
# CLAUDE.md's own reasoning for restricting severe takeover to
# Extreme/Severe in the first place.
#
# Bottom 20px (HEIGHT-20..HEIGHT-1, ~31% of the panel) is enough for a
# title row + up to two message rows while leaving the top two-thirds of
# whatever's underneath fully legible -- "subordinate to whatever's
# showing", not a second full-screen takeover.
# =============================================================================
NOTIFY_NORMAL_COLOR = (80, 190, 255)   # cool blue -- reads "FYI", not "SEVERE"
NOTIFY_URGENT_COLOR = (255, 170, 40)   # warm amber -- distinct from both the
                                        # cool normal banner and severe weather's reds


def draw_notify_banner(frame, title, message, color, scroll=0.0):
    """Composite a bottom-third notify banner over an already-rendered
    frame. `title`/`message` must already be paneltext.panel_text()-folded
    by the caller (arcade_server.py's /api/notify handler), same rule as
    every other externally-sourced string reaching this module.

    Returns a NEW bytes object (mirrors draw_alert_frame()'s contract) --
    `frame` itself is never mutated, so a caller holding a reference to
    the pre-banner frame elsewhere is unaffected."""
    buf = bytearray(frame)
    y0 = HEIGHT - 20   # 44 -- bottom third
    bg = (0, 6, 16)
    for y in range(y0, HEIGHT):
        for x in range(WIDTH):
            put_px(buf, x, y, bg)
    for x in range(WIDTH):           # thin accent divider, not a pulse
        put_px(buf, x, y0, color)

    draw_text_centered(buf, y0 + 3, fit_text(str(title or ""), WIDTH - 6), color)

    msg = str(message or "")
    lines = wrap_text(msg, WIDTH - 6, max_lines=2)
    # If wrap_text had to drop trailing words to fit 2 lines, the folded
    # message doesn't fit the banner's budget -- overflow uses the
    # project's existing answer for long text (draw_marquee, the same
    # scrolling tape news/ticker/gameday/flights already use) instead of
    # inventing a third truncation style.
    overflow = sum(len(ln.split()) for ln in lines) < len(msg.split())
    if overflow:
        draw_marquee(buf, HEIGHT - 8, msg, (220, 230, 255), scroll)
    elif len(lines) == 1:
        draw_text_centered(buf, HEIGHT - 8, lines[0], (220, 230, 255))
    elif lines:
        # Two real rows: HEIGHT-11 and HEIGHT-5 -- HEIGHT-5=59 is the real
        # last-legal row for a 5px glyph (the exact off-by-one that
        # clipped PlaneWatchEngine's distance/altitude row once already;
        # not repeating it here).
        draw_text_centered(buf, HEIGHT - 11, lines[0], (220, 230, 255))
        draw_text_centered(buf, HEIGHT - 5, lines[1], (220, 230, 255))
    return bytes(buf)


# =============================================================================
# BIG-MOMENT CELEBRATION -- a full-panel graphic for a sports mode to fire
# when something genuinely notable just happened (home run, goal, finish,
# buzzer-beater...). Lives here, not in sports.py/mma.py, because it is
# SHARED across every sport that can trigger it -- one graphic, one feel,
# same reasoning as draw_alert_frame being one severe-weather treatment
# shared by WeatherEngine and the global takeover rather than each mode
# inventing its own.
#
# CONTRACT: any engine that can fire a celebration implements
# `pop_big_moment()`, returning either None or a dict:
#   {"kind": str, "line1": str, "line2": str, "color": (r,g,b)}
# `kind`/`line1`/`line2` must already be paneltext.panel_text()-folded by
# the producing engine -- this module draws them as-is, same rule as every
# other externally-sourced string in the project. `color` is the accent to
# burst with; pass the team/fighter's real color when one exists (same
# "real hue, not invented" rule as _hex_to_rgb's brightness floor), or a
# neutral warm gold when there isn't one (golf, MMA).
#
# `pop_big_moment()` is a POP, not a peek: calling it consumes the moment,
# so a caller that polls every tick fires the celebration exactly once per
# real event, the same one-shot idiom Pulse uses for "never flash on the
# first value seen" (here: never re-fire on every subsequent read).
#
# ORIGINAL DESIGN, DELIBERATELY NOT A BROADCAST RECREATION. No league
# logo, no referee signal, no copied graphic package -- an expanding
# radial burst + rotating sunburst rays + an impact-frame white flash on
# the first few ticks, which reads as "something exciting happened"
# without reproducing anything trademarked. Reuses Pulse's blink-not-hold
# philosophy (motion reads as an event, a static color reads as a choice)
# scaled up to fill the whole panel, closer to GAME DAY's RESULT view
# drama than to sports' routine scoring flash.
# =============================================================================
CELEBRATION_TICKS = 90     # ~4.5s at ambient's 0.05s tick rate -- TIER_INTERRUPT's hold
_CX, _CY = WIDTH // 2, WIDTH // 2 - 2   # burst center, nudged up for text room
# Largest ring/sweep radius that keeps EVERY drawn point inside the real
# 0..63 panel from this center -- REAL BUG, found only once render_audit's
# instrumented put_px was actually run against draw_celebration() for the
# first time (nothing in the normal audit sweep had ever forced a
# celebration to fire before, so this shipped and stayed invisible): the
# original ring radius formula reached ~37px from a centre at (32,30),
# well past the edge in every direction. min(_CX, WIDTH-1-_CX, _CY,
# HEIGHT-1-_CY) is the true safe bound; -2 for margin against rounding.
_MAX_BURST_R = min(_CX, WIDTH - 1 - _CX, _CY, HEIGHT - 1 - _CY) - 2

# ---- INTENSITY TIERS -------------------------------------------------------
# THREE tiers, defined by WHAT THE DEVICE DOES, not by adjectives. Three and
# not four on purpose: a fourth tier invites everything to settle into the
# middle, and the whole point is that the top tier stays rare.
#
#   TIER_FLASH     -- does NOT interrupt ambient at all. A short banner drawn
#                     INSIDE the owning mode's own frame, so it is only seen
#                     if you already happen to be looking at that mode.
#   TIER_INTERRUPT -- full-panel celebration. EXACTLY the behaviour every
#                     sports detector already had, unchanged, so nothing
#                     regresses.
#   TIER_TAKEOVER  -- full-panel, longer, and the ONLY tier allowed to
#                     pre-empt a celebration already playing. Reserved for
#                     genuinely rare "something is wrong / go look NOW".
#
# TIER_FLASH is the load-bearing addition. Without it every candidate event
# is a binary "steal the whole panel or do nothing", and that pressure is
# exactly what inflates a top tier until it means nothing. With it, this
# project can be GENEROUS about noticing and STINGY about interrupting.
TIER_FLASH = 1
TIER_INTERRUPT = 2
TIER_TAKEOVER = 3

TIER_TICKS = {
    TIER_FLASH: 40,          # ~2s, in-mode only
    TIER_INTERRUPT: 90,      # ~4.5s -- CELEBRATION_TICKS, unchanged
    TIER_TAKEOVER: 120,      # ~6s
}

# Which system fired a moment. Selects the BACKDROP only -- every tier and
# every system shares one renderer, one text hierarchy and one set of timing
# beats, so a celebration always reads as this device speaking. See
# CELEBRATION_BACKDROPS for why the backdrop is the thing that varies.
SYSTEM_SPORTS = "sports"
SYSTEM_FLIGHTS = "flights"
SYSTEM_SATELLITE = "satellite"


def _burst_ring(buf, t, radius, color, n=28, phase=0.0):
    """One ring of points around (_CX, _CY) -- cheap (no fill), and a few
    of these at staggered radii is what reads as an expanding shockwave."""
    for i in range(n):
        a = phase + (2 * math.pi * i) / n
        x = int(_CX + radius * math.cos(a))
        y = int(_CY + radius * math.sin(a) * 0.92)   # slight squash: panel isn't square-safe at the edges
        put_px(buf, x, y, color)


def _sunburst_rays(buf, t, color, n=10):
    """Rotating rays from center to edge -- the "energy" backdrop behind
    the rings, distinct enough from a plain radial gradient to read as
    deliberately drawn rather than a blur."""
    rot = t * 0.12
    for i in range(n):
        a = rot + (2 * math.pi * i) / n
        dx, dy = math.cos(a), math.sin(a)
        for r in range(4, 30, 2):
            x = int(_CX + dx * r)
            y = int(_CY + dy * r)
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                fade = max(0.15, 1.0 - r / 34.0)
                put_px(buf, x, y, tuple(int(c * fade) for c in color))


def _backdrop_sports(buf, t, color, moment=None):
    """Rings + rotating rays -- stadium energy. The ORIGINAL burst,
    unchanged, so the sports moments that already shipped look exactly
    as they did before tiers existed.

    ADDS a small per-sport identity icon as an accent, reusing the SAME
    shape definition the header glyph draws (SPORT_ICONS / draw_diamond
    for baseball), just at 2x scale -- the "one shape language, multiple
    contexts" pattern, not a second design. Drawn in the top-left corner,
    outside the centered text plate's reach, so it never collides with
    the kind/line1/line2 block drawn afterward. The burst/ray backdrop
    itself is UNCHANGED -- this only adds an accent on top of it, per
    the "backdrop stays each mode's own visual language, only the
    accent/detail varies" rule already used for the flights/satellite
    backdrops."""
    _sunburst_rays(buf, t, color)
    # Three rings at staggered radii/phases so they don't read as one
    # blob -- each expands and wraps, giving continuous outward motion
    # for the whole hold rather than a single pulse that goes static.
    for k in range(3):
        radius = ((t * 1.6 + k * 11) % (_MAX_BURST_R - 3)) + 3
        ring_color = tuple(int(c * (1.0 - 0.4 * (k / 3))) for c in color)
        _burst_ring(buf, t, radius, ring_color, phase=k * 1.9)
    sport = (moment or {}).get("sport")
    icon_color = (255, 255, 255)
    if sport == "baseball":
        # Reuse the existing diamond glyph directly rather than adding a
        # redundant second baseball shape -- drawn "empty" (no real base
        # state to show here) since a celebration accent isn't a live
        # base/out state, just an identity mark.
        draw_diamond(buf, 4, 4, [False, False, False],
                     on_col=icon_color, off_col=tuple(int(c * 0.35) for c in icon_color))
    else:
        fn = SPORT_ICONS.get(sport)
        if fn is not None:
            fn(buf, 4, 4, icon_color, scale=2)


def _backdrop_flights(buf, t, color, moment=None):
    """A radar sweep wedge -- the flights mode's OWN heartbeat, blown up
    to full-panel. Deliberately not the sports burst: a celebration
    should feel like it belongs to the system that fired it while still
    obviously being the same device speaking, and the sweep is the one
    piece of visual language this mode already owns.

    Cost-shaped on purpose: `step`/`rstep` are coarser than
    draw_scope_sweep's so a bigger radius doesn't cost more than the
    burst it replaces (~500 put_px, comparable to the sunburst's rays
    plus rings)."""
    sweep = t * 7.0                      # faster than the scope's own 3 deg/tick -- this is an event, not an idle
    trail_deg, step, rstep = 84, 4, 2
    for k in range(0, trail_deg, step):
        fade = (1.0 - k / float(trail_deg)) ** 2
        col = tuple(int(c * fade) for c in color)
        if col == (0, 0, 0):
            continue
        a = math.radians(sweep - k)
        sa, ca = math.sin(a), math.cos(a)
        for r in range(4, _MAX_BURST_R, rstep):
            put_px(buf, int(_CX + r * sa), int(_CY - r * ca), col)
    # One expanding ring: reads as "contact", and keeps outward motion
    # going even at the moment the wedge is pointing away from you.
    _burst_ring(buf, t, ((t * 1.9) % (_MAX_BURST_R - 4)) + 4,
                tuple(int(c * 0.75) for c in color), n=32)


def _backdrop_satellite(buf, t, color, moment=None):
    """A rising horizon-to-horizon arc with a travelling marker -- the
    sky-dome mode's own pass-arc language (see
    SatelliteEngine._draw_pass_arc), scaled to full panel. Reads as
    "something is crossing the sky right now", which is precisely the
    only thing this system ever interrupts for."""
    y_horizon, y_top = 50, 12
    for x in range(3, WIDTH - 3):
        put_px(buf, x, y_horizon, tuple(int(c * 0.25) for c in color))
    pts = []
    for i in range(33):
        f = i / 32.0
        x = int(4 + f * (WIDTH - 8))
        y = int(y_horizon - math.sin(f * math.pi) * (y_horizon - y_top))
        pts.append((x, y))
    for x, y in pts:
        put_px(buf, x, y, tuple(int(c * 0.55) for c in color))
    # The marker sweeps the arc repeatedly for the whole hold -- one
    # traverse would go static halfway through a 6s takeover.
    idx = int((t * 0.9) % len(pts))
    mx, my = pts[idx]
    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        put_px(buf, mx + dx, my + dy, color)
    put_px(buf, mx, my, (255, 255, 255))


# Backdrop is the ONE thing that varies per system. Everything else --
# the impact flash, the dark plate, the kind/line1/line2 hierarchy, the
# timing beats -- is shared, which is what keeps four systems reading as
# one product instead of four. (Learned from the ambient "channel ident"
# experiment that got reverted: per-mode LAYOUTS meant two things to
# maintain per mode and the copy nobody looked at drifted. Varying only
# the backdrop keeps a single layout and a single renderer.)
CELEBRATION_BACKDROPS = {
    SYSTEM_SPORTS: _backdrop_sports,
    SYSTEM_FLIGHTS: _backdrop_flights,
    SYSTEM_SATELLITE: _backdrop_satellite,
}


def draw_celebration(buf, t, moment, total=CELEBRATION_TICKS):
    """One frame of the big-moment celebration. `t` counts UP from 0 at
    the moment it fired; `moment` is whatever pop_big_moment() returned.

    `total` is the tier's own hold length (TIER_TICKS) -- the backdrop
    motion is all `t`-relative and wraps, so a longer TAKEOVER hold just
    means more revolutions rather than needing any different math."""
    fill(buf, (0, 0, 0))
    color = moment.get("color") or (255, 200, 40)

    # Impact frame: a near-white flash on the first two ticks, the same
    # "hit" beat a real broadcast graphic uses before settling into its
    # sustained look -- done here with plain brightness, not a copied
    # asset. A TAKEOVER gets a longer flash: the extra beat is what makes
    # the top tier register as different before any text resolves.
    flash_ticks = 4 if moment.get("tier") == TIER_TAKEOVER else 2
    if t < flash_ticks:
        fill(buf, tuple(min(255, int(c * 0.6 + 90)) for c in color))

    CELEBRATION_BACKDROPS.get(moment.get("system"), _backdrop_sports)(buf, t, color, moment)

    # Dark plate behind the text so it stays legible over the burst --
    # same reasoning as any header's contrast treatment elsewhere in this
    # project, just centered instead of top-anchored.
    kind = str(moment.get("kind") or "")
    line1 = str(moment.get("line1") or "")
    line2 = str(moment.get("line2") or "")
    lines = [ln for ln in (kind, line1, line2) if ln]
    block_h = len(lines) * 8
    y0 = _CY - block_h // 2 + 4
    for y in range(max(0, y0 - 2), min(HEIGHT, y0 + block_h + 2)):
        for x in range(WIDTH):
            cur = buf[(y * WIDTH + x) * 3:(y * WIDTH + x) * 3 + 3]
            put_px(buf, x, y, tuple(int(c * 0.28) for c in cur))

    # Kind (e.g. "HOME RUN") flashes white/color like Pulse; the two
    # detail lines hold steady so the moment stays readable while it
    # celebrates.
    flash = (t // 3) % 2 == 0
    kind_color = (255, 255, 255) if flash else color
    y = y0
    if kind:
        draw_text_centered(buf, y, fit_text(kind, WIDTH - 6, scale=1), kind_color)
        y += 8
    if line1:
        draw_text_centered(buf, y, fit_text(line1, WIDTH - 6), (235, 238, 245))
        y += 8
    if line2:
        draw_text_centered(buf, y, fit_text(line2, WIDTH - 6), (170, 178, 195))

    return bytes(buf)


# TIER_FLASH's in-mode banner. Deliberately NOT a small celebration: it
# has no burst, no impact frame and no rings, because the entire point of
# the tier is that it does not perform. It states a fact and leaves.
FLASH_BAND_Y = 11            # band occupies y=11..21; text sits at y=13
FLASH_BAND_H = 11


def draw_flash_banner(buf, t, moment, total=None):
    """A compact banner drawn INSIDE the owning mode's own frame. Never
    reaches AmbientEngine's celebration path at all, so it can neither
    interrupt a rotation nor compete with a real celebration.

    The band FILLS its rows rather than compositing over them: the
    caller is responsible for only invoking this on a view whose rows
    11..21 carry no text (FlightEngine's SCOPE does not -- its only text
    is the header above and the range legend below). That keeps this
    structurally incapable of producing the text-on-text collision
    render_audit exists to catch, rather than relying on it being
    noticed later."""
    total = total or TIER_TICKS[TIER_FLASH]
    color = moment.get("color") or (255, 200, 40)
    # Fade the band out over its last third so it leaves rather than
    # vanishing -- a hard cut reads as a glitch at this size.
    left = max(0, total - t)
    fade = min(1.0, left / (total / 3.0))
    for y in range(FLASH_BAND_Y, min(HEIGHT, FLASH_BAND_Y + FLASH_BAND_H)):
        edge = y in (FLASH_BAND_Y, FLASH_BAND_Y + FLASH_BAND_H - 1)
        row = tuple(int(c * (0.75 if edge else 0.16) * fade) for c in color)
        for x in range(WIDTH):
            put_px(buf, x, y, row)
    kind = str(moment.get("kind") or "")
    line1 = str(moment.get("line1") or "")
    txt = f"{kind} {line1}".strip() if kind and line1 else (kind or line1)
    draw_text_centered(buf, FLASH_BAND_Y + 3, fit_text(txt, WIDTH - 4),
                       tuple(int(c * fade) for c in (255, 255, 255)))


class BigMomentSource:
    """The ONE mechanism any mode uses to report "something worth looking
    up from what you're doing just happened".

    Mixed into SportsEngine, FlightEngine and SatelliteEngine so all
    three genuinely share this rather than each growing a parallel
    implementation that drifts -- the same reasoning that put the fold in
    paneltext.py instead of per-module, and the scroll control in
    Browsable instead of per-mode.

    TIER SEPARATION IS STRUCTURAL, NOT CONVENTIONAL. TIER_FLASH moments
    go into their own slot (`_flash`) that AmbientEngine never reads, so
    a low-tier moment CANNOT compete with a real celebration by
    construction -- not merely by a comparison somewhere that a future
    change could forget.
    """

    def _init_big_moments(self):
        self._pending_big_moment = None
        self._flash = None
        self._flash_t = 0

    # ---- the interrupt channel (TIER_INTERRUPT / TIER_TAKEOVER) --------
    def peek_big_moment(self):
        """The pending moment WITHOUT consuming it. AmbientEngine peeks
        every engine, picks the highest tier, and pops only the winner --
        so a trivial moment can no longer pre-empt a critical one purely
        by dict iteration order, and the loser stays queued for the next
        tick instead of being silently dropped."""
        return self._pending_big_moment

    def pop_big_moment(self):
        m, self._pending_big_moment = self._pending_big_moment, None
        return m

    def _set_big_moment(self, kind, line1, line2="", color=None,
                        tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport=None):
        """`kind`/`line1`/`line2` must already be paneltext.panel_text()-
        folded by the caller, same as every other externally-sourced
        string these engines draw.

        A TIER_FLASH moment is routed to the in-mode slot instead of the
        interrupt queue -- see the class docstring on why that split is
        structural.

        OVERWRITE IS TIER-GATED. The original one-slot "last write wins"
        was correct when only sports could fire (two sports moments in
        the same tick are genuinely interchangeable by recency). Across
        four systems it is not: a routine TIER_INTERRUPT arriving one
        tick after a TIER_TAKEOVER would silently discard the more
        important event. A lesser moment is now dropped instead of
        clobbering a bigger one still waiting to be shown.
        """
        moment = {"kind": kind, "line1": line1, "line2": line2,
                  "color": color or (255, 200, 40), "tier": tier, "system": system,
                  "sport": sport}
        if tier == TIER_FLASH:
            self._flash = moment
            self._flash_t = TIER_TICKS[TIER_FLASH]
            return
        cur = self._pending_big_moment
        if cur and cur.get("tier", TIER_INTERRUPT) > tier:
            return
        self._pending_big_moment = moment

    # ---- the in-mode channel (TIER_FLASH) ------------------------------
    def _tick_flash(self):
        """Call once per tick() from the owning engine."""
        if self._flash_t > 0:
            self._flash_t -= 1
            if self._flash_t <= 0:
                self._flash = None

    def _draw_flash(self, buf):
        """Call at the END of a frame whose rows 11..21 carry no text --
        see draw_flash_banner()'s own note. Returns True if it drew."""
        if self._flash_t <= 0 or not self._flash:
            return False
        total = TIER_TICKS[TIER_FLASH]
        draw_flash_banner(buf, total - self._flash_t, self._flash, total)
        return True


# =============================================================================
# SHARED RADAR / SCOPE SYSTEM -- one visual language, TWO DIFFERENT PROJECTIONS.
#
# Used by BOTH FlightEngine (ground radar) and SatelliteEngine (sky dome).
# What is shared is the DRAWING: home at centre, dotted range rings, a
# rotating sweep beam with a fading trail, and target marks that brighten as
# the beam passes them. What is deliberately NOT shared is the MATH that
# decides where a target sits -- those are genuinely different projections
# and collapsing them into one formula would make one of the two wrong:
#
#   FLIGHTS   -- GROUND radar. Polar: bearing + ground DISTANCE from home.
#                Centre = home on the ground, edge = RADIUS_NM away.
#   SATELLITE -- SKY DOME. Zenithal: bearing + ELEVATION ANGLE.
#                Centre = straight up (zenith), edge = the horizon (0 deg).
#                This is the standard planetarium/all-sky convention.
#
# Each engine computes its own `r_frac` (0.0 at centre .. 1.0 at the edge)
# and hands it to scope_xy(); everything below this line is projection-blind.
#
# RANGE SCALE IS NON-LINEAR FOR FLIGHTS, AND THAT IS A REAL FINDING, NOT A
# STYLE CHOICE. Measured against live traffic near MYR: of 9 real objects
# (8 aircraft + the airport itself), SIX landed inside a 6px radius on a
# linear 40nm scale -- an unreadable blob at the centre with the outer half
# of the scope empty -- because most interesting traffic near a home
# location is approach traffic within ~6nm. A sqrt scale puts ZERO of those
# 9 in that blob while preserving exact distance ORDER, and the rings are
# labelled with their TRUE nautical-mile values so nothing is
# misrepresented. The satellite dome stays LINEAR in elevation, which is
# the correct convention for a sky plot and needs no such correction.
# =============================================================================
# Geometry budgeted so the whole scope fits BETWEEN the shared header and a
# footer text row: header occupies y=0..8, the scope spans y=10..56 at these
# values, and the footer glyph row starts at y=58 (5px tall, last row 62 --
# inside HEIGHT-5=59, the real bound a 5px glyph must start at or before).
SCOPE_CX = WIDTH // 2
SCOPE_CY = 33
SCOPE_R = 23

# Targets never fade to nothing behind the sweep. The object is really up
# there for the whole rotation, so making it vanish for most of the cycle
# would be the display lying for the sake of the effect -- the sweep is
# decoration over continuously-known data, not a sensor that only learns
# about a target when the beam hits it.
SCOPE_TARGET_FLOOR = 0.38

# NOTABLE brightness floor (2026-08-08) -- the one piece of visual
# hierarchy this scope was missing. Before this, EVERY target dimmed the
# same way as the sweep passed it (down to SCOPE_TARGET_FLOOR), so a
# MAYDAY squawk and a routine airliner read identically off-beam --
# "notable" only showed up as TEXT elsewhere on the card, never on the
# scope itself. A real notable aircraft (see flights._notable()'s own
# rank tiers) now never dims below this floor, regardless of where the
# sweep currently is -- still modulated brighter as the beam passes it,
# same as everything else, just with a higher off-beam baseline.
#
# Deliberately NOT the same signal as the window ring. The owner asked
# directly whether "in window" and "notable" should collapse into one
# "brighter" treatment or stay visually distinct -- they stay distinct
# on purpose: a window aircraft and a heavy/helicopter are different
# KINDS of interesting (one is about where YOU happen to be looking,
# one is about what the aircraft itself IS), and collapsing both into
# brightness would recreate the exact ambiguity that prompted this fix
# in the first place -- two different reasons to look would read as the
# same "this one's brighter" cue with no way to tell why. The window
# ring stays a categorical shape+color marker; NOTABLE_GLOW_FLOOR is a
# categorical brightness marker; a window aircraft that is ALSO notable
# correctly shows both at once rather than either overriding the other.
NOTABLE_GLOW_FLOOR = 0.75


def scope_xy(bearing_deg, r_frac, cx=SCOPE_CX, cy=SCOPE_CY, radius=SCOPE_R):
    """(bearing, normalised radius) -> pixel. The ONE place the polar->screen
    convention lives: 0 deg = North = straight UP, angles increase clockwise,
    matching both a compass rose and an azimuth reading."""
    r = max(0.0, min(1.0, r_frac)) * radius
    a = math.radians(bearing_deg if bearing_deg is not None else 0.0)
    return (cx + r * math.sin(a), cy - r * math.cos(a))


def draw_scope_rings(buf, ring_fracs, color=(22, 52, 34),
                     cx=SCOPE_CX, cy=SCOPE_CY, radius=SCOPE_R):
    """Dotted concentric range rings at caller-supplied radius fractions.

    The CALLER decides the fractions, because that is projection-specific
    (sqrt of distance for flights, linear elevation for the sky dome) --
    this only draws them."""
    for frac in ring_fracs:
        rr = max(1.0, frac * radius)
        n = max(16, int(rr * 4))          # enough points that the ring reads as a ring
        for i in range(n):
            a = 2 * math.pi * i / n
            put_px(buf, int(round(cx + rr * math.cos(a))),
                   int(round(cy + rr * math.sin(a))), color)


def draw_scope_crosshair(buf, color=(22, 52, 34),
                         cx=SCOPE_CX, cy=SCOPE_CY, radius=SCOPE_R):
    """N/S and E/W tick marks at the rim -- orientation cues that cost four
    short strokes instead of four text labels.

    Text labels were tried and reverted same-session: real feedback was
    that letters read as clunky against the sweep/rings aesthetic this is
    going for. The fix for "how do I orient this" is a real landmark (the
    coastline, the airport), not instrument-panel compass letters -- see
    the ground-radar coastline note below."""
    for k in range(3):
        put_px(buf, cx, cy - radius + k, color)          # N
        put_px(buf, cx, cy + radius - k, color)          # S
        put_px(buf, cx - radius + k, cy, color)          # W
        put_px(buf, cx + radius - k, cy, color)          # E


def draw_scope_sweep(buf, sweep_deg, color=(18, 120, 58), trail_deg=72,
                     cx=SCOPE_CX, cy=SCOPE_CY, radius=SCOPE_R):
    """Rotating beam with a quadratically-fading trail behind it.

    Drawn as radial lines rather than a filled wedge: at this size a wedge
    is a bright blob that drowns the targets, and radial lines are also
    far cheaper (~1900 put_px per frame, measured 0.6ms -- 1.2% of the
    50ms frame budget, so this stays well clear of the per-pixel-Python
    cost rule that transitions.py exists to respect)."""
    step = 2                     # degrees between trail spokes
    for k in range(0, trail_deg, step):
        # Cubic falloff, not quadratic: the trail has to read as a decaying
        # wake behind a bright leading edge. A shallower curve fills the
        # wedge almost uniformly and the scope turns into a floodlit blob
        # that competes with the targets it exists to reveal -- confirmed
        # by rendering it against real traffic, not reasoned about.
        fade = (1.0 - k / float(trail_deg)) ** 3
        col = (int(color[0] * fade), int(color[1] * fade), int(color[2] * fade))
        if col == (0, 0, 0):
            continue
        a = math.radians(sweep_deg - k)
        sa, ca = math.sin(a), math.cos(a)
        for r in range(2, radius + 1):
            put_px(buf, int(round(cx + r * sa)), int(round(cy - r * ca)), col)


def scope_glow(bearing_deg, sweep_deg, trail_deg=72):
    """How lit a target is right now: 1.0 as the beam crosses it, decaying
    to SCOPE_TARGET_FLOOR behind it (never to zero -- see the note above)."""
    if bearing_deg is None:
        return SCOPE_TARGET_FLOOR
    d = (sweep_deg - bearing_deg) % 360.0
    if d > trail_deg:
        return SCOPE_TARGET_FLOOR
    return 1.0 - (d / float(trail_deg)) * (1.0 - SCOPE_TARGET_FLOOR)


def draw_scope_target(buf, x, y, color, glow=1.0, big=False):
    """A target blip. `big` draws a plus instead of a dot, for the one or
    two objects that matter more than the rest (an airport, the ISS)."""
    col = (int(color[0] * glow), int(color[1] * glow), int(color[2] * glow))
    xi, yi = int(round(x)), int(round(y))
    put_px(buf, xi, yi, col)
    if big:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            put_px(buf, xi + dx, yi + dy, col)
    else:
        # A single pixel disappears against the sweep; one soft neighbour
        # pair keeps a plain target readable without it reading as "big".
        dim = (col[0] // 2, col[1] // 2, col[2] // 2)
        put_px(buf, xi + 1, yi, dim)
        put_px(buf, xi, yi + 1, dim)


# PART 3 -- real, INTUITIVE aircraft icons on the flight radar scope,
# replacing the plain dot every target used to draw. Not decorative: each
# shape is chosen so the KIND of traffic is readable from the icon alone,
# oriented to real heading (`track_deg`) the same rotation math
# `FlightEngine._draw_plane_icon` already uses for the DETAIL card, just
# scaled down to what a 46px-diameter scope can actually resolve.
#
# HONEST CONSTRAINT, stated rather than glossed over: at this pixel
# density (up to 8 real aircraft inside a 23px radius), four visually
# DISTINCT heading-oriented silhouettes is optimistic -- a business jet's
# narrower dart and an airliner's wider one are a real, deliberate
# difference in the pixel offsets below, but at 1:1 LED pixel scale they
# will often read as "a small pointed shape" to a human eye rather than
# unmistakably different aircraft classes. The helicopter and the plain
# GA dash are the two shapes that stay unmistakable at this scale (one
# has no heading-oriented wings at all, the other is a rotor disk, not a
# dart) -- those two carry the real weight of "readable at a glance".
SCOPE_ICON_HELI = "HELI"
SCOPE_ICON_AIRLINER = "AIRLINER"
SCOPE_ICON_BIZJET = "BIZJET"
SCOPE_ICON_GA = "GA"


def draw_scope_aircraft(buf, x, y, heading_deg, kind, color, glow=1.0, big=False):
    """One aircraft blip, shaped by `kind` (one of the SCOPE_ICON_*
    constants) and rotated to `heading_deg` (real ADS-B track, not
    guessed) using the identical fwd/right rotation convention
    `FlightEngine._draw_plane_icon` uses for the DETAIL card -- one
    rotation math, shared across scales, not independently invented per
    context. `heading_deg is None` falls back to a fixed "up" orientation
    rather than an uncertainty ring (unlike the DETAIL icon): at 1-2px
    spread, a dim ring around a target this small would read as clutter,
    not signal, and the DETAIL card already carries the honest
    heading-unknown treatment for whichever aircraft is actually
    selected.

    REDESIGNED 2026-08-08 after real feedback: the four kinds only
    differed by SIZE before this (same cross, scaled), which is why they
    didn't read as different categories at 3-5px -- and the helicopter
    was 9 disconnected `put_px` dots with no connecting stroke, which is
    why it read as a violet blob rather than a shape (confirmed against
    a real rendered screenshot, not just described). Every kind below is
    now a genuinely different SILHOUETTE FAMILY, not a scaled copy of
    one shape, and every stroke is connected -- no shape here is drawn as
    loose unconnected points:
      - GA:       a single stroke -- a lone dash, no wings at all.
      - BIZJET:   a small cross with the wing set AFT of centre (closer
                  to the tail than the nose), a real structural cue most
                  business jets share (low, rear-mounted wing).
      - AIRLINER: a wide cross, wing centred on the fuselage -- the
                  "biggest, most symmetric" silhouette, matching a
                  widebody/narrowbody's actual proportions best.
      - HELI:     a connected T -- a horizontal rotor bar that stays
                  screen-level (drawn fixed, only mirrored left/right by
                  which way the aircraft is facing, never rotated to
                  heading -- a helicopter's rotor disk doesn't visually
                  "point" anywhere) sitting over a short vertical mast
                  with one tail-boom pixel kicked out behind it. The
                  rotor bar staying perfectly horizontal while every
                  fixed-wing icon rotates with real heading is itself a
                  recognition cue over time, not just the shape alone.
    Still 2 strokes (or fewer) per icon, same budget this project
    already proved safe against a real lag complaint earlier this
    session -- this redesign changes SHAPE, not stroke COUNT.
    """
    col = (int(color[0] * glow), int(color[1] * glow), int(color[2] * glow))
    theta = math.radians(heading_deg if heading_deg is not None else 0.0)
    xi, yi = int(round(x)), int(round(y))
    scale = 1.25 if big else 1.0

    if kind == SCOPE_ICON_HELI:
        # Fixed screen-space T, mirrored only -- see the docstring above
        # for why this deliberately does NOT rotate with heading.
        face_right = math.sin(theta) >= 0
        fx = 1 if face_right else -1
        span = int(round(3 * scale))
        mast_top = -2
        mast_bot = int(round(2 * scale))
        draw_line(buf, xi - span, yi + mast_top, xi + span, yi + mast_top, col)
        draw_line(buf, xi, yi + mast_top, xi, yi + mast_bot, col)
        # Tail boom kicks out BEHIND the facing direction -- a real
        # helicopter's tail trails away from its nose, so this one pixel
        # is what actually tells left-facing from right-facing at a
        # glance, not just the mirrored rotor bar (which is symmetric).
        put_px(buf, xi - fx * (span - 1), yi + mast_bot, col)
        return

    # Every fixed-wing kind still shares ONE rotation convention (the
    # DETAIL card uses the same fwd/right math) -- only the STROKE
    # LAYOUT differs per kind now, not just a size multiplier on one
    # shared layout.
    fwd = (math.sin(theta), -math.cos(theta))
    right = (math.cos(theta), math.sin(theta))

    def pt(fx, fy):
        return (x + fx * fwd[0] + fy * right[0], y + fx * fwd[1] + fy * right[1])

    if kind == SCOPE_ICON_GA:
        # A lone dash -- no wing stroke at all. "Less drawn" is the
        # point: the smallest, simplest real silhouette on this scope,
        # deliberately not a shrunk copy of the cross family below.
        nose_fx = 2.2 * scale
        a, b = pt(nose_fx, 0), pt(-nose_fx, 0)
        draw_line(buf, a[0], a[1], b[0], b[1], col)
        return

    nose_fx, wing_fy, wing_fx = {
        # wing_fx is where the wing stroke sits along the fuselage axis,
        # relative to the fuselage's own centre (0 = centred on the
        # aircraft, negative = set back toward the tail).
        SCOPE_ICON_AIRLINER: (3.2, 2.6, 0.0),
        SCOPE_ICON_BIZJET:   (2.6, 1.5, -0.9),
    }[kind]
    nose_fx, wing_fy, wing_fx = nose_fx * scale, wing_fy * scale, wing_fx * scale
    a, b = pt(nose_fx, 0), pt(-nose_fx, 0)
    draw_line(buf, a[0], a[1], b[0], b[1], col)
    a, b = pt(wing_fx, -wing_fy), pt(wing_fx, wing_fy)
    draw_line(buf, a[0], a[1], b[0], b[1], col)


def draw_window_ring(buf, x, y, color):
    """A small 4-point diamond ring around an in-window aircraft's blip --
    the SIMPLE version of the window-filter visual treatment (see
    flights.py's window-filter note): a 1px ring at a fixed radius, drawn
    UNDER the aircraft icon so the icon's own shape is never obscured.
    Deliberately not heading-oriented or scaled by distance -- at this
    pixel density (a handful of px between blips) a plain fixed diamond
    is the difference a real "which of these am I supposed to notice"
    flag needs, not a design system of its own. A genuinely designed
    treatment (glow animation, distinct icon family, etc.) is a bigger
    call the owner should make deliberately, not guess at here."""
    xi, yi = int(round(x)), int(round(y))
    for dx, dy in ((0, -3), (0, 3), (-3, 0), (3, 0)):
        put_px(buf, xi + dx, yi + dy, color)


def draw_scope_airport(buf, x, y, color, glow=1.0):
    """A small runway-strip glyph -- two parallel end-cap ticks joined by
    a short bar, unmistakably 'a landing strip' rather than a generic
    plus/waypoint mark. Deliberately NOT oriented to the airport's real
    runway heading: `flights.load_airport()`/`location_config.json`
    store only lat/lon/name, no runway bearing, and hardcoding one real
    airport's heading (MYR's is genuinely 18/36, confirmed by a real ATC
    transmission captured this same session -- 'RUNWAY 18V ALPHA') would
    be silently WRONG the moment the configured home airport changes to
    a different one. A canonical vertical orientation is an honest
    generic 'this is a runway' glyph, not a claimed real bearing this
    project doesn't actually have on file."""
    col = (int(color[0] * glow), int(color[1] * glow), int(color[2] * glow))
    xi, yi = int(round(x)), int(round(y))
    for dy in (-2, -1, 0, 1, 2):
        put_px(buf, xi, yi + dy, col)
    for dx in (-1, 1):
        put_px(buf, xi + dx, yi - 2, col)
        put_px(buf, xi + dx, yi + 2, col)


def draw_scope_home(buf, color=(235, 242, 255), cx=SCOPE_CX, cy=SCOPE_CY):
    """Home at the centre -- a small diamond, distinct in SHAPE from every
    target mark so it reads as 'you are here' rather than 'another blip'."""
    for dx, dy in ((0, -2), (0, 2), (-2, 0), (2, 0),
                   (-1, -1), (1, -1), (-1, 1), (1, 1)):
        put_px(buf, cx + dx, cy + dy, color)


# ---- HERO-SCALE FILLED SILHOUETTES (2026-08-08, PLANE-IN-WINDOW takeover) --
# A NEW rendering treatment of the SAME four kinds `_ac_kind()`/
# `_hangar_kind()` already classify aircraft into everywhere else in this
# project (scope icon, DETAIL card icon, Hangar sprite) -- reuses that
# classification, adds new drawing code at a new bigger, FILLED scale.
# Geometric only (triangles/ovals via a scanline fill over `put_px`), same
# "simple original geometry, no real logos/liveries" rule the smaller
# icons above already follow. Only ONE of these draws per frame (a
# full-screen takeover shows a single aircraft at a time), a very
# different cost profile than the scope's up-to-8-icons-per-frame case
# this project has already had a real lag complaint about and fixed (see
# the ICON/PERFORMANCE REVISIT note) -- a handful of scanline rows is
# cheap even done fresh every tick.
def _fill_poly(buf, pts, color):
    """Plain scanline polygon fill -- pts is a list of (x, y), assumed to
    describe a simple (non-self-intersecting) polygon. Used only for the
    hero silhouettes below, at most once per frame."""
    ys = [p[1] for p in pts]
    y0, y1 = int(math.floor(min(ys))), int(math.ceil(max(ys)))
    n = len(pts)
    for y in range(y0, y1 + 1):
        yc = y + 0.5
        xs = []
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            if ay == by:
                continue
            if (yc >= ay) != (yc >= by):
                t = (yc - ay) / (by - ay)
                xs.append(ax + t * (bx - ax))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            x0, x1 = int(round(xs[i])), int(round(xs[i + 1]))
            for x in range(x0, x1 + 1):
                put_px(buf, x, y, color)


def draw_hero_silhouette(buf, cx, cy, kind, color, scale=1.0):
    """Large FILLED silhouette for the plane-in-window takeover screen and
    the ceremonial Hangar detail card -- hero-scale, always facing "up"
    (this is a portrait/showcase treatment, not a heading-oriented radar
    icon; heading isn't the point here). Dispatches on the SAME
    `FlightEngine._ac_kind()`/`_hangar_kind()` buckets used everywhere
    else, so the shape choice is consistent with the scope/DETAIL/Hangar
    icons -- just filled and much bigger."""
    s = scale
    if kind == SCOPE_ICON_HELI:
        # Fuselage blob (a small filled oval) + a thin tail boom + a
        # rotor disk outline overhead -- the one hero shape that stays
        # unmistakable at any scale, same reasoning as the small scope
        # icon's own helicopter treatment.
        rx, ry = 7 * s, 5 * s
        pts = []
        for i in range(16):
            a = i / 16 * 2 * math.pi
            pts.append((cx + rx * math.cos(a), cy + 4 * s + ry * math.sin(a)))
        _fill_poly(buf, pts, color)
        for dy in range(int(-16 * s), int(-4 * s)):
            put_px(buf, int(round(cx)), int(round(cy + dy)), color)
        for i in range(20):
            a = i / 20 * 2 * math.pi
            put_px(buf, int(round(cx + 15 * s * math.cos(a))),
                   int(round(cy - 16 * s + 4 * s * math.sin(a))), rim(color, 0.5))
        for dx in range(int(-3 * s), int(3 * s) + 1):
            put_px(buf, int(round(cx + dx)), int(round(cy + 9 * s)), color)
        return

    # Fixed-wing kinds: one filled dart (nose to swept wingtips to tail),
    # width/length varying per kind -- the same real, deliberate
    # proportion difference the small scope icon uses, just filled solid
    # instead of stroked.
    nose_fy, wing_fy, tail_fx = {
        SCOPE_ICON_AIRLINER: (16.0, 11.0, 9.0),
        SCOPE_ICON_BIZJET:   (15.0, 7.0, 8.0),
        SCOPE_ICON_GA:       (13.0, 4.5, 7.0),
    }.get(kind, (16.0, 11.0, 9.0))
    nose_fy, wing_fy, tail_fx = nose_fy * s, wing_fy * s, tail_fx * s
    nose = (cx, cy - nose_fy)
    wing_l = (cx - wing_fy, cy + nose_fy * 0.15)
    wing_r = (cx + wing_fy, cy + nose_fy * 0.15)
    tail = (cx, cy + tail_fx)
    _fill_poly(buf, [nose, wing_r, tail, wing_l], color)
    # Small tailplane, its own thin filled triangle -- reads as a tail
    # fin without needing a whole second silhouette family.
    tail_l = (cx - wing_fy * 0.35, cy + tail_fx * 0.7)
    tail_r = (cx + wing_fy * 0.35, cy + tail_fx * 0.7)
    _fill_poly(buf, [tail, tail_r, tail_l], color)


def draw_first_sighting_ring(buf, cx, cy, color, phase=0.0):
    """A soft expanding ring under the hero silhouette, for a genuinely
    first-ever real Hangar sighting only (see hangar.LOG's own real
    times_seen field -- never guessed). Kept "medium" weight per the
    owner's spec: one dim ring, animated by `phase` (an engine-owned
    tick counter, not real-world time -- consistent with every other
    render-side animation in this project), not a design competing with
    the hero shape for attention. Reuses `draw_window_ring()`'s own
    "ring under the icon" placement convention rather than inventing a
    third one."""
    r = 20.0 + 6.0 * math.sin(phase)
    dim = rim(color, 0.35)
    n = 24
    for i in range(n):
        a = i / n * 2 * math.pi
        put_px(buf, int(round(cx + r * math.cos(a))),
               int(round(cy + r * math.sin(a))), dim)


def draw_marquee(buf, y, text, color, scroll, scale=1, gap="   "):
    """Seamless looping scroller -- the shared tape used by every ticker
    mode. Draws two copies so the wrap has no visible seam."""
    s = text + gap
    pitch = 4 * scale
    total = pitch * len(s)
    if total <= 0:
        return
    off = int(scroll) % total
    for rep in (0, 1):
        x = -off + rep * total
        for ch in s:
            if x > WIDTH:
                break
            if x > -pitch:
                draw_text3x5(buf, x, y, ch, color, scale=scale)
            x += pitch


# =============================================================================
# SNAKE — classic: walls kill, grow on food, speed scales, 1-input buffer
# =============================================================================
class SnakeEngine:
    name = "snake"
    GRID = 32
    CELL = WIDTH // 32  # 2 px

    BG = (0, 0, 0)              # pure black → background shows through
    HEAD = (80, 255, 220)       # aqua neon — pops on fire/party/matrix
    BODY_NEAR = (40, 255, 120)
    BODY_FAR = (0, 140, 90)
    FOOD = (255, 40, 200)       # hot magenta food (not red-on-fire)
    FOOD_GLOW = (180, 20, 255)
    DEAD = (255, 60, 60)

    # False = classic lethal walls (default). True = portal/wrap mode.
    # Every major Snake ships lethal walls as the default and offers wrap as an
    # opt-in variant; on speedrun.com's board Wall (1199 runs) outdraws Portal
    # (635) ~1.9x, and measured here wall deaths are <1% of all deaths and zero
    # past length 50 — wrap solves a problem that isn't killing anyone.
    WRAP = False

    def __init__(self):
        self.tick_rate = 0.12
        self.reset()

    def reset(self):
        c = self.GRID // 2
        self.body = [(c, c), (c - 1, c), (c - 2, c), (c - 3, c)]
        self.dir = RIGHT
        self.queue = []
        self.food = self._place_food()
        self.death_flash = 0
        self.score = 0
        self.pulse = 0
        self._set_speed()

    def _set_speed(self):
        # Two-stage ramp. Early on, a gear change every 5 foods you can actually
        # feel. Past 40 the gears get finer but keep coming, so difficulty is
        # still climbing at score 150 — the old curve floored at score 35 and
        # then never changed again for the remaining ~90% of a run.
        s = self.score
        if s < 40:
            self.tick_rate = 0.125 - (s // 5) * 0.009
        else:
            self.tick_rate = max(0.042, 0.062 - ((s - 40) // 12) * 0.002)

    def _step(self, cell, d):
        """The one place that knows about walls vs wrap. None = fatal."""
        x, y = cell[0] + d[0], cell[1] + d[1]
        if self.WRAP:
            return (x % self.GRID, y % self.GRID)
        if 0 <= x < self.GRID and 0 <= y < self.GRID:
            return (x, y)
        return None

    def _place_food(self):
        # set(), not list membership: `(x,y) not in self.body` was O(len) inside
        # a 1024-cell loop — 5.3 ms per call at length 1000, against a ~10 ms
        # budget shared with auto().
        occ = set(self.body)
        free = [(x, y) for x in range(self.GRID) for y in range(self.GRID)
                if (x, y) not in occ]
        return random.choice(free) if free else None   # None = board full = win

    def input(self, cmd):
        d = {"up": UP, "down": DOWN, "left": LEFT, "right": RIGHT}.get(cmd)
        if not d:
            return
        # Queue turns instead of keeping one pending direction. Validating
        # against the CURRENT heading meant a fast, legal two-step (going right:
        # up, then left) threw the second press away, because left still looked
        # like a reverse of right. Validate against the last QUEUED heading so
        # both turns land, one per tick — this is what makes tight play feel
        # responsive rather than dropped.
        ref = self.queue[-1] if self.queue else self.dir
        if d == ref or (d[0] == -ref[0] and d[1] == -ref[1]):
            return                       # same heading, or a 180° into itself
        if len(self.queue) < 2:          # 2 is enough to buffer a corner
            self.queue.append(d)

    def _flood(self, start, blocked, limit=None):
        # The old default limit=120 saturated on 95.4% of calls, so two
        # directions routinely tied at the cap and the tie-break went arbitrary.
        if limit is None:
            limit = self.GRID * self.GRID
        stack = [start]
        seen = {start}
        while stack and len(seen) < limit:
            cell = stack.pop()
            for d in DIRS:
                n = self._step(cell, d)
                if n and n not in blocked and n not in seen:
                    seen.add(n)
                    stack.append(n)
        return len(seen)

    def _reachable(self, start, goal, blocked):
        """Can `start` still reach `goal` around `blocked`? This is the real
        safety test: a square may go temporarily unreachable so long as it
        reopens when the tail moves."""
        if start == goal:
            return True
        q = deque([start])
        seen = {start}
        while q:
            cur = q.popleft()
            for d in DIRS:
                n = self._step(cur, d)
                if n is None or n in seen or n in blocked:
                    continue
                if n == goal:
                    return True
                seen.add(n)
                q.append(n)
        return False

    def _path_to_food(self, body_list):
        """Full BFS path head→food (not just the first step) so the whole trip
        can be simulated before we commit to it."""
        h = body_list[0]
        goal = self.food
        if goal is None:
            return None
        static = set(body_list[:-1])
        q = deque([h])
        parent = {h: None}
        while q:
            cur = q.popleft()
            if cur == goal and cur != h:
                path = []
                while parent[cur] is not None:
                    path.append(cur)
                    cur = parent[cur]
                path.reverse()
                return path
            for d in DIRS:
                n = self._step(cur, d)
                if n is None or n in parent:
                    continue
                if n in static and n != goal:
                    continue
                parent[n] = cur
                q.append(n)
        return None

    def auto(self):
        """Take the food only if, after eating it, the head can still reach the
        cell the tail will vacate. The old gate compared a truncated flood
        against len//2, which at length >=100 passed 15278/15283 = 100% of the
        time — every death measured was the snake coiling into its own dead end."""
        body = list(self.body)
        h = body[0]
        food = self.food

        def legal(d):
            if d[0] == -self.dir[0] and d[1] == -self.dir[1]:
                return False
            n = self._step(h, d)
            return n is not None and n not in set(body[:-1])

        path = self._path_to_food(body)
        if path:
            sim = list(body)
            for cell in path:
                sim.insert(0, cell)
                if cell != food:
                    sim.pop()
            step = (path[0][0] - h[0], path[0][1] - h[1])
            if self.WRAP:   # a wrapped step reads as +/-31; renormalise to a unit dir
                g = self.GRID - 1
                step = (0 if step[0] == 0 else (1 if step[0] in (1, -g) else -1),
                        0 if step[1] == 0 else (1 if step[1] in (1, -g) else -1))
            if legal(step) and self._reachable(sim[0], sim[-1], set(sim[1:-1])):
                self.queue = [step]
                return

        # No safe path to food: keep the tail reachable first, then max space.
        best, best_key = None, None
        for d in DIRS:
            if not legal(d):
                continue
            n = self._step(h, d)
            nb = [n] + body[:-1]
            safe = 1 if self._reachable(nb[0], nb[-1], set(nb[1:-1])) else 0
            space = self._flood(n, set(nb[:-1]))
            dist = (abs(n[0] - food[0]) + abs(n[1] - food[1])) if food else 0
            key = (-safe, -space, -dist, 0 if d == self.dir else 1)
            if best_key is None or key < best_key:
                best_key, best = key, d
        if best:
            self.queue = [best]

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        self.pulse += 1
        if self.queue:
            self.dir = self.queue.pop(0)

        n = self._step(self.body[0], self.dir)
        # Tail cell is free this frame if we aren't about to grow
        will_grow = n is not None and n == self.food
        blocked = set(self.body if will_grow else self.body[:-1])
        if n is None or n in blocked:
            self.death_flash = 8
            return
        self.body.insert(0, n)
        if will_grow:
            self.score += 1
            self.food = self._place_food()
            self._set_speed()
            if self.food is None:      # board full — a win, not food buried at (0,0)
                self.death_flash = 8
        else:
            self.body.pop()

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        # Soft halo + pulse on food so it pops on HUB75
        if self.food is not None:      # None only on a full-board win
            fx, fy = self.food
            glow = self.FOOD if (self.pulse // 3) % 2 == 0 else self.FOOD_GLOW
            put_cell(buf, fx, fy, self.CELL, self.DEAD if flashing else glow)
        n = max(1, len(self.body) - 1)
        for idx, (gx, gy) in enumerate(self.body):
            if flashing:
                col = self.DEAD
            elif idx == 0:
                col = self.HEAD
            else:
                col = lerp_color(self.BODY_NEAR, self.BODY_FAR, idx / n)
            put_cell(buf, gx, gy, self.CELL, col)
        return bytes(buf)


# =============================================================================
# TETRIS — 7-bag, SRS rotation + wall kicks, levels, ghost, lock delay
# =============================================================================
# Spawn orientations are the real Super Rotation System spawn states.
# Guideline-ish hues, pushed to LED neon so stack stays readable over FX
TETROMINOES = {
    "I": ([(0, 1), (1, 1), (2, 1), (3, 1)], (0, 255, 255)),
    "O": ([(1, 0), (2, 0), (1, 1), (2, 1)], (255, 240, 40)),
    "T": ([(1, 0), (0, 1), (1, 1), (2, 1)], (220, 60, 255)),
    "S": ([(1, 0), (2, 0), (0, 1), (1, 1)], (40, 255, 100)),
    "Z": ([(0, 0), (1, 0), (1, 1), (2, 1)], (255, 50, 80)),
    "J": ([(0, 0), (0, 1), (1, 1), (2, 1)], (60, 120, 255)),
    "L": ([(2, 0), (0, 1), (1, 1), (2, 1)], (255, 160, 30)),
}

# SRS rotates each piece inside a FIXED box, not inside the bounding box of its
# current cells. Using the cell bounding box (what this used to do) makes the I
# piece climb: its cells are 4x1, so rotating twice returned it one row higher
# than it started and it never came back down.
SRS_BOX = {"I": 4, "O": 2, "T": 3, "S": 3, "Z": 3, "J": 3, "L": 3}

# Wall kicks, indexed by (from_rotation, to_rotation). The guideline tables are
# written with y pointing UP; this grid has y growing DOWNWARD, so the y term is
# negated when a kick is applied. Without these, rotating against a wall or into
# the stack just fails, and T-spins are impossible.
_KICKS_JLSTZ = {
    (0, 1): ((0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)),
    (1, 0): ((0, 0), (1, 0), (1, -1), (0, 2), (1, 2)),
    (1, 2): ((0, 0), (1, 0), (1, -1), (0, 2), (1, 2)),
    (2, 1): ((0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)),
    (2, 3): ((0, 0), (1, 0), (1, 1), (0, -2), (1, -2)),
    (3, 2): ((0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)),
    (3, 0): ((0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)),
    (0, 3): ((0, 0), (1, 0), (1, 1), (0, -2), (1, -2)),
}
_KICKS_I = {
    (0, 1): ((0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)),
    (1, 0): ((0, 0), (2, 0), (-1, 0), (2, 1), (-1, -2)),
    (1, 2): ((0, 0), (-1, 0), (2, 0), (-1, 2), (2, -1)),
    (2, 1): ((0, 0), (1, 0), (-2, 0), (1, -2), (-2, 1)),
    (2, 3): ((0, 0), (2, 0), (-1, 0), (2, 1), (-1, -2)),
    (3, 2): ((0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)),
    (3, 0): ((0, 0), (1, 0), (-2, 0), (1, -2), (-2, 1)),
    (0, 3): ((0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)),
}


def rotate_cells(cells, kind, cw=True):
    """Rotate inside the piece's fixed SRS box. Pure — no engine state."""
    if kind == "O":
        return [list(c) for c in cells]
    n = SRS_BOX[kind] - 1
    if cw:
        return [[n - y, x] for x, y in cells]
    return [[y, n - x] for x, y in cells]


def srs_kicks(kind, frm, to):
    if kind == "O":
        return ((0, 0),)
    table = _KICKS_I if kind == "I" else _KICKS_JLSTZ
    return table.get((frm, to), ((0, 0),))


# Line clear points × (level+1) — modern guideline (single/double/triple/tetris).
# Matches the guideline soft-drop (1/cell) and hard-drop (2/cell) awards below;
# the old table here was the NES one, which does not belong with those.
_LINE_SCORE = (0, 100, 300, 500, 800)


class TetrisEngine:
    name = "tetris"
    COLS = 10
    ROWS = 20
    CELL = 3
    X_OFF = (WIDTH - COLS * 3) // 2
    Y_OFF = (HEIGHT - ROWS * 3) // 2

    BG = (0, 0, 0)
    FRAME = (0, 40, 70)         # deep cyan well — visible, not pure black
    GHOST = (0, 50, 60)         # dim cyan ghost, still > composite threshold
    DEAD = (255, 40, 60)
    FLASH = (255, 255, 255)

    def __init__(self):
        self.tick_rate = 0.48
        self.reset()

    def reset(self):
        self.grid = [[None] * self.COLS for _ in range(self.ROWS)]
        self.score = 0
        self.lines = 0
        self.level = 0
        self.death_flash = 0
        self.clear_flash = 0
        self.clear_rows = []
        self.lock_timer = 0
        self._plan = None
        # Guideline hold: one swap per piece, reset when a piece locks.
        self.hold = None
        self.hold_used = False
        self.bag = []
        self._refill_bag()
        self._spawn()
        self._set_gravity()

    def _refill_bag(self):
        piece = list(TETROMINOES)
        random.shuffle(piece)
        self.bag.extend(piece)

    def _set_gravity(self):
        # Level 0 slow → faster; floor keeps it playable on LED
        self.tick_rate = max(0.07, 0.48 * (0.82 ** self.level))

    def _spawn(self):
        # Needs >3 so the NEXT queue always has three pieces to show; the
        # 7-bag refill threshold already covers it, but be explicit.
        if len(self.bag) < 7:
            self._refill_bag()
        self.kind = self.bag.pop(0)
        cells, self.color = TETROMINOES[self.kind]
        self.cells = [list(c) for c in cells]
        self.rot = 0
        self.px = self.COLS // 2 - 2
        self.py = 0
        self.lock_timer = 0
        self.lock_moves = 0
        self._plan = None
        if self._collides(self.cells, self.px, self.py):
            self.death_flash = 10

    def _collides(self, cells, px, py):
        for cx, cy in cells:
            x, y = px + cx, py + cy
            if x < 0 or x >= self.COLS or y >= self.ROWS:
                return True
            if y >= 0 and self.grid[y][x] is not None:
                return True
        return False

    def _rotate(self, cells):
        return rotate_cells(cells, self.kind, cw=True)

    def _ghost_y(self):
        gy = self.py
        while not self._collides(self.cells, self.px, gy + 1):
            gy += 1
        return gy

    def input(self, cmd):
        if self.death_flash > 0 or self.clear_flash > 0:
            return
        moved = False
        if cmd == "left" and not self._collides(self.cells, self.px - 1, self.py):
            self.px -= 1
            moved = True
        elif cmd == "right" and not self._collides(self.cells, self.px + 1, self.py):
            self.px += 1
            moved = True
        elif cmd == "down":
            if not self._collides(self.cells, self.px, self.py + 1):
                self.py += 1
                self.score += 1          # soft-drop point
                moved = True
            else:
                self.lock_timer = 99     # force lock
        elif cmd in ("rotate", "up"):
            r = self._rotate(self.cells)
            to = (self.rot + 1) % 4
            for kx, ky in srs_kicks(self.kind, self.rot, to):
                # Guideline kick tables are written y-up; this grid is y-down.
                if not self._collides(r, self.px + kx, self.py - ky):
                    self.cells = r
                    self.px += kx
                    self.py -= ky
                    self.rot = to
                    moved = True
                    break
        elif cmd == "hold":
            # Guideline: one hold per piece. Swapping respawns at the top.
            if not self.hold_used:
                self.hold_used = True
                swap = self.hold
                self.hold = self.kind
                if swap is None:
                    self._spawn()
                else:
                    self.bag.insert(0, swap)
                    self._spawn()
            return
        elif cmd == "drop":
            dist = 0
            while not self._collides(self.cells, self.px, self.py + 1):
                self.py += 1
                dist += 1
            self.score += dist * 2       # hard-drop points
            self._lock()
            return
        if moved and self._collides(self.cells, self.px, self.py + 1):
            # Guideline "move reset": a move or rotation while grounded restarts
            # the lock delay, but only 15 times — otherwise a piece can be spun
            # in place forever and the game never progresses ("infinity").
            if self.lock_moves < 15:
                self.lock_moves += 1
                self.lock_timer = 0

    def _lock(self):
        self.hold_used = False
        for cx, cy in self.cells:
            x, y = self.px + cx, self.py + cy
            if 0 <= y < self.ROWS:
                self.grid[y][x] = self.color
            elif y < 0:
                self.death_flash = 10
                return
        full = [y for y in range(self.ROWS) if all(self.grid[y][x] is not None
                                                    for x in range(self.COLS))]
        if full:
            self.clear_rows = full
            self.clear_flash = 4
            n = len(full)
            self.score += _LINE_SCORE[n] * (self.level + 1)
            self.lines += n
            new_level = self.lines // 10
            if new_level != self.level:
                self.level = new_level
                self._set_gravity()
        else:
            self._spawn()

    def _finish_clear(self):
        keep = [self.grid[y] for y in range(self.ROWS) if y not in self.clear_rows]
        while len(keep) < self.ROWS:
            keep.insert(0, [None] * self.COLS)
        self.grid = keep
        self.clear_rows = []
        self._spawn()

    def _apply_cells(self, grid, cells, px, py):
        g = [row[:] for row in grid]
        for cx, cy in cells:
            y = py + cy
            if 0 <= y < self.ROWS:
                g[y][px + cx] = 1
        # clear full lines
        keep = [row for row in g if any(c is None for c in row)]
        cleared = self.ROWS - len(keep)
        while len(keep) < self.ROWS:
            keep.insert(0, [None] * self.COLS)
        return keep, cleared

    def _score_grid(self, grid, cleared=0, landing_y=0):
        heights = [0] * self.COLS
        holes = 0
        well = 0
        for x in range(self.COLS):
            seen = False
            for y in range(self.ROWS):
                if grid[y][x] is not None:
                    if not seen:
                        heights[x] = self.ROWS - y
                        seen = True
                elif seen:
                    holes += 1
        agg = sum(heights)
        bump = sum(abs(heights[i] - heights[i + 1]) for i in range(self.COLS - 1))
        # Wells: deep single-column dips (good for I-pieces later)
        for x in range(self.COLS):
            left = heights[x - 1] if x > 0 else 99
            right = heights[x + 1] if x < self.COLS - 1 else 99
            if left > heights[x] and right > heights[x]:
                well += min(left, right) - heights[x]
        max_h = max(heights) if heights else 0
        # Tuned like a strong amateur bot (Pierre Dellacherie lineage)
        return (cleared * cleared * 1.6
                + cleared * 1.4
                - 0.50 * agg
                - 0.18 * bump
                - 0.90 * holes
                - 0.25 * well
                - 0.15 * max_h
                + 0.08 * landing_y)

    def _best_placement(self):
        best, best_score = None, -1e18
        cur = [list(c) for c in self.cells]
        seen = []
        next_kind = self.bag[0] if self.bag else None
        for _ in range(4):
            key = tuple(sorted(map(tuple, cur)))
            if key in seen:
                break
            seen.append(key)
            xs = [c[0] for c in cur]
            for px in range(-min(xs), self.COLS - max(xs)):
                if self._collides(cur, px, 0):
                    continue
                py = 0
                while not self._collides(cur, px, py + 1):
                    py += 1
                g2, cleared = self._apply_cells(self.grid, cur, px, py)
                s = self._score_grid(g2, cleared, py)
                # One-ply look-ahead with next bag piece (Tetris strength leap)
                if next_kind:
                    ncells, _ = TETROMINOES[next_kind]
                    ncur = [list(c) for c in ncells]
                    best_n = -1e18
                    for __ in range(4 if next_kind != "O" else 1):
                        nxs = [c[0] for c in ncur]
                        for npx in range(-min(nxs), self.COLS - max(nxs)):
                            # collide against g2
                            def col(cells, ppx, ppy):
                                for cx, cy in cells:
                                    x, y = ppx + cx, ppy + cy
                                    if x < 0 or x >= self.COLS or y >= self.ROWS:
                                        return True
                                    if y >= 0 and g2[y][x] is not None:
                                        return True
                                return False
                            if col(ncur, npx, 0):
                                continue
                            npy = 0
                            while not col(ncur, npx, npy + 1):
                                npy += 1
                            g3, c2 = self._apply_cells(g2, ncur, npx, npy)
                            best_n = max(best_n, self._score_grid(g3, c2, npy))
                        # rotate next
                        if next_kind == "O":
                            break
                        ncur = rotate_cells(ncur, next_kind, cw=True)
                    if best_n > -1e17:
                        s = 0.65 * s + 0.35 * best_n
                if s > best_score:
                    best_score, best = s, (list(map(list, cur)), px)
            cur = self._rotate(cur)
        return best

    def auto(self):
        if self.death_flash or self.clear_flash:
            return
        if self._plan is None:
            self._plan = self._best_placement()
        if not self._plan:
            self.input("drop")
            return
        target_cells, target_px = self._plan
        if sorted(map(tuple, self.cells)) != sorted(map(tuple, target_cells)):
            self.input("rotate")
        elif self.px < target_px:
            self.input("right")
        elif self.px > target_px:
            self.input("left")
        else:
            self.input("drop")

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        if self.clear_flash > 0:
            self.clear_flash -= 1
            if self.clear_flash == 0:
                self._finish_clear()
            return
        # Gravity
        if not self._collides(self.cells, self.px, self.py + 1):
            self.py += 1
            self.lock_timer = 0
            self.lock_moves = 0          # reached a new low row -> fresh resets
        else:
            self.lock_timer += 1
            # ~lock delay: a few gravity ticks to slide/rotate before lock
            if self.lock_timer >= 2:
                self._lock()

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        for y in range(-1, self.ROWS + 1):
            put_cell(buf, -1, y, self.CELL, self.FRAME, self.X_OFF, self.Y_OFF)
            put_cell(buf, self.COLS, y, self.CELL, self.FRAME, self.X_OFF, self.Y_OFF)
        for x in range(self.COLS):
            put_cell(buf, x, self.ROWS, self.CELL, self.FRAME, self.X_OFF, self.Y_OFF)
        for y in range(self.ROWS):
            for x in range(self.COLS):
                c = self.grid[y][x]
                if not c:
                    continue
                if y in self.clear_rows and self.clear_flash:
                    col = self.FLASH if self.clear_flash % 2 else c
                else:
                    col = self.DEAD if flashing else c
                put_cell(buf, x, y, self.CELL, col, self.X_OFF, self.Y_OFF)
        if self.death_flash == 0 and self.clear_flash == 0:
            gy = self._ghost_y()
            if gy != self.py:
                for cx, cy in self.cells:
                    put_cell(buf, self.px + cx, gy + cy, self.CELL, self.GHOST,
                             self.X_OFF, self.Y_OFF)
            for cx, cy in self.cells:
                put_cell(buf, self.px + cx, self.py + cy, self.CELL, self.color,
                         self.X_OFF, self.Y_OFF)
        self._draw_hud(buf)
        return bytes(buf)

    # ------------------------------------------------------------------ HUD
    # The well is 30px wide and centred, which left 14 usable px on each side
    # doing nothing. Every guideline Tetris puts HOLD on the left and the NEXT
    # queue on the right, and those are the two things a player actually needs
    # to see to plan. Level and line count fill the rest.
    def _draw_piece(self, buf, kind, x0, y0, cell=2, dim=1.0):
        """Draw a tetromino centred in an 8x8 box at (x0, y0)."""
        cells, col = TETROMINOES[kind]
        xs = [c[0] for c in cells]
        ys = [c[1] for c in cells]
        if dim != 1.0:
            col = lerp_color(col, (0, 0, 0), 1.0 - dim)
        ox = x0 + (8 - (max(xs) - min(xs) + 1) * cell) // 2 - min(xs) * cell
        oy = y0 + (8 - (max(ys) - min(ys) + 1) * cell) // 2 - min(ys) * cell
        for cx, cy in cells:
            for dy in range(cell):
                for dx in range(cell):
                    put_px(buf, ox + cx * cell + dx, oy + cy * cell + dy, col)

    def _num_r(self, buf, val, xr, y, color):
        """Right-aligned 3x5 number ending at column xr."""
        t = str(val)
        draw_text3x5(buf, xr - (len(t) * 4 - 1), y, t, color)

    def _draw_hud(self, buf):
        LBL_HOLD = (0, 150, 200)
        LBL_NEXT = (0, 150, 200)
        LVL = (255, 210, 40)
        LNS = (200, 220, 255)
        # ---- left: HOLD ----
        for x in range(2, 12):
            put_px(buf, x, 2, LBL_HOLD)
        if self.hold:
            # Dimmed while it cannot be swapped again — the rule is invisible
            # otherwise and players burn the hold without knowing it's spent.
            self._draw_piece(buf, self.hold, 3, 5, 2,
                             0.35 if self.hold_used else 1.0)
        # ---- left: level + lines ----
        put_px(buf, 2, 40, LVL)
        put_px(buf, 3, 40, LVL)
        self._num_r(buf, self.level, 12, 42, LVL)
        put_px(buf, 2, 51, LNS)
        put_px(buf, 3, 51, LNS)
        self._num_r(buf, min(self.lines, 999), 12, 53, LNS)
        # ---- right: NEXT x3, nearest at the top and full brightness ----
        for x in range(52, 62):
            put_px(buf, x, 2, LBL_NEXT)
        for i in range(3):
            if i >= len(self.bag):
                break
            self._draw_piece(buf, self.bag[i], 53, 5 + i * 10, 2,
                             1.0 if i == 0 else (0.55 if i == 1 else 0.3))


# =============================================================================
# PONG — Atari 1972 (Alcorn) rules adapted for 64×64
#
# Canonical mechanics we take from the original:
#   • First to 11 points
#   • Paddle divided into 8 segments → discrete return angles
#     (centre = shallow, edges = steep) — not continuous trig
#   • Ball speeds up the longer a rally lasts; a miss resets speed
#   • Paddles cannot quite reach the top of the screen (classic
#     hardware quirk Alcorn kept as a skill feature)
# =============================================================================
class PongEngine:
    name = "pong"
    tick_rate = 0.020

    BG = (0, 0, 0)
    MID = (0, 60, 50)           # dashed net — teal, readable on dark FX
    BALL = (255, 255, 255)
    YOU = (50, 255, 140)        # lime paddle
    CPU = (80, 180, 255)        # sky paddle
    DEAD = (255, 50, 50)
    SCORE_YOU = (80, 255, 140)
    SCORE_CPU = (100, 180, 255)

    PADDLE_H = 16          # 8 clean 2px segments
    PADDLE_W = 2
    MARGIN = 3
    WIN = 11               # Atari Pong: race to 11
    SEGMENTS = 8
    # Base horizontal speed, px/tick, at the slow tier.
    BASE = 1.15
    # Alcorn's speed law: the hit counter steps the ball to 2x then 3x the base
    # horizontal speed at 4 and 12 hits, and a miss resets it. We had 1:1.33:1.76,
    # barely half the intended ramp — that is the main reason rallies read as flat.
    TIER_AT = (4, 12)
    TIER_MULT = (1.0, 2.0, 3.0)
    # In the original the SEGMENT sets the vertical speed and the edges are the
    # steep ones: 3,2,1,0 | 0,1,2,3 with two dead-flat centre segments. Ours was
    # 6:4.5:2.75:1 with no flat centre, so every return was a similar mid angle.
    VY_SHAPE = (-3.0, -2.0, -1.0, 0.0, 0.0, 1.0, 2.0, 3.0)
    VY_SCALE = 0.55        # scales the shape against BASE
    # Match escalation: the ball gets faster and the paddles get smaller as the
    # match goes on, so the closing points are visibly tighter than the opening.
    RAMP_PER_PT = 0.012    # + this fraction of BASE per point scored
    SHRINK_PER_PT = 0.20   # paddle px lost per point scored
    SHRINK_MIN = 10
    TOP_DEAD = 2           # classic quirk: paddles cannot reach the very top

    # Fallibility. Both paddles ran the same exact 500-step predictor and could
    # always reach the result, so neither could ever miss: measured a 2063-hit
    # rally still 0–0 after 60k ticks (~28 minutes) — the match literally could
    # not progress. Paddle reach is ±9.2px while the only error was ±0.8px of
    # aim noise, roughly 11x too small to ever matter. Real play misses because
    # you commit late and misread fast, steep balls.
    REACT_X = 34            # don't commit until the ball is this close
    # Tuned by sweep: median rally 7 hits (p90 13, max ~30), race to 11 in
    # ~1.6 min, ~11.6 points/min. Aim error is smaller than before because the
    # canonical steep angles already do the work of beating a paddle.
    # Sweep over 12 seeds x 9000 ticks. At 2.6/5.0 the median rally was 5 hits
    # and the ball reached the 12-hit (3x) tier in only 4% of ticks -- the top
    # gear was effectively dead code. At 2.0/3.5 the median rally is 13, the 3x
    # tier is live 14.5% of the time, and the sides are even (42% vs 62%).
    # 8.0 pts/min still beats the old 5.65, and rallies now build.
    AIM_ERR_BASE = 2.0      # px of misjudgement on a slow ball
    AIM_ERR_SPEED = 3.5     # extra px per unit of speed above the slow tier
    # Equal skill: at 0.85 the left paddle won 83% of attract matches, so the
    # same side won five matches in six. Measured 46% at parity.
    SKILL_YOU = 1.0
    SKILL_CPU = 1.0

    def __init__(self):
        self.reset()

    def reset(self):
        self.score = 0
        self.you_pts = 0
        self.cpu_pts = 0
        self.y_you = float(HEIGHT // 2)
        self.y_cpu = float(HEIGHT // 2)
        self.death_flash = 0
        self.loser = 0         # 1 = you lost, 2 = cpu lost; drives the end flash
        self.serve_wait = 12
        self.rally = 0
        self.hits = 0          # consecutive paddle contacts this rally
        self._serve(to_right=random.choice([True, False]))

    def _base(self):
        """Base horizontal speed, escalating with match progress."""
        return self.BASE * (1.0 + self.RAMP_PER_PT * (self.you_pts + self.cpu_pts))

    def _ph(self):
        """Paddle height, shrinking with match progress."""
        h = self.PADDLE_H - self.SHRINK_PER_PT * (self.you_pts + self.cpu_pts)
        return max(self.SHRINK_MIN, h)

    def _seg_vy(self, seg):
        return self.VY_SHAPE[seg] * self.VY_SCALE * self._base()

    def _hx(self):
        """Current horizontal speed from hit tier (Alcorn 1x / 2x / 3x)."""
        b = self._base()
        if self.hits < self.TIER_AT[0]:
            return b * self.TIER_MULT[0]
        if self.hits < self.TIER_AT[1]:
            return b * self.TIER_MULT[1]
        return b * self.TIER_MULT[2]

    def _serve(self, to_right=True):
        self.bx = float(WIDTH // 2)
        self.by = float(HEIGHT // 2)
        self.hits = 0
        self.bvx = self._base() * self.TIER_MULT[0] * (1 if to_right else -1)
        # Serve with a mild random segment angle (not full edge)
        seg = random.randint(2, 5)
        self.bvy = self._seg_vy(seg) * 0.55
        if self.bvy == 0.0:                     # centre segments are dead flat
            self.bvy = random.choice([-1, 1]) * 0.25 * self._base()
        self.serve_wait = 12
        self.rally = 0
        self._roll_aim_error()

    def _paddle_bounds(self, y):
        half = self._ph() / 2
        # Classic quirk: paddles cannot reach the top of the screen
        return clamp(y, half + self.TOP_DEAD, HEIGHT - 1 - half)

    def input(self, cmd):
        if cmd == "up":
            self.y_you = self._paddle_bounds(self.y_you - 2.6)
        elif cmd == "down":
            self.y_you = self._paddle_bounds(self.y_you + 2.6)

    def _predict_intercept(self, x_goal, side_right=True):
        bx, by = float(self.bx), float(self.by)
        bvx, bvy = float(self.bvx), float(self.bvy)
        if self.serve_wait:
            return HEIGHT / 2
        if side_right and bvx <= 0:
            return HEIGHT / 2
        if not side_right and bvx >= 0:
            return HEIGHT / 2
        for _ in range(500):
            bx += bvx
            by += bvy
            if by < 1:
                by, bvy = 1, abs(bvy)
            elif by > HEIGHT - 2:
                by, bvy = HEIGHT - 2, -abs(bvy)
            if side_right and bx >= x_goal:
                return by
            if not side_right and bx <= x_goal:
                return by
        return by

    def _steer_paddle(self, y, target, max_step=2.5, gain=0.55):
        err = target - y
        return self._paddle_bounds(y + clamp(err * gain, -max_step, max_step))

    def _roll_aim_error(self):
        """Re-roll each side's misjudgement for this leg of the rally.

        Rolled once when the ball turns around, not per tick: a player misreads
        a shot and commits to it — per-frame noise would just average out to
        perfect tracking, which is what made rallies endless.
        """
        spd = abs(self.bvx) + abs(self.bvy) * 0.6
        slow = self.BASE * self.TIER_MULT[0]
        scale = (self.AIM_ERR_BASE
                 + self.AIM_ERR_SPEED * max(0.0, spd - slow))
        self._err_you = random.uniform(-scale, scale) * self.SKILL_YOU
        self._err_cpu = random.uniform(-scale, scale) * self.SKILL_CPU

    def _aim(self, x_goal, side_right, err):
        """Where this paddle believes the ball is going."""
        if self.serve_wait:
            return HEIGHT / 2
        incoming = (self.bvx > 0) if side_right else (self.bvx < 0)
        if not incoming:
            return HEIGHT / 2                  # ball leaving — recover to centre
        if abs(self.bx - x_goal) > self.REACT_X:
            return HEIGHT / 2                  # too early to commit
        return self._predict_intercept(x_goal, side_right=side_right) + err

    def auto(self):
        x_goal = self.MARGIN + self.PADDLE_W + 0.5
        target = self._aim(x_goal, False, getattr(self, "_err_you", 0.0))
        self.y_you = self._steer_paddle(self.y_you, target, max_step=2.7, gain=0.62)

    def _move_cpu(self):
        x_goal = WIDTH - self.MARGIN - self.PADDLE_W - 0.5
        target = self._aim(x_goal, True, getattr(self, "_err_cpu", 0.0))
        self.y_cpu = self._steer_paddle(self.y_cpu, target, max_step=2.7, gain=0.62)

    def _bounce_paddle(self, paddle_y, going_right):
        """Alcorn 8-segment paddle: discrete angle + rally speed tier."""
        ph = self._ph()
        half = ph / 2
        # Map ball Y onto paddle → segment 0..7
        rel = (self.by - (paddle_y - half)) / max(1e-6, ph)
        seg = int(clamp(rel * self.SEGMENTS, 0, self.SEGMENTS - 1))
        self.hits += 1
        self.rally += 1
        sp = self._hx()
        self.bvx = sp * (1 if going_right else -1)
        self.bvy = self._seg_vy(seg)
        # Tiny noise so returns aren't pixel-identical forever
        self.bvy += random.uniform(-0.04, 0.04)
        self._roll_aim_error()      # new leg, new misread
        # NB: no + self.rally — that made .score fall when a rally ended.
        self.score = self.you_pts * 100 + self.cpu_pts

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        self._move_cpu()
        if self.serve_wait > 0:
            self.serve_wait -= 1
            return
        # Top tier is ~3.45 px/tick; substep so the ball cannot cross the
        # paddle plane inside a single integration step.
        steps = max(1, int(abs(self.bvx)) + 1)
        for _ in range(steps):
            if self._advance(1.0 / steps):
                return

    def _advance(self, f):
        self.bx += self.bvx * f
        self.by += self.bvy * f

        if self.by < 1:
            self.by = 1
            self.bvy = abs(self.bvy)
        elif self.by > HEIGHT - 2:
            self.by = HEIGHT - 2
            self.bvy = -abs(self.bvy)

        half = self._ph() / 2
        left_x = self.MARGIN + self.PADDLE_W
        right_x = WIDTH - self.MARGIN - self.PADDLE_W

        if self.bx <= left_x + 0.5 and self.bvx < 0:
            if abs(self.by - self.y_you) <= half + 1.2:
                self.bx = left_x + 0.5
                self._bounce_paddle(self.y_you, going_right=True)
            elif self.bx < self.MARGIN:
                self.cpu_pts += 1
                self.score = self.you_pts * 100 + self.cpu_pts
                if self.cpu_pts >= self.WIN:
                    self.death_flash = 16
                    self.loser = 1               # you lost
                else:
                    self._serve(to_right=True)   # speed resets on miss
                return True

        if self.bx >= right_x - 0.5 and self.bvx > 0:
            if abs(self.by - self.y_cpu) <= half + 1.2:
                self.bx = right_x - 0.5
                self._bounce_paddle(self.y_cpu, going_right=False)
            elif self.bx > WIDTH - self.MARGIN:
                self.you_pts += 1
                self.score = self.you_pts * 100 + self.cpu_pts
                if self.you_pts >= self.WIN:
                    self.death_flash = 16
                    self.loser = 2               # cpu lost
                else:
                    self._serve(to_right=False)
                return True
        return False

    def _paddle(self, buf, x, cy, color):
        half = int(self._ph()) // 2
        for dy in range(-half, half + 1):
            for dx in range(self.PADDLE_W):
                put_px(buf, x + dx, int(cy) + dy, color)

    def _score_pips(self, buf, pts, x0, color):
        # Two rows of pips so 11 still fits
        for i in range(min(pts, self.WIN)):
            px = x0 + (i % 6) * 3
            py = 2 if i < 6 else 5
            put_px(buf, px, py, color)
            put_px(buf, px + 1, py, color)

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        for y in range(2, HEIGHT - 1, 3):
            put_px(buf, WIDTH // 2, y, self.MID)
        self._score_pips(buf, self.you_pts, 6, self.SCORE_YOU)
        self._score_pips(buf, self.cpu_pts, WIDTH // 2 + 6, self.SCORE_CPU)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        # Only the LOSER flashes red. This was hard-coded to the left paddle,
        # so winning a match lit up your own paddle in the death colour --
        # the same bug Tron had, and with no text on a 64x64 panel the flash
        # is the only thing that tells you who won.
        you_c = (self.DEAD if self.loser == 1 else self.YOU) if flashing else self.YOU
        cpu_c = (self.DEAD if self.loser == 2 else self.CPU) if flashing else self.CPU
        self._paddle(buf, self.MARGIN, self.y_you, you_c)
        self._paddle(buf, WIDTH - self.MARGIN - self.PADDLE_W, self.y_cpu, cpu_c)
        if self.death_flash == 0:
            bx, by = int(self.bx), int(self.by)
            put_blob(buf, bx, by, self.BALL, outline=True)
            put_px(buf, bx, by, self.BALL)
            put_px(buf, bx + 1, by, self.BALL)
        return bytes(buf)


# =============================================================================
# BREAKOUT — lives, serve-on-paddle, row scoring, multi-hit top, levels
# =============================================================================
class BreakoutEngine:
    name = "breakout"
    tick_rate = 0.028

    # Canonical Atari field: 8 rows x 14 bricks.
    COLS, ROWS = 14, 8
    BRICK_W, BRICK_H = 4, 3
    X_OFF = 4
    Y_OFF = 4
    BG = (0, 0, 0)
    PADDLE = (240, 250, 255)
    BALL = (255, 255, 255)
    DEAD = (255, 40, 60)
    LIFE = (255, 80, 120)
    SILVER = (190, 200, 215)
    GOLD = (120, 92, 12)      # dark bronze: must NOT read as the yellow rows
    # Canonical row colours + points, two rows each: red 7, orange 5, green 3, yellow 1.
    ROW_STYLE = [
        ((255, 45, 55), 7), ((255, 45, 55), 7),
        ((255, 130, 15), 5), ((255, 130, 15), 5),
        ((40, 235, 80), 3), ((40, 235, 80), 3),
        ((255, 225, 30), 1), ((255, 225, 30), 1),
    ]
    SEG_VX = (-1.15, -0.85, -0.5, -0.2, 0.2, 0.5, 0.85, 1.15)

    # Speed law (Atari): base + one gear per speed-up event.
    SPEED_BASE = 1.15
    SPEED_STEP = 0.26
    SPEED_MAX = 2.60
    PADDLE_W = 12
    PADDLE_W_HALF = 6

    # ---- power-up capsules -------------------------------------------------
    # kind: (colour, good?, label)
    CAP_MULTI = 0
    CAP_EXPAND = 1
    CAP_LASER = 2
    CAP_SHRINK = 3
    CAP_FAST = 4
    CAP_STYLE = (
        ((0, 230, 255), True),    # multiball  - cyan
        ((60, 120, 255), True),   # expand     - blue
        ((255, 40, 40), True),    # laser      - red
        ((255, 60, 220), False),  # shrink     - magenta
        ((255, 140, 0), False),   # fast ball  - orange
    )
    CAP_WEIGHTS = (30, 25, 15, 18, 12)   # 70% good / 30% bad
    CAP_DROP_P = 0.12                    # per destroyed brick
    CAP_FALL = 0.50                      # px/tick: 4-5x slower than the ball
    LASER_TICKS = 600                    # ~17s at tick_rate 0.028
    LASER_COOLDOWN = 14

    def __init__(self):
        self.reset()

    # ---------------------------------------------------------------- setup
    def reset(self):
        self.level = 1
        self.lives = 3
        self.score = 0
        self.death_flash = 0
        self._new_board()
        self._serve_ball()

    def _silver_hp(self):
        # Arkanoid: silver takes 2 hits, +1 every 8 stages.
        return min(4, 2 + (self.level - 1) // 8)

    def _new_board(self):
        self.gear = 0
        self.hits = 0
        self.hit_orange = False
        self.hit_red = False
        self.halved = False
        silver_rows = 0 if self.level == 1 else min(2, self.level // 2)
        gold_n = 0 if self.level < 3 else min(6, 2 * (self.level - 2))
        self.bricks = []
        for r in range(self.ROWS):
            col, pts = self.ROW_STYLE[r % len(self.ROW_STYLE)]
            row = []
            for _ in range(self.COLS):
                if r < silver_rows:
                    row.append([self._silver_hp(), 12, self.SILVER, 1])
                else:
                    row.append([1, pts, col, 0])
            self.bricks.append(row)
        # Gold (indestructible) never in the top two rows: the field must stay
        # tunnel-able and gold up top would wall off the ceiling permanently.
        spots = [(r, c) for r in range(2, self.ROWS) for c in range(self.COLS)]
        random.shuffle(spots)
        for r, c in spots[:gold_n]:
            self.bricks[r][c] = [99, 0, self.GOLD, 2]
        self.remaining = sum(1 for row in self.bricks for b in row
                             if b and b[3] != 2)
        self.caps = []       # at most one in flight (Arkanoid rule)
        self.bolts = []
        self.laser_t = 0
        self.laser_cd = 0
        self.expand = False
        self.shrunk = False

    def _apply_paddle(self):
        w = self.PADDLE_W_HALF if self.halved else self.PADDLE_W
        if self.expand:
            w += 5
        if self.shrunk:
            w = max(4, w // 2)
        self.pw = w
        self.px = int(clamp(self.px, self.pw // 2 + 1, WIDTH - 2 - self.pw // 2))

    def _serve_ball(self):
        self.px = WIDTH // 2
        self.expand = False
        self.shrunk = False
        self.laser_t = 0
        self.caps = []
        self.bolts = []
        self._apply_paddle()
        self.attached = True
        sp = self._base_speed()
        self.balls = [[float(self.px), float(HEIGHT - 7),
                       random.choice([-1, 1]) * sp * 0.65, -sp]]

    # ------------------------------------------------------------- physics
    def _base_speed(self):
        return min(self.SPEED_MAX,
                   self.SPEED_BASE + min(self.level - 1, 4) * 0.06
                   + self.gear * self.SPEED_STEP)

    def _bump_gear(self, r):
        """Atari speed-up law: after the 4th hit, the 12th hit, and on first
        contact with the orange rows and with the red rows."""
        old = self._base_speed()
        self.hits += 1
        if self.hits in (4, 12):
            self.gear += 1
        if r in (2, 3) and not self.hit_orange:
            self.hit_orange = True
            self.gear += 1
        if r in (0, 1) and not self.hit_red:
            self.hit_red = True
            self.gear += 1
        new = self._base_speed()
        if new != old and old > 0:
            k = new / old
            for b in self.balls:
                b[2] *= k
                b[3] *= k

    def input(self, cmd):
        if cmd == "left":
            self.px = max(self.pw // 2 + 1, self.px - 3)
        elif cmd == "right":
            self.px = min(WIDTH - 2 - self.pw // 2, self.px + 3)
        elif cmd in ("up", "rotate", "drop"):
            if self.attached:
                self.attached = False
                self.balls[0][3] = -abs(self.balls[0][3])
            elif self.laser_t > 0:
                self._fire()
        if self.attached:
            self.balls[0][0] = float(self.px)

    def _fire(self):
        if self.laser_cd > 0:
            return
        self.laser_cd = self.LASER_COOLDOWN
        self.bolts.append([self.px - self.pw // 2 + 1, HEIGHT - 6])
        self.bolts.append([self.px + self.pw // 2 - 1, HEIGHT - 6])

    def _brick_rect(self, c, r):
        x0 = self.X_OFF + c * self.BRICK_W
        y0 = self.Y_OFF + r * self.BRICK_H
        return x0, y0, x0 + self.BRICK_W - 2, y0 + self.BRICK_H - 2

    def _brick_at(self, x, y):
        cx = x - self.X_OFF
        cy = y - self.Y_OFF
        if cx < 0 or cy < 0:
            return None
        c, r = cx // self.BRICK_W, cy // self.BRICK_H
        if 0 <= r < self.ROWS and 0 <= c < self.COLS and self.bricks[r][c]:
            return c, r
        return None

    def _maybe_drop(self, c, r):
        if self.caps:
            return                        # Arkanoid: one capsule at a time
        if random.random() >= self.CAP_DROP_P:
            return
        kind = random.choices(range(5), weights=self.CAP_WEIGHTS)[0]
        x0 = self.X_OFF + c * self.BRICK_W
        y0 = self.Y_OFF + r * self.BRICK_H
        self.caps.append([float(x0), float(y0), kind])

    def _damage(self, c, r, laser=False):
        b = self.bricks[r][c]
        if b[3] == 2:
            return False                  # gold: indestructible
        b[0] -= 1
        if b[0] <= 0:
            self.score += b[1] * self.level
            self.bricks[r][c] = None
            self.remaining -= 1
            self._maybe_drop(c, r)
        else:
            b[2] = lerp_color(b[2], (255, 255, 255), 0.35)
            self.score += 1
        if not laser:
            self._bump_gear(r)
        return True

    def _sweep_bricks(self, ball, prev_x, prev_y):
        dx, dy = ball[0] - prev_x, ball[1] - prev_y
        steps = max(1, int(max(abs(dx), abs(dy)) * 2) + 1)
        for s in range(1, steps + 1):
            t = s / steps
            x, y = prev_x + dx * t, prev_y + dy * t
            ix, iy = int(x), int(y)
            hit = self._brick_at(ix, iy)
            if not hit:
                continue
            c, r = hit
            self._damage(c, r)
            came_vertical = self._brick_at(int(prev_x), iy) == (c, r) or dy == 0
            came_horizontal = self._brick_at(ix, int(prev_y)) == (c, r) or dx == 0
            if came_vertical and not came_horizontal:
                ball[3] = -ball[3]
            elif came_horizontal and not came_vertical:
                ball[2] = -ball[2]
            else:
                ball[3] = -ball[3]
            ball[0] = x - dx * (1.0 / steps)
            ball[1] = y - dy * (1.0 / steps)
            return True
        return False

    # ------------------------------------------------------------- capsules
    def _collect(self, kind):
        self.score += 20 * self.level
        if kind == self.CAP_MULTI:
            extra = []
            for b in self.balls[:3]:
                sp = (b[2] ** 2 + b[3] ** 2) ** 0.5 or self._base_speed()
                for ang in (-0.45, 0.45):
                    vx = b[2] * 0.85 + ang * sp
                    vy = -abs(b[3]) if b[3] < 0 else b[3]
                    n = (vx * vx + vy * vy) ** 0.5 or 1.0
                    extra.append([b[0], b[1], vx / n * sp, vy / n * sp])
            self.balls.extend(extra[:6])
            del self.balls[8:]
        elif kind == self.CAP_EXPAND:
            self.expand = True
            self.shrunk = False
            self._apply_paddle()
        elif kind == self.CAP_LASER:
            self.laser_t = self.LASER_TICKS
        elif kind == self.CAP_SHRINK:
            self.shrunk = True
            self.expand = False
            self._apply_paddle()
        elif kind == self.CAP_FAST:
            self.gear += 2                # DX-Ball "Fast Ball": jump to top speed
            new = self._base_speed()
            for b in self.balls:
                n = (b[2] ** 2 + b[3] ** 2) ** 0.5 or 1.0
                b[2] = b[2] / n * new
                b[3] = b[3] / n * new

    def _step_caps(self):
        half = self.pw / 2
        for cap in self.caps[:]:
            cap[1] += self.CAP_FALL
            if cap[1] >= HEIGHT - 5 and cap[1] <= HEIGHT - 2:
                cx = cap[0] + 1.5
                if abs(cx - self.px) <= half + 1.5:
                    self._collect(cap[2])
                    self.caps.remove(cap)
                    continue
            if cap[1] > HEIGHT:
                self.caps.remove(cap)

    def _step_bolts(self):
        for bo in self.bolts[:]:
            bo[1] -= 2
            if bo[1] < 1:
                self.bolts.remove(bo)
                continue
            hit = self._brick_at(bo[0], int(bo[1]))
            if hit:
                if self._damage(hit[0], hit[1], laser=True):
                    self.bolts.remove(bo)

    # ------------------------------------------------------------------- AI
    def _sim_ball_to_paddle(self, ball):
        bx, by = float(ball[0]), float(ball[1])
        bvx, bvy = float(ball[2]), float(ball[3])
        solid = set()
        for r in range(self.ROWS):
            for c in range(self.COLS):
                if self.bricks[r][c]:
                    x0 = self.X_OFF + c * self.BRICK_W
                    y0 = self.Y_OFF + r * self.BRICK_H
                    for yy in range(y0, y0 + self.BRICK_H):
                        for xx in range(x0, x0 + self.BRICK_W):
                            solid.add((xx, yy))
        paddle_y = HEIGHT - 5
        for t in range(500):
            pbx, pby = bx, by
            bx += bvx
            by += bvy
            if bx < 1:
                bx, bvx = 1, abs(bvx)
            elif bx > WIDTH - 2:
                bx, bvx = WIDTH - 2, -abs(bvx)
            if by < 1:
                by, bvy = 1, abs(bvy)
            if by >= paddle_y and bvy > 0:
                return bx, t
            # Sweep the step, matching _sweep_bricks; endpoint-only sampling
            # flies straight through bricks at gear speed and the predicted
            # landing point is then wrong by half the board.
            dx, dy = bx - pbx, by - pby
            ns = max(1, int(max(abs(dx), abs(dy)) * 2) + 1)
            for k in range(1, ns + 1):
                f = k / ns
                sx, sy = pbx + dx * f, pby + dy * f
                ix, iy = int(sx), int(sy)
                if (ix, iy) in solid:
                    if int(pbx) != ix:
                        bvx = -bvx
                    else:
                        bvy = -bvy
                    solid.discard((ix, iy))
                    bx, by = sx - dx / ns, sy - dy / ns
                    break
        return bx, 500

    def _swept_hit(self, x0, y0, x1, y1):
        """First brick on the segment (x0,y0)->(x1,y1), sampled exactly the way
        _sweep_bricks samples it. Predicting with end-of-tick sampling instead
        makes the AI blind to every brick the ball flies over in one step,
        which at gear-4 speed (>2px/tick against 3px-tall bricks) is most of
        them — that is what stranded the board on 4 bricks for 4700 ticks."""
        dx, dy = x1 - x0, y1 - y0
        steps = max(1, int(max(abs(dx), abs(dy)) * 2) + 1)
        for s in range(1, steps + 1):
            t = s / steps
            hit = self._brick_at(int(x0 + dx * t), int(y0 + dy * t))
            if hit:
                return hit
        return None

    def _best_return_segment(self, ix):
        base = self._base_speed()
        best_t = 1 << 30
        ties = []
        for s, kx in enumerate(self.SEG_VX):
            bx, by = float(ix), float(HEIGHT - 6)
            bvx, bvy = kx * base * 0.85, -abs(base)
            for t in range(200):
                px_, py_ = bx, by
                bx += bvx
                by += bvy
                if bx < 1:
                    bx, bvx = 1, abs(bvx)
                elif bx > WIDTH - 2:
                    bx, bvx = WIDTH - 2, -abs(bvx)
                if by < 1:
                    by, bvy = 1, abs(bvy)
                if by >= HEIGHT - 5 and bvy > 0:
                    break
                hit = self._swept_hit(px_, py_, bx, by)
                if hit:
                    if self.bricks[hit[1]][hit[0]][3] == 2:
                        break             # gold: bounces, never scores
                    if t < best_t:
                        best_t, ties = t, [s]
                    elif t == best_t:
                        ties.append(s)
                    break
        if ties:
            return random.choice(ties)
        return random.randrange(len(self.SEG_VX))

    def auto(self):
        if self.attached:
            self.attached = False
            counts = [0] * self.COLS
            for r in range(self.ROWS):
                for c in range(self.COLS):
                    b = self.bricks[r][c]
                    if b and b[3] != 2:
                        counts[c] += b[1]
            if any(counts):
                best_c = max(range(self.COLS), key=lambda c: counts[c])
                aim = self.X_OFF + best_c * self.BRICK_W + 2
                self.balls[0][2] = 0.7 if aim >= self.px else -0.7
            return

        # Fire the laser whenever a destructible brick sits over the paddle.
        if self.laser_t > 0 and self.laser_cd == 0:
            for r in range(self.ROWS - 1, -1, -1):
                h = self._brick_at(self.px, self.Y_OFF + r * self.BRICK_H)
                if h and self.bricks[h[1]][h[0]][3] != 2:
                    self._fire()
                    break

        # Intercept the ball that lands soonest.
        target = None
        eta = 1 << 30
        for b in self.balls:
            if b[3] > 0 or b[1] > HEIGHT * 0.45:
                tx, tt = self._sim_ball_to_paddle(b)
                if tt < eta:
                    eta, target = tt, tx
        if target is not None:
            seg = self._best_return_segment(target)
            rel = (seg + 0.5) / 8.0
            target = target + (self.pw / 2.0) - rel * self.pw
        else:
            best = self.px
            best_s = -1
            for r in range(self.ROWS):
                for c in range(self.COLS):
                    b = self.bricks[r][c]
                    if not b or b[3] == 2:
                        continue
                    s = b[1] * (self.ROWS - r)
                    if s > best_s:
                        best_s = s
                        best = self.X_OFF + c * self.BRICK_W + 2
            lead = self.balls[0][0] if self.balls else self.px
            target = 0.55 * lead + 0.45 * best
            eta = 1 << 30

        # Capsules: chase a good one, dodge a bad one — but only in the slack
        # time before the ball needs the paddle back.
        if self.caps:
            cx, cy, kind = self.caps[0]
            good = self.CAP_STYLE[kind][1]
            ceta = (HEIGHT - 4 - cy) / self.CAP_FALL
            if good:
                if ceta < eta - 6:
                    target = cx + 1.5
            else:
                if ceta < 26 and abs(cx + 1.5 - target) < self.pw / 2 + 3:
                    target = cx + 1.5 + (self.pw + 8 if cx < WIDTH / 2 else -self.pw - 8)

        err = target - self.px
        step = 3 if abs(err) > 6 else 2
        self.px = int(clamp(self.px + clamp(err, -step, step),
                            self.pw // 2 + 1, WIDTH - 2 - self.pw // 2))

    # ----------------------------------------------------------------- tick
    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        if self.remaining == 0:
            self.level += 1
            self.score += 50 * self.level
            self._new_board()
            self._serve_ball()
            return

        if self.laser_t > 0:
            self.laser_t -= 1
        if self.laser_cd > 0:
            self.laser_cd -= 1
        self._step_caps()
        self._step_bolts()

        if self.attached:
            self.balls[0][0] = float(self.px)
            self.balls[0][1] = float(HEIGHT - 7)
            return

        half = self.pw / 2
        for ball in self.balls[:]:
            prev_x, prev_y = ball[0], ball[1]
            ball[0] += ball[2]
            ball[1] += ball[3]

            if ball[0] < 1:
                ball[0] = 1
                ball[2] = abs(ball[2])
            elif ball[0] > WIDTH - 2:
                ball[0] = WIDTH - 2
                ball[2] = -abs(ball[2])
            if ball[1] < 1:
                ball[1] = 1
                ball[3] = abs(ball[3])
                # Atari: paddle halves once the ball reaches the top wall.
                if not self.halved:
                    self.halved = True
                    self._apply_paddle()
                    half = self.pw / 2

            if ball[3] > 0 and ball[1] >= HEIGHT - 5:
                if abs(ball[0] - self.px) <= half + 1.5:
                    rel = clamp((ball[0] - (self.px - half)) / max(1e-6, self.pw),
                                0.0, 0.999)
                    seg = int(rel * 8)
                    base = self._base_speed()
                    ball[2] = self.SEG_VX[seg] * base * 0.85
                    ball[3] = -abs(base)
                    ball[1] = HEIGHT - 6
                elif ball[1] > HEIGHT - 1:
                    self.balls.remove(ball)
                    continue

            if self._sweep_bricks(ball, prev_x, prev_y):
                continue
            if abs(ball[3]) < 0.35:
                ball[3] = -0.55 if ball[3] <= 0 else 0.55

        if not self.balls:
            self.lives -= 1
            if self.lives <= 0:
                self.death_flash = 12
            else:
                self._serve_ball()

    # ---------------------------------------------------------------- frame
    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        for r in range(self.ROWS):
            for c in range(self.COLS):
                b = self.bricks[r][c]
                if not b:
                    continue
                x0, y0, x1, y1 = self._brick_rect(c, r)
                col = self.DEAD if flashing else b[2]
                for y in range(y0, y1 + 1):
                    for x in range(x0, x1 + 1):
                        put_px(buf, x, y, col)
                if b[3] == 2 and not flashing:
                    # metallic highlight on the top edge — the dark body plus a
                    # bright rim is what separates gold from the yellow rows.
                    for x in range(x0, x1 + 1):
                        put_px(buf, x, y0, (255, 226, 150))
        for i in range(self.lives):
            put_px(buf, 2 + i * 3, HEIGHT - 1, self.LIFE)
            put_px(buf, 3 + i * 3, HEIGHT - 1, self.LIFE)
        # capsule: 4x2 pill. Good = solid with a bright core, bad = hollow ring.
        for cx, cy, kind in self.caps:
            col, good = self.CAP_STYLE[kind]
            dim = lerp_color(col, (0, 0, 0), 0.45)
            ix, iy = int(cx), int(cy)
            if good:
                for dx in range(4):
                    put_px(buf, ix + dx, iy, col)
                    put_px(buf, ix + dx, iy + 1, dim)
                put_px(buf, ix + 1, iy, (255, 255, 255))
                put_px(buf, ix + 2, iy, (255, 255, 255))
            else:
                put_px(buf, ix, iy, col)
                put_px(buf, ix + 3, iy, col)
                put_px(buf, ix, iy + 1, col)
                put_px(buf, ix + 3, iy + 1, col)
                put_px(buf, ix + 1, iy + 1, dim)
                put_px(buf, ix + 2, iy + 1, dim)
        for bo in self.bolts:
            put_px(buf, bo[0], int(bo[1]), (255, 90, 90))
            put_px(buf, bo[0], int(bo[1]) + 1, (150, 30, 30))
        half = self.pw // 2
        pc = self.DEAD if flashing else self.PADDLE
        for x in range(self.px - half, self.px + half + 1):
            for y in range(HEIGHT - 4, HEIGHT - 2):
                put_px(buf, x, y, pc)
        if self.laser_t > 0 and not flashing:
            put_px(buf, self.px - half, HEIGHT - 5, (255, 60, 60))
            put_px(buf, self.px + half, HEIGHT - 5, (255, 60, 60))
        if self.death_flash == 0:
            for b in self.balls:
                bx, by = int(b[0]), int(b[1])
                put_px(buf, bx, by, self.BALL)
                put_px(buf, bx + 1, by, self.BALL)
                put_px(buf, bx, by + 1, self.BALL)
                put_px(buf, bx + 1, by + 1, self.BALL)
        return bytes(buf)


# =============================================================================
# TRON — competitive light-cycle duel with persistent learning brain
# =============================================================================
class _TronBrain:
    """Shared evaluation weights; improves from match outcomes (persisted)."""
    PATH = Path(__file__).resolve().parent / "tron_brain.json"
    KEYS = (
        "space", "look", "voronoi", "mobility", "head_on", "cut",
        "straight", "corridor", "wall_hug", "dist", "low_space",
        "opp_space", "articulation",
    )
    DEFAULT = {
        "space": 2.8,
        "look": 1.4,
        "voronoi": 0.62,
        "mobility": 6.5,
        "head_on": -48.0,
        "cut": 72.0,
        "straight": 1.8,
        "corridor": -10.0,
        "wall_hug": 3.5,
        "dist": -0.22,
        "low_space": -95.0,
        "opp_space": -1.35,
        "articulation": 4.0,
        "games": 0,
        "wins_you": 0,
        "wins_cpu": 0,
        "draws": 0,
    }

    def __init__(self):
        self.w = dict(self.DEFAULT)
        self._load()

    def _load(self):
        try:
            if self.PATH.is_file():
                data = json.loads(self.PATH.read_text())
                for k in self.KEYS:
                    if k in data:
                        self.w[k] = float(data[k])
                for k in ("games", "wins_you", "wins_cpu", "draws"):
                    if k in data:
                        self.w[k] = int(data[k])
        except Exception:
            pass

    def save(self):
        try:
            self.PATH.write_text(json.dumps(self.w, indent=2, sort_keys=True))
        except Exception:
            pass

    def clamp(self):
        bounds = {
            "space": (0.5, 6.0),
            "look": (0.2, 4.0),
            "voronoi": (0.05, 2.0),
            "mobility": (1.0, 14.0),
            "head_on": (-90.0, -5.0),
            "cut": (10.0, 140.0),
            "straight": (0.0, 5.0),
            "corridor": (-30.0, 0.0),
            "wall_hug": (0.0, 12.0),
            "dist": (-1.0, 0.5),
            "low_space": (-200.0, -20.0),
            "opp_space": (-4.0, -0.2),
            "articulation": (0.0, 12.0),
        }
        for k, (lo, hi) in bounds.items():
            self.w[k] = lo if self.w[k] < lo else hi if self.w[k] > hi else self.w[k]

    def learn(self, move_log, winner):
        """
        winner: 1=cyan(you), 2=magenta(cpu), 0=draw.
        Reinforce feature averages of the winner; punish the loser.
        """
        if not move_log:
            return
        games = int(self.w.get("games", 0))
        lr = 0.055 / (1.0 + games * 0.015)
        buckets = {1: [], 2: []}
        for side, feats in move_log:
            buckets.setdefault(side, []).append(feats)

        def avg(feats_list):
            if not feats_list:
                return {}
            out = {}
            for f in feats_list:
                for k, v in f.items():
                    out[k] = out.get(k, 0.0) + v
            n = float(len(feats_list))
            return {k: v / n for k, v in out.items()}

        a1, a2 = avg(buckets.get(1, [])), avg(buckets.get(2, []))
        if winner == 1:
            self.w["wins_you"] = int(self.w.get("wins_you", 0)) + 1
            sign = {1: 1.0, 2: -1.0}
        elif winner == 2:
            self.w["wins_cpu"] = int(self.w.get("wins_cpu", 0)) + 1
            sign = {1: -1.0, 2: 1.0}
        else:
            self.w["draws"] = int(self.w.get("draws", 0)) + 1
            sign = {1: -0.2, 2: -0.2}

        for side, a in ((1, a1), (2, a2)):
            if not a:
                continue
            s = sign[side]
            for k in self.KEYS:
                if k not in a:
                    continue
                # normalize feature magnitude so big floods don't dominate
                v = a[k]
                nv = v / (1.0 + abs(v) * 0.02)
                self.w[k] = float(self.w[k]) + lr * s * nv

        self.w["games"] = games + 1
        self.clamp()
        if (games + 1) % 2 == 0:
            self.save()


# Module-level brain survives engine resets within a process
_TRON_BRAIN = None

def _tron_brain():
    global _TRON_BRAIN
    if _TRON_BRAIN is None:
        _TRON_BRAIN = _TronBrain()
    return _TRON_BRAIN


class TronEngine:
    """
    Competitive light-cycle duel:
      * Fixed classic spawn (left vs right, centre Y, face each other).
      * Deep evaluation + adversarial 2-ply for both bikes.
      * Shared learning brain that updates every round and persists to disk.
    """
    name = "tron"
    tick_rate = 0.055

    BG = (0, 0, 0)
    WALL = (0, 55, 80)
    YOU = (0, 255, 220)
    YOU_HEAD = (220, 255, 255)
    CPU = (255, 40, 140)
    CPU_HEAD = (255, 200, 230)
    DEAD = (255, 50, 50)

    SPAWN_PAD = 3

    def __init__(self):
        self.brain = _tron_brain()
        self.reset()

    def reset(self):
        self.score = 0
        self.wins = 0
        self.cpu_wins = 0
        self.death_flash = 0
        # Which trail flashes red at the end of a round: 1 = cyan lost,
        # 2 = magenta lost, 0 = head-on draw so both do. Without this the
        # frame() flash was hard-coded to the cyan trail, so a round the
        # cyan side WON still lit up cyan in the death colour.
        self.flash_side = 0
        self.boost = 0
        self.round_n = 0
        self.move_log = []
        self._new_round()

    def _new_round(self):
        self.trail = [[0] * WIDTH for _ in range(HEIGHT)]
        for x in range(WIDTH):
            self.trail[0][x] = 3
            self.trail[HEIGHT - 1][x] = 3
        for y in range(HEIGHT):
            self.trail[y][0] = 3
            self.trail[y][WIDTH - 1] = 3

        # Fixed classic spawn — never random
        mid = HEIGHT // 2
        self.you = [self.SPAWN_PAD, mid]
        self.cpu = [WIDTH - 1 - self.SPAWN_PAD, mid]
        self.ydir = RIGHT
        self.cdir = LEFT
        self.next_ydir = RIGHT
        self.trail[self.you[1]][self.you[0]] = 1
        self.trail[self.cpu[1]][self.cpu[0]] = 2
        self.boost = 0
        self.tick_rate = 0.055
        self.round_n += 1
        self.steps = 0
        self.move_log = []

    def input(self, cmd):
        d = {"up": UP, "down": DOWN, "left": LEFT, "right": RIGHT}.get(cmd)
        if d and not (d[0] == -self.ydir[0] and d[1] == -self.ydir[1]):
            self.next_ydir = d
        elif cmd in ("drop", "rotate"):
            self.boost = 8
            self.tick_rate = 0.032

    def _free(self, x, y):
        return 0 <= x < WIDTH and 0 <= y < HEIGHT and self.trail[y][x] == 0

    def _options(self, pos, cur):
        opts = []
        for d in DIRS:
            if d[0] == -cur[0] and d[1] == -cur[1]:
                continue
            if self._free(pos[0] + d[0], pos[1] + d[1]):
                opts.append(d)
        return opts

    def _flood(self, pos, extra_block=None, limit=260):
        from collections import deque
        sx, sy = int(pos[0]), int(pos[1])
        blocked = extra_block or set()
        if not (0 <= sx < WIDTH and 0 <= sy < HEIGHT):
            return 0
        q = deque()
        seen = set()
        if self.trail[sy][sx] == 0 and (sx, sy) not in blocked:
            q.append((sx, sy))
            seen.add((sx, sy))
        else:
            for d in DIRS:
                nx, ny = sx + d[0], sy + d[1]
                if self._free(nx, ny) and (nx, ny) not in blocked:
                    q.append((nx, ny))
                    seen.add((nx, ny))
        while q and len(seen) < limit:
            x, y = q.popleft()
            for d in DIRS:
                nx, ny = x + d[0], y + d[1]
                if (nx, ny) in seen or (nx, ny) in blocked:
                    continue
                if 0 <= nx < WIDTH and 0 <= ny < HEIGHT and self.trail[ny][nx] == 0:
                    seen.add((nx, ny))
                    q.append((nx, ny))
        return len(seen)

    def _voronoi(self, me, opp, limit=320):
        from collections import deque
        q = deque()
        owner = {}
        me_t, opp_t = (me[0], me[1]), (opp[0], opp[1])
        q.append((me_t[0], me_t[1], 0, 1))
        q.append((opp_t[0], opp_t[1], 0, 2))
        owner[me_t] = (0, 1)
        owner[opp_t] = (0, 2)
        while q and len(owner) < limit:
            x, y, dist, who = q.popleft()
            if owner.get((x, y), (999, 0))[0] < dist:
                continue
            for d in DIRS:
                nx, ny = x + d[0], y + d[1]
                if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                    continue
                if self.trail[ny][nx] != 0 and (nx, ny) not in (me_t, opp_t):
                    continue
                nd = dist + 1
                prev = owner.get((nx, ny))
                if prev is None or nd < prev[0]:
                    owner[(nx, ny)] = (nd, who)
                    q.append((nx, ny, nd, who))
                elif prev and nd == prev[0] and prev[1] != who:
                    owner[(nx, ny)] = (nd, 0)
        mine = sum(1 for _, w in owner.values() if w == 1)
        theirs = sum(1 for _, w in owner.values() if w == 2)
        return mine, theirs

    def _lookahead_space(self, pos, d, steps=5):
        painted = []
        x, y = pos[0], pos[1]
        cur = d
        for _ in range(steps):
            nx, ny = x + cur[0], y + cur[1]
            if not self._free(nx, ny) or (nx, ny) in painted:
                break
            painted.append((nx, ny))
            x, y = nx, ny
            if self._free(x + cur[0], y + cur[1]) and (x + cur[0], y + cur[1]) not in painted:
                continue
            for nd in DIRS:
                if nd[0] == -cur[0] and nd[1] == -cur[1]:
                    continue
                tx, ty = x + nd[0], y + nd[1]
                if self._free(tx, ty) and (tx, ty) not in painted:
                    cur = nd
                    break
            else:
                break
        for px, py in painted:
            self.trail[py][px] = 9
        space = self._flood([x, y], limit=220) if painted else 0
        for px, py in painted:
            self.trail[py][px] = 0
        return space

    def _wall_hug(self, x, y, d):
        """Right-hand wall preference — competitive endgame staple."""
        rx, ry = d[1], -d[0]
        lx, ly = -d[1], d[0]
        right = not self._free(x + rx, y + ry)
        left = not self._free(x + lx, y + ly)
        if right and not left:
            return 1.0
        if left and not right:
            return 0.35
        if left and right:
            return -0.8  # tunnel — handled separately
        return 0.0

    def _corridor(self, x, y, d):
        lx, ly = -d[1], d[0]
        rx, ry = d[1], -d[0]
        lw = not self._free(x + lx, y + ly)
        rw = not self._free(x + rx, y + ry)
        if lw and rw:
            return 1.0
        if lw or rw:
            return 0.25
        return 0.0

    def _features(self, pos, cur, opp, d):
        """Feature vector for learning + evaluation (deterministic)."""
        nx, ny = pos[0] + d[0], pos[1] + d[1]
        self.trail[ny][nx] = 9
        space = self._flood([nx, ny], limit=280)
        look = self._lookahead_space([nx, ny], d, steps=5)
        mine, theirs = self._voronoi([nx, ny], opp, limit=340)
        # Opponent space if they stay put (pressure)
        opp_space = self._flood(opp, extra_block={(nx, ny)}, limit=200)
        self.trail[ny][nx] = 0

        mobility = 0
        for od in DIRS:
            if od[0] == -d[0] and od[1] == -d[1]:
                continue
            if self._free(nx + od[0], ny + od[1]):
                mobility += 1

        head_on = 0.0
        for od in DIRS:
            if (opp[0] + od[0], opp[1] + od[1]) == (nx, ny):
                head_on = 1.0
                break

        opp_esc = 0
        for od in DIRS:
            ox, oy = opp[0] + od[0], opp[1] + od[1]
            if (ox, oy) == (nx, ny):
                continue
            if self._free(ox, oy):
                opp_esc += 1
        cut = 1.0 if opp_esc == 0 else (0.45 if opp_esc == 1 else 0.0)

        dist = abs(nx - opp[0]) + abs(ny - opp[1])
        straight = 1.0 if d == cur else 0.0
        corridor = self._corridor(nx, ny, d)
        wall_hug = self._wall_hug(nx, ny, d)
        low = 1.0 if space < 18 else (0.4 if space < 35 else 0.0)
        # articulation-ish: mobility collapse
        artic = 1.0 if mobility <= 1 and space > 20 else 0.0

        return {
            "space": float(space),
            "look": float(look),
            "voronoi": float(mine - theirs),
            "mobility": float(mobility),
            "head_on": head_on,
            "cut": cut,
            "straight": straight,
            "corridor": corridor,
            "wall_hug": wall_hug,
            "dist": float(dist),
            "low_space": low,
            "opp_space": float(opp_space),
            "articulation": artic,
        }

    def _score_feats(self, feats):
        w = self.brain.w
        s = 0.0
        for k in _TronBrain.KEYS:
            s += float(w[k]) * float(feats.get(k, 0.0))
        return s

    def _pick(self, pos, cur, opp, side):
        """
        Competitive policy:
          1) score all legal moves with learned weights
          2) adversarial refine top candidates (assume opp also maximises)
        """
        opts = self._options(pos, cur)
        if not opts:
            return cur

        ranked = []
        for d in opts:
            feats = self._features(pos, cur, opp, d)
            base = self._score_feats(feats)
            ranked.append((base, d, feats))
        ranked.sort(key=lambda t: t[0], reverse=True)

        # Adversarial 2-ply on top candidates — intense, not random
        best_d, best_s, best_feats = ranked[0][1], -1e18, ranked[0][2]
        consider = ranked[: min(3, len(ranked))]
        scored = []
        for base, d, feats in consider:
            nx, ny = pos[0] + d[0], pos[1] + d[1]
            # Simulate our claim; assume opponent also maximises their score
            self.trail[ny][nx] = 9
            opp_cur = self.cdir if side == 1 else self.ydir
            opp_opts = self._options(opp, opp_cur)
            if not opp_opts:
                opp_opts = [od for od in DIRS
                            if self._free(opp[0] + od[0], opp[1] + od[1])]
            worst_for_me = base
            if opp_opts:
                opp_best = -1e18
                opp_choice = opp_opts[0]
                for od in opp_opts:
                    of = self._features(opp, opp_cur, [nx, ny], od)
                    os_ = self._score_feats(of)
                    if os_ > opp_best:
                        opp_best = os_
                        opp_choice = od
                ox = opp[0] + opp_choice[0]
                oy = opp[1] + opp_choice[1]
                if self._free(ox, oy):
                    self.trail[oy][ox] = 9
                    my_space = self._flood([nx, ny], limit=200)
                    their_space = self._flood([ox, oy], limit=200)
                    self.trail[oy][ox] = 0
                    worst_for_me = (
                        base * 0.55
                        + my_space * 2.0
                        - their_space * 1.6
                        + (40.0 if my_space > their_space + 15 else 0.0)
                    )
            self.trail[ny][nx] = 0
            scored.append((worst_for_me, d, feats))

            if worst_for_me > best_s:
                best_s = worst_for_me
                best_d = d
                best_feats = feats

        # Fixed spawn + a fully deterministic evaluator means a converged
        # brain plays the literal same game every round: measured, round
        # lengths locked into a period-2 cycle ([607, 568, 607, 568, ...])
        # regardless of seed, since nothing in the decision path varies.
        # Break near-ties (within 1.5 of the best adversarial score) with a
        # light random weighting -- still always picks a top-tier move, but
        # stops two fixed strategies from replaying identically forever.
        near = [(s, d, f) for s, d, f in scored if best_s - s <= 1.5]
        if len(near) > 1:
            lo = min(s for s, _, _ in near)
            weights = [(s - lo) + 0.5 for s, _, _ in near]
            s, d, f = random.choices(near, weights=weights)[0]
            best_s, best_d, best_feats = s, d, f

        # Hard survival override: if any move has much more space, take it
        spaces = []
        for base, d, feats in ranked:
            spaces.append((feats["space"], d, feats))
        spaces.sort(reverse=True)
        if spaces and spaces[0][0] >= 25:
            # if best adversarial pick is a dead pocket vs a spacious alt, override
            pick_space = best_feats.get("space", 0)
            if pick_space + 18 < spaces[0][0]:
                best_d = spaces[0][1]
                best_feats = spaces[0][2]

        self.move_log.append((side, best_feats))
        return best_d

    def auto(self):
        """Demo: cyan is full competitive AI (same brain as magenta)."""
        self.next_ydir = self._pick(self.you, self.ydir, self.cpu, side=1)

    def _finish_round(self, winner):
        """winner 1/2/0 — learn + flash."""
        self.brain.learn(self.move_log, winner)
        self.move_log = []
        if winner == 1:
            self.wins += 1
            self.score += 15 + self.wins * 4
            self.death_flash = 16
            self.flash_side = 2          # magenta crashed
        elif winner == 2:
            self.cpu_wins += 1
            self.death_flash = 16
            self.flash_side = 1          # cyan crashed
        else:
            self.death_flash = 18
            self.flash_side = 0          # head-on: both die

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self._new_round()
            return
        if self.boost > 0:
            self.boost -= 1
            if self.boost == 0:
                self.tick_rate = 0.055

        # CPU's adversarial ply reads self.ydir as "the opponent's current
        # direction" (`opp_cur` inside _pick). auto() computed next_ydir using
        # the OLD self.ydir (cpu hadn't moved yet either), so committing
        # self.ydir here before calling cdir's _pick let the CPU see the
        # you-side's move for THIS tick already locked in, while the you-side
        # only ever saw the CPU's direction from the END of last tick -- a
        # one-sided information edge, every single tick. Over 1468 real
        # rounds that alone produced a 90.5% CPU win rate despite both sides
        # sharing one "same brain." Computing cdir first, against the same
        # stale ydir that auto() used, restores the symmetry.
        self.cdir = self._pick(self.cpu, self.cdir, self.you, side=2)
        self.ydir = self.next_ydir

        def step(pos, d):
            nx, ny = pos[0] + d[0], pos[1] + d[1]
            if not self._free(nx, ny):
                return None
            return [nx, ny]

        nyou = step(self.you, self.ydir)
        ncpu = step(self.cpu, self.cdir)

        you_dead = nyou is None or (ncpu is not None and nyou == ncpu)
        cpu_dead = ncpu is None or (nyou is not None and ncpu == nyou)

        if you_dead and cpu_dead:
            self._finish_round(0)
            return
        if you_dead:
            self._finish_round(2)
            return
        if cpu_dead:
            self._finish_round(1)
            return

        self.you, self.cpu = nyou, ncpu
        self.trail[self.you[1]][self.you[0]] = 1
        self.trail[self.cpu[1]][self.cpu[0]] = 2
        self.score += 1
        self.steps += 1

        # Late-game speed-up for intensity (both still using same brain)
        if self.steps > 80 and self.boost == 0:
            self.tick_rate = 0.045
        if self.steps > 160 and self.boost == 0:
            self.tick_rate = 0.038

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        # The loser flashes red; the winner pulses to its bright head colour.
        # On a 64x64 panel with no text this is the only way to read the
        # result of a round, so it has to name the right side.
        you_lost = self.flash_side in (0, 1)
        cpu_lost = self.flash_side in (0, 2)
        you_c = (self.DEAD if you_lost else self.YOU_HEAD) if flashing else self.YOU
        cpu_c = (self.DEAD if cpu_lost else self.CPU_HEAD) if flashing else self.CPU
        for y in range(HEIGHT):
            row = self.trail[y]
            for x in range(WIDTH):
                v = row[x]
                if v == 3:
                    put_px(buf, x, y, self.WALL)
                elif v == 1:
                    put_px(buf, x, y, you_c)
                elif v == 2:
                    put_px(buf, x, y, cpu_c)
        if self.death_flash == 0:
            put_px(buf, self.you[0], self.you[1], self.YOU_HEAD)
            put_px(buf, self.cpu[0], self.cpu[1], self.CPU_HEAD)
        return bytes(buf)


# =============================================================================
# FLAPPY — classic impulse physics; simple "stay in gap centre" auto
# =============================================================================
class FlappyEngine:
    name = "flappy"
    tick_rate = 0.05

    # Pure black sky so dynamic FX show through as the “sky”
    BG = (0, 0, 0)
    BIRD = (255, 240, 60)       # bright lemon — not lost on fire/aurora
    BIRD_WING = (255, 160, 40)
    BIRD_BEAK = (255, 90, 200)  # magenta beak accent
    # Magenta/cyan pipes — avoid green (vanishes on Matrix / Forest FX)
    PIPE = (0, 220, 200)
    PIPE_LIP = (255, 60, 180)
    PIPE_INNER = (0, 160, 150)
    GROUND = (30, 20, 50)
    GROUND_TOP = (255, 80, 160)
    DEAD = (255, 40, 60)

    # Tuned so one flap ≈ half a gap, readable on 64×64 (~20Hz feel)
    GRAVITY = 0.22
    FLAP = -1.55
    MAX_FALL = 2.4
    PIPE_W = 6

    def __init__(self):
        self.reset()

    def reset(self):
        self.y = float(HEIGHT // 2)
        self.vy = 0.0
        self.x = 15
        self.pipes = []
        self.score = 0
        self.death_flash = 0
        self.flap_cd = 0
        self.anim = 0
        # Opening gap always near spawn height so first pipe is fair
        self._spawn_pipe(WIDTH + 8, center=HEIGHT // 2)
        self._spawn_pipe(WIDTH + 40)

    def _gap_h(self):
        # Generous gaps; slow tighten so runs stay fun on LED
        return max(16, 22 - self.score // 10)

    def _spawn_pipe(self, x, center=None):
        gh = self._gap_h()
        margin = 8
        lo, hi = margin, HEIGHT - 3 - gh - margin
        if center is not None:
            gy = int(clamp(center - gh // 2 + random.randint(-2, 2), lo, hi))
        elif self.pipes:
            prev_c = self.pipes[-1][1] + self.pipes[-1][2] // 2
            gy = int(clamp(prev_c - gh // 2 + random.randint(-8, 8), lo, hi))
        else:
            gy = random.randint(lo, hi)
        self.pipes.append([float(x), gy, gh, False])

    def input(self, cmd):
        if cmd in ("up", "rotate", "drop") and self.flap_cd <= 0:
            self.vy = self.FLAP
            self.flap_cd = 3
            self.anim = 4

    def auto(self):
        """Precision centre-hold: stay in the gap mid-line so long runs look intentional."""
        nxt = None
        for p in self.pipes:
            if p[0] + self.PIPE_W + 2 >= self.x:
                nxt = p
                break
        if nxt:
            gap_top = nxt[1]
            gap_bot = nxt[1] + nxt[2]
            # Hold dead-centre; slight bias up only when pipe is far
            target = gap_top + nxt[2] * 0.5
            if nxt[0] > self.x + 22:
                target = gap_top + nxt[2] * 0.48
        else:
            gap_top, gap_bot = 4, HEIGHT - 6
            target = HEIGHT * 0.5

        if self.y > gap_bot - 2.5 or self.y > HEIGHT - 7:
            if self.vy >= -0.05:
                self.vy = self.FLAP
                self.anim = 4
            return
        # Tight band around centre — flaps earlier so it doesn't bob wildly
        if self.y > target + 0.6 and self.vy >= -0.05:
            self.vy = self.FLAP
            self.anim = 4

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        if self.flap_cd > 0:
            self.flap_cd -= 1
        if self.anim > 0:
            self.anim -= 1

        self.vy = min(self.MAX_FALL, self.vy + self.GRAVITY)
        self.y += self.vy

        ground = HEIGHT - 3
        if self.y < 2:
            self.y = 2
            if self.vy < 0:
                self.vy = 0
        if self.y > ground - 1.5:
            self.death_flash = 10
            return

        scroll = 1.0 + min(0.45, self.score * 0.015)
        for p in self.pipes:
            p[0] -= scroll
            if not p[3] and p[0] + self.PIPE_W < self.x:
                p[3] = True
                self.score += 1

        self.pipes = [p for p in self.pipes if p[0] > -self.PIPE_W - 2]
        spacing = max(24, 34 - min(6, self.score // 6))
        if not self.pipes or self.pipes[-1][0] < WIDTH - spacing:
            self._spawn_pipe(WIDTH + 2)

        by = self.y
        for p in self.pipes:
            px, gy, gh, _ = p
            if px < self.x + 1 and px + self.PIPE_W > self.x:
                if by < gy + 1.2 or by > gy + gh - 1.2:
                    self.death_flash = 10
                    return

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        for p in self.pipes:
            px, gy, gh = int(p[0]), p[1], p[2]
            for x in range(px, px + self.PIPE_W):
                edge = x in (px, px + self.PIPE_W - 1)
                mid = x in (px + 1, px + 2) if self.PIPE_W >= 4 else False
                body = self.PIPE_LIP if edge else (self.PIPE_INNER if mid else self.PIPE)
                for y in range(0, gy):
                    put_px(buf, x, y, body)
                for y in range(gy + gh, HEIGHT - 3):
                    put_px(buf, x, y, body)
                # Bright cap lips
                if 0 <= gy - 1:
                    put_px(buf, x, gy - 1, self.PIPE_LIP)
                put_px(buf, x, gy + gh, self.PIPE_LIP)
                if self.PIPE_W >= 4 and not edge:
                    put_px(buf, x, max(0, gy - 2), self.PIPE)
                    put_px(buf, x, min(HEIGHT - 4, gy + gh + 1), self.PIPE)
        # Ground strip — grass green, not purple/blue
        for x in range(WIDTH):
            put_px(buf, x, HEIGHT - 3, self.GROUND_TOP)
            put_px(buf, x, HEIGHT - 2, self.GROUND)
            put_px(buf, x, HEIGHT - 1, self.GROUND)
        if self.death_flash == 0 or not flashing:
            col = self.DEAD if flashing else self.BIRD
            bx, by = self.x, int(self.y)
            # Dark rim so the bird stays visible on loud FX underlays
            o = rim(col, 0.2)
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    if abs(dx) == 2 or abs(dy) == 2:
                        put_px(buf, bx + dx, by + dy, o)
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    put_px(buf, bx + dx, by + dy, col)
            wing = self.BIRD_WING if self.anim else col
            put_px(buf, bx - 1, by + (0 if self.anim else 1), wing)
            put_px(buf, bx + 2, by, self.BIRD_BEAK if not flashing else self.DEAD)
        # Tiny score pips top-left so ambient runs feel competitive
        for i in range(min(self.score, 20)):
            put_px(buf, 1 + (i % 10) * 2, 1 + (i // 10) * 2, (80, 80, 90))
        return bytes(buf)


# =============================================================================
# SPACE INVADERS — bunkers, mystery ship, column-only bombs, lives, waves
# =============================================================================
class InvadersEngine:
    name = "invaders"
    tick_rate = 0.05

    BG = (0, 0, 0)
    SHIP = (80, 255, 160)
    ALIEN_C = [(255, 50, 200), (255, 200, 40), (60, 220, 255), (180, 100, 255)]
    BULLET = (255, 255, 120)
    BOMB = (255, 80, 40)
    BUNKER = (40, 255, 120)
    UFO = (255, 60, 100)
    DEAD = (255, 40, 60)
    LIFE = (80, 255, 160)

    COLS, ROWS = 8, 4
    AW, AH = 5, 3
    BOOM_LIFE = 5
    BOOM_C = (255, 240, 180)

    # Three species, two animation frames each, drawn at 5x3 — the smallest
    # size that still gives an invader a readable silhouette. The old renderer
    # used `(dx+dy+phase) % 2 == 0 or 0 < dx < AW-1`, where the `or` arm is
    # true for the whole middle of every sprite, so all it ever animated was
    # the two outermost pixels: every alien was a solid block that flickered
    # at the edges. Row index picks the species, matching the arcade's
    # squid / crab / octopus stack.
    SPRITES = (
        ((".X.X.", ".XXX.", "X.X.X"), (".X.X.", ".XXX.", "..X..")),   # squid
        (("X...X", ".XXX.", "X.X.X"), ("X...X", ".XXX.", ".X.X.")),   # crab
        (("..X..", "XXXXX", "X...X"), ("..X..", "XXXXX", ".X.X.")),   # octopus
    )
    # Species per row, matching the points ladder (30/20/20/10) the way the
    # original cabinet stacks them — not one species per row index.
    SPECIES_BY_ROW = (0, 1, 1, 2)
    SHIP_SPRITE = ("..X..", "XXXXX", "XXXXX")
    UFO_SPRITE = (".XXX.", "XXXXX")

    PACE_FULL = 13      # ticks between fleet steps with the formation full
    PACE_WAVE = 0.93    # per-wave multiplier; must stay <1 but never reach 0

    def __init__(self):
        self.reset()

    def reset(self):
        self.score = 0
        self.lives = 3
        self.wave = 1
        self.death_flash = 0
        self._new_wave()

    def _new_wave(self):
        self.ship_x = WIDTH // 2
        self.bullet = None
        self.bombs = []
        self.dir = 1
        self.move_cd = 0
        self.fire_cd = 0
        self.anim = 0
        self.ufo = None
        self.ufo_cd = random.randint(80, 140)
        self.booms = []          # [x, y, age, big] — hit feedback
        self.aliens = []
        ox, oy = 5, 8
        # Classic arcade scoring (approx Taito/Midway): top 30, mid 20, bottom 10
        # Each new wave starts one row lower — the original pressure curve.
        for r in range(self.ROWS):
            for c in range(self.COLS):
                pts = (30, 20, 20, 10)[min(r, 3)]
                self.aliens.append({
                    "x": ox + c * 7,
                    "y": oy + r * 6 + min(6, (self.wave - 1) * 2),
                    "r": r, "pts": pts,
                })
        # Bunkers: 3 destructible blocks
        self.bunkers = []
        for bx in (12, 30, 48):
            for dy in range(3):
                for dx in range(6):
                    if not (dy == 2 and 1 < dx < 4):  # notch
                        self.bunkers.append([bx + dx, HEIGHT - 12 + dy, 2])

    def input(self, cmd):
        if cmd == "left":
            self.ship_x = max(3, self.ship_x - 2)
        elif cmd == "right":
            self.ship_x = min(WIDTH - 4, self.ship_x + 2)
        elif cmd in ("up", "rotate", "drop"):
            if self.bullet is None and self.fire_cd <= 0:
                self.bullet = [self.ship_x, HEIGHT - 8]
                self.fire_cd = 3

    def _bunker_cols(self):
        """Column -> rows of live bunker voxels, for shot/bomb path tests."""
        cols = {}
        for bu in self.bunkers:
            cols.setdefault(bu[0], []).append(bu[1])
        return cols

    def _shot_outcome(self, sx, bcols):
        """Trace a bullet fired from column sx. -> (points, hit_own_bunker).

        Steps exactly as tick() does (3px per tick from HEIGHT-8, same band
        tests in the same order) so the AI cannot believe in a shot the game
        would not actually award. The bunker leg is the one that matters: the
        old heuristic ignored bunkers entirely, so the AI would sit behind its
        own cover blasting it to rubble — 49% of its shots scored nothing and
        it demolished the very thing keeping it alive.
        """
        by = HEIGHT - 8
        while True:
            by -= 3
            if by < 0:
                return 0.0, False
            for a in self.aliens:
                if a["x"] <= sx <= a["x"] + self.AW and a["y"] <= by <= a["y"] + self.AH:
                    # Low aliens are the urgent ones; break up the formation
                    # from the bottom the way good human play does.
                    return a["pts"] + a["y"] * 0.8, False
            if self.ufo and abs(sx - self.ufo[0]) <= 3 and by <= 5:
                return 120.0, False
            for row in bcols.get(sx, ()):
                if abs(row - by) <= 1:
                    return 0.0, True

    def _sim_bombs(self, target, bcols, horizon=26):
        """Walk our own move toward `target` against every falling bomb.

        Returns ticks-until-hit, or None if we get there alive. The previous
        code scored a static danger field, which meant it evaluated the
        destination but never the trip — it would walk straight through a bomb
        to reach a 'safe' column.
        """
        speed = 1 + self.wave // 3
        x = self.ship_x
        bombs = [[b[0], b[1]] for b in self.bombs]
        for t in range(horizon):
            if x < target:
                x = min(target, x + 2)
            elif x > target:
                x = max(target, x - 2)
            if not bombs:
                return None
            alive = []
            for b in bombs:
                b[1] += speed
                if b[1] >= HEIGHT:
                    continue
                blocked = False
                for row in bcols.get(b[0], ()):
                    if abs(row - b[1]) <= 1:
                        blocked = True
                        break
                if blocked:
                    continue
                if abs(b[0] - x) <= 2 and b[1] >= HEIGHT - 6:
                    return t
                alive.append(b)
            bombs = alive
        return None

    def auto(self):
        """Pick a column by forward simulation: survive first, then shoot.

        Survival is scored on a scale that shot value cannot outbid. The old
        weighting let a 10-point alien (worth ~37 after weighting) outrank an
        incoming bomb (~4), so the AI reliably traded its life for one hit and
        died as often as random input.
        """
        bcols = self._bunker_cols()
        can_fire = self.bullet is None and self.fire_cd <= 0

        # Columns under the lowest alien of each file are where bombs spawn.
        # Loitering there is what kept the old AI under an alien 84% of the time.
        # Columns a bomb can appear in, weighted by how little warning we would
        # get. A file whose lowest alien is nearly on top of us can drop a bomb
        # that lands before we can clear the 3px hit box (we move 2px/tick), so
        # that is worth fleeing pre-emptively; a high file barely matters.
        bomb_speed = 1 + self.wave // 3
        spawn_risk = {}
        for a in self._bottom_aliens():
            warn = ((HEIGHT - 6) - (a["y"] + self.AH)) / bomb_speed
            risk = max(0.0, 30.0 - warn * 3.0)
            for dx in range(self.AW + 1):
                c = a["x"] + dx
                spawn_risk[c] = max(spawn_risk.get(c, 0.0), risk)

        # Clearing the outermost file makes the formation narrower, so it
        # travels further before hitting a wall — and every wall touch is a
        # 3px drop. Shaving the edges literally buys descent time, which is
        # what we were losing waves to.
        edge_cols = set()
        if self.aliens:
            xs = [a["x"] for a in self.aliens]
            for ex in (min(xs), max(xs)):
                for dx in range(self.AW + 1):
                    edge_cols.add(ex + dx)

        best_x, best_s = self.ship_x, -1e18
        for sx in range(3, WIDTH - 3):
            reach = abs(sx - self.ship_x)
            if reach > 20:
                continue
            s = 0.0
            hit_t = self._sim_bombs(sx, bcols)
            if hit_t is not None:
                # Dominant term: no shot is worth 1000 points. Dying later is
                # still better than dying now — it leaves room to react.
                s -= 1000.0 - hit_t * 15.0
            pts, own_bunker = self._shot_outcome(sx, bcols)
            # Keep aiming while a bullet is in flight, just more cheaply. The
            # old code zeroed shot value whenever it could not fire *right now*,
            # so it wandered off target between shots instead of lining the
            # next one up — which is why it lost most waves to the invasion.
            s += pts * (1.2 if can_fire else 0.6)
            if own_bunker:
                s -= 40.0              # never chew through our own cover
            if sx in edge_cols:
                s += 8.0               # see above
            s -= spawn_risk.get(sx, 0.0)
            if sx in bcols:
                s += 6.0               # hull-down behind an intact bunker
            # Room to run. Hugging a wall halves our escape routes, and most
            # remaining bomb deaths were in a corner with nowhere left to go.
            s -= max(0.0, 10 - sx) * 1.2 + max(0.0, sx - (WIDTH - 11)) * 1.2
            s -= reach * 0.35
            if s > best_s:
                best_s, best_x = s, sx

        if best_x < self.ship_x - 1:
            self.ship_x = max(3, self.ship_x - 2)
        elif best_x > self.ship_x + 1:
            self.ship_x = min(WIDTH - 4, self.ship_x + 2)

        # Fire independently of movement. The engine lets a tick both move and
        # shoot (input() treats them as separate commands), but the old auto()
        # only fired when it was already parked on target, throwing away ~40%
        # of the available fire rate against a formation that never stops.
        # Evaluated after the move, since the bullet spawns at the new column.
        if can_fire:
            pts, own_bunker = self._shot_outcome(self.ship_x, bcols)
            if pts >= 10 and not own_bunker:
                self.bullet = [self.ship_x, HEIGHT - 8]
                self.fire_cd = 3

    def _bottom_aliens(self):
        cols = {}
        for a in self.aliens:
            key = a["x"] // 7
            if key not in cols or a["y"] > cols[key]["y"]:
                cols[key] = a
        return list(cols.values())

    def tick(self):
        # Age hit-bursts first: tick() early-returns while death_flash runs, and
        # freezing the bursts mid-expansion read as a stutter on the panel.
        if self.booms:
            for b in self.booms:
                b[2] += 1
            self.booms = [b for b in self.booms if b[2] < self.BOOM_LIFE]

        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                if self.lives <= 0:
                    self.reset()
                else:
                    self.bullet = None
                    self.bombs = []
                    self.ship_x = WIDTH // 2
            return

        if not self.aliens:
            self.wave += 1
            self.score += 50 * self.wave
            self._new_wave()
            return

        if self.fire_cd > 0:
            self.fire_cd -= 1
        self.anim += 1

        # UFO
        self.ufo_cd -= 1
        if self.ufo is None and self.ufo_cd <= 0:
            self.ufo = [0, 3]
            self.ufo_cd = random.randint(100, 180)
        if self.ufo:
            self.ufo[0] += 1
            if self.ufo[0] > WIDTH:
                self.ufo = None

        # March
        self.move_cd -= 1
        if self.move_cd <= 0:
            xs = [a["x"] for a in self.aliens]
            left, right = min(xs), max(xs) + self.AW
            if (right >= WIDTH - 1 and self.dir > 0) or (left <= 1 and self.dir < 0):
                self.dir *= -1
                for a in self.aliens:
                    a["y"] += 3
            else:
                for a in self.aliens:
                    a["x"] += self.dir
            # Classic: march accelerates as the formation thins (last aliens are terrifying).
            # The cabinet moved exactly ONE alien per frame, so the whole fleet took
            # N frames to advance -- its speed is proportional to 1/N and does not
            # depend on the wave at all. The old rule subtracted the wave instead
            # (14 - wave - killed//2), which pinned pace at 1 from wave 13 on: the
            # speed law vanished *and* late waves opened at max speed with all 32
            # aliens up. Wave now scales the pace multiplicatively, so it always
            # thins from fast to fastest and never saturates.
            n = len(self.aliens)
            total = self.COLS * self.ROWS
            pace = self.PACE_FULL * (n / total) * (self.PACE_WAVE ** (self.wave - 1))
            self.move_cd = max(1, round(pace))

        # Player bullet
        if self.bullet:
            self.bullet[1] -= 3
            bx, by = self.bullet
            if by < 0:
                self.bullet = None
            else:
                hit = None
                for a in self.aliens:
                    if a["x"] <= bx <= a["x"] + self.AW and a["y"] <= by <= a["y"] + self.AH:
                        hit = a
                        break
                if hit:
                    self.score += hit["pts"]
                    self.booms.append([hit["x"] + self.AW // 2,
                                       hit["y"] + self.AH // 2, 0, False])
                    self.aliens.remove(hit)
                    self.bullet = None
                elif self.ufo and abs(bx - self.ufo[0]) <= 3 and by <= 5:
                    # Cabinet mystery-ship table: 50/100/150 common, 300 rare.
                    self.score += random.choices(
                        [50, 100, 150, 300], weights=[35, 30, 20, 15])[0]
                    self.booms.append([self.ufo[0], self.ufo[1], 0, True])
                    self.ufo = None
                    self.bullet = None
                else:
                    for bu in self.bunkers:
                        if bu[0] == bx and abs(bu[1] - by) <= 1:
                            bu[2] -= 1
                            if bu[2] <= 0:
                                self.bunkers.remove(bu)
                            self.bullet = None
                            break

        # Bombs from bottom of columns only (classic)
        if self.aliens and random.random() < 0.06 + self.wave * 0.01:
            shooter = random.choice(self._bottom_aliens())
            self.bombs.append([shooter["x"] + 2, shooter["y"] + self.AH])

        alive = []
        for b in self.bombs:
            b[1] += 1 + self.wave // 3
            if b[1] >= HEIGHT:
                continue
            blocked = False
            for bu in list(self.bunkers):
                if bu[0] == b[0] and abs(bu[1] - b[1]) <= 1:
                    bu[2] -= 1
                    if bu[2] <= 0:
                        self.bunkers.remove(bu)
                    blocked = True
                    break
            if blocked:
                continue
            if abs(b[0] - self.ship_x) <= 2 and b[1] >= HEIGHT - 6:
                self.lives -= 1
                self.death_flash = 8
                return
            alive.append(b)
        self.bombs = alive

        if any(a["y"] + self.AH >= HEIGHT - 7 for a in self.aliens):
            self.lives = 0
            self.death_flash = 12

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        phase = (self.anim // 6) % 2
        for a in self.aliens:
            col = self.DEAD if flashing else self.ALIEN_C[a["r"] % len(self.ALIEN_C)]
            rows = self.SPRITES[self.SPECIES_BY_ROW[a["r"] % len(self.SPECIES_BY_ROW)]][phase]
            for dy, srow in enumerate(rows):
                for dx, ch in enumerate(srow):
                    if ch == "X":
                        put_px(buf, a["x"] + dx, a["y"] + dy, col)
        for bu in self.bunkers:
            put_px(buf, bu[0], bu[1], self.BUNKER if bu[2] > 1 else lerp_color(self.BUNKER, (0, 0, 0), 0.5))
        if self.ufo:
            for dy, srow in enumerate(self.UFO_SPRITE):
                for dx, ch in enumerate(srow):
                    if ch == "X":
                        put_px(buf, self.ufo[0] + dx - 2, self.ufo[1] + dy, self.UFO)
        sc = self.DEAD if flashing else self.SHIP
        for dy, srow in enumerate(self.SHIP_SPRITE):
            for dx, ch in enumerate(srow):
                if ch == "X":
                    put_px(buf, self.ship_x + dx - 2, HEIGHT - 5 + dy, sc)
        for i in range(self.lives):
            put_px(buf, 2 + i * 3, HEIGHT - 1, self.LIFE)
        if self.bullet:
            put_px(buf, self.bullet[0], self.bullet[1], self.BULLET)
        for b in self.bombs:
            put_px(buf, b[0], b[1], self.BOMB)
        # Hit bursts last so they read over everything: an expanding diamond
        # that fades out. On a 64px panel a kill with no feedback just looks
        # like the alien blinked out of existence.
        for bx, by, age, big in self.booms:
            f = age / self.BOOM_LIFE
            col = lerp_color(self.BOOM_C, self.BG, f)
            rad = int(round((3.0 if big else 2.0) * (0.4 + f * 1.6)))
            for dy in range(-rad, rad + 1):
                for dx in range(-rad, rad + 1):
                    if abs(dx) + abs(dy) == rad:
                        put_px(buf, bx + dx, by + dy, col)
            if age == 0:
                put_px(buf, bx, by, (255, 255, 255))
        return bytes(buf)


# =============================================================================
# LIFE — interesting seeds (methuselahs, guns, rakes), colour by age
# =============================================================================
class LifeEngine:
    name = "life"
    tick_rate = 0.1

    BG = (0, 0, 0)              # black field → ambient FX shows through dead cells
    CYCLE_MEM = 16              # longest oscillator period we can recognise
    FADE = 5                    # frames of dissolve between seeds
    QUIET_CELLS = 10            # per-gen cell flips below which nothing is happening
    GEN_CAP = 900               # hard reseed, ~90s at tick_rate — keeps variety
    SPARSE_POP = 45             # under this the 64x64 panel just reads as empty
    TREND_GENS = 60             # lookback for "is it still growing?"
    # Seeds that translate or pulse forever without ever developing.
    NON_BLOOMING = ("glider", "lwss", "pentadecathlon")

    # Famous patterns as relative (x,y) live cells.
    SEEDS = {
        # The R-pentomino: 5 cells that churn for 1103 generations before
        # settling. This entry used to be a byte-for-byte copy of "glider",
        # so the single most interesting seed in the list never once ran.
        #   .OO
        #   OO.
        #   .O.
        "rpent": [(1, 0), (2, 0), (0, 1), (1, 1), (1, 2)],
        "acorn": [(1, 0), (3, 1), (0, 2), (1, 2), (4, 2), (5, 2), (6, 2)],
        "diehard": [(6, 0), (0, 1), (1, 1), (1, 2), (5, 2), (6, 2), (7, 2)],
        "glider": [(1, 0), (2, 1), (0, 2), (1, 2), (2, 2)],
        "lwss": [(1, 0), (4, 0), (0, 1), (0, 2), (4, 2), (0, 3), (1, 3), (2, 3), (3, 3)],
        # Period-15 oscillator — the slow pulse reads beautifully at 10fps.
        "pentadecathlon": [
            (2, 0), (7, 0),
            (0, 1), (1, 1), (3, 1), (4, 1), (5, 1), (6, 1), (8, 1), (9, 1),
            (2, 2), (7, 2),
        ],
        "pulsar_arm": [  # one seed that often blooms
            (2, 0), (3, 0), (4, 0), (8, 0), (9, 0), (10, 0),
            (0, 2), (5, 2), (7, 2), (12, 2),
            (0, 3), (5, 3), (7, 3), (12, 3),
            (0, 4), (5, 4), (7, 4), (12, 4),
            (2, 5), (3, 5), (4, 5), (8, 5), (9, 5), (10, 5),
        ],
        # This really is the full 36-cell Gosper glider gun, not a stub — it
        # fires a glider every 30 generations forever. Renamed accordingly.
        "gosper_gun": [
            (0, 4), (0, 5), (1, 4), (1, 5),
            (10, 4), (10, 5), (10, 6), (11, 3), (11, 7),
            (12, 2), (12, 8), (13, 2), (13, 8),
            (14, 5), (15, 3), (15, 7), (16, 4), (16, 5), (16, 6), (17, 5),
            (20, 2), (20, 3), (20, 4), (21, 2), (21, 3), (21, 4),
            (22, 1), (22, 5), (24, 0), (24, 1), (24, 5), (24, 6),
            (34, 2), (34, 3), (35, 2), (35, 3),
        ],
    }

    def __init__(self):
        self.score = 0
        self.gen = 0
        self.dying = 0
        self.fresh = 0
        self.reset()

    def reset(self):
        self.grid = [[0] * WIDTH for _ in range(HEIGHT)]
        self.age = [[0] * WIDTH for _ in range(HEIGHT)]
        self.gen = 0
        self.hist = []          # recent full-grid hashes, oldest first
        self.cycling = 0        # consecutive gens that repeated a recent state
        self.quiet = 0          # consecutive gens with almost no change
        self.pophist = []       # population per gen, for the growth test
        self.dying = 0
        self.fresh = self.FADE
        # "noise" used to be in this pool: random density soup that settles
        # into disconnected blocks and blinkers reads exactly as "just
        # random," which was the whole complaint this seed system exists to
        # fix. Every remaining kind is a named, structured pattern.
        kind = random.choice(["pattern", "pattern", "gliders", "guns"])
        if kind == "gliders":
            # Was 4-9 gliders: about 30 live cells on 4096, which reads as an
            # empty panel. More of them, plus spaceships, and clustered so they
            # actually collide and make something happen.
            for _ in range(random.randint(12, 18)):
                self._stamp("glider",
                            random.randint(2, WIDTH - 6),
                            random.randint(2, HEIGHT - 6),
                            random.randint(0, 3))
            for _ in range(random.randint(2, 4)):
                self._stamp("lwss",
                            random.randint(2, WIDTH - 8),
                            random.randint(2, HEIGHT - 8),
                            random.randint(0, 3))
        elif kind == "guns":
            # A gun never goes stable and never thins out — it just keeps
            # streaming gliders across the panel. Best-looking seed we have.
            self._stamp("gosper_gun", 2, random.randint(2, 20), 0)
            if random.random() < 0.5:
                self._stamp("gosper_gun", 26, random.randint(34, 52), 2)
        else:
            name = random.choice(list(self.SEEDS))
            self._stamp(name, WIDTH // 2 - 8, HEIGHT // 2 - 6, random.randint(0, 3))
            if name in self.NON_BLOOMING:
                # A spaceship or a bare oscillator on its own never develops —
                # it just translates or pulses on an otherwise black panel.
                # Give it company so something actually happens. Methuselahs
                # (rpent, acorn) are left alone: they start tiny on purpose and
                # bloom into hundreds of cells over the next few hundred gens.
                for _ in range(random.randint(4, 7)):
                    self._stamp(random.choice(["glider", "lwss"]),
                                random.randint(2, WIDTH - 8),
                                random.randint(2, HEIGHT - 8),
                                random.randint(0, 3))
                self._stamp(random.choice(["rpent", "acorn"]),
                            random.randint(8, WIDTH - 16),
                            random.randint(8, HEIGHT - 16),
                            random.randint(0, 3))
            elif random.random() < 0.5:
                self._stamp(random.choice(["glider", "lwss", "acorn"]),
                            random.randint(4, WIDTH - 12),
                            random.randint(4, HEIGHT - 12),
                            random.randint(0, 3))
        self.pop = sum(sum(r) for r in self.grid)

    def _stamp(self, name, ox, oy, rot=0):
        """Place a pattern, rotated, without letting it wrap around the torus.

        Rotation makes coordinates negative, and the old version pushed those
        straight through `% WIDTH` — which teleported the negative half of the
        pattern to the opposite edge. The 36-cell Gosper gun came out torn in
        two at rotations 2 and 3, i.e. half the time it was drawn. Normalising
        to a 0-based box and clamping the origin keeps every pattern whole.
        """
        cells = self.SEEDS.get(name, self.SEEDS["glider"])
        pts = []
        for x, y in cells:
            for _ in range(rot % 4):
                x, y = -y, x
            pts.append((x, y))
        minx = min(p[0] for p in pts)
        miny = min(p[1] for p in pts)
        w = max(p[0] for p in pts) - minx + 1
        h = max(p[1] for p in pts) - miny + 1
        ox = clamp(ox, 0, max(0, WIDTH - w))
        oy = clamp(oy, 0, max(0, HEIGHT - h))
        for x, y in pts:
            self.grid[oy + y - miny][ox + x - minx] = 1

    def input(self, cmd):
        if cmd in ("up", "down", "left", "right", "rotate", "drop"):
            self.reset()          # a keypress should reseed instantly, no fade

    def _retire(self):
        """Begin fading out toward a fresh seed."""
        if self.dying == 0:
            self.dying = self.FADE

    def auto(self):
        """Reseed once the board stops being worth watching.

        The old test was `stable > 18`, where `stable` only ever counted
        *identical* consecutive generations — so it caught still lifes and
        nothing else. A board that settles into blinkers (period 2) alternates
        forever, and measurement showed exactly that: a true period of 2 while
        the counter sat at 0. In practice 6 of 7 reseeds came from the blunt
        `gen > 900` fallback, leaving a frozen-looking panel for up to 90s.
        """
        if (self.cycling >= 8            # locked into an oscillator or still life
                or self.quiet >= 40      # technically alive, visually inert
                or self.pop < 6          # down to a lone glider or less
                or self._sparse_and_stalled()
                or self.gen > self.GEN_CAP):
            self._retire()

    def _pop(self):
        return self.pop

    def tick(self):
        if self.dying > 0:
            self.dying -= 1
            if self.dying == 0:
                self.reset()
            return
        if self.fresh > 0:
            self.fresh -= 1

        g = self.grid
        # Horizontal triple-sums first, so neighbour counting costs 3 lookups
        # per cell instead of 8. Slicing does the torus wrap at C speed.
        rs = [list(map(_add3, r[-1:] + r[:-1], r, r[1:] + r[:1])) for r in g]

        nxt = []
        pop = 0
        changed = 0
        for y in range(HEIGHT):
            row = g[y]
            arow = self.age[y]
            # `t` counts the 3x3 block including the cell itself, so the
            # neighbour count is t - s. Alive iff 3 neighbours, or 2 and living.
            nrow = [1 if (t - s == 3 or (t - s == 2 and s)) else 0
                    for t, s in zip(map(_add3, rs[y - 1], rs[y],
                                        rs[(y + 1) % HEIGHT]), row)]
            if nrow != row:
                # Only pay for the cell-level diff on rows that actually moved;
                # on a settled board that is a handful of rows, not all 64.
                changed += sum(a ^ b for a, b in zip(nrow, row))
            pop += sum(nrow)
            self.age[y] = [(min(40, a + 1) if n else 0)
                           for n, a in zip(nrow, arow)]
            nxt.append(nrow)

        self.grid = nxt
        self.pop = pop
        self.gen += 1
        self.score += 1          # generations survived, not reseed count

        # Cycle detection over the last CYCLE_MEM states catches every
        # oscillator up to that period, not just period 1.
        h = hash(tuple(map(tuple, nxt)))
        if h in self.hist:
            self.cycling += 1
        else:
            self.cycling = 0
        self.hist.append(h)
        if len(self.hist) > self.CYCLE_MEM:
            del self.hist[0]

        # A glider crawling across an otherwise dead field never repeats a
        # state, so cycle detection alone would let it sit there indefinitely.
        # Churn is the catch-all for "technically evolving, visually not":
        # a lone glider flips ~5 cells a generation, one blinker flips 4.
        self.quiet = self.quiet + 1 if changed <= self.QUIET_CELLS else 0

        self.pophist.append(pop)
        if len(self.pophist) > self.TREND_GENS:
            del self.pophist[0]

        if pop == 0:
            self._retire()

    def _sparse_and_stalled(self):
        """Too few cells to look like anything, and no longer growing.

        Population alone is the wrong test: an acorn starts at 7 cells and
        takes hundreds of generations to bloom, so a flat floor would kill the
        best seeds in the list. Requiring that it also stopped growing over the
        last TREND_GENS lets slow bloomers run while catching the genuinely
        dead case — a handful of gliders drifting on a black panel, which has
        steady population and steady churn and so slipped past every other
        test, leaving a near-empty screen up for the full 90s cap.
        """
        return (self.pop < self.SPARSE_POP
                and len(self.pophist) == self.TREND_GENS
                and self.pop <= self.pophist[0])

    def _brightness(self):
        """Dissolve between seeds instead of hard-cutting."""
        if self.dying > 0:
            return self.dying / float(self.FADE)
        if self.fresh > 0:
            return 1.0 - self.fresh / float(self.FADE + 1)
        return 1.0

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        b = self._brightness()
        if b <= 0.0:
            return bytes(buf)
        # Hue shifts slowly with generation for living-art feel
        phase = (self.gen % 180) / 180.0
        tint_r = int(20 * math.sin(phase * 6.28))
        tint_b = int(15 * math.cos(phase * 6.28))
        for y in range(HEIGHT):
            row = self.grid[y]
            arow = self.age[y]
            for x in range(WIDTH):
                if not row[x]:
                    continue
                a = arow[x]
                # Births flash white for exactly one generation. Without it the
                # whole board is one flat colour and you cannot see *where* the
                # rule is firing — the active frontier is the interesting part.
                # Then a neon age ramp: green → cyan → blue → magenta.
                if a <= 1:
                    col = (255, 255, 235)
                elif a < 4:
                    col = (80, 255, 160)
                elif a < 12:
                    col = (40, 255, 220)
                elif a < 24:
                    col = (100, 140, 255)
                else:
                    col = (220, 80, 255)
                col = (clamp(col[0] + tint_r, 0, 255), col[1],
                       clamp(col[2] + tint_b, 0, 255))
                if b < 1.0:
                    col = lerp_color(self.BG, col, b)
                put_px(buf, x, y, col)
        return bytes(buf)


# =============================================================================
# DODGE — fair lanes/gaps, near-miss points, smooth difficulty
# =============================================================================
class DodgeEngine:
    name = "dodge"
    tick_rate = 0.045

    BG = (0, 0, 0)
    YOU = (80, 255, 200)        # aqua runner
    BLOCK = (255, 50, 90)       # hot rose slabs
    BLOCK2 = (255, 200, 30)
    BLOCK3 = (160, 90, 255)
    DEAD = (255, 40, 60)
    SAFE = (0, 0, 0)

    def __init__(self):
        self.reset()

    def reset(self):
        self.x = WIDTH // 2
        self.blocks = []  # x, y, w, kind
        self.score = 0
        self.ticks = 0
        self.death_flash = 0
        self.spawn_cd = 0
        self.combo = 0

    def input(self, cmd):
        if cmd == "left":
            self.x = max(2, self.x - 3)
        elif cmd == "right":
            self.x = min(WIDTH - 3, self.x + 3)

    def auto(self):
        """Always sit in the centre of the nearest gap — simple, sharp, high scores.

        Each wave is two slabs with a hole. Find that hole, park in its middle,
        move at full speed. Looks great as ambient wall art.
        """
        speed = max(1, 1 + min(2, self.score // 15))
        if not self.blocks:
            mid = WIDTH // 2
            if self.x < mid - 1:
                self.x = min(WIDTH - 3, self.x + 3)
            elif self.x > mid + 1:
                self.x = max(2, self.x - 3)
            return

        # Threat order: highest y first = closest to hitting the player (NOT newest spawn)
        ys = sorted({b[1] for b in self.blocks if -4 <= b[1] < HEIGHT - 2}, reverse=True)
        if not ys:
            return

        def gap_center(y_band):
            occ = [False] * WIDTH
            for b in self.blocks:
                if abs(b[1] - y_band) > 2:
                    continue
                for dx in range(b[2]):
                    xx = b[0] + dx
                    if 0 <= xx < WIDTH:
                        occ[xx] = True
            best = None
            x = 0
            while x < WIDTH:
                if occ[x]:
                    x += 1
                    continue
                x0 = x
                while x < WIDTH and not occ[x]:
                    x += 1
                w = x - x0
                if w >= 3:
                    c = (x0 + x - 1) // 2
                    key = (w, -abs(c - self.x))
                    if best is None or key > best[0]:
                        best = (key, c)
            return best[1] if best else None

        # Survive the imminent wave first
        target = gap_center(ys[0])
        if target is None:
            target = self.x
        # If already safe for the imminent hit and next wave exists, start drifting
        if len(ys) > 1 and abs(self.x - target) <= 2:
            frames = (HEIGHT - 5 - ys[0]) / speed
            if frames > 5:
                nxt = gap_center(ys[1])
                if nxt is not None:
                    target = int(0.65 * target + 0.35 * nxt)

        if self.x < target - 1:
            self.x = min(WIDTH - 3, self.x + 3)
        elif self.x > target + 1:
            self.x = max(2, self.x - 3)

    def _spawn(self):
        """Always leave a passable gap — never soft-lock the player."""
        # Player is 3px wide; gap must stay human-steerable at panel refresh rates
        gap_w = max(9, 16 - self.score // 25)
        # Prefer gaps you can reach from current x in time
        reach = 10 + self.score // 10
        lo = max(1, self.x - reach - gap_w // 2)
        hi = min(WIDTH - gap_w - 1, self.x + reach - gap_w // 2)
        if lo > hi:
            lo, hi = 1, WIDTH - gap_w - 1
        gap_x = random.randint(lo, hi)
        kind = random.randint(0, 2)
        if gap_x > 2:
            self.blocks.append([0, -3, gap_x, kind, False])
        rx = gap_x + gap_w
        if rx < WIDTH - 1:
            self.blocks.append([rx, -3, WIDTH - rx, kind, False])

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        self.ticks += 1
        self.spawn_cd -= 1
        speed = 1 + min(2, self.score // 15)
        if self.spawn_cd <= 0:
            self._spawn()
            self.spawn_cd = max(9, 18 - self.score // 10)

        alive = []
        hit = False
        for b in self.blocks:
            b[1] += speed
            if b[1] < HEIGHT + 2:
                alive.append(b)
                if b[1] + 2 >= HEIGHT - 4 and b[1] <= HEIGHT - 2:
                    if self.x + 1 >= b[0] and self.x - 1 < b[0] + b[2]:
                        hit = True
            else:
                self.score += 1
                self.combo += 1
                if self.combo and self.combo % 5 == 0:
                    self.score += 2  # streak bonus
        self.blocks = alive
        if hit:
            self.death_flash = 10
            return
        # Near-miss: block passed close to player. `== HEIGHT - 5` only ever
        # landed at speed 1-2; at speed 3 (score >= 30, i.e. most of a real
        # run) -3 + 3k never equals HEIGHT-5=59, since 59 isn't a multiple of
        # 3 -- the bonus was silently dead exactly when players were good
        # enough to be earning it. A window + an award flag per block fixes
        # both the reachability and the risk of awarding it more than once
        # per block while it sits in the window.
        for b in self.blocks:
            if not b[4] and HEIGHT - 6 <= b[1] <= HEIGHT - 4:
                b[4] = True
                dist = min(abs(self.x - b[0]), abs(self.x - (b[0] + b[2] - 1)))
                if dist <= 3:
                    self.score += 1

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        colors = (self.BLOCK, self.BLOCK2, self.BLOCK3)
        for b in self.blocks:
            col = self.DEAD if flashing else colors[b[3] % 3]
            for dy in range(2):
                for dx in range(b[2]):
                    put_px(buf, b[0] + dx, b[1] + dy, col)
        # Draw the runner across the full height of its own hitbox. The
        # collision band is rows 58-63 (see tick), but this used to draw a
        # 3x2 sliver at rows 61-62 -- so a block could kill you while it
        # still looked a body-length short, which reads as the game
        # cheating. A figure that fills the box it actually occupies is both
        # fairer to read and far easier to find: it was previously the
        # smallest thing on a screen full of wide bold bars.
        col = self.DEAD if flashing else self.YOU
        top = HEIGHT - 6
        put_px(buf, self.x, top, col)                      # head
        for dy in (1, 2):
            for dx in (-1, 0, 1):
                put_px(buf, self.x + dx, top + dy, col)    # shoulders + torso
        stride = (self.ticks // 3) & 1                     # legs pump when running
        put_px(buf, self.x - 1, top + 3, col)
        put_px(buf, self.x + 1, top + 3, col)
        put_px(buf, self.x - (1 if stride else 0), top + 4, col)
        put_px(buf, self.x + (0 if stride else 1), top + 4, col)
        put_px(buf, self.x, top, (255, 255, 255))          # bright crown
        return bytes(buf)


# =============================================================================
# 2048 — proper merges, win state, stronger auto (corner strategy)
# =============================================================================
class Game2048Engine:
    name = "2048"
    tick_rate = 0.16

    SIZE = 4
    CELL = 14
    GAP = 2
    X_OFF = (WIDTH - (SIZE * 14 + 3 * 2)) // 2
    Y_OFF = (HEIGHT - (SIZE * 14 + 3 * 2)) // 2

    BG = (0, 0, 0)
    # Empty cells stay pure black → dynamic background shows through the board
    EMPTY = (0, 0, 0)
    COLORS = {
        2: (60, 80, 140),
        4: (80, 100, 200),
        8: (255, 140, 40),
        16: (255, 100, 30),
        32: (255, 60, 50),
        64: (255, 40, 40),
        128: (255, 220, 40),
        256: (255, 240, 80),
        512: (180, 255, 60),
        1024: (40, 255, 180),
        2048: (40, 255, 220),
        4096: (200, 100, 255),
        8192: (255, 80, 220),
    }
    DEAD = (255, 40, 60)
    WIN = (255, 240, 80)
    INK_LIGHT = (255, 255, 255)
    INK_DARK = (10, 10, 18)

    def __init__(self):
        self.reset()

    def reset(self):
        self.grid = [[0] * self.SIZE for _ in range(self.SIZE)]
        self.score = 0
        self.death_flash = 0
        self.win_flash = 0
        self.won = False
        self._pending = None
        self._spawn()
        self._spawn()

    def _spawn(self):
        empty = [(r, c) for r in range(self.SIZE) for c in range(self.SIZE)
                 if self.grid[r][c] == 0]
        if not empty:
            return False
        r, c = random.choice(empty)
        self.grid[r][c] = 4 if random.random() < 0.1 else 2
        return True

    def _line_left(self, line):
        tiles = [v for v in line if v]
        out = []
        gained = 0
        i = 0
        while i < len(tiles):
            if i + 1 < len(tiles) and tiles[i] == tiles[i + 1]:
                v = tiles[i] * 2
                out.append(v)
                gained += v
                i += 2
            else:
                out.append(tiles[i])
                i += 1
        out += [0] * (self.SIZE - len(out))
        return out, gained

    def _move_grid(self, grid, direction):
        gained = 0
        moved = False
        g = [row[:] for row in grid]
        if direction == "left":
            rows = []
            for r in range(self.SIZE):
                nl, ga = self._line_left(g[r])
                if nl != g[r]:
                    moved = True
                gained += ga
                rows.append(nl)
            g = rows
        elif direction == "right":
            rows = []
            for r in range(self.SIZE):
                nl, ga = self._line_left(list(reversed(g[r])))
                nl = list(reversed(nl))
                if nl != g[r]:
                    moved = True
                gained += ga
                rows.append(nl)
            g = rows
        elif direction == "up":
            for c in range(self.SIZE):
                col = [g[r][c] for r in range(self.SIZE)]
                nl, ga = self._line_left(col)
                if nl != col:
                    moved = True
                gained += ga
                for r in range(self.SIZE):
                    g[r][c] = nl[r]
        elif direction == "down":
            for c in range(self.SIZE):
                col = [g[r][c] for r in range(self.SIZE)]
                nl, ga = self._line_left(list(reversed(col)))
                nl = list(reversed(nl))
                if nl != col:
                    moved = True
                gained += ga
                for r in range(self.SIZE):
                    g[r][c] = nl[r]
        return g, gained, moved

    def _can_move(self, grid=None):
        g = grid if grid is not None else self.grid
        for r in range(self.SIZE):
            for c in range(self.SIZE):
                if g[r][c] == 0:
                    return True
                v = g[r][c]
                if c + 1 < self.SIZE and g[r][c + 1] == v:
                    return True
                if r + 1 < self.SIZE and g[r + 1][c] == v:
                    return True
        return False

    def _apply(self, direction):
        g, gained, moved = self._move_grid(self.grid, direction)
        if not moved:
            return False
        self.grid = g
        self.score += gained
        self._spawn()
        if not self.won and any(v >= 2048 for row in self.grid for v in row):
            self.won = True
            self.win_flash = 10
        if not self._can_move():
            self.death_flash = 14
        return True

    def input(self, cmd):
        if cmd in ("up", "down", "left", "right"):
            self._pending = cmd

    def _eval(self, grid):
        empty = sum(1 for row in grid for v in row if v == 0)
        mono = 0
        # Prefer increasing toward bottom-right (snake monotonicity)
        for r in range(self.SIZE):
            for c in range(self.SIZE - 1):
                a, b = grid[r][c], grid[r][c + 1]
                if a and b:
                    mono += math.log2(b) - math.log2(a) if b >= a else -math.log2(a)
        for c in range(self.SIZE):
            for r in range(self.SIZE - 1):
                a, b = grid[r][c], grid[r + 1][c]
                if a and b:
                    mono += math.log2(b) - math.log2(a) if b >= a else -math.log2(a)
        max_v = max((max(row) for row in grid), default=0)
        corner = 0.0
        if max_v and grid[self.SIZE - 1][self.SIZE - 1] == max_v:
            corner = math.log2(max_v) * 5.5
        elif max_v:
            # Soft penalty if max not in corner
            for r, c in ((self.SIZE - 1, self.SIZE - 2), (self.SIZE - 2, self.SIZE - 1)):
                if grid[r][c] == max_v:
                    corner = math.log2(max_v) * 2.0
        smooth = 0.0
        for r in range(self.SIZE):
            for c in range(self.SIZE):
                if not grid[r][c]:
                    continue
                for dr, dc in ((0, 1), (1, 0)):
                    rr, cc = r + dr, c + dc
                    if rr < self.SIZE and cc < self.SIZE and grid[rr][cc]:
                        smooth -= abs(math.log2(grid[r][c]) - math.log2(grid[rr][cc]))
        merges = 0
        for r in range(self.SIZE):
            for c in range(self.SIZE):
                v = grid[r][c]
                if not v:
                    continue
                if c + 1 < self.SIZE and grid[r][c + 1] == v:
                    merges += 1
                if r + 1 < self.SIZE and grid[r + 1][c] == v:
                    merges += 1
        return empty * 14.5 + mono * 1.1 + corner + smooth * 0.9 + merges * 2.0

    def _expectimax(self, grid, depth, is_player):
        if depth == 0:
            return self._eval(grid)
        if is_player:
            best = -1e18
            any_move = False
            for d in ("up", "left", "down", "right"):
                g2, gained, moved = self._move_grid(grid, d)
                if not moved:
                    continue
                any_move = True
                best = max(best, gained * 0.02 + self._expectimax(g2, depth - 1, False))
            return best if any_move else self._eval(grid) - 1000
        # Chance node: average over empty spawns (2 with 90%, 4 with 10%)
        empties = [(r, c) for r in range(self.SIZE) for c in range(self.SIZE) if grid[r][c] == 0]
        if not empties:
            return self._eval(grid) - 500
        # Cap samples so auto stays realtime on the arcade loop
        sample = empties if len(empties) <= 6 else random.sample(empties, 6)
        total = 0.0
        for r, c in sample:
            for val, p in ((2, 0.9), (4, 0.1)):
                g2 = [row[:] for row in grid]
                g2[r][c] = val
                total += p * self._expectimax(g2, depth - 1, True)
        return total / len(sample)

    def _label(self, v):
        """Text that fits a 12×12 inner tile with 3×5 digits."""
        if v <= 0:
            return ""
        if v < 1000:
            return str(v)
        # 1024 → 1K, 2048 → 2K, …
        if v % 1024 == 0:
            return f"{v // 1024}K"
        return str(v)[:3]

    def auto(self):
        """Strong corner bot: deep expectimax + hard bias to keep max tile parked."""
        order = ("up", "left", "down", "right")
        best, best_s = None, -1e18
        max_now = max(max(row) for row in self.grid)
        for d in order:
            g, gained, moved = self._move_grid(self.grid, d)
            if not moved:
                continue
            s = gained * 0.03 + self._expectimax(g, depth=2, is_player=False)
            # Never eject the max tile from the bottom-right corner if we can help it
            if max_now and g[self.SIZE - 1][self.SIZE - 1] == max_now:
                s += 25
            elif max_now and self.grid[self.SIZE - 1][self.SIZE - 1] == max_now:
                if g[self.SIZE - 1][self.SIZE - 1] != max_now:
                    s -= 40
            if s > best_s:
                best_s, best = s, d
        self._pending = best or random.choice(order)

    def tick(self):
        if self.death_flash > 0:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        if self.win_flash > 0:
            self.win_flash -= 1
        if self._pending:
            self._apply(self._pending)
            self._pending = None

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        flashing = self.death_flash > 0 and self.death_flash % 2 == 0
        winning = self.win_flash > 0 and self.win_flash % 2 == 0
        for r in range(self.SIZE):
            for c in range(self.SIZE):
                x0 = self.X_OFF + c * (self.CELL + self.GAP)
                y0 = self.Y_OFF + r * (self.CELL + self.GAP)
                v = self.grid[r][c]
                if v == 0:
                    col = self.EMPTY
                else:
                    col = self.COLORS.get(v, (255, 255, 255))
                if flashing and v:
                    col = self.DEAD
                elif winning and v >= 2048:
                    col = self.WIN
                for y in range(self.CELL):
                    for x in range(self.CELL):
                        if 0 < x < self.CELL - 1 and 0 < y < self.CELL - 1:
                            put_px(buf, x0 + x, y0 + y, col)
                        elif v:
                            put_px(buf, x0 + x, y0 + y,
                                   (max(0, col[0] // 3), max(0, col[1] // 3), max(0, col[2] // 3)))
                # Numbers — the whole point of watching 2048
                if v and not (flashing and self.death_flash % 2 == 0):
                    label = self._label(v)
                    # 3×5 glyphs, 1px gaps → width = 4*len - 1
                    tw = 4 * len(label) - 1
                    th = 5
                    tx = x0 + (self.CELL - tw) // 2
                    ty = y0 + (self.CELL - th) // 2
                    ink = self.INK_DARK if (col[0] + col[1] + col[2]) > 420 else self.INK_LIGHT
                    # Soft shadow for readability on bright tiles
                    if ink == self.INK_LIGHT:
                        draw_text3x5(buf, tx + 1, ty + 1, label, (0, 0, 0))
                    draw_text3x5(buf, tx, ty, label, ink)
        return bytes(buf)


# =============================================================================
# BOOT — theatrical curtain-rise splash that plays once on power-on, then
# hands off to the system menu. Stage curtains part from the centre, the
# system logo is revealed through the widening gap (not just faded in), then
# a bright flourish before it cuts to the menu. Any input skips straight
# there, so it never gets in the way of someone who's already seen it.
# =============================================================================
class TickerEngine(Browsable):
    """Stock + crypto ticker.

    The first non-game mode, and deliberately built to prove the shared
    contract holds for data modes too: this class does no I/O at all. It
    reads whatever market.FEED has already cached on its own thread and
    renders it, so a slow or dead network can never stall the render loop --
    which on the WLED panel would mean dropped frames, and on a Pi would
    mean a visibly stuttering panel.

    Layout is a spotlight over a scrolling tape: the spotlight is what makes
    it readable from across a room (one symbol, big), the tape is what makes
    it feel like a ticker. On a 32x32 production panel the spotlight alone
    still works, which is the sizing rule from PRODUCTION.md -- smaller
    panels show less at once, never fewer features.
    """

    name = "ticker"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    UP = (60, 230, 110)
    DOWN = (255, 70, 80)
    FLAT = (170, 178, 200)
    STALE = (255, 170, 40)

    SPOTLIGHT_TICKS = 90          # ~4.5s per symbol at this tick rate

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.rows = []
        self.age = None
        self.err = None
        self.cur = 0
        self.hold = 0
        self.scroll = 0.0
        self.cycling = True
        self.ticks = 0
        self._init_scroll()

    def has_content(self):
        """Ticker is not in AmbientEngine.SEQUENCE today, but every other
        data mode implements this and _available() calls it unguarded --
        so without it, simply adding "ticker" to that tuple would raise
        AttributeError at runtime. Cheap to provide, and it makes the
        data-mode contract uniform.

        Note this reads self.rows, not self.data: TickerEngine predates
        the shared data-dict convention and keeps its own attributes.
        """
        return bool(self.rows)

    # ---- input ---------------------------------------------------------
    def _step(self, direction):
        if self.rows:
            self.cur = (self.cur + direction) % len(self.rows)
            self.hold = 0

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        if cmd in ("rotate", "drop"):
            self.cycling = not self.cycling      # park on one symbol

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation ----------------------------------------------------
    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        self.rows, self.age, self.err = market.FEED.get()
        if self.rows:
            self.cur %= len(self.rows)
        self.scroll += 0.5
        if self.cycling and self.rows and self.browse.auto_ok:
            self.hold += 1
            if self.hold >= self.SPOTLIGHT_TICKS:
                self.hold = 0
                self.cur = (self.cur + 1) % len(self.rows)

    # ---- render --------------------------------------------------------
    @staticmethod
    def _fmt_price(v):
        # Keep it short enough to fit: precision matters least where the
        # number is biggest.
        if v >= 10000:
            return f"{v:,.0f}".replace(",", "")
        if v >= 1000:
            return f"{v:.0f}"
        if v >= 1:
            return f"{v:.2f}"
        return f"{v:.4f}"

    def _tint(self, pct):
        if pct > 0.05:
            return self.UP
        if pct < -0.05:
            return self.DOWN
        return self.FLAT

    def _draw_change_bar(self, buf, y, pct, col):
        """A centre-anchored magnitude bar: grows right on a gain, left on
        a loss, from a fixed centre tick. Gives the percentage a physical
        size you can read across a room, where the digits themselves are
        far too small to resolve -- and unlike a naive left-to-right bar,
        the direction is unmistakable because the origin never moves.
        Uses a SQUARE-ROOT scale, not linear: real daily moves cluster
        under 2% while the occasional crypto move hits 15%, so a linear
        bar rendered almost everything as 1-2 invisible pixels. sqrt gives
        small everyday moves real visible length while still leaving
        headroom for the big ones (±12% saturates)."""
        half = (WIDTH - 8) // 2
        cx = WIDTH // 2
        for x in range(cx - half, cx + half):
            put_px(buf, x, y, (24, 28, 38))
        n = int(math.sqrt(min(1.0, abs(pct) / 12.0)) * half)
        for i in range(n):
            x = cx + i if pct > 0 else cx - i
            put_px(buf, x, y, col)
            put_px(buf, x, y + 1, rim(col, 0.5))
        for dy in (-1, 0, 1, 2):
            put_px(buf, cx, y + dy, (90, 98, 118))     # fixed centre tick

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        accent = (90, 190, 255)

        if not self.rows:
            draw_header(buf, "MARKETS", accent)
            msg = "NO DATA" if self.err else "LOADING"
            draw_text_centered(buf, 30, msg, self.DOWN if self.err else self.INK_DIM)
            draw_text_centered(buf, 40, "." * (1 + (self.ticks // 12) % 3), self.INK_DIM)
            return bytes(buf)

        row = self.rows[self.cur]
        col = self._tint(row["pct"])
        pct = row["pct"]

        draw_header(buf, "MARKETS", accent,
                    right_tag=f"{self.cur + 1}/{len(self.rows)}",
                    stale=self.age is not None and self.age > 180)

        # Symbol: the hero, big and in the move's colour.
        # fit_text, NOT a blind [:4]: seven characters fit at scale=2, and
        # the old 4-char slice silently corrupted real symbols -- MATIC
        # (a supported coin) rendered "MATI", and GOOGL rendered "GOOG",
        # which is a DIFFERENT REAL TICKER (both are Alphabet share
        # classes). Worse, the scrolling tape below showed the correct
        # full symbol, so one screen displayed two different tickers for
        # the same row.
        draw_text_centered(buf, 11, fit_text(row["sym"], WIDTH - 6, scale=2), col, scale=2)

        # Price: the precise value, bright white-ish so it reads as the
        # authoritative number rather than competing with the symbol.
        draw_text_centered(buf, 25, self._fmt_price(row["price"]), (215, 222, 240))

        # Arrow + percentage on one baseline. Row i widens downward, so a
        # gain puts the apex at the TOP and a loss at the BOTTOM -- getting
        # this backwards is silent and worse than useless, since colour and
        # arrow would disagree and the arrow is the half that still reads
        # from across the room.
        chg = f"{abs(pct):.2f}%"
        arrow_w = 7
        ax = (WIDTH - (text_w(chg) + arrow_w)) // 2
        if pct > 0.05:
            for i in range(3):
                for x in range(-i, i + 1):
                    put_px(buf, ax + 2 + x, 33 + i, col)
        elif pct < -0.05:
            for i in range(3):
                w = 2 - i
                for x in range(-w, w + 1):
                    put_px(buf, ax + 2 + x, 33 + i, col)
        else:
            for x in range(5):
                put_px(buf, ax + x, 34, col)
        draw_text3x5(buf, ax + arrow_w, 33, chg, col)

        self._draw_change_bar(buf, 41, pct, col)

        draw_dots(buf, 46, len(self.rows), self.cur, on=accent)
        draw_divider(buf, 49)

        parts = []
        for r in self.rows:
            sign = "+" if r["pct"] >= 0 else "-"
            parts.append(f"{r['sym']} {self._fmt_price(r['price'])} "
                         f"{sign}{abs(r['pct']):.1f}%")
        draw_marquee(buf, 53, "   ".join(parts), self.INK_DIM, self.scroll)
        return bytes(buf)


class SatelliteEngine(Browsable, BigMomentSource):
    """Visible satellite passes -- the ISS and every other bright object,
    in ONE unified system.

    UNIFIED 2026-08-01, replacing a three-view design (ISS PASS, ISS LIVE,
    SKY) that had grown into two products sharing one engine rather than
    one coherent product. The tell: SKY's live-pass arc (real rise/peak
    azimuth, real progress along the pass) was already better, more
    honest code than ISS LIVE's decorative orbit-ring animation, while
    ISS PASS's chip-style urgency treatment was better than SKY's plain
    text -- so "level SKY up to ISS" would have been wrong in half the
    cases, and so would the reverse. See the commit for the full
    before/after comparison.

    TWO STATES, not three:
      UPCOMING     -- countdown to the next pass, browsable list
      OVERHEAD-NOW -- it's happening; the real horizon-to-horizon arc

    ONE LIST, not two data sources. skypass.FEED already includes the ISS
    -- it is genuinely one of the ~157 naked-eye objects in CelesTrak's
    `visual` catalogue -- so the ISS is simply an entry, sorted the same
    chronological way as everything else. On a night it isn't visible
    (which is the normal case: verified zero visible ISS passes over this
    location for three straight days), it is just absent, exactly like
    any other quiet object -- not a screen showing stale ISS data because
    the mode has nothing better to offer.

    THE ISS KEEPS ONE THING NOTHING ELSE HAS: continuous live telemetry
    (altitude/speed/sunlit) from satellite.FEED's wheretheiss.at position,
    which exists whether or not a pass is happening. That is real,
    unique data, so it gets a SLOT within the shared layout when the
    current entry is the ISS -- the same pattern as MLB's diamond
    appearing inside the shared sports renderer, not a separate screen.
    """

    name = "satellite"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    VISIBLE = (60, 230, 110)
    STALE = (255, 170, 40)
    HERO = (235, 242, 255)
    LOSE = (255, 70, 80)
    ORBIT = (40, 46, 66)
    ISS = (255, 226, 60)      # ISS badge tint -- the one visual distinction
                               # an entry gets; never a different layout.

    ACCENT = (150, 190, 255)  # ONE accent for the whole mode, was gold+blue

    # WINDOW FILTER (2026-08-08) -- the SAME violet as FlightEngine.WINDOW_RING,
    # deliberately, not a new color. One window, one config, one visual
    # convention: a viewer who has already learned "violet ring = visible out
    # my window" on the flight scope should not have to learn a second color
    # for the sky dome. See satellite.in_window()/draw_window_ring()'s notes.
    WINDOW_RING = (190, 110, 255)

    VIEW_TICKS = 160          # ~8s per pass while auto-advancing UPCOMING

    # THIRD view, ADDITIVE. The settled UPCOMING/OVERHEAD-NOW pair is
    # unchanged and still chooses itself from whether a pass is happening
    # -- that is VIEW_PASSES below, one slot covering both states exactly
    # as before. VIEW_SCOPE is a full-sky dome showing EVERY catalogued
    # object above the horizon at once, which is a different question
    # ("what is up there right now") from the one the pass views answer
    # ("when do I go outside, and where do I look").
    VIEW_PASSES = 0
    VIEW_SCOPE = 1
    SCOPE_TICKS = 240          # ~12s on the dome before returning to passes
    SWEEP_DEG_PER_TICK = 3.0

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"configured": False, "label": "HOME", "pos": None,
                    "pos_age": None, "err": None}
        self.sky = {"passes": [], "available": False, "err": None}
        self.cur = 0             # browse cursor into self.sky["passes"]
        self.hold = 0
        self.cycling = True
        self.ticks = 0
        self.view = self.VIEW_PASSES
        self.sweep = 0.0
        self._overhead_ids = set()   # (norad_id, rise) seen overhead last tick
        self._init_scroll()
        self._init_big_moments()
        # Seen-pass cursor for _detect_go_outside_pass() -- same one-shot
        # adopt-then-diff idiom as every other detector in this project
        # (GameDayEngine._seen_done, SportsEngine's five detectors): the
        # first read adopts whatever's already overhead/about to start
        # without firing, so a pass already in progress when this mode
        # opens can't replay itself as a fresh moment.
        self._seen_go_outside = None

    # ---- input -------------------------------------------------------
    def has_content(self):
        """Worth showing? A real home location AND (an upcoming pass OR a
        genuinely visible object crossing the sky RIGHT NOW).

        Second clause added 2026-08-02: the dome can show real bright
        objects mid-pass on a clear night while the predictor has nothing
        QUEUED next -- before this, that exact night would never surface
        the dome in ambient at all, withholding one of the best visuals in
        the whole project at the moment it would land best. Deliberately
        NOT "true whenever ANY object is above the horizon" -- confirmed
        live that in broad daylight 8-14 objects sit above the horizon and
        sky_now() correctly marks 0 of them `visible`, because "above the
        horizon" and "sunlit" are both true for plenty of objects at noon
        and none of those are visible to a person outside.
        Uses sky_now()'s `visible` flag (elevation + sunlit + observer
        darkness, same three-part test predict() itself uses), never the
        raw list -- the raw list would make this mode claim content nearly
        around the clock, exactly the invented-worth failure the "never
        invent" rule exists to prevent. Still deliberately NOT "true
        whenever ISS position resolves" the way the old always-on ISS LIVE
        view was -- continuous ISS telemetry with nothing visible is
        trivia, not the go-outside moment this mode exists for."""
        if not self.data.get("configured"):
            return False
        if self.sky.get("passes"):
            return True
        return any(o.get("visible") for o in (self.sky.get("sky_now") or []))

    def _step(self, direction):
        ps = self.sky.get("passes") or []
        if not ps:
            return
        self.cur = (self.cur + direction) % len(ps)
        self.hold = 0

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        if cmd in ("up", "down"):
            # SatelliteEngine is not VERTICAL_BROWSE, so Browsable._axis()
            # leaves up/down free to flip to the full-sky dome and back.
            self.view = (self.VIEW_SCOPE if self.view == self.VIEW_PASSES
                         else self.VIEW_PASSES)
            self.hold = 0
        elif cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass

    # ---- simulation ----------------------------------------------------
    def _now(self):
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    def _seconds_to_rise(self, p):
        return (p["rise"] - self._now()).total_seconds()

    def _is_overhead(self, p):
        secs = self._seconds_to_rise(p)
        return -p["duration_s"] <= secs <= 0

    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        self.data = satellite.FEED.get()
        lat, lon, _lbl = satellite.FEED.get_location()
        self.sky = skypass.FEED.get(lat, lon)
        ps = self.sky.get("passes") or []
        if ps:
            self.cur %= len(ps)
        else:
            self.cur = 0
            # NO QUEUED PASS is not the same as nothing to show -- a real
            # object can be visibly crossing the sky right now with
            # nothing predicted NEXT (has_content() reflects exactly this
            # case; see its docstring). VIEW_PASSES has nothing to draw
            # for an empty pass list, so force the dome rather than
            # leaving the view pinned to a screen that would render "NO
            # VISIBLE PASSES" while a real object is genuinely up there --
            # found by rendering this exact scenario, not by inspection:
            # has_content() alone does not guarantee frame() shows the
            # content it claims to have.
            if any(o.get("visible") for o in (self.sky.get("sky_now") or [])):
                self.view = self.VIEW_SCOPE

        # A pass beginning overhead PREEMPTS wherever the cursor was --
        # same idiom as a golf notable move or a GAME DAY finish: the
        # moment is the content. Identified by (norad_id, rise) rather
        # than list index, since the list can be recomputed mid-pass.
        #
        # When MORE THAN ONE pass goes overhead in the same tick -- real,
        # not hypothetical: caught live 2026-08-01, OAO 3 (peak 75.8 deg)
        # and SEASAT 1 (peak 19.8 deg) both rose in the same tick -- jump
        # to whichever is the BETTER pass, not whichever the loop happens
        # to see last. "Last in list order" is arbitrary; a dramatically
        # better pass losing to a mediocre one purely by iteration order
        # would be a silent quality regression at the exact moment the
        # mode is supposed to be showing the best of what's happening.
        now_overhead = set()
        best_new = None            # (rank, peak_el, index) among newly-overhead
        newly_overhead_passes = []
        for i, p in enumerate(ps):
            key = (p.get("norad_id"), p["rise"])
            if self._is_overhead(p):
                now_overhead.add(key)
                if key not in self._overhead_ids:
                    newly_overhead_passes.append(p)
                    rank = skypass.quality_rank(p)[1]
                    cand = (rank, p["peak_el"], i)
                    if best_new is None or cand[:2] > best_new[:2]:
                        best_new = cand
        if best_new is not None:
            self.cur = best_new[2]
        self._overhead_ids = now_overhead
        self._detect_go_outside_pass(newly_overhead_passes)

        # Sweep is the mode's heartbeat -- advanced every tick regardless of
        # view or pause, same as the flight scope. One float add.
        self.sweep = (self.sweep + self.SWEEP_DEG_PER_TICK) % 360.0

        if self.cycling and self.browse.auto_ok and ps:
            cur_overhead = self._is_overhead(ps[self.cur])
            # A pass ACTUALLY HAPPENING outranks the dome and pins the view
            # to it -- that is the go-outside moment this whole mode exists
            # for, and the settled OVERHEAD-NOW behaviour must not become
            # something the new scope can interrupt.
            if cur_overhead:
                self.view = self.VIEW_PASSES
                self.hold = 0
            else:
                self.hold += 1
                if self.view == self.VIEW_SCOPE:
                    if self.hold >= self.SCOPE_TICKS:
                        self.hold = 0
                        self.view = self.VIEW_PASSES
                elif self.hold >= self.VIEW_TICKS:
                    self.hold = 0
                    self.cur = (self.cur + 1) % len(ps)
                    # After walking the pass list once, give the dome a
                    # turn -- but only if it has something real to draw.
                    if self.cur == 0 and (self.sky.get("sky_now") or []):
                        self.view = self.VIEW_SCOPE

        self.score = len(ps)

    # A pass this close to the zenith is genuinely rare -- ELEV_EXCELLENT
    # (skypass.py, 60 deg) already gates TIER_INTERRUPT below; this sits
    # well above it on purpose, reserved for the "directly overhead"
    # moment, not merely "a good pass".
    GO_OUTSIDE_TAKEOVER_EL = 80.0

    def _detect_go_outside_pass(self, newly_overhead_passes):
        """TIER_TAKEOVER for a >=80 deg peak-elevation pass beginning
        right now (near-zenith, directly overhead); TIER_INTERRUPT for
        any other GO-OUTSIDE-grade pass beginning right now
        (skypass.quality_rank rank 3, the same BRIGHT tier the UPCOMING
        chip already uses). No TIER_FLASH here, deliberately: a lower-
        grade pass beginning is already fully served by the existing
        chip/arc treatment in-mode, and flashing on top of a screen
        that's ALREADY showing the pass would be redundant, not useful --
        unlike flights, where the scope does not otherwise call out a
        new aircraft type at all.

        The ISS is NOT special-cased here, deliberately -- unifying it
        into one ordinary catalogue entry was the entire point of the
        2026-08-01 satellite rework, and re-privileging it in the
        celebration path would undo that. It fires on its own real
        merits, same as every other object.

        One-shot via self._seen_go_outside: None on the very first tick
        adopts silently (a pass already overhead when the mode starts
        must not fire), same idiom as every other detector here."""
        if self._seen_go_outside is None:
            self._seen_go_outside = True
            return
        if not newly_overhead_passes:
            return
        best = max(newly_overhead_passes, key=lambda p: p.get("peak_el") or 0)
        el = best.get("peak_el") or 0
        name = best.get("name") or "OBJECT"
        color = self.ISS if best.get("is_iss") else self.ACCENT
        if el >= self.GO_OUTSIDE_TAKEOVER_EL:
            self._set_big_moment("OVERHEAD NOW", name, f"{el:.0f} DEG PEAK",
                                 color, tier=TIER_TAKEOVER, system=SYSTEM_SATELLITE)
        elif skypass.quality_rank(best)[1] == 3:
            self._set_big_moment("GO OUTSIDE", name, f"{el:.0f} DEG PEAK",
                                 color, tier=TIER_INTERRUPT, system=SYSTEM_SATELLITE)

    def ambient_weight(self):
        """Weighted by the most urgent thing this mode currently has to
        show -- the best pass across the WHOLE list, not the ISS alone.
        The ISS being invisible for days must not suppress a genuinely
        bright SpaceMobile or Cosmos pass from earning real dwell time."""
        ps = self.sky.get("passes") or []
        if not ps:
            return 1.0
        if any(self._is_overhead(p) for p in ps):
            return 3.0                     # it's happening right now
        best_rank = max((skypass.quality_rank(p)[1] for p in ps), default=1)
        weight = {3: 2.5, 2: 1.5}.get(best_rank, 1.0)
        # WINDOW FILTER (2026-08-08): a small ADDITIVE nudge, same relationship
        # to the base weight as flights.WINDOW_BOOST has to _notable() rank --
        # never overriding a stronger existing signal (quality still decides
        # the tier), only breaking a tie in favor of "and it's out my window".
        # 0.25 is kept well under the smallest real gap between tiers here
        # (0.5, between GOOD's 1.5 and BRIGHT's 2.5) so it can never cross one.
        if any(p.get("in_window") for p in ps):
            weight += 0.25
        return weight

    # ---- render --------------------------------------------------------
    @staticmethod
    def _fmt_countdown(secs):
        secs = max(0, int(secs))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}H {m:02d}M"
        if m > 0:
            return f"{m}M {s:02d}S"
        return f"{s}S"

    def _draw_stat_pair(self, buf, y, left_label, left_val, right_label, right_val):
        draw_text3x5(buf, 3, y, left_label, (86, 94, 116))
        draw_text3x5(buf, 3, y + 6, left_val, self.INK)
        rw = max(text_w(right_label), text_w(right_val))
        rx = WIDTH - 3 - rw
        draw_text3x5(buf, rx, y, right_label, (86, 94, 116))
        draw_text3x5(buf, rx, y + 6, right_val, self.INK)

    def _frame_unconfigured(self, buf):
        draw_header(buf, "SKY", self.ACCENT)
        draw_text_centered(buf, 26, "SET HOME", self.INK_DIM)
        draw_text_centered(buf, 34, "LOCATION", self.INK_DIM)
        return bytes(buf)

    def _draw_chip(self, buf, y, tag, color):
        """The shared urgency chip. Was ISS-PASS-only; every object gets
        it now. A filled background reads before any text resolves --
        the single most important fact here is "worth going outside?"."""
        tw = text_w(tag)
        x0 = (WIDTH - tw - 6) // 2
        for x in range(x0, x0 + tw + 6):
            for yy in range(y, y + 8):
                put_px(buf, x, yy, rim(color, 0.22))
        draw_text_centered(buf, y + 2, tag, color)

    def _chip_tag(self, rank):
        if rank >= 3:
            return "GO OUTSIDE"
        if rank == 2:
            return "GOOD PASS"
        return "VISIBLE"

    def _frame_upcoming(self, p):
        buf = blank()
        fill(buf, self.BG)
        ps = self.sky.get("passes") or []
        stale = bool(self.sky.get("age") and self.sky["age"] > 3600 * 2)
        is_iss = bool(p.get("is_iss"))

        draw_header(buf, "SKY", self.ACCENT,
                    right_tag=f"{self.cur + 1}/{len(ps)}", stale=stale)

        # Laid out with a Y-CURSOR, not fixed offsets -- a fixed 1px gap
        # after a scale=2 name looked fine on short strings and collided
        # on longer ones ("TERRA" overlapped "1H 50M"). budget WIDTH-4:
        # WIDTH-6 was one pixel too tight and truncated exactly-15-char
        # names like "SPACEMOBILE-001" for no real reason.
        name = p["name"]
        budget = WIDTH - 4
        scale = 2 if text_w(name, 2) <= budget else 1
        name_col = self.ISS if is_iss else self.INK
        y = 10
        draw_text_centered(buf, y, fit_text(name, budget, scale),
                           name_col, scale=scale)
        y += 5 * scale + 3

        secs = self._seconds_to_rise(p)
        draw_text_centered(buf, y, self._fmt_countdown(secs), self.HERO, scale=2)
        y += 10 + 2

        tag, rank = skypass.quality_rank(p)
        col = self.VISIBLE if rank >= 2 else self.INK
        self._draw_chip(buf, y, self._chip_tag(rank), col)
        # WINDOW FILTER (2026-08-08): chronological order is left alone --
        # ps stays soonest-first (see skypass.predict()'s own docstring),
        # because reordering it would make a window pass jump ahead of a
        # sooner non-window one, which is confusing on a COUNTDOWN screen in
        # a way it isn't on the flight scope's distance ranking. Instead the
        # SAME violet ring flights already established is drawn as a small
        # badge beside the chip -- flags this specific pass without adding a
        # second tier of text next to GO OUTSIDE/GOOD PASS/VISIBLE, and
        # without inventing a new visual (see draw_window_ring()'s own note
        # on staying the "simple" version).
        if p.get("in_window"):
            tw = text_w(self._chip_tag(rank))
            ring_x = (WIDTH - tw - 6) // 2 - 6
            draw_window_ring(buf, ring_x, y + 4, self.WINDOW_RING)

        pos = self.data.get("pos")
        if is_iss and pos:
            # The telemetry SLOT: real, continuous data that exists only
            # for the ISS, drawn in the row that otherwise carries the
            # compass/duration line -- same real estate, different
            # content, exactly like a sport-specific glyph inside the
            # shared sports renderer.
            sun = "SUNLIT" if pos.get("sunlit") else "DARK"
            line = f"{km_to_mi(pos['alt_km']):.0f}MI  {sun}"
        else:
            line = "%s TO %s  %s" % (skypass.compass(p["rise_az"]),
                                     skypass.compass(p["peak_az"]),
                                     self._fmt_dur(p["duration_s"]))
        draw_text_centered(buf, 56, fit_text(line, WIDTH - 6), self.INK_DIM)
        return bytes(buf)

    @staticmethod
    def _fmt_dur(secs):
        m, s = divmod(int(secs or 0), 60)
        return f"{m}M{s:02d}" if m else f"{s}S"

    def _frame_overhead(self, p):
        """OVERHEAD-NOW. One arc renderer for every object -- this is
        SKY's original arc, kept as-is because it was already the more
        honest of the two live treatments: real rise/peak azimuth, real
        elevation, a marker at genuine real-time progress along the pass,
        not a decorative orbit ring."""
        buf = blank()
        fill(buf, self.BG)
        is_iss = bool(p.get("is_iss"))

        draw_header(buf, "SKY", self.ACCENT, right_tag="NOW")

        name = p["name"]
        budget = WIDTH - 4
        scale = 2 if text_w(name, 2) <= budget else 1
        name_col = self.ISS if is_iss else self.VISIBLE
        draw_text_centered(buf, 11, fit_text(name, budget, scale),
                           name_col, scale=scale)

        pos = self.data.get("pos")
        y_top = 24
        if is_iss and pos:
            # Speed lives HERE rather than in the upcoming view: while it
            # is actually crossing the sky is when "17,000 mph" means
            # something. The arc compresses a few px to make room.
            y_top = 27
            draw_text_centered(buf, 19, f"{kmh_to_mph(pos['vel_kmh']):.0f} MPH",
                               self.ISS)

        self._draw_pass_arc(buf, p, self._seconds_to_rise(p), y_top=y_top)
        return bytes(buf)

    def _draw_pass_arc(self, buf, p, secs, y_top=24):
        """The pass as a real arc: horizon to horizon, peaking at the
        pass's actual maximum elevation, with a marker at where the
        object is RIGHT NOW. Elevation maps to height, azimuth to
        horizontal position -- the picture is the information."""
        y_horizon = 50
        dur = max(1, p["duration_s"])
        prog = min(1.0, max(0.0, (-secs) / dur))
        peak_frac = max(0.05, min(1.0, p["peak_el"] / 90.0))

        for x in range(4, WIDTH - 4):
            put_px(buf, x, y_horizon, (40, 46, 60))

        pts = []
        for i in range(41):
            t = i / 40.0
            x = int(6 + t * (WIDTH - 12))
            h = math.sin(t * math.pi) * peak_frac
            y = int(y_horizon - h * (y_horizon - y_top))
            pts.append((x, y))
        for x, y in pts:
            put_px(buf, x, y, (70, 90, 130))

        idx = min(len(pts) - 1, int(prog * (len(pts) - 1)))
        cx, cy = pts[idx]
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            put_px(buf, cx + dx, cy + dy, self.VISIBLE)
        put_px(buf, cx, cy, (255, 255, 255))

        draw_text3x5(buf, 3, y_horizon + 3, skypass.compass(p["rise_az"]), self.INK_DIM)
        rt = skypass.compass(p["peak_az"])
        draw_text3x5(buf, WIDTH - 3 - text_w(rt), y_horizon + 3, rt, self.INK_DIM)

    # ---- full-sky scope (SKY-DOME projection: bearing + ELEVATION) -------
    # Deliberately NOT the flight scope's math. Here the radius axis is
    # ELEVATION ANGLE, not ground distance: centre = zenith (straight up),
    # edge = the horizon. That is the standard all-sky/planetarium
    # convention, and it is the natural full-sky generalisation of the
    # existing single-pass horizon arc. Linear in elevation, which needs
    # none of the sqrt correction the ground radar required -- elevation is
    # already bounded 0..90 and real objects spread across it evenly.
    SCOPE_RING_EL = (60, 30)     # plus the horizon itself at the rim

    @staticmethod
    def _dome_r_frac(el_deg):
        """Elevation -> normalised radius. 90 deg (zenith) = 0.0 (centre),
        0 deg (horizon) = 1.0 (rim)."""
        if not isinstance(el_deg, (int, float)):
            return None
        return max(0.0, min(1.0, (90.0 - el_deg) / 90.0))

    def _frame_scope(self):
        objs = self.sky.get("sky_now") or []
        buf = blank()
        fill(buf, self.BG)
        draw_header(buf, "SKY", self.ACCENT, right_tag=f"{len(objs)}",
                    stale=bool(self.sky.get("sky_now_age")
                               and self.sky["sky_now_age"] > 30))

        draw_scope_rings(buf, [self._dome_r_frac(el) for el in self.SCOPE_RING_EL]
                         + [1.0], color=(20, 34, 58))
        draw_scope_crosshair(buf, color=(20, 34, 58))
        draw_scope_sweep(buf, self.sweep, color=(24, 66, 120))

        for o in objs:
            frac = self._dome_r_frac(o.get("el"))
            az = o.get("az")
            if frac is None or az is None:
                continue
            x, y = scope_xy(az, frac)
            # WINDOW FILTER (2026-08-08): the centerpiece of the satellite
            # window feature -- "is this object visible out my window RIGHT
            # NOW", answered with the exact same primitive and placement
            # flights.py's scope already established (draw_window_ring()
            # under the icon, drawn first so it's never obscured). `az` is
            # skypass.sky_now()'s live current azimuth, same bearing
            # convention as flights' dir_deg, confirmed via skypass.py
            # before reuse -- see satellite.in_window()'s own note.
            if o.get("in_window"):
                draw_window_ring(buf, x, y, self.WINDOW_RING)
            # `visible` (elevation + sunlit + observer darkness), not just
            # `sunlit`, drives the bright/dim split -- an object that is
            # sunlit but it's broad daylight here, or one in Earth's
            # shadow, is above the horizon but genuinely cannot be seen
            # from the ground right now, so it draws present-but-dim
            # rather than given the same weight as one you could actually
            # go out and look at. Same `visible` flag has_content() uses
            # to decide whether the dome is worth showing at all.
            if o.get("is_iss"):
                col = self.ISS
            elif o.get("visible"):
                col = self.VISIBLE
            else:
                col = (64, 72, 96)
            draw_scope_target(buf, x, y, col,
                              glow=scope_glow(az, self.sweep),
                              big=bool(o.get("is_iss")))

        # Zenith marker: the observer looking straight up. Same "you are
        # here" role the home diamond plays on the ground radar, which is
        # why it reuses that mark rather than inventing a second one.
        draw_scope_home(buf, color=(120, 130, 155))

        if not objs:
            # y=31 collided with the crosshair's new W/E letter labels
            # (both sit at cy-2=31) -- caught by render_audit's rotate-
            # driven coverage, not by eye. y=40 clears the W/E row
            # (ends y=36) and the S label row (starts y=47) with margin.
            draw_text_centered(buf, 40, "NOTHING UP", self.INK_DIM)
        lbl = "EL " + "/".join(str(e) for e in self.SCOPE_RING_EL) + "/0"
        draw_text_centered(buf, 58, fit_text(lbl, WIDTH - 4), (86, 94, 116))
        return bytes(buf)

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        if not self.data.get("configured"):
            return self._frame_unconfigured(buf)

        if self.view == self.VIEW_SCOPE:
            return self._frame_scope()

        ps = self.sky.get("passes") or []
        if not ps:
            draw_header(buf, "SKY", self.ACCENT)
            if not self.sky.get("available"):
                draw_text_centered(buf, 26, "PREDICTOR", self.INK_DIM)
                draw_text_centered(buf, 34, "UNAVAILABLE", self.INK_DIM)
            elif self.sky.get("err"):
                draw_text_centered(buf, 30, "NO SKY DATA", self.LOSE)
            else:
                draw_text_centered(buf, 26, "NO VISIBLE", self.INK_DIM)
                draw_text_centered(buf, 34, "PASSES", self.INK_DIM)
            return bytes(buf)

        p = ps[self.cur % len(ps)]
        if self._is_overhead(p):
            return self._frame_overhead(p)
        return self._frame_upcoming(p)


class FlightEngine(Browsable, BigMomentSource):
    """Live ADS-B flight tracker.

    Same discipline as Ticker/Satellite: no I/O in this class. Reads
    whatever flights.FEED has cached and spotlights one aircraft at a time,
    rotating every ~12s per the spec (10-15s). left/right steps through the
    list manually; the face button pauses the rotation.

    Identifier display is deliberately scale=1, not the scale=2 the ticker
    uses for its 3-4 char symbols -- real callsigns run up to 7-8
    characters (DAL1362, GJS4494), and forcing scale=2 would either clip
    or need per-string width math to avoid it. Safe and always-fits beats
    flashy-but-fragile for exactly the class of bug the ticker had.
    """

    name = "flights"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    PLANE = (120, 200, 255)
    ROUTE = (255, 226, 60)
    STALE = (255, 170, 40)
    DOT_ON = (150, 160, 185)
    DOT_OFF = (40, 44, 56)

    VIEW_TICKS = 240          # ~12s per aircraft at this tick rate

    COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

    # Altitude-band colour coding for the heading icon. Real ATC displays
    # use altitude, not distance, as the primary "what kind of traffic is
    # this" signal -- low/climbing traffic near an airport behaves and
    # matters differently from cruising traffic overhead, regardless of
    # how far away either one is. Distance is still shown as text (NM +
    # compass direction) so nothing is lost, it's just not what drives the
    # icon's colour.
    ALT_BANDS = (
        (5000,  (255, 90, 70)),    # LOW -- near the ground, climbing/descending
        (15000, (255, 200, 60)),   # MID -- climbing/descending, still close-in
        (30000, (120, 200, 255)),  # CRUISE -- typical airliner cruise band
    )
    ALT_HIGH_COLOR = (200, 140, 255)   # HIGH -- above typical cruise (long-haul/military)
    ALT_UNKNOWN_COLOR = (150, 160, 185)

    @classmethod
    def _alt_color(cls, alt_ft):
        if not isinstance(alt_ft, (int, float)):
            return cls.ALT_UNKNOWN_COLOR
        for cutoff, color in cls.ALT_BANDS:
            if alt_ft < cutoff:
                return color
        return cls.ALT_HIGH_COLOR

    @staticmethod
    def _ac_kind(ac):
        """Which scope icon this real aircraft gets -- from its real
        ADS-B emitter category (flights.CAT_*), plus real ICAO type
        designator to split GA from a business jet WHERE that's
        actually knowable (see flights.BIZJET_TYPES's own honesty note:
        both share the same category, so type is the only real signal
        that can tell them apart, and type isn't always broadcast)."""
        cat = ac.get("category") or ""
        if cat == flights.CAT_ROTOR:
            return SCOPE_ICON_HELI
        if cat in (flights.CAT_LARGE, flights.CAT_HIGH_VORTEX, flights.CAT_HEAVY):
            return SCOPE_ICON_AIRLINER
        if cat in (flights.CAT_LIGHT, flights.CAT_SMALL):
            t = (ac.get("type") or "").strip().upper()
            if t in flights.BIZJET_TYPES:
                return SCOPE_ICON_BIZJET
            return SCOPE_ICON_GA
        # No/unknown category (real, happens) -- default to the airliner
        # dart rather than guessing GA or heli specifically; most real
        # traffic this project has observed IS category A3 large, so
        # this default is the statistically honest fallback, not an
        # arbitrary one (see flights.py's own 213-aircraft sample note).
        return SCOPE_ICON_AIRLINER

    # Dead-reckoning duration cap -- 2x the real poll cadence
    # (flights.POSITION_REFRESH, ~15s). If a real poll is late/stalled,
    # extrapolation freezes at this ceiling rather than projecting an
    # aircraft further and further into an increasingly unreliable future
    # on stale velocity data -- same "an honest gap beats a lie" principle
    # as the selection-loss-on-range-exit handling above.
    DR_MAX_MULT = 2.0

    def _update_dead_reckoning(self, ac_list, new_poll, t_ref):
        """Render-side position extrapolation between real ADS-B polls.

        WHY: flights.FEED refreshes every ~15s (flights.POSITION_REFRESH);
        without this, every aircraft on the scope sits frozen at its last-
        polled bearing/distance and teleports once per poll -- real user
        feedback was that this reads as static/jumpy, not "live". Every
        aircraft dict already carries REAL gs_kt (ground speed, kt) and
        track_deg (real heading) from ADS-B -- this is physics-based
        estimation from observed real velocity, not invented motion, the
        same category of derived-but-real inference flights._phase()
        already does for CLIMB/DESCEND/CRUISE classification, just
        continuous instead of discrete.

        ZERO NEW I/O: pure math over ac_list, which tick() already read
        from flights.FEED.get() this same call.

        MATH: dist_nm/dir_deg (real, ADS-B bearing-FROM-home convention,
        confirmed against every other scope_xy() use in this project) are
        converted to a local nm-plane position matching scope_xy()'s own
        0=N-up/clockwise convention (x = cx + r*sin(bearing), y = cy -
        r*cos(bearing)): x_nm = dist_nm*sin(dir_deg), y_nm =
        dist_nm*cos(dir_deg) (north = +y). On a REAL poll (new_poll=True,
        detected by tick() from flights.FEED.get()'s own `age` signal
        going down -- no second I/O source needed), this real position
        becomes the new dead-reckoning REFERENCE for that aircraft, keyed
        by the SAME identity `_sel_key()` selection already uses (hex
        preferred, ident fallback) -- the real poll always wins, no
        blending toward it.

        EVERY tick (not just on a poll), if gs_kt/track_deg are BOTH real
        (neither None -- honest degrade otherwise: hold the last known
        REAL position, extrapolate nothing), advance from the reference:
        speed_nm_per_sec = gs_kt / 3600, dx = speed_nm_per_sec *
        sin(track_deg) * elapsed, dy = speed_nm_per_sec * cos(track_deg) *
        elapsed, elapsed clamped to [0, DR_MAX_MULT * POSITION_REFRESH].
        Recomputed dist_nm/dir_deg from the advanced (x_nm+dx, y_nm+dy)
        are stashed on the aircraft dict as `_ext_dist_nm`/`_ext_dir_deg`
        for _frame_scope()'s icon position ONLY -- every other read of
        `dist_nm`/`dir_deg` (text fields, sorting, notability) still sees
        the real polled values untouched, so nothing ever displays a
        number that isn't literally what ADS-B reported.

        No explicit outward clamp at RADIUS_NM's rim is needed:
        _scope_r_frac() already clamps r_frac to [0, 1] via
        `min(1.0, dist_nm / RADIUS_NM)` before the sqrt, exactly the same
        handling a real near-rim aircraft already gets -- an extrapolated
        aircraft drawn past the rim just draws AT the rim, matching
        existing behaviour rather than inventing new edge-of-scope
        handling.

        BOUNDED: self._dr is rebuilt to only the keys currently in
        `ac_list` every call -- an aircraft that leaves RADIUS_NM (already
        dropped by tick()'s SELECTION-LOSS CHECK for selection, same
        underlying list) has its dead-reckoning state cleaned up here too,
        never accumulating.
        """
        live_keys = set()
        for ac in ac_list:
            key = self._sel_key(ac)
            if key is None:
                continue
            dist_nm = ac.get("dist_nm")
            dir_deg = ac.get("dir_deg")
            if not isinstance(dist_nm, (int, float)) or dir_deg is None:
                continue          # no real position fix at all -- nothing to seed
            live_keys.add(key)
            if new_poll or key not in self._dr:
                x_nm = dist_nm * math.sin(math.radians(dir_deg))
                y_nm = dist_nm * math.cos(math.radians(dir_deg))
                self._dr[key] = {"x_nm": x_nm, "y_nm": y_nm, "t_ref": t_ref}

        for key in list(self._dr):
            if key not in live_keys:
                del self._dr[key]

        now = time.time()
        for ac in ac_list:
            key = self._sel_key(ac)
            ref = self._dr.get(key)
            if ref is None:
                continue
            gs = ac.get("gs_kt")
            trk = ac.get("track_deg")
            if not isinstance(gs, (int, float)) or trk is None:
                continue          # honest degrade -- hold last known real position
            elapsed = now - ref["t_ref"]
            elapsed = max(0.0, min(elapsed, self.DR_MAX_MULT * flights.POSITION_REFRESH))
            speed_nm_s = gs / 3600.0
            dx = speed_nm_s * math.sin(math.radians(trk)) * elapsed
            dy = speed_nm_s * math.cos(math.radians(trk)) * elapsed
            x_nm, y_nm = ref["x_nm"] + dx, ref["y_nm"] + dy
            ac["_ext_dist_nm"] = math.hypot(x_nm, y_nm)
            ac["_ext_dir_deg"] = math.degrees(math.atan2(x_nm, y_nm)) % 360.0

    # Real flown-path trail -- ~5min of real polls at flights.POSITION_
    # REFRESH (~15s): 20 * 15s = 300s. Bounded per aircraft, evicted the
    # moment an aircraft leaves the tracked list (see _update_trail()).
    TRAIL_MAX_POINTS = 20

    def _update_trail(self, ac_list, new_poll):
        """Bounded per-aircraft REAL flown-path history, for the "show the
        path it's flown" feature -- keyed by the SAME `_sel_key()` identity
        as selection and dead reckoning above, reusing that exact x_nm/
        y_nm local-plane representation rather than a second scheme.

        Sampled ONLY on a REAL poll refresh (`new_poll`), never per render
        tick -- a trail built from the dead-reckoned/extrapolated icon
        position above would compound estimation error into a feature
        whose entire point is showing where the aircraft REALLY was, not
        where physics guesses it might be between polls.

        BOUNDED the same way every other keyed cache in this project is
        (THE HANGAR, self._dr above): capped at TRAIL_MAX_POINTS real
        samples per aircraft, and rebuilt to only the keys currently in
        `ac_list` every call -- an aircraft that leaves range or hasn't
        been seen in a while has its trail dropped here, never
        accumulating unbounded.
        """
        live_keys = set()
        for ac in ac_list:
            key = self._sel_key(ac)
            if key is None:
                continue
            live_keys.add(key)
            if not new_poll:
                continue          # only sample real polls, not every render tick
            dist_nm = ac.get("dist_nm")
            dir_deg = ac.get("dir_deg")
            if not isinstance(dist_nm, (int, float)) or dir_deg is None:
                continue          # no real position fix -- nothing to record
            x_nm = dist_nm * math.sin(math.radians(dir_deg))
            y_nm = dist_nm * math.cos(math.radians(dir_deg))
            pts = self._trail.setdefault(key, [])
            pts.append((x_nm, y_nm))
            if len(pts) > self.TRAIL_MAX_POINTS:
                del pts[0]
        for key in list(self._trail):
            if key not in live_keys:
                del self._trail[key]

    def _trail_points_px(self, key, cx, cy, r):
        """Real trail history for one aircraft, converted to scope pixel
        coordinates through the SAME sqrt-scaled projection every other
        scope element uses (`_scope_r_frac()` + `scope_xy()`) -- a trail
        drawn on a different scale than the live icon would visibly
        mismatch it."""
        out = []
        for x_nm, y_nm in (self._trail.get(key) or []):
            dist_nm = math.hypot(x_nm, y_nm)
            brg = math.degrees(math.atan2(x_nm, y_nm)) % 360.0
            frac = self._scope_r_frac(dist_nm)
            if frac is None:
                continue
            out.append(scope_xy(brg, frac, cx=cx, cy=cy, radius=r))
        return out

    @staticmethod
    def _hangar_kind(type_code):
        """Which sprite kind a THE HANGAR entry gets -- same four
        SCOPE_ICON_* buckets `_ac_kind()` uses for the live scope, but
        keyed on the real ICAO `type` code alone (a persisted Hangar
        entry has no live ADS-B `category` field to read, only `type`).
        Uses flights.HANGAR_HELI_TYPES/HANGAR_AIRLINER_TYPES/BIZJET_TYPES
        -- all three seeded ONLY from the real 198 entries already in
        hangar_log.jsonl (see flights.py's own comment on that table for
        the full audit trail). Anything not positively identified in one
        of those three sets -- including a genuinely unmatched real type
        code, or no type broadcast at all -- silently falls back to GA,
        per the owner's explicit decision: Hangar's real population
        already skews GA/light, so that is the honest statistical
        default, not a guess and not a distinct 'unknown' mark."""
        t = (type_code or "").strip().upper()
        if t in flights.HANGAR_HELI_TYPES:
            return SCOPE_ICON_HELI
        if t in flights.HANGAR_AIRLINER_TYPES:
            return SCOPE_ICON_AIRLINER
        if t in flights.BIZJET_TYPES:
            return SCOPE_ICON_BIZJET
        return SCOPE_ICON_GA

    def __init__(self):
        self.score = 0
        self.reset()

    # THREE views: the RADAR SCOPE (everything at once, spatial), the
    # DETAIL card (one aircraft, everything known about it), and the ATC
    # LOG (real transmissions, general-airport by default -- see
    # atc.py/CLAUDE.md; PERSONAL-RIG-ONLY). Deliberately a graduated
    # drill-down rather than one crowded screen -- at 64px there is no
    # room to label blips on the scope itself, so the scope answers "what
    # is around me and where", the card answers "what is that one", and
    # the log answers "what is actually being said right now". Same split
    # the sports mode already uses (ticker row -> expand), one level
    # deeper.
    VIEW_SCOPE = 0
    VIEW_DETAIL = 1
    VIEW_ATC_LOG = 2
    # THE HANGAR -- a FOURTH view, but deliberately NOT a fourth stop on
    # the rotate cycle above. It isn't "more detail on the selected
    # aircraft" or "the selected target's radio log" -- it's a whole-
    # device history, orthogonal to whatever is currently selected, same
    # relationship SatelliteEngine's sky dome has to its pass list. Reused
    # that exact idiom: up/down toggles it, since FlightEngine (like
    # SatelliteEngine) is NOT VERTICAL_BROWSE, so up/down is unclaimed --
    # no new input plumbing, no lengthening of the already-3-stop rotate
    # chain with a view that doesn't belong in it.
    VIEW_HANGAR = 3
    SCOPE_TICKS = 260         # ~13s on the scope before walking the cards
    SWEEP_DEG_PER_TICK = 3.0  # ~6s per full rotation at tick_rate 0.05
    ATC = (255, 170, 60)      # warm amber, distinct from PLANE blue / ROUTE yellow -- reads as "radio"
    ATC_PAGE_LINES = 7        # lines per page -- see _frame_atc_log()'s y-budget comment
    ATC_MATCH = (80, 235, 130)  # bright green -- a confirmed real-ident match, visually unmistakable
    WINDOW_RING = (190, 110, 255)  # violet -- distinct from every ALT_BANDS icon color,
                                    # ATC_MATCH's green, AND the (255,255,255) "selected"
                                    # white, so "in the window" is never confused with
                                    # "radio-matched" or "currently selected"
                                 # from the general-log amber (never the same color as "just data")
    ATC_PAGE_TICKS = 90       # ~4.5s per page, long enough to actually read one before it turns

    # PART 2: the airport is a genuinely selectable target on the scope,
    # not just a drawn marker -- this is its selection key, in the same
    # sel_key namespace as a real aircraft's hex/ident. Never collides
    # with one: no real ICAO24 hex or callsign looks like this.
    AIRPORT_KEY = "__AIRPORT__"

    def reset(self):
        self.data = {"aircraft": [], "age": None, "home_label": "HOME",
                    "configured": False, "err": None}
        # SELECTION IS IDENTITY-KEYED, not positional. `self.sel_key` is
        # the stable key (hex, falling back to ident) of whichever real
        # aircraft is selected, or None meaning "no explicit selection --
        # default to the top of the list". This replaced a bare list
        # index (`self.cur`) that silently pointed at whatever aircraft
        # now occupied that slot once the live-reordering ADS-B list
        # reshuffled underneath it -- a real correctness bug, not
        # theoretical: flights.FEED re-sorts by notability/distance every
        # refresh, so an index survives across ticks but the AIRCRAFT AT
        # that index does not.
        self.sel_key = None
        # PART 2: the airport is now a genuinely selectable target, not
        # just a drawn marker -- stepping past the last real aircraft
        # (or the first, going the other way) lands on it. Sentinel key,
        # never collides with a real hex/ident.
        # Route-line marquee: full destination city, never truncated --
        # scrolls when it doesn't fit rather than cutting it. NOT named
        # `self.scroll`, which Browsable's own `self.browse`/other
        # marquee modes already claim for a different purpose (see
        # CLAUDE.md's own documented trap about that exact collision).
        self.route_scroll = 0.0
        self._route_scroll_key = None
        # THE HANGAR (hangar.py) -- the persistent collection, read
        # fresh every tick() (cheap: hangar.LOG.get() is a pure
        # in-memory read, same never-blocks contract as every other
        # FEED here). hangar_idx pages through it via the same left/
        # right Browsable machinery every other list in this project
        # uses, branched on view exactly like ATC_LOG's paging already is.
        self.hangar_entries = []
        self.hangar_idx = 0
        self.hold = 0
        self.cycling = True
        self.ticks = 0
        self.pulse = Pulse()
        self.view = self.VIEW_SCOPE
        self.sweep = 0.0
        self.airport = None
        self.atc_entries = []
        # ATC log PAGINATION -- a real transmission runs well past what
        # 7 lines can hold (confirmed up to 250+ real characters), and
        # nothing may ever be silently cut off. self.atc_pages holds
        # EVERY page across the current CONTENT SET (recomputed only
        # when that content actually changes, not every frame) -- each
        # page is {"lines": [...], "ts": <the real entry it came from>}
        # so a multi-entry aircraft "conversation" can show each
        # transmission's own real age, not one caption for the whole
        # set. atc_page cycles through them automatically and can also
        # be stepped manually.
        self.atc_pages = [{"lines": [], "ts": None}]
        self.atc_page = 0
        self.atc_hold = 0
        # Identifies the current content set (which selection, which
        # real entries by timestamp) so tick() knows when to rebuild
        # self.atc_pages -- replaces phase 2's single `_atc_page_ts`
        # now that content can span more than one entry.
        self._atc_content_key = (None, ())
        # PART 2 -- the real display ident of the currently SELECTED
        # aircraft (not the matched one), or None for the general/
        # airport log. Set every tick(); read by _frame_atc_log() to
        # decide which of the two views to draw.
        self.atc_scoped_ident = None
        # PHASE 3 -- confidence-gated real-ident match for the current
        # GENERAL-VIEW entry, or None. NEVER a silent filter: the
        # general log above always shows every real transmission
        # regardless of this value, this only controls whether an EXTRA
        # highlight is drawn on top. Meaningless (and left None) inside
        # a per-aircraft filtered view -- see tick().
        self.atc_match_ident = None
        # Distinguishes "auto-cycle spotlighted this aircraft" from "the
        # user pressed rotate to select it" -- without this, the existing
        # spotlight rotation (scope -> detail -> next aircraft -> ... ->
        # scope) would silently walk the view away from an aircraft
        # someone deliberately opened a few seconds later. Same "browsing
        # while expanded stays expanded" rule sports already follows for
        # its own select-to-expand.
        self._auto_detail = False
        self._init_scroll()
        self._init_big_moments()
        # Seen-squawk/seen-airship cursors -- same one-shot adopt-then-
        # diff idiom as every other detector in this project. Separate
        # sets: an emergency squawk and an airship are unrelated events
        # and must not share a baseline.
        self._seen_squawks = None
        self._seen_airships = None
        # First-ever-type detector (THE HANGAR-powered TIER_FLASH) --
        # None until the first read adopts whatever types are already in
        # the collection, so a device that's been running a while
        # doesn't flash on every type already logged the moment this
        # code ships.
        self._seen_hangar_types = None
        # DEAD RECKONING (2026-08-08) -- see _update_dead_reckoning()'s own
        # docstring for the full picture. self._dr maps sel_key -> {x_nm,
        # y_nm, t_ref}: the local flat-plane position AT THE LAST REAL POLL
        # and the real wall-clock time it was observed. Bounded the same
        # way every other keyed cache in this project is -- rebuilt to only
        # the keys currently in range every tick, see _update_dead_
        # reckoning()'s own cleanup pass.
        self._dr = {}
        # Previous tick's `age` (seconds since the real ADS-B snapshot was
        # fetched) -- the freshness signal flights.FlightFeed.get() already
        # returns. `age` increases smoothly with real wall time between
        # polls and drops back down the instant a genuinely new snapshot
        # lands, so "age went DOWN since last tick" is a reliable, zero-
        # extra-I/O way to detect a real poll without adding a second
        # signal. None until the first real read.
        self._dr_age_prev = None
        # REAL FLOWN-PATH TRAIL (2026-08-08) -- see _update_trail()'s own
        # docstring. self._trail maps sel_key -> a list of REAL observed
        # (x_nm, y_nm) local-plane positions, one per real poll refresh --
        # reuses the exact identity keying and local-plane representation
        # _dr (dead reckoning) above already established, deliberately not
        # a second scheme. Bounded to TRAIL_MAX_POINTS per aircraft and
        # rebuilt to only currently-tracked keys every tick, same
        # discipline as _dr and every other keyed cache in this project.
        self._trail = {}

        # PLANE-IN-WINDOW TAKEOVER hand-off (2026-08-08): PlaneWatchEngine
        # sets flights.FEED's one-shot pending-detail slot right before
        # `.launch = "flights"` -- consumed here (never peeked) so this
        # engine jumps straight to VIEW_DETAIL for THAT aircraft instead
        # of landing on the plain scope. `_ceremonial_key` marks that this
        # arrival is a ceremony (richer detail card -- collection index,
        # FIRST SIGHTING/SEEN xN band, age) rather than an ordinary manual
        # select; cleared the moment the selection changes to anything
        # else (see _step()).
        self._ceremonial_key = None
        pending = flights.FEED.pop_pending_detail()
        if pending:
            self.sel_key = pending
            self.view = self.VIEW_DETAIL
            self._auto_detail = True     # don't let the spotlight rotation walk away immediately
            self._ceremonial_key = pending

    # ---- input -----------------------------------------------------------
    def has_content(self):
        """Needs a home location AND at least one aircraft actually in the
        sky right now -- 'CLEAR SKIES' is honest but it is not content to
        dwell on for 20 seconds in a rotation."""
        return bool(self.data.get("configured")) and bool(self.data.get("aircraft"))

    @staticmethod
    def _sel_key(ac):
        """The stable identity key for one real aircraft -- hex (ICAO24)
        preferred, ident as fallback for the rare payload missing hex.
        THE ONE place this lookup happens, so selection, auto-cycle, and
        ATC correlation all agree on what "the same aircraft" means."""
        return ac.get("hex") or ac.get("ident")

    @staticmethod
    def _route_status(route, airport):
        """DEPARTING/ARRIVING/None -- the owner's own framing: "a plane
        myrtle-to-wherever means departing, wherever-to-myrtle means
        arriving." Compares the flight's REAL resolved origin/dest airport
        code (adsbdb, IATA preferred/ICAO fallback, see _fetch_route())
        against the configured home airport code. No route or no
        configured home airport -> None, never guessed.

        The common case is neither matching -- most tracked traffic is
        passing near home, not to/from it -- so that's left unlabeled
        (TRANSIT has no badge) rather than forcing a label onto the
        majority case, same "no badge for the mundane case" rule flight
        phase CRUISE already follows.

        HONEST LIMITATION: `flights.load_airport()` stores only ONE code
        form (whatever the owner configured, e.g. MYR/IATA), not both
        IATA and ICAO. adsbdb's route field prefers IATA too, so this
        matches correctly in the common case, but a route that only
        resolved an ICAO code (e.g. "KMYR") against an IATA-configured
        home ("MYR") would false-negative to TRANSIT rather than
        DEPARTING/ARRIVING -- a real format-mismatch gap, not fabricated
        past by guessing a conversion.
        """
        if not route or not airport:
            return None
        home = (airport.get("code") or "").upper()
        if not home:
            return None
        origin = (route.get("origin") or "").upper()
        dest = (route.get("dest") or "").upper()
        if origin and origin == home:
            return "DEPARTING"
        if dest and dest == home:
            return "ARRIVING"
        return None

    @classmethod
    def _find_by_key(cls, aircraft, key):
        """Index of the aircraft matching `key`, or None if it isn't in
        the current list -- e.g. it flew out of RADIUS_NM. Never falls
        back to a position; a miss is a miss, not "whatever's now there"."""
        if key is None:
            return None
        for i, ac in enumerate(aircraft):
            if cls._sel_key(ac) == key:
                return i
        return None

    def _step(self, direction):
        # Context-sensitive: left/right browses AIRCRAFT on the scope/
        # detail views (unchanged), but PAGES through the transcript
        # while the ATC log is open -- aircraft selection has no meaning
        # there, and manual paging is the override on top of the
        # guaranteed auto-advance in tick(). Resets atc_hold so a manual
        # flip buys a fresh full read window rather than turning again
        # almost immediately.
        if self.view == self.VIEW_ATC_LOG:
            if len(self.atc_pages) > 1:
                self.atc_page = (self.atc_page + direction) % len(self.atc_pages)
                self.atc_hold = 0
            return
        # THE HANGAR -- left/right pages one entry at a time through the
        # collection, same tap-to-step/hold-to-accelerate Browsable
        # machinery every other list here uses. Selection/aircraft
        # stepping has no meaning while this view is open.
        if self.view == self.VIEW_HANGAR:
            if self.hangar_entries:
                self.hangar_idx = (self.hangar_idx + direction) % len(self.hangar_entries)
            return
        aircraft = self.data.get("aircraft") or []
        # PART 2: the airport is a real stop in the SAME step cycle, not
        # a separate control -- appended after every aircraft so
        # stepping past the last one (or before the first, going the
        # other way) reaches it. Only when it's actually configured;
        # nothing to select if there's no home airport set.
        targets = [self._sel_key(ac) for ac in aircraft]
        if self.airport:
            targets.append(self.AIRPORT_KEY)
        n = len(targets)
        if not n:
            return
        # Step FROM the current selection's real position in today's
        # list, not from a stale index -- a manual left/right always
        # means "the item next to the one I'm looking at right now",
        # which only means the same thing every time if it's resolved by
        # identity first.
        idx = targets.index(self.sel_key) if self.sel_key in targets else None
        if idx is None:
            idx = 0 if direction > 0 else -1   # first press with nothing selected: land on the near edge
        new_idx = (idx + direction) % n
        self.sel_key = targets[new_idx]
        if self.sel_key != self._ceremonial_key:
            # A manual step away from the plane-in-window hand-off's
            # aircraft ends the ceremony -- browsing to a different
            # aircraft should show the ordinary detail card, not the
            # richer one meant for the aircraft that was just in view.
            self._ceremonial_key = None
        # The airport has no DETAIL card (no altitude/route/heading to
        # show) -- landing on it while a per-aircraft detail view is
        # open falls back to the scope rather than trying to render an
        # aircraft-shaped card for a runway.
        if self.sel_key == self.AIRPORT_KEY and self.view == self.VIEW_DETAIL:
            self.view = self.VIEW_SCOPE
        self.hold = 0

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        # SELECT-TO-EXPAND, same convention SportsEngine already uses --
        # `rotate` IS the select button on this hardware (the phone
        # remote's centre action). On the scope, left/right already moves
        # the browse cursor over an aircraft; rotate now walks FORWARD
        # through a graduated drill-down (SCOPE -> DETAIL card for the
        # selected aircraft -> ATC LOG -> back to SCOPE) rather than a
        # simple two-state toggle, since a third real view exists now.
        # `drop` is the fast way back to the overview from EITHER deeper
        # view, and still toggles auto-advance when already on the scope
        # -- identical split to sports, not a new idiom invented here.
        if cmd == "rotate":
            if self.sel_key == self.AIRPORT_KEY:
                # The airport has no DETAIL card -- its whole "drill
                # down" is the general frequency log, so rotate is a
                # simple two-state toggle here, not the three-stop cycle
                # an aircraft gets.
                self.view = (self.VIEW_ATC_LOG if self.view != self.VIEW_ATC_LOG
                             else self.VIEW_SCOPE)
            else:
                self.view = {
                    self.VIEW_SCOPE: self.VIEW_DETAIL,
                    self.VIEW_DETAIL: self.VIEW_ATC_LOG,
                    self.VIEW_ATC_LOG: self.VIEW_SCOPE,
                    # VIEW_HANGAR isn't part of the SCOPE->DETAIL->ATC_LOG
                    # drill-down (it's a separate up/down toggle) -- but it
                    # was missing here entirely, so pressing rotate while on
                    # it raised KeyError and left self.view stuck on HANGAR,
                    # silently hiding the live scope. Same "any deeper view
                    # goes back to SCOPE" rule drop already uses.
                    self.VIEW_HANGAR: self.VIEW_SCOPE,
                }[self.view]
            self._auto_detail = False    # manual either way -- see reset()
            self.hold = 0
        elif cmd == "drop":
            if self.view != self.VIEW_SCOPE:
                self.view = self.VIEW_SCOPE
                self._auto_detail = False
            else:
                self.cycling = not self.cycling
            self.hold = 0
        elif cmd in ("up", "down"):
            # THE HANGAR toggle -- identical idiom to SatelliteEngine's
            # own up/down dome toggle (see its input()): unclaimed axis
            # (FlightEngine is not VERTICAL_BROWSE) reused for a whole-
            # view switch that's orthogonal to selection, rather than
            # invented as a new gesture.
            self.view = self.VIEW_HANGAR if self.view != self.VIEW_HANGAR else self.VIEW_SCOPE
            self._auto_detail = False
            self.hold = 0

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        self.route_scroll += 0.5   # same per-tick speed every other marquee in this project uses
        self.data = flights.FEED.get()
        # DEAD RECKONING poll-boundary detection -- see
        # _update_dead_reckoning()'s own docstring. `age` (seconds since
        # flights.FEED's cached snapshot was actually fetched) increases
        # smoothly with real wall time between polls and drops back down
        # the instant a genuinely new snapshot lands, so a decrease is a
        # reliable "a real poll just landed" signal with zero extra I/O.
        # `t_ref` anchors dead-reckoning to the real fetch time
        # (now - age), not merely "now", so the elapsed-time math below
        # stays accurate even though this tick may run a little after the
        # actual poll landed.
        _age = self.data.get("age")
        _now = time.time()
        _new_poll = (_age is not None) and (
            self._dr_age_prev is None or _age < self._dr_age_prev)
        self._dr_age_prev = _age
        _t_ref = (_now - _age) if _age is not None else _now
        self.airport = flights.load_airport()
        # THE HANGAR -- read every tick, same as every other FEED here
        # (hangar.LOG.get() is a pure in-memory read, never blocks).
        # Clamped with %, same cursor-safety idiom every other list in
        # this project uses so a shrinking list (a real eviction at the
        # cap) can never IndexError or strand the cursor.
        self.hangar_entries = hangar.LOG.get()
        if self.hangar_entries:
            self.hangar_idx %= len(self.hangar_entries)
        # PERSONAL-RIG-ONLY (see atc.py/CLAUDE.md). atc.FEED degrades
        # honestly to an empty list if the worker process has never run
        # or mlx-whisper isn't installed -- reading it costs nothing
        # (AtcLogFeed caches and only re-parses the file every 2s) so it
        # is fetched every tick same as every other feed here, not
        # specially gated to when the log view is showing.
        self.atc_entries = atc.FEED.get()

        # PART 2 -- WHICH content the log shows depends on what's
        # selected. An aircraft selected (and still actually in range --
        # _find_by_key, not a stale index) gets its CONVERSATION: every
        # real transmission in the retained log that names it, newest
        # first. Nothing selected, or the airport selected, gets the
        # unfiltered general frequency (unchanged from phase 2: just the
        # single newest transmission).
        #
        # "Conversation" is honest about its own granularity: this is
        # real filtering of real transcript chunks, not reconstructed
        # dialogue turns. Each chunk can contain more than this one
        # aircraft's transmission (a shared frequency also carries the
        # controller and any other aircraft talking in the same 20s
        # window) -- see CLAUDE.md's note on why clean per-utterance
        # speaker attribution was investigated and found NOT derivable
        # from this ASR output (no diarization, no utterance boundaries,
        # real cross-talk within a single chunk). Filtering by which
        # chunks mention this aircraft's real callsign is the honest
        # ceiling of what "its conversation" can mean here.
        ac_list_now = self.data.get("aircraft") or []
        sel_idx = (self._find_by_key(ac_list_now, self.sel_key)
                   if self.sel_key not in (None, self.AIRPORT_KEY) else None)
        scoped_ac = ac_list_now[sel_idx] if sel_idx is not None else None
        self.atc_scoped_ident = scoped_ac.get("ident") if scoped_ac else None

        if self.atc_scoped_ident:
            content_entries = [e for e in self.atc_entries
                               if atc.match_callsign(e.get("text", ""), [self.atc_scoped_ident])
                               == self.atc_scoped_ident]
            self.atc_match_ident = None   # the general-log highlight has no meaning inside a filtered view
        else:
            content_entries = self.atc_entries[:1]

        # Recompute pages when the actual CONTENT changed (which
        # selection, which real entries by timestamp) -- not every tick,
        # and not merely because the feed was re-read with the same
        # data. A fresh transmission, or a fresh selection, always
        # starts back at page 1 rather than wherever the cursor was.
        content_key = (self.atc_scoped_ident, tuple(e.get("ts") for e in content_entries))
        content_changed = content_key != self._atc_content_key
        if content_changed:
            self._atc_content_key = content_key
            self.atc_page = 0
            self.atc_hold = 0
            if not self.atc_scoped_ident:
                self.atc_match_ident = None     # fresh general entry: reset, retried below

        # PHASE 3 -- confidence-gated callsign match against the REAL
        # currently-tracked aircraft, GENERAL VIEW ONLY. Airline+flight-
        # number only, exact match required -- see
        # atc.match_callsign()'s own docstring for why GA tail numbers
        # are never attempted. Inside a per-aircraft filtered view every
        # page already involves that aircraft by construction, so a
        # redundant "MATCH: X" tag there would just repeat the header.
        #
        # RETRIED EVERY TICK WHILE UNMATCHED, not gated to "content
        # changed" alone -- a REAL bug, found by checking against real
        # live data rather than trusting the synthetic test that passed:
        # flights.FEED's background thread may not have completed its
        # first fetch yet on the exact tick a transmission arrives, so
        # matching only once at that moment silently missed a real,
        # correct match (SOUTHWEST 1437 -> SWA1437) purely because the
        # aircraft list was still empty at that specific instant. Once
        # matched, this stops re-trying (no wasted work); if it never
        # matches, the retry is one cheap regex pass per tick against a
        # short string, negligible next to the 50ms frame budget.
        if not self.atc_scoped_ident and content_entries and self.atc_match_ident is None:
            real_idents = [ac.get("ident") for ac in ac_list_now]
            new_match = atc.match_callsign(content_entries[0].get("text", ""), real_idents)
            if new_match:
                self.atc_match_ident = new_match
                content_changed = True   # force a page recompute: the match tag now reserves a line

        if content_changed:
            # A match costs one line of page budget (ATC_PAGE_LINES - 1)
            # so the tag never competes with the transcript text itself
            # for room -- recomputed here too so a match discovered on a
            # LATER tick (see above) still gets its line reserved rather
            # than overlapping the first line of already-paginated text.
            page_lines = (self.ATC_PAGE_LINES - 1
                         if (not self.atc_scoped_ident and self.atc_match_ident)
                         else self.ATC_PAGE_LINES)
            pages = []
            for e in content_entries:
                lines = wrap_text(e.get("text", ""), WIDTH - 4, max_lines=None)
                for i in range(0, len(lines), page_lines):
                    pages.append({"lines": lines[i:i + page_lines], "ts": e.get("ts")})
            self.atc_pages = pages or [{"lines": [], "ts": None}]
        # Auto-advance through pages while the log is actually on screen,
        # so a long real transmission -- or a long real conversation --
        # is GUARANTEED to be seen in full rather than permanently stuck
        # on page 1. This is the "no cutoffs, everything must be seen"
        # requirement, not a nicety.
        if self.view == self.VIEW_ATC_LOG and len(self.atc_pages) > 1:
            self.atc_hold += 1
            if self.atc_hold >= self.ATC_PAGE_TICKS:
                self.atc_hold = 0
                self.atc_page = (self.atc_page + 1) % len(self.atc_pages)
        ac_list = ac_list_now
        # DEAD RECKONING -- pure math over ac_list, zero new I/O. Must run
        # AFTER ac_list is final (this tick's real snapshot) and BEFORE
        # _frame_scope() reads _ext_dist_nm/_ext_dir_deg off each aircraft
        # dict. See _update_dead_reckoning()'s own docstring for the math.
        self._update_dead_reckoning(ac_list, _new_poll, _t_ref)
        # REAL FLOWN-PATH TRAIL -- pure math over ac_list, zero new I/O,
        # same "after ac_list is final, before _frame_scope() reads it"
        # placement as dead reckoning immediately above. See
        # _update_trail()'s own docstring for why it samples only on a
        # real poll (_new_poll), never every tick.
        self._update_trail(ac_list, _new_poll)
        # Flash on the NEAREST aircraft changing -- that is the "something
        # new is overhead" moment, not merely the list reordering.
        self.pulse.note(ac_list[0]["ident"] if ac_list else None)
        n = len(ac_list)

        # SELECTION-LOSS CHECK: if something was selected and it is no
        # longer in today's list, it left RADIUS_NM -- do not silently
        # re-point the selection at whatever real aircraft now happens to
        # sit in the old slot (that WAS the bug). Drop the selection and
        # fall back to the scope, same as arriving fresh: an honest "back
        # to overview" beats a selection that quietly became a lie.
        # AIRPORT_KEY is exempt from the "still in the aircraft list"
        # check (it's never in that list by construction) but still
        # clears if the airport itself gets unconfigured out from under
        # the selection.
        if self.sel_key == self.AIRPORT_KEY:
            if not self.airport:
                self.sel_key = None
                if self.view != self.VIEW_SCOPE:
                    self.view = self.VIEW_SCOPE
                    self._auto_detail = False
                    self.hold = 0
        elif self.sel_key is not None and self._find_by_key(ac_list, self.sel_key) is None:
            self.sel_key = None
            if self.view != self.VIEW_SCOPE:
                self.view = self.VIEW_SCOPE
                self._auto_detail = False
                self.hold = 0

        # Sweep advances every tick regardless of view or pause: it is the
        # mode's heartbeat, and freezing it while browsing would make a
        # live scope look crashed. Cheap -- one float add, no propagation.
        self.sweep = (self.sweep + self.SWEEP_DEG_PER_TICK) % 360.0
        # Auto-advance is suspended while a DETAIL view was reached by
        # manual select (rotate) -- same "browsing while expanded stays
        # expanded" rule sports' select-to-expand already follows. Only a
        # detail view the SPOTLIGHT ROTATION itself opened continues to
        # auto-walk; the moment a real person picks a specific aircraft,
        # the mode must stop pulling the view away from it on a timer.
        auto_ok = self.view == self.VIEW_SCOPE or self._auto_detail
        if self.cycling and n and self.browse.auto_ok and auto_ok:
            self.hold += 1
            if self.view == self.VIEW_SCOPE:
                if self.hold >= self.SCOPE_TICKS:
                    self.hold = 0
                    self.view = self.VIEW_DETAIL
                    self._auto_detail = True
                    self.sel_key = self._sel_key(ac_list[0])
            elif self.hold >= self.VIEW_TICKS:
                self.hold = 0
                idx = self._find_by_key(ac_list, self.sel_key)
                new_idx = ((idx if idx is not None else -1) + 1) % n
                self.sel_key = self._sel_key(ac_list[new_idx])
                # Back to the scope after walking the whole list once, so
                # the overview is a recurring anchor rather than a screen
                # you only see when the mode first comes up.
                if new_idx == 0:
                    self.view = self.VIEW_SCOPE
                    self._auto_detail = False
        self._tick_flash()
        self._detect_big_moments(ac_list)
        self.score = n

    # ---- BIG MOMENTS -- see BigMomentSource/CELEBRATION_BACKDROPS above ---
    def _detect_big_moments(self, ac_list):
        """Called once per tick with the CURRENT real aircraft list.
        Three detectors, deliberately not five or six -- see each one's
        own docstring for why it's included and, more importantly, why
        the many things NOT here (a heavy, a low-and-transitioning
        aircraft, a routine helicopter) were deliberately excluded: they
        already have a badge and a scope color, and promoting common
        real events to an interrupt is exactly the failure mode that
        makes a top tier stop meaning anything."""
        self._detect_emergency_squawk(ac_list)
        self._detect_airship(ac_list)
        self._detect_new_hangar_type()

    def _detect_emergency_squawk(self, ac_list):
        """TIER_TAKEOVER. Keyed directly off flights._notable()'s own
        real classification (rank 5 = HIJACK/NORADIO/MAYDAY from a real
        emergency squawk code) rather than re-deriving squawk parsing --
        one real source of truth, not two. The rarest, least ambiguous
        "go look now" this mode can produce; genuinely cannot be
        fabricated to test (see this feature's own design note --
        flagged honestly, not worked around with a synthetic trigger).

        One-shot per real ident PER SIGHTING: a squawk can legitimately
        be re-set on a later refresh after clearing, so this tracks
        which idents are CURRENTLY squawking, not an ever-growing seen
        set -- an aircraft that squawks 7700 twice in one session on two
        separate real emergencies should fire twice, not once."""
        current = {ac["ident"] for ac in ac_list
                  if ac.get("notable") and ac["notable"][1] == 5 and ac.get("ident")}
        if self._seen_squawks is None:
            self._seen_squawks = current
            return
        new = current - self._seen_squawks
        self._seen_squawks = current
        if new:
            ac = next(a for a in ac_list if a.get("ident") in new)
            tag = ac["notable"][0]
            self._set_big_moment(tag, ac["ident"],
                                 flights._type_name(ac.get("type")) or "",
                                 (255, 70, 70), tier=TIER_TAKEOVER, system=SYSTEM_FLIGHTS)

    def _detect_airship(self, ac_list):
        """TIER_INTERRUPT. Category B2 (lighter-than-air) -- ONE real
        instance in this project's own 213-aircraft sample. Rare enough
        to interrupt without becoming routine; common categories that
        already got their own real-sample counts checked (6 rotorcraft,
        11 heavies out of 213) are deliberately NOT promoted here for
        exactly that reason -- they are common enough that an interrupt
        would stop being special within days."""
        current = {ac["ident"] for ac in ac_list
                  if ac.get("notable") and ac["notable"][0] == "AIRSHIP" and ac.get("ident")}
        if self._seen_airships is None:
            self._seen_airships = current
            return
        new = current - self._seen_airships
        self._seen_airships = current
        if new:
            ac = next(a for a in ac_list if a.get("ident") in new)
            self._set_big_moment("AIRSHIP", ac["ident"],
                                 flights._type_name(ac.get("type")) or "",
                                 (200, 170, 255), tier=TIER_INTERRUPT, system=SYSTEM_FLIGHTS)

    def _detect_new_hangar_type(self):
        """TIER_FLASH -- THE HANGAR-powered. Deliberately keyed on
        aircraft TYPE, not registration: a new REGISTRATION is common (11
        distinct real aircraft were logged in this feature's first few
        minutes of real operation) and would make this fire constantly
        during exactly the period a new device is still building its
        collection. A new TYPE CODE is rare and self-limiting -- it
        naturally approaches zero as the collection matures, which is
        what keeps a flash a flash instead of becoming background noise.

        Reads self.hangar_entries, already refreshed this tick -- no new
        I/O, pure composition of data this engine already has."""
        types = {e["type"] for e in self.hangar_entries if e.get("type")}
        if self._seen_hangar_types is None:
            self._seen_hangar_types = types
            return
        new = types - self._seen_hangar_types
        self._seen_hangar_types = types
        if new:
            t = sorted(new)[0]   # deterministic if more than one type is new in the same tick
            name = flights._type_name(t) or t
            self._set_big_moment("NEW TYPE", name, "",
                                 self.HANGAR, tier=TIER_FLASH, system=SYSTEM_FLIGHTS)

    # ---- radar scope (GROUND projection: bearing + ground distance) -------
    def _scope_r_frac(self, dist_nm):
        """Ground distance -> normalised scope radius, SQUARE-ROOT scaled.

        Not a style choice: measured against real live traffic near MYR,
        6 of 9 real objects (8 aircraft plus the airport) fell inside a
        6px radius on a LINEAR 40nm scale -- an unreadable blob with the
        outer half of the scope empty -- because most interesting traffic
        near home is approach traffic inside ~6nm. Sqrt puts zero of those
        in that blob, preserves exact distance ORDER, and the rings are
        labelled with their true nm values so the compression is stated
        rather than hidden. See draw_scope_rings()'s note.
        """
        if not isinstance(dist_nm, (int, float)) or dist_nm < 0:
            return None
        return math.sqrt(min(1.0, dist_nm / float(flights.RADIUS_NM)))

    # True nautical-mile values the rings represent. Labelled on-screen --
    # a non-linear scale that does not say so would be misleading.
    SCOPE_RING_NM = (10, 20, 40)

    # This mode's own, bigger scope -- overrides the SCOPE_CX/CY/R module
    # defaults (still used as-is by the satellite dome, untouched) via
    # the cx/cy/radius params every draw_scope_* primitive already
    # accepts. Verified it still clears the legend row below via
    # render_audit before shipping.
    FLT_CX, FLT_CY, FLT_R = SCOPE_CX, 32, 26

    def _frame_scope(self, aircraft):
        buf = blank()
        fill(buf, self.BG)
        draw_header(buf, "RADAR", self.PLANE,
                    right_tag=f"{len(aircraft)}",
                    stale=bool(self.data.get("age") and self.data["age"] > 60))

        cx, cy, r = self.FLT_CX, self.FLT_CY, self.FLT_R
        draw_scope_rings(buf, [math.sqrt(nm / float(flights.RADIUS_NM))
                               for nm in self.SCOPE_RING_NM], cx=cx, cy=cy, radius=r)

        # REAL coastline (flights.COASTLINE -- Natural Earth data, see its
        # own docstring for provenance), not a decorative shape. This is
        # the orientation fix that replaced the N/S/E/W text labels: real
        # feedback was that compass letters read as clunky against this
        # view, and a landmark you actually recognise (the shoreline,
        # which the airport itself sits inside of) is the more intuitive
        # answer to "where am I looking" than instrument-panel letters.
        # Drawn as CONNECTED segments through the same bearing/distance ->
        # scope_xy() pipeline every other element uses, so it moves
        # correctly if the configured home ever changes -- not baked in
        # as fixed pixels.
        lat, lon, _lbl = satellite.FEED.get_location()
        pts = []
        for clat, clon in flights.COASTLINE:
            brg, nm = flights.bearing_distance(lat, lon, clat, clon)
            frac = self._scope_r_frac(nm)
            pts.append(scope_xy(brg, frac, cx=cx, cy=cy, radius=r) if frac is not None else None)
        for a, b in zip(pts, pts[1:]):
            if a is not None and b is not None:
                draw_line(buf, a[0], a[1], b[0], b[1], (26, 78, 108))

        draw_scope_crosshair(buf, cx=cx, cy=cy, radius=r)
        draw_scope_sweep(buf, self.sweep, cx=cx, cy=cy, radius=r)

        # FEATURE 1 + 2/3: the currently SELECTED aircraft's real flown
        # path, and a real route-bearing ray toward wherever it's real
        # origin/destination airport actually is. Deliberately only the
        # ONE selected aircraft, never all 8 -- this project already hit
        # and fixed a real lag complaint from over-drawing this exact
        # scope earlier this session (6 strokes -> 2 per aircraft icon);
        # drawing every aircraft's trail every frame would reopen it.
        # Both are context layers (same tier as the coastline/airport
        # marker), drawn before the aircraft loop so the live icon paints
        # over them, never the reverse.
        if self.sel_key not in (None, self.AIRPORT_KEY):
            sel_ac = next((a for a in aircraft if self._sel_key(a) == self.sel_key), None)
            if sel_ac is not None:
                # Trail: dim/muted version of the aircraft's own real
                # altitude-band color -- reads as "history", stays
                # visually secondary to the bright live icon.
                trail_pts = self._trail_points_px(self.sel_key, cx, cy, r)
                if len(trail_pts) >= 2:
                    base_col = self._alt_color(sel_ac.get("alt_ft"))
                    trail_col = tuple(c // 3 for c in base_col)
                    for (x0, y0), (x1, y1) in zip(trail_pts, trail_pts[1:]):
                        draw_line(buf, x0, y0, x1, y1, trail_col)
                # Route-bearing ray(s): only when a real route resolved
                # AND adsbdb gave real coordinates for the end(s) being
                # drawn -- no route, no guessed ray, ever. "Departing
                # means show where it's headed, arriving means show where
                # it came from, transit shows both" -- the ray toward the
                # end that ISN'T home is the informative one; a ray back
                # to home when departing FROM home would be redundant.
                route = sel_ac.get("route")
                if route:
                    status = self._route_status(route, self.airport)
                    if status == "DEPARTING":
                        ends = [("dest_lat", "dest_lon")]
                    elif status == "ARRIVING":
                        ends = [("origin_lat", "origin_lon")]
                    else:
                        ends = [("origin_lat", "origin_lon"), ("dest_lat", "dest_lon")]
                    home_lat, home_lon, _hlbl = satellite.FEED.get_location()
                    for lat_k, lon_k in ends:
                        lat, lon = route.get(lat_k), route.get(lon_k)
                        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                            brg, _nm = flights.bearing_distance(home_lat, home_lon, lat, lon)
                            # Short ray, not a full rim-to-rim line -- a
                            # handful of pixels of real bearing, cheap and
                            # secondary to the live traffic it's context for.
                            rx, ry = scope_xy(brg, 0.42, cx=cx, cy=cy, radius=r)
                            draw_line(buf, cx, cy, rx, ry, (60, 110, 80))

        # Airport first, UNDER the aircraft: it is context, not traffic,
        # and a plane on final should never be hidden by the runway it is
        # heading for.
        if self.airport:
            lat, lon, _lbl = satellite.FEED.get_location()
            brg, nm = flights.bearing_distance(
                lat, lon, self.airport["lat"], self.airport["lon"])
            frac = self._scope_r_frac(nm)
            if frac is not None:
                x, y = scope_xy(brg, frac, cx=cx, cy=cy, radius=r)
                # PART 2: the airport is a genuinely selectable target
                # now (see AIRPORT_KEY/_step), not just a drawn marker --
                # it gets the same white "selected" treatment an
                # aircraft does when it's the current sel_key. PART 3:
                # a runway glyph instead of a generic plus, see
                # draw_scope_airport()'s own docstring.
                port_sel = (self.sel_key == self.AIRPORT_KEY)
                port_col = (255, 255, 255) if port_sel else self.ROUTE
                draw_scope_airport(buf, x, y, port_col, glow=scope_glow(brg, self.sweep))

        for ac in aircraft:
            # DEAD RECKONING (2026-08-08): the ICON POSITION only uses the
            # extrapolated dist/bearing when _update_dead_reckoning() set
            # one this tick (real gs_kt/track_deg were both present) --
            # falls back to the real polled dist_nm/dir_deg otherwise
            # (honest degrade, or simply the tick a fresh poll just
            # landed on). Every other read of this aircraft dict --
            # text fields, sorting, notability, window/ATC logic --
            # still uses the real values; only this x/y is smoothed, so
            # the window ring and selection ring (drawn at this same x,y
            # below) track the smoothed icon automatically.
            dist_nm = ac.get("_ext_dist_nm", ac.get("dist_nm"))
            brg = ac.get("_ext_dir_deg", ac.get("dir_deg"))
            frac = self._scope_r_frac(dist_nm)
            if frac is None or brg is None:
                continue          # no position fix -- plot nothing, guess nothing
            x, y = scope_xy(brg, frac, cx=cx, cy=cy, radius=r)
            col = self._alt_color(ac.get("alt_ft"))
            # IDENTITY-keyed, not positional -- see reset()'s note on
            # self.sel_key. A reordering list can no longer silently move
            # the highlight to a different real aircraft.
            sel = (self._sel_key(ac) == self.sel_key)
            # PHASE 3 reciprocal touch: the aircraft a real ATC
            # transmission was just confidence-matched to gets the same
            # bright-green treatment on the scope, so the correlation is
            # visible from either direction -- picking the aircraft shows
            # the transmission (ATC LOG view), and the transmission shows
            # the aircraft (here). Still never a filter: every aircraft
            # keeps drawing regardless of match status.
            matched = ac.get("ident") and ac["ident"] == self.atc_match_ident
            mark_col = self.ATC_MATCH if matched else ((255, 255, 255) if sel else col)
            # WINDOW FILTER (2026-08-07): a small ring under the icon for
            # any aircraft currently visible out the configured window --
            # see satellite.in_window()/draw_window_ring()'s own notes.
            # Drawn BEFORE the aircraft icon so the ring never covers it.
            if ac.get("in_window"):
                draw_window_ring(buf, x, y, self.WINDOW_RING)
            # PART 3: a real, heading-oriented icon instead of a plain
            # dot -- see draw_scope_aircraft()'s own honesty note on how
            # distinctly each kind actually reads at this pixel density.
            kind = self._ac_kind(ac)
            # NOTABLE_GLOW_FLOOR: see its own module-level docstring for
            # why this is a SEPARATE signal from the window ring above,
            # not a merged one. max(), not replace -- the sweep can still
            # push a notable aircraft brighter than the floor as it
            # passes, this only raises the OFF-beam baseline.
            glow = scope_glow(brg, self.sweep)
            if ac.get("notable"):
                glow = max(glow, NOTABLE_GLOW_FLOOR)
            draw_scope_aircraft(buf, x, y, ac.get("track_deg"), kind, mark_col,
                                glow=glow, big=(sel or matched))

        draw_scope_home(buf, cx=cx, cy=cy)
        # Tried an alternating text legend ("<>=HOME +=MYR") same session,
        # reverted: real feedback was that text captions read as clunky
        # against this view's aesthetic. Orientation now comes from real
        # landmarks (the coastline outline, the airport mark) rather than
        # instrument-panel labels -- see the coastline note above.
        #
        # MILES, not NM -- this project converts to imperial at the
        # render layer everywhere else (km_to_mi/kmh_to_mph/nm_to_mi in
        # this same file); the ring geometry itself still comes from the
        # real nm values (that's what the ADS-B distance and
        # flights.RADIUS_NM actually are), only the printed label
        # changes. One compact line, not "NM" appended, to leave the
        # bigger scope its full bottom margin.
        mi_txt = "/".join(str(round(nm_to_mi(nm))) for nm in self.SCOPE_RING_NM) + "MI"
        draw_text_centered(buf, 59, fit_text(mi_txt, WIDTH - 4), (70, 76, 92))
        # TIER_FLASH -- drawn LAST, over whatever scope content sits in
        # its band. That's the intended tradeoff for a cheap, in-mode-
        # only notice (see draw_flash_banner()'s own docstring on why
        # SCOPE specifically is safe for this: its only TEXT lives in the
        # header above and this legend row below, so a banner overwriting
        # rows 11..21 can only ever cover graphics, never collide with
        # another text draw).
        self._draw_flash(buf)
        return bytes(buf)

    # ---- render --------------------------------------------------------
    @staticmethod
    def _compass(deg):
        if deg is None:
            return ""
        i = int((deg + 22.5) % 360 // 45)
        return FlightEngine.COMPASS[i]

    # ---- heading-oriented plane icon ------------------------------------
    # A minimal three-stroke glyph (fuselage + wings + tailplane) computed
    # from real ADS-B heading (track_deg), not a fixed 8-direction sprite
    # table -- this is deliberately NOT a real airline/aircraft logo (no
    # trademark/IP exposure), just an abstract "which way is it pointing"
    # silhouette in the same spirit as a radar-scope aircraft mark.
    _ICON_FUSELAGE = (9.5, -8.0)   # (nose fx, tail fx) along the heading axis -- enlarged for readability
    _ICON_WING = 7.0                 # main wing half-span, at fx=0
    _ICON_TAIL = 2.8                 # tailplane half-span, at fx=tail

    @classmethod
    def _draw_plane_icon(cls, buf, cx, cy, heading_deg, color, kind=None):
        """Same top-down/side-view split as the scope's
        `draw_scope_aircraft`, logically consistent with it: a
        helicopter is a fixed 2D side-view sprite (mirrored, not
        rotated), everything else is a real top-down silhouette rotated
        to heading, now thickened (a second parallel fuselage line) to
        read as a filled shape at this bigger DETAIL-card size."""
        theta = math.radians(heading_deg if heading_deg is not None else 0)

        if kind == SCOPE_ICON_HELI:
            face_right = math.sin(theta) >= 0
            xi, yi = int(round(cx)), int(round(cy))
            for dx, dy in [(0, 0), (0, 2), (-4, -4), (-2, -4), (0, -4), (2, -4), (4, -4),
                           (-4, 4), (-6, 6)]:
                put_px(buf, xi + (dx if face_right else -dx), yi + dy, color)
            return

        # Forward unit vector (0 deg = up on screen); right unit vector is
        # forward rotated +90 deg, used for the wing/tail cross-strokes.
        fwd = (math.sin(theta), -math.cos(theta))
        right = (math.cos(theta), math.sin(theta))

        def pt(fx, fy):
            return (cx + fx * fwd[0] + fy * right[0],
                    cy + fx * fwd[1] + fy * right[1])

        nose, tail = pt(cls._ICON_FUSELAGE[0], 0), pt(cls._ICON_FUSELAGE[1], 0)
        wing_l, wing_r = pt(0, -cls._ICON_WING), pt(0, cls._ICON_WING)
        tail_l, tail_r = pt(cls._ICON_FUSELAGE[1], -cls._ICON_TAIL), pt(cls._ICON_FUSELAGE[1], cls._ICON_TAIL)

        draw_line(buf, *nose, *tail, color)
        draw_line(buf, *wing_l, *wing_r, color)
        draw_line(buf, *tail_l, *tail_r, color)
        put_px(buf, int(round(nose[0])), int(round(nose[1])), color)   # nose pip -- reads as "front" even at low res

        # Heading-unknown (no track data) gets a dim ring instead of a
        # confidently-pointed icon that would be showing a fake direction.
        if heading_deg is None:
            for i in range(16):
                a = i / 16 * 2 * math.pi
                put_px(buf, int(round(cx + 8 * math.cos(a))), int(round(cy + 8 * math.sin(a))), rim(color, 0.4))

    AMBIENT_STYLE = "push_up"       # aircraft climb away

    def ambient_weight(self):
        acs = self.data.get("aircraft") or []
        if any((a.get("notable") or (None, 0))[1] >= 3 for a in acs):
            return 2.5            # something genuinely unusual overhead
        return 1.0 if acs else 0.5


    def _frame_unconfigured(self, buf):
        msg = "SET LOCATION"
        draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 24, msg, self.INK)
        sub = "TO TRACK SKY"
        draw_text3x5(buf, (WIDTH - (4 * len(sub) - 1)) // 2, 34, sub, self.INK_DIM)
        return bytes(buf)

    def _frame_idle(self):
        buf = blank()
        fill(buf, self.BG)
        msg = "CLEAR SKIES"
        draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 26, msg, self.INK_DIM)
        sub = "NO AIRCRAFT NEARBY" if not self.data.get("err") else "CONNECTION ERROR"
        # 19 chars is too wide at 64px on one line -- split cleanly rather
        # than let it run off the edge.
        if 4 * len(sub) - 1 > WIDTH - 4:
            words = sub.split()
            mid = len(words) // 2
            line1, line2 = " ".join(words[:mid]), " ".join(words[mid:])
            draw_text3x5(buf, max(2, (WIDTH - (4 * len(line1) - 1)) // 2), 36, line1, self.INK_DIM)
            draw_text3x5(buf, max(2, (WIDTH - (4 * len(line2) - 1)) // 2), 44, line2, self.INK_DIM)
        else:
            draw_text3x5(buf, (WIDTH - (4 * len(sub) - 1)) // 2, 36, sub, self.INK_DIM)
        return bytes(buf)

    ATC_LOG_RECENT_SECONDS = 60   # below this, reads as "just happened"; at/above, as "the last one heard"

    @staticmethod
    def _fmt_ago(secs):
        """'14S', '3M 22S', '1H 05M' -- how long ago, not a countdown, so
        this is deliberately a different shape from SatelliteEngine's
        _fmt_countdown (which counts DOWN to a future rise) even though
        the digit math is the same; the direction is the whole point of
        the label and conflating the two would read backwards."""
        secs = max(0, int(secs))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}H {m:02d}M"
        if m:
            return f"{m}M {s:02d}S"
        return f"{s}S"

    HANGAR = (190, 160, 255)   # soft lavender -- distinct from PLANE blue/ROUTE yellow/ATC amber

    @staticmethod
    def _fmt_age_long(secs):
        """Same shape as _fmt_ago but DAYS-aware -- THE HANGAR is a
        persistent collection, so 'first seen' can genuinely be weeks
        old, unlike every other _fmt_ago caller in this class which
        only ever deals in minutes/hours (a live transmission, a
        satellite pass)."""
        secs = max(0, int(secs))
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        if d:
            return f"{d}D {h}H"
        if h:
            return f"{h}H {m:02d}M"
        if m:
            return f"{m}M {s:02d}S"
        return f"{s}S"

    def _frame_hangar(self):
        """THE HANGAR -- the persistent collection (hangar.py), paged
        one aircraft at a time. See hangar.py's own docstring for the
        identity/retention rules; this method only ever reads
        self.hangar_entries (refreshed in tick()), never touches
        hangar.py directly."""
        buf = blank()
        fill(buf, self.BG)
        n = len(self.hangar_entries)

        if not n:
            # Never invents an entry -- an empty collection (device just
            # set up, or genuinely nothing with a broadcast registration
            # has passed overhead yet) is shown honestly, same shape as
            # the ATC log's own empty state.
            draw_header(buf, "HANGAR", self.HANGAR)
            draw_text_centered(buf, 28, "NONE SEEN", self.INK_DIM)
            draw_text_centered(buf, 36, "YET", self.INK_DIM)
            return bytes(buf)

        idx = self.hangar_idx % n
        e = self.hangar_entries[idx]
        draw_header(buf, e.get("reg") or "UNKNOWN", self.HANGAR, right_tag=f"{idx + 1}/{n}")

        # Static, non-rotating sprite -- owner decision #3: the Hangar is
        # browsed one entry at a time (same interaction pattern as the
        # flight DETAIL card, never more than one sprite drawn per
        # frame), so it's safe to reuse THAT card's 3-stroke budget
        # (`_draw_plane_icon`, one fuselage line + one wing line + one
        # tailplane line + a nose pip) rather than the radar scope's
        # tighter 2-stroke budget -- the same "only one draws per frame"
        # reasoning that already makes the DETAIL card safely thicker
        # than the scope. `heading_deg=None` reuses that method's
        # existing fixed "up" orientation + dim uncertainty ring, since a
        # persisted Hangar entry has no live heading to draw -- no new
        # static-bitmap code path needed. Kind comes from `_hangar_kind()`
        # (owner decision #2's real-seeded lookup table), falling back to
        # GA for anything unmatched (owner decision #1) rather than a
        # distinct "unknown" mark.
        self._draw_plane_icon(buf, WIDTH // 2, 19, None, self.HANGAR,
                              kind=self._hangar_kind(e.get("type")))

        # Real, readable type name (flights.ICAO_TYPE_NAMES -- same
        # static reference table the DETAIL card already uses), falling
        # back honestly to "TYPE UNKNOWN" for the small real fraction of
        # aircraft that never broadcast a type code.
        typ = flights._type_name(e.get("type")) or "TYPE UNKNOWN"
        draw_text_centered(buf, 31, fit_text(typ, WIDTH - 4), self.INK)

        airline = e.get("airline")
        if airline:
            draw_text_centered(buf, 38, fit_text(airline, WIDTH - 4), self.INK_DIM)

        # Repeat visitors get the SAME green treatment as an ATC
        # confidence-match -- "this one's been here before" is the
        # collection's own version of a confirmed, notable fact.
        times = e.get("times_seen") or 1
        seen_col = self.ATC_MATCH if times > 1 else self.INK_DIM
        seen_txt = f"SEEN {times}X" if times > 1 else "FIRST SIGHTING"
        draw_text_centered(buf, 45, fit_text(seen_txt, WIDTH - 4), seen_col)

        age = max(0.0, time.time() - (e.get("first_seen") or 0))
        draw_text_centered(buf, 52, fit_text(f"{self._fmt_age_long(age)} AGO", WIDTH - 4),
                           (86, 94, 116))
        return bytes(buf)

    def _frame_atc_log(self):
        """TWO views over one log -- see atc.py/CLAUDE.md, PERSONAL-RIG-
        ONLY, and PART 2's writeup there for why per-utterance speaker
        attribution isn't built (not honestly derivable from this ASR
        output).

        `self.atc_scoped_ident` set (an aircraft is selected): shows
        that aircraft's CONVERSATION -- every real transmission in the
        retained log that names it, newest first, real filtering of
        real data. Nothing selected, or the airport selected: shows the
        GENERAL frequency log (just the single newest transmission,
        unchanged from phase 2) plus the phase 3 confidence-gated match
        highlight, since there IS no single "selected aircraft" for that
        highlight to be redundant with here.

        PAGINATED, NEVER TRUNCATED, in both cases. A real transmission
        runs 100-250+ characters, which does not fit 7 lines -- explicit
        requirement: nothing may ever be silently cut off.
        self.atc_pages (computed once per content-change in tick(), not
        here) holds every page across the WHOLE current content set,
        each carrying its own source entry's real timestamp so a
        multi-transmission conversation shows each message's own real
        age, not one caption for the lot. tick() auto-advances through
        them and _step() lets left/right override manually.
        """
        buf = blank()
        fill(buf, self.BG)

        has_content = bool(self.atc_pages) and self.atc_pages[0].get("ts") is not None
        title = "ATC LOG" if not self.atc_scoped_ident else fit_text(self.atc_scoped_ident, 30)
        accent = self.ATC if not self.atc_scoped_ident else self.ATC_MATCH

        if not has_content:
            # Never invents a transmission. For the general view: the
            # worker process isn't running, mlx-whisper isn't installed,
            # or the frequency has genuinely been quiet for the whole
            # LOG_MAX_AGE_SECONDS window -- all three collapse to the
            # same honest "nothing to show". For a selected aircraft:
            # real transmissions exist on the frequency, just none that
            # name THIS aircraft yet -- a different, more specific
            # honest state, worth its own wording.
            draw_header(buf, title, accent)
            if self.atc_scoped_ident:
                # "NO TRANSMISSIONS" alone is 17 chars / 66px -- 2px over
                # the 64px panel. Caught by render_audit against this
                # exact state, not by eye. Split across two lines like
                # the general-view "NO ATC DATA / YET" message already
                # does, rather than truncating a word.
                draw_text_centered(buf, 24, "NO", self.INK_DIM)
                draw_text_centered(buf, 32, "TRANSMISSIONS", self.INK_DIM)
                draw_text_centered(buf, 40, "YET", self.INK_DIM)
            else:
                draw_text_centered(buf, 28, "NO ATC DATA", self.INK_DIM)
                draw_text_centered(buf, 36, "YET", self.INK_DIM)
            return bytes(buf)

        n_pages = len(self.atc_pages)
        page_tag = f"P{self.atc_page + 1}/{n_pages}" if n_pages > 1 else None
        draw_header(buf, title, accent, right_tag=page_tag)

        page = self.atc_pages[self.atc_page]
        age = max(0.0, time.time() - (page.get("ts") or 0))
        # "14S AGO" reads as live and current; past ATC_LOG_RECENT_SECONDS
        # it switches to "LAST: Xm Ys" so a five-minute-old transmission
        # is never mistaken for something that just happened -- exactly
        # the "show the last one, but be honest about when" behaviour
        # asked for, not a blank screen the moment the frequency goes
        # quiet. "LAST TX ... AGO" (the original wording) was a REAL
        # truncation caught by render_audit against a real aged entry --
        # at up to "LAST TX 4M 59S AGO" (19 chars, 75px) it overran the
        # 60px budget and silently lost "AGO". Shortened to "LAST:" so
        # the widest real case ("LAST: 4M 59S", 13 chars) fits with
        # margin instead of trimming the message that was supposed to be
        # the whole point of this label. Uses THIS PAGE's own entry age,
        # not the newest overall -- a conversation's older transmissions
        # correctly read as older once paged to.
        if age < self.ATC_LOG_RECENT_SECONDS:
            caption = f"{self._fmt_ago(age)} AGO"
        else:
            caption = f"LAST: {self._fmt_ago(age)}"
        draw_text_centered(buf, 10, fit_text(caption, WIDTH - 4), accent)

        y = 17
        if not self.atc_scoped_ident and self.atc_match_ident:
            # PHASE 3, GENERAL VIEW ONLY: a confidence-gated match,
            # visually UNMISTAKABLE (bright green, distinct from the
            # general-log amber) so it reads as "confirmed", never
            # blended into the ordinary text. Costs exactly the one line
            # of page budget tick() already reserved for it
            # (ATC_PAGE_LINES - 1) -- never silently eats into the
            # transcript text's own room. Not drawn in a per-aircraft
            # scoped view -- the header already names that aircraft.
            draw_text_centered(buf, y, fit_text(f"MATCH: {self.atc_match_ident}", WIDTH - 4),
                               self.ATC_MATCH)
            y += 6

        # 6-7 lines (depending on whether the match row above is showing)
        # leaves y=59 clear, the real HEIGHT-5 boundary a 5px glyph must
        # start at or before.
        for i, ln in enumerate(page.get("lines") or []):
            draw_text3x5(buf, max(2, (WIDTH - text_w(ln)) // 2), y + i * 6, ln, self.INK)
        return bytes(buf)

    def frame(self):
        if not self.data.get("configured"):
            buf = blank()
            fill(buf, self.BG)
            return self._frame_unconfigured(buf)

        # THE HANGAR dispatches BEFORE the "no aircraft right now ->
        # CLEAR SKIES" check below -- deliberately. The collection has
        # nothing to do with whether anything is in the sky THIS
        # instant (it's arguably most useful exactly when the sky is
        # empty), and the exact bug this project's CLAUDE.md names
        # repeatedly is a real fact (a view has real content) getting
        # silently blocked by an unrelated piece of state deciding what
        # renders next. Checked directly here, not folded into
        # has_content()/_frame_idle()'s aircraft-only logic.
        if self.view == self.VIEW_HANGAR:
            return self._frame_hangar()

        aircraft = self.data.get("aircraft") or []
        if not aircraft:
            return self._frame_idle()

        if self.view == self.VIEW_SCOPE:
            return self._frame_scope(aircraft)
        if self.view == self.VIEW_ATC_LOG:
            return self._frame_atc_log()

        buf = blank()
        fill(buf, self.BG)
        idx = self._find_by_key(aircraft, self.sel_key)
        if idx is None:
            idx = 0   # nothing explicitly selected yet -- default to the top of the list
        ac = aircraft[idx]

        # PLANE-IN-WINDOW ceremonial arrival: this exact aircraft is the
        # one PlaneWatchEngine just handed off to (see reset()'s pending-
        # detail consumption) -- show the richer showcase card instead of
        # the ordinary one. A dedicated renderer, not an in-place
        # extension: the ordinary card's vertical budget below is already
        # fully audited with zero spare rows (see its own comment), so
        # there is no room to layer ceremonial fields into it without
        # reopening the exact collision risk that budget exists to avoid.
        if self._ceremonial_key is not None and self.sel_key == self._ceremonial_key:
            return self._frame_detail_ceremonial(ac)

        alt = ac.get("alt_ft")
        col = self._alt_color(alt)

        # Vertical budget, top to bottom, checked to not collide (icon is
        # the tallest/widest element and was the thing worth double
        # checking -- its reach at cy=24 is roughly y=15..33):
        #   ident 2-6 | dots 9 (own row, no text on it) | icon 15-33 |
        #   line2 35-39 | line3 43-47 | divider 50 | route 53-57 |
        #   airline 59-63 (HEIGHT=64, last legal row)
        # Header carries the callsign itself -- it IS the identity of what
        # you're looking at, so it earns the title slot rather than a
        # generic "FLIGHTS" label, and the accent rule takes the altitude
        # band colour so the band reads before any text does.
        ident = (ac.get("ident") or "UNKNOWN")[:8]
        # A notable tag replaces the position counter in the header: what
        # makes this aircraft worth noticing is more useful than which
        # index it is, and the dots at the bottom already show position.
        # FEATURE 3: DEPARTING/ARRIVING (see _route_status()'s own
        # docstring) takes priority over both when it applies -- "is this
        # plane leaving or arriving" is the most immediately useful real
        # fact about it when it's true, and reuses the header's existing
        # right_tag slot rather than adding a new row (this card's
        # vertical budget is already fully audited, see the comment
        # above). The common TRANSIT case (neither matches) falls through
        # to the existing notable/position behavior unchanged.
        note = ac.get("notable")
        route_status = self._route_status(ac.get("route"), self.airport)
        right_tag = route_status or (note[0] if note else f"{idx + 1}/{len(aircraft)}")
        draw_header(buf, ident, self.pulse.mix(col),
                    right_tag=right_tag,
                    stale=bool(self.data.get("age") and self.data["age"] > 60))

        # Heading-oriented icon, the visual centerpiece -- colour-coded by
        # altitude band (see ALT_BANDS) since that reads as "kind of
        # traffic" better than raw distance would at this size.
        self._draw_plane_icon(buf, WIDTH // 2, 22, ac.get("track_deg"), col, kind=self._ac_kind(ac))

        # Phase arrow: climbing/descending only, nothing drawn for cruise
        # or an undetermined phase -- same "no badge for the mundane
        # case" rule as the notable tag. Placed in the LEFT MARGIN at the
        # icon's vertical band (x=3): the icon rotates with heading and
        # its bounding circle (~11px radius around cx=32,cy=22) can reach
        # anywhere from x=21 to x=43 depending on orientation, so x<21 is
        # the one horizontal band that is always clear regardless of
        # which way the aircraft is pointed.
        phase = ac.get("phase")
        if phase in (flights.PHASE_CLIMB, flights.PHASE_DESCEND):
            draw_trend_arrow(buf, 3, 16, phase == flights.PHASE_CLIMB, col)

        # Readable name when the ICAO type code is in flights.ICAO_TYPE_NAMES
        # ("B738" -> "737-800"), the bare code otherwise -- never a guessed
        # name for a code the table doesn't recognise. .upper() defensively:
        # ADS-B type codes are conventionally uppercase, but "conventionally"
        # is exactly the word that already burned the airline-name field
        # once (see paneltext.py's tally, instance 2).
        typ = flights._type_name(ac.get("type")) or ""
        typ = typ.upper()

        # ADS-B natively reports altitude in FEET (already imperial, left
        # as-is) but ground speed in KNOTS and distance in NAUTICAL miles
        # -- both aviation-standard, neither what someone reading a wall
        # panel means. Converted to mph / statute miles for display.
        gs = ac.get("gs_kt")
        dist = ac.get("dist_nm")
        compass = self._compass(ac.get("dir_deg"))
        left = f"{kt_to_mph(gs):.0f}MPH" if isinstance(gs, (int, float)) else "-"
        right = f"{nm_to_mi(dist):.0f}MI {compass}".strip() if isinstance(dist, (int, float)) else "-"

        # Altitude is the hero value: it's the number the icon's colour
        # encodes, so making it big is what teaches the colour band
        # without needing a legend. The type code rides with it only when
        # it can't fit on the stats row below (see below) -- composed once
        # here so the row is never drawn twice.
        alt_txt = f"{alt:.0f}FT" if isinstance(alt, (int, float)) else (typ or "-")

        # The type only goes on the stats row if it genuinely fits in the
        # gap between the two side stats. Centring it unconditionally
        # overlapped both of them on a wide case (598MPH + 45MI NW) --
        # caught by rendering, invisible to a code read. It's the least
        # important of the three, so it yields.
        gap_start = 2 + text_w(left)
        gap_end = WIDTH - 2 - text_w(right)
        gap = gap_end - gap_start
        type_fits_inline = bool(typ) and gap >= text_w(typ) + 6
        if typ and not type_fits_inline and isinstance(alt, (int, float)):
            alt_txt = f"{typ} {alt:.0f}FT"

        draw_text_centered(buf, 33, fit_text(alt_txt, WIDTH - 4), col)
        draw_text3x5(buf, 2, 41, left, self.INK_DIM)
        draw_text3x5(buf, WIDTH - 2 - text_w(right), 41, right, self.INK_DIM)
        if type_fits_inline:
            # Centred WITHIN THE GAP the fit-check just measured, not
            # across the full panel -- draw_text_centered() always centres
            # on WIDTH, which is a different, wider span whenever `left`
            # and `right` aren't symmetric (e.g. left="-", right="24MI NE"
            # skews the true gap well off-centre). A real collision this
            # way: P46T vs "24MI NE" passed the fit check (30px of gap for
            # a 15px string) and still visually overlapped, because the
            # centred-on-64px draw started at x=24 while the gap the check
            # validated actually started at x=5. Found by driving
            # select-to-expand across every real aircraft with
            # render_audit's collision detector, not by eye.
            typ_x = gap_start + max(0, (gap - text_w(typ)) // 2)
            draw_text3x5(buf, typ_x, 41, typ, (86, 94, 116))

        draw_dots(buf, 47, len(aircraft), idx, on=col, cap=8)
        draw_divider(buf, 50)

        route = ac.get("route")
        if route and route.get("origin") and route.get("dest"):
            # Real city names when adsbdb provided them ("RALEIGH/DURHAM >
            # NEW YORK"), falling back to the airport codes it always
            # provides ("RDU>LGA") when either municipality is missing.
            # fit_text() truncates gracefully rather than clipping mid-word
            # if a long city pair still doesn't fit at scale 1 -- same
            # safety net already used for the airline name below.
            o_city, d_city = route.get("origin_city"), route.get("dest_city")
            codes = f"{route['origin']}>{route['dest']}".upper()
            # DESTINATION, spelled out in full, is the priority -- a raw
            # 3-letter code ("RDU", "LGA") isn't legible to someone who
            # doesn't already know airport codes, and "where is this
            # plane going" is the more useful real-world question than
            # "where did it come from". Codes are the last resort, only
            # when adsbdb gave no city name at all for the destination.
            if o_city and d_city:
                route_line = f"{o_city} > {d_city}"
            elif d_city:
                route_line = f"> {d_city}"
            else:
                route_line = codes
            # FEATURE 2: real country context, appended rather than given
            # its own fixed row -- this card's vertical budget is fully
            # audited already (see the comment at the top of this block;
            # CLAUDE.md is explicit that fixed offsets have caused real
            # collision bugs here more than once). Piggybacking on the
            # ALREADY-marquee-safe route line means a longer string can
            # never collide with anything: it either fits at scale 1 or
            # scrolls, exactly like the route line already does on its
            # own. Destination country preferred (matches the
            # destination-first city preference above); falls back to
            # origin's when only that one resolved.
            country = route.get("dest_country") or route.get("origin_country")
            if country:
                route_line = f"{route_line} - {country}"
            # NEVER TRUNCATED -- scrolls instead when it's wider than the
            # panel, same marquee `draw_marquee` already uses for news/
            # ticker, rather than cutting the destination name short.
            # Reset to the start whenever the text itself changes (a
            # different aircraft, or a freshly-resolved route), so it
            # doesn't jump in mid-scroll and always begins readable.
            if route_line != self._route_scroll_key:
                self._route_scroll_key = route_line
                self.route_scroll = 0.0
            if text_w(route_line) <= WIDTH - 4:
                draw_text_centered(buf, 53, route_line, self.ROUTE)
            else:
                draw_marquee(buf, 53, route_line, self.ROUTE, self.route_scroll)
            # adsbdb returns mixed-case names ("United Airlines"); already
            # folded through paneltext.panel_text() in flights.py at the
            # I/O boundary (not a bare .upper() here) so a diacritic or
            # curly-quoted airline name can't silently drop a character,
            # same discipline every other feed in this project follows.
            airline = fit_text(route.get("airline") or "", WIDTH - 4)
            if airline:
                draw_text_centered(buf, 59, airline, (86, 94, 116))
        else:
            draw_text_centered(buf, 55, "NO ROUTE DATA", (86, 94, 116))
        return bytes(buf)

    # ---- PLANE-IN-WINDOW ceremonial detail card (2026-08-08) -----------
    def _frame_detail_ceremonial(self, ac):
        """The rich post-takeover showcase: large filled silhouette,
        registration + real Hangar collection index, type code + name,
        FIRST SIGHTING/SEEN xN status band, real age since first sighting,
        airline when known. Real data only -- no Hangar entry for this
        registration (a real, small honest gap -- see hangar.py's own
        ~1-2% no-broadcast-registration note) degrades to a neutral
        "TRACKING" state rather than a guessed status.

        Y-CURSOR layout, not fixed offsets -- CLAUDE.md is explicit that
        fixed rows have caused real collision bugs repeatedly whenever
        content varies (which it does here: an entry may or may not have
        an airline, may be a first sighting or a hundredth)."""
        buf = blank()
        fill(buf, self.BG)
        col = self._alt_color(ac.get("alt_ft"))
        reg = ac.get("reg")
        entries = hangar.LOG.get() if reg else []
        entry = next((e for e in entries if e.get("reg") == reg), None)

        idx_tag = None
        if entry:
            # Real ordinal: this aircraft's position by REAL first_seen,
            # oldest first, out of the real total collection size --
            # "AIRCRAFT #N OF M", not an invented number.
            by_first = sorted(entries, key=lambda e: e.get("first_seen") or 0)
            ordinal = next((i for i, e in enumerate(by_first) if e.get("reg") == reg), None)
            if ordinal is not None:
                idx_tag = f"#{ordinal + 1}/{len(entries)}"

        title = reg or (ac.get("ident") or "UNKNOWN")
        draw_header(buf, title, col, right_tag=idx_tag)

        # Hero silhouette -- same classification as every other icon in
        # this project (_ac_kind reads live category/type), new filled
        # drawing routine, new bigger scale.
        y = 27
        draw_hero_silhouette(buf, WIDTH // 2, y, self._ac_kind(ac), col, scale=0.8)

        is_first = bool(entry and (entry.get("times_seen") or 1) <= 1)
        if is_first:
            draw_first_sighting_ring(buf, WIDTH // 2, y, self.HANGAR, phase=self.ticks * 0.08)

        typ_code = (ac.get("type") or "").strip().upper()
        typ_name = flights._type_name(typ_code) or "TYPE UNKNOWN"
        line = (f"{typ_code} {typ_name}" if typ_code and typ_code != typ_name.upper()
                else typ_name)
        draw_text_centered(buf, 38, fit_text(line, WIDTH - 4), self.INK)

        # Fixed 7px-row cadence for exactly three possible rows, chosen so
        # the LAST one (airline, the least likely to be present) lands at
        # y=59 -- HEIGHT-5, the real last-legal row for a 5px glyph. Not a
        # "cursor that only advances by what actually drew" in the strict
        # sense (this content genuinely has at most 3 known rows, unlike
        # the sports/baseball case CLAUDE.md's y-cursor lesson was about),
        # but still driven by which rows are ACTUALLY populated below
        # rather than reserving blank space for a missing one.
        if entry:
            times = entry.get("times_seen") or 1
            if times <= 1:
                draw_text_centered(buf, 45, "FIRST SIGHTING", self.HANGAR)
            else:
                draw_text_centered(buf, 45, fit_text(f"SEEN {times}X", WIDTH - 4), self.ATC_MATCH)
            age = max(0.0, time.time() - (entry.get("first_seen") or 0))
            draw_text_centered(buf, 52, fit_text(f"{self._fmt_age_long(age)} AGO", WIDTH - 4),
                               (86, 94, 116))
            airline = entry.get("airline")
            if airline:
                draw_text_centered(buf, 59, fit_text(airline, WIDTH - 4), (86, 94, 116))
        else:
            # Genuinely not in THE HANGAR (no broadcast registration, or
            # not recorded yet this cycle) -- honest neutral state, never
            # a guessed FIRST SIGHTING claim.
            draw_text_centered(buf, 48, "TRACKING", self.INK_DIM)
        return bytes(buf)


class NotifyEngine:
    """URGENT-priority Home Assistant notification takeover (task #8,
    2026-08-08). Built with the EXACT same shape as PlaneWatchEngine
    (above) -- read its docstring for the two-takeover-pattern reasoning
    this project already settled; this is deliberately the SAME pattern
    (real mode swap, GAME-DAY-style), not the composite-only one
    (draw_notify_banner(), used instead for normal-priority).

    Zero-arg constructible (set_mode()'s contract), registered in
    engines.ENGINES["notify"], deliberately NOT added to
    MenuEngine.NATIVE_GAMES/AmbientEngine.SEQUENCE -- force-triggered only
    by arcade_server.py's /api/notify handler, never chosen from a menu or
    a rotation, has_content() always False, same contract every other
    takeover-only engine here honours.

    HAND-OFF: set_mode() always constructs ENGINES[base]() with ZERO
    args, so there is no way to pass title/message/color/"which mode to
    return to" directly through the mode switch. This reuses the IDENTICAL
    one-shot module-level slot idiom flights.py's push_pending_detail()/
    pop_pending_detail() already established for PlaneWatchEngine's own
    hand-off problem (see notify.push_pending()/pop_pending()) -- reset()
    consumes it (never peeks).

    RETURN MODE is NOT a hardcoded fallback like PlaneWatchEngine's
    `.launch = "flights"` -- a notification can arrive during literally
    anything (clock, a game, flights, gameday...), so arcade_server.py's
    /api/notify handler captures "what was running right before this
    interrupted it" the same way set_mode() itself already does
    (`prev = self.mode`) and threads that through the same pending slot.
    engines.RESTING_MODE is only the fallback for the genuinely
    unrecoverable case (no prev_mode was captured at all).

    DISMISSAL: any input() press, or the ~15s ceiling (TOTAL_CEILING_TICKS
    -- same 300-tick/~15s duration as PlaneWatchEngine, for consistency
    across the project's two force-triggered takeovers), hands back to
    the captured return mode.
    """

    name = "notify"
    tick_rate = 0.05

    TOTAL_CEILING_TICKS = 300     # ~15s -- matches PlaneWatchEngine's own ceiling

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        payload = notify.pop_pending() or {}
        self.title = str(payload.get("title") or "")
        self.message = str(payload.get("message") or "")
        self.color = payload.get("color") or NOTIFY_URGENT_COLOR
        self.return_mode = payload.get("prev_mode") or RESTING_MODE
        self.ticks = 0
        self.scroll = 0.0
        self.launch = None

    def has_content(self):
        # Force-triggered only -- never polled by AmbientEngine's rotation,
        # same contract PlaneWatchEngine/GameDayEngine honour for the same
        # reason: a takeover cannot take a turn.
        return False

    def _dismiss(self):
        self.launch = self.return_mode

    def input(self, cmd):
        # Any press dismisses -- there is nothing else meaningful to do
        # with left/right/rotate/drop on a notification, same reasoning
        # as PlaneWatchEngine.input().
        self._dismiss()

    def auto(self):
        pass

    def tick(self):
        if self.launch:
            return
        self.ticks += 1
        self.scroll += 1.6
        if self.ticks >= self.TOTAL_CEILING_TICKS:
            self._dismiss()

    def frame(self):
        buf = blank()
        fill(buf, (0, 0, 0))
        for x in range(WIDTH):
            put_px(buf, x, 2, self.color)
            put_px(buf, x, HEIGHT - 3, self.color)

        draw_text_centered(buf, 8, fit_text(self.title, WIDTH - 8), self.color)

        lines = wrap_text(self.message, WIDTH - 8, max_lines=3)
        overflow = sum(len(ln.split()) for ln in lines) < len(self.message.split())
        if overflow:
            draw_marquee(buf, 30, self.message, (220, 230, 255), self.scroll)
        elif lines:
            band_top, band_bot = 18, HEIGHT - 12
            block_h = len(lines) * 8 - 2
            y0 = band_top + max(0, ((band_bot - band_top) - block_h) // 2)
            for i, ln in enumerate(lines):
                draw_text_centered(buf, y0 + i * 8, ln, (220, 230, 255))

        # HEIGHT-9=55 -- footer row, well clear of the real HEIGHT-5=59
        # last-legal row for a 5px glyph.
        draw_text_centered(buf, HEIGHT - 9, "PRESS ANY BUTTON", rim(self.color, 0.7))
        return bytes(buf)


class PlaneWatchEngine:
    """PLANE-IN-WINDOW high-priority takeover (2026-08-08).

    ARCHITECTURE, stated explicitly because this project already has TWO
    different takeover patterns that solve different problems and this
    one deliberately copies only ONE of them:

    - The severe-weather takeover (arcade_server._severe_alert_frame())
      COMPOSITES over whatever the current mode already rendered, applied
      AFTER everything in the render loop. It never swaps `self.mode` and
      never captures input -- the mode underneath keeps running and keeps
      receiving button presses. That does not fit "dismissible via any
      button press, lands on a specific detail card when it ends", because
      there is no real mode underneath to own that press or that hand-off.
    - GAME DAY (GameDayEngine) is a REAL mode swap
      (arcade_server.set_mode("gameday")), so it fully owns input and
      hands the panel back on its own via the SAME `.launch` attribute
      hand-off BootEngine/MenuEngine already use.

    This is built GAME-DAY-style: a real mode swap, registered in
    engines.ENGINES like gameday, NOT in PLAYABLE/MenuEngine.NATIVE_GAMES/
    AmbientEngine.SEQUENCE (force-triggered, never chosen from a menu or a
    rotation). Constructed with ZERO args, matching set_mode()'s contract
    (`ENGINES[base]()`) -- it pulls its own batch from
    flights.FEED.pop_window_takeover_batch() in reset(), which is reading
    already-cached feed state (the same "engine calls FEED" boundary every
    other engine here respects), not new I/O.

    CYCLING: closest-first/notable-secondary, exactly as flights.py's
    pop_window_takeover_batch() already sorted it. ~4-5s per aircraft
    (HOLD_TICKS_PER_AC), an overall ~15s safety ceiling
    (TOTAL_CEILING_TICKS). With enough aircraft that strict per-aircraft
    holds would exceed the ceiling, per-aircraft hold time is COMPRESSED
    (divided down, floored at a still-readable ~2s) rather than truncating
    the cycle early or letting it run over -- every aircraft that entered
    together gets at least a look, which matches "multiple aircraft
    entering together cycle... never chaotic" better than dropping some
    silently. A SINGLE-aircraft batch holds until dismissed or the ceiling
    (never loops pointlessly on itself).

    DISMISSAL: any input() press, or the ceiling running out, hands off to
    flights' detail card for WHICHEVER aircraft is currently shown --
    flights.FEED.push_pending_detail(key) then `.launch = "flights"`,
    consumed by FlightEngine.reset() (see its own docstring on this same
    hand-off) to jump straight to the ceremonial VIEW_DETAIL instead of
    the plain scope.
    """

    name = "planewatch"
    tick_rate = 0.05

    BG = (0, 0, 0)
    HERO = (225, 235, 255)
    ACCENT = (120, 200, 255)
    RING = (190, 160, 255)   # same lavender as FlightEngine.HANGAR -- "this is a Hangar fact"

    HOLD_TICKS_PER_AC = 90        # ~4.5s at tick_rate 0.05
    HOLD_TICKS_MIN = 40           # ~2s floor when compressing for a large batch
    TOTAL_CEILING_TICKS = 300     # ~15s overall safety ceiling

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        # Reading flights.FEED's own cached, already-sorted batch -- zero
        # new I/O, the same "engine calls FEED.get()-shaped method" rule
        # every other engine in this project follows.
        self.batch = flights.FEED.pop_window_takeover_batch() or []
        self.idx = 0
        self.hold = 0
        self.ticks = 0
        self.launch = None
        n = len(self.batch)
        if n <= 1:
            self.per_ac_ticks = None   # single aircraft: hold until dismissed/ceiling, no pointless loop
        else:
            self.per_ac_ticks = max(self.HOLD_TICKS_MIN,
                                    min(self.HOLD_TICKS_PER_AC, self.TOTAL_CEILING_TICKS // n))

    def has_content(self):
        # Force-triggered only -- never polled by AmbientEngine's rotation
        # (it is not in AmbientEngine.SEQUENCE), same contract GameDayEngine
        # honours for the same reason: a takeover cannot take a turn.
        return False

    def _current(self):
        if not self.batch:
            return None
        return self.batch[self.idx % len(self.batch)]

    @staticmethod
    def _key(ac):
        return ac.get("hex") or ac.get("ident")

    def _dismiss(self):
        ac = self._current()
        if ac:
            key = self._key(ac)
            if key:
                flights.FEED.push_pending_detail(key)
        self.launch = "flights"

    def input(self, cmd):
        # ANY press dismisses early to the currently-shown aircraft's
        # detail card -- this mode has nothing else meaningful to do with
        # left/right/rotate/drop, so every one of them means "I've seen
        # this, show me more."
        self._dismiss()

    def auto(self):
        pass

    def tick(self):
        if self.launch:
            return
        if not self.batch:
            self._dismiss()
            return
        self.ticks += 1
        self.hold += 1
        if self.ticks >= self.TOTAL_CEILING_TICKS:
            self._dismiss()
            return
        if self.per_ac_ticks is not None and self.hold >= self.per_ac_ticks:
            self.hold = 0
            if self.idx + 1 >= len(self.batch):
                # Cycled through every aircraft that entered together --
                # hand off showing the last (least-close/least-notable) one.
                self._dismiss()
            else:
                self.idx += 1

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        ac = self._current()
        if ac is None:
            return bytes(buf)

        kind = FlightEngine._ac_kind(ac)
        col = FlightEngine._alt_color(ac.get("alt_ft"))

        reg = ac.get("reg")
        entry = None
        if reg:
            entry = next((e for e in hangar.LOG.get() if e.get("reg") == reg), None)
        is_first = bool(entry and (entry.get("times_seen") or 1) <= 1)

        cy = 34
        if is_first:
            draw_first_sighting_ring(buf, WIDTH // 2, cy, self.RING, phase=self.ticks * 0.08)
        draw_hero_silhouette(buf, WIDTH // 2, cy, kind, col, scale=1.1)

        draw_text_centered(buf, 3, "IN VIEW", self.ACCENT)
        ident = reg or (ac.get("ident") or "UNKNOWN")
        draw_text_centered(buf, 11, fit_text(ident, WIDTH - 4), (255, 255, 255))

        typ = flights._type_name(ac.get("type")) or ""
        if typ:
            draw_text_centered(buf, 54, fit_text(typ, WIDTH - 4), FlightEngine.INK)

        dist_nm = ac.get("dist_nm")
        alt_ft = ac.get("alt_ft")
        parts = []
        if isinstance(dist_nm, (int, float)):
            parts.append(f"{nm_to_mi(dist_nm):.0f}MI")
        if isinstance(alt_ft, (int, float)):
            parts.append(f"{alt_ft:.0f}FT")
        if parts:
            draw_text_centered(buf, 59, fit_text(" ".join(parts), WIDTH - 4), FlightEngine.INK_DIM)

        # Position within the batch, only when there's more than one --
        # a small, honest "there's more" signal, same spirit as
        # draw_dots() elsewhere, kept to plain text since a full dot row
        # would compete with the hero silhouette for attention.
        if len(self.batch) > 1:
            draw_text_centered(buf, 47, f"{self.idx + 1}/{len(self.batch)}", FlightEngine.INK_DIM)
        return bytes(buf)


class SportsEngine(Browsable, BigMomentSource):
    """Live sports scoreboard (NFL/NBA/MLB/NHL via ESPN).

    Same discipline as Ticker/Satellite/Flights: no I/O in this class at
    all. It reads whatever sports.FEED has already cached on its own
    thread and renders it.

    Two views, same shape as SatelliteEngine's PASS/LIVE split: PINNED is
    the hero view -- the owner's chosen team, full-screen, persistent --
    and TICKER is the secondary rotating view over every other game in the
    configured leagues. Auto-cycles between them; left/right jumps
    manually, rotate/drop pauses the auto-cycle (identical control scheme
    to every other data mode).

    With no favorite team configured, PINNED has nothing to show, so the
    mode just stays on TICKER -- a real, complete scoreboard view, not a
    dead end waiting for setup.

    Win probability is only ever drawn when sports.FEED actually handed
    one back. It doesn't exist in ESPN's public data for NHL at all (see
    sports.py's docstring for how that was confirmed against a real
    completed game, not assumed) -- so NHL pinned games simply never show
    a win% line, which is the correct behavior, not a bug.
    """

    name = "sports"
    tick_rate = 0.05

    # LEFT/RIGHT walks games, UP/DOWN walks leagues. Grouping is by LEAGUE
    # rather than sport: ESPN nests sports -> leagues -> events, so sport is
    # the native outer key, but people name LEAGUES ("is the NWSL game
    # on?"). Grouping by sport would also merge ATP with WTA and PGA with
    # LPGA -- genuinely separate events -- and lump three unrelated soccer
    # competitions together. League order follows ESPN's own payload order,
    # which is editorially sensible and, more importantly, STABLE: sorting
    # by "has a live game" would move leagues under the viewer's fingers.
    VERTICAL_BROWSE = True

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    WIN = (60, 230, 110)
    LOSE = (255, 70, 80)
    LIVE = (255, 226, 60)
    STALE = (255, 170, 40)
    FLASH = (255, 255, 255)

    RANK = (255, 226, 60)
    HERO_INK = (235, 242, 255)     # primary value text in the universal/detail views
    BASE_ON = (255, 226, 60)
    BASE_OFF = (46, 50, 62)
    OUT_ON = (255, 90, 80)
    OUT_OFF = (46, 50, 62)

    LEAGUE_COLOR = {
        "NFL": (255, 90, 120), "NBA": (255, 140, 40),
        "MLB": (120, 200, 255), "NHL": (150, 200, 255),
        "EPL": (100, 220, 120), "NCAAF": (255, 160, 200), "NCAAB": (255, 120, 60),
    }

    VIEW_TICKS = 200          # ~10s per view at this tick rate
    TRANSITION_TICKS = 8      # ~0.4s slide between PINNED and TICKER
    DETAIL_TRANSITION_TICKS = 14  # ~0.7s -- a little longer than the panel
                                   # slide above; entering a game's detail
                                   # view is a bigger moment and reads
                                   # better with a touch more time to land
    SPOTLIGHT_TICKS = 90      # ~4.5s per game in the ticker view
    FLASH_TICKS = 14

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"games": [], "favorite": None, "favorite_game": None,
                    "win_prob": None, "age": None, "err": None}
        # PANELS, not a fixed view index. `view` used to be 0=PINNED /
        # 1=TICKER, where slot 0 was CONTESTED: the favourite team and the
        # pinned golfer both wanted it, and whichever lost silently became
        # unreachable. tick() then force-set view=1 on top of that, which
        # is what hid the golfer view entirely.
        #
        # Now every panel that HAS data is an entry in an explicit list and
        # gets its own turn. Nothing can be crowded out, because nothing
        # shares a slot -- adding a future pinned thing cannot hide an
        # existing one.
        self.panels = []
        self.panel_i = 0
        self._last_view = None       # None = first frame, so nothing slides in
        self._prev_frame = None      # last rendered frame, for the outgoing slide
        self._trans_from = None
        self._trans_i = 0
        self._trans_ticks = self.TRANSITION_TICKS
        self._trans_style = transitions.DEFAULT_STYLE
        self.hold = 0
        self.cur = 0                     # index into games, for TICKER view
        # UNIVERSAL ticker: every sport ESPN is currently featuring, from
        # sports.FEED.get_universal(). A league with nothing on is simply
        # not in here -- never rendered as an empty or "no games" row.
        self.universal = []
        self.ucur = 0
        self.detail = None            # event id being shown expanded, or None
        # Pinned golfer, resolved by the feed each poll.
        self.golf_pinned = None
        self.golf_event = None
        self.golf_move = None
        self.golf_pulse = Pulse(ticks=30)   # longer than the standard 14: a
                                             # notable move deserves to be seen
        self._last_golf_big_moment = None   # one-shot tracking for
                                             # _detect_golf_big_moment, kept
                                             # separate from golf_pulse's own
                                             # key (see that method's docstring)
        # Pinned tennis player -- same "where is MY player" framing as
        # golf, resolved by the feed each poll (find_pinned_tennis_player).
        self.tennis_pinned = None
        self.tennis_event = None
        self._init_scroll()
        self.cycling = True
        self.ticks = 0
        self.scroll = 0.0
        self._last_home_score = None
        self._last_away_score = None
        self._last_event_id = None
        self.score_flash = 0
        # BIG-MOMENT queue -- see draw_celebration()'s module docstring for
        # the shared contract. Per-sport detection (HR, goal, finish,
        # buzzer-beater...) calls _set_big_moment() from within tick(); this
        # engine only owns the one-slot queue and the pop, not any
        # detection logic. Detection is per-sport and lives in whichever
        # per-sport tick/parse path already has the data (same split as
        # SPORT_RENDERERS/SPORT_DETAIL_RENDERERS -- a sport opts in, an
        # unclaimed sport contributes nothing and that is correct, not a
        # gap to fill generically).
        self._init_big_moments()
        # Seen-play cursor for _detect_mlb_home_run() -- same one-shot
        # idiom as GameDayEngine._seen_done. None until the first read
        # adopts a baseline, so a game already in progress can't replay
        # its earlier home runs the moment the mode is opened.
        self._seen_home_runs = None
        # Seen-play cursor for _detect_nfl_touchdown() -- identical
        # one-shot idiom to _seen_home_runs above.
        self._seen_nfl_touchdowns = None
        # Seen-play cursor for _detect_nhl_goal() -- identical one-shot
        # idiom to _seen_home_runs above.
        self._seen_nhl_goals = None
        # Seen-play cursor for _detect_basketball_clutch_shot() --
        # identical one-shot idiom to _seen_home_runs above.
        self._seen_basketball_clutch = None
        # State for _detect_soccer_goal -- the per-game "seen" set, keyed
        # off the currently-watched event_id so a game change starts
        # clean instead of replaying goals already shown (same reasoning
        # as GameDayEngine._seen_done for MMA finishes).
        self._soccer_goal_event_id = None
        self._soccer_goal_seen = set()
        # One-shot cursor for _detect_mma_finish() -- same idiom as
        # GameDayEngine._seen_done: ids of MMA/PFL events already
        # state=="post" the first time this engine reads the universal
        # feed are ADOPTED, not fired on, so opening ambient after a
        # card already finished doesn't replay it.
        self._seen_mma_done = None
        # Poll throttle for the per-game big-moment detectors
        # (_detect_mlb_home_run / _detect_nfl_touchdown / _detect_nhl_goal /
        # _detect_basketball_clutch_shot / _detect_soccer_goal). Each of
        # those calls a real network fetch (sports._fetch_*_plays /
        # fetch_new_soccer_goals) that was gated ONLY on "favorite's game
        # is live", not on time -- and _detect_big_moments() runs from
        # tick(), which for SportsEngine fires every tick_rate (0.05s, see
        # arcade_server._game_frame()'s `now - last_tick >= eng.tick_rate`
        # gate). That combination meant a live favorite game was refetching
        # its own SUMMARY_URL up to 20x/second, unthrottled -- found during
        # the 2026-08-08 polling-load audit (CLAUDE.md's "Cut redundant
        # per-league polling" task). Same cadence as
        # sports.WINPROB_REFRESH (20s) -- that constant already governs a
        # background poll of the exact same SUMMARY_URL for the exact same
        # game, so these detectors match its pace instead of inventing a
        # separate one.
        self._detector_last_poll = {}

    def _detector_due(self, key):
        """True at most once per sports.WINPROB_REFRESH seconds for a given
        detector `key`, and always true the first time. Keeps the
        per-game big-moment detectors (HR/TD/goal/clutch/soccer-goal) from
        re-hitting SUMMARY_URL on every tick while a favorite's game is
        live -- see the _detector_last_poll comment in __init__."""
        now = time.time()
        last = self._detector_last_poll.get(key, 0.0)
        if now - last < sports.WINPROB_REFRESH:
            return False
        self._detector_last_poll[key] = now
        return True

    # ---- input -----------------------------------------------------------
    def has_content(self):
        """Anything on anywhere -- the universal feed covers every sport
        ESPN is featuring, so this is only empty when genuinely nothing is
        happening, not merely when the configured leagues are off-season."""
        return bool(self.universal or self.data.get("games"))

    # pop_big_moment()/_set_big_moment() now come from BigMomentSource --
    # see that class for the shared contract every system uses. Sports
    # moments are always TIER_INTERRUPT (unchanged behaviour) and always
    # SYSTEM_SPORTS; every _set_big_moment call below states both
    # explicitly rather than relying on a default, so a future reader
    # never has to go check what the default is.

    PANEL_TEAM = "team"
    PANEL_GOLF = "golf"
    PANEL_TENNIS = "tennis"
    PANEL_EVENTS = "events"

    def _build_panels(self):
        """Every panel with real data behind it, in a stable order.

        Order is deliberate: the things you explicitly PINNED come before
        the general rotation, because you asked for them by name."""
        p = []
        if self.data.get("favorite_game"):
            p.append(self.PANEL_TEAM)
        if self.golf_pinned:
            p.append(self.PANEL_GOLF)
        if self.tennis_pinned:
            p.append(self.PANEL_TENNIS)
        if self.universal:
            p.append(self.PANEL_EVENTS)
        return p

    def _panel(self):
        if not self.panels:
            return None
        return self.panels[self.panel_i % len(self.panels)]

    # ---- league grouping (the vertical axis) -----------------------------
    def _league_key(self, ev):
        return (ev.get("sport"), ev.get("league"))

    def _league_order(self):
        """Leagues present, in the feed's own (stable) order."""
        out = []
        for e in self.universal:
            k = self._league_key(e)
            if k not in out:
                out.append(k)
        return out

    def _league_indices(self, key):
        return [i for i, e in enumerate(self.universal) if self._league_key(e) == key]

    def _current_league(self):
        ev = self._current_event()
        return self._league_key(ev) if ev else None

    def _step(self, direction):
        """LEFT/RIGHT: next game WITHIN the current league.

        Wraps inside the league rather than spilling into the next one --
        the two axes stay independent, so moving sideways never silently
        changes which league you are in."""
        if not self.universal:
            return
        key = self._current_league()
        idxs = self._league_indices(key)
        if not idxs:
            return
        pos = idxs.index(self.ucur) if self.ucur in idxs else 0
        self.ucur = idxs[(pos + direction) % len(idxs)]
        if self.detail is not None:
            self.detail = self.universal[self.ucur]["id"]
        self.hold = 0

    def _step_v(self, direction):
        """UP/DOWN: next LEAGUE, landing on its most interesting game.

        Lands on a live game if the league has one, else its first -- so
        arriving in a league shows you something happening rather than
        whichever fixture happens to be first."""
        if not self.universal:
            return
        order = self._league_order()
        if not order:
            return
        key = self._current_league()
        pos = order.index(key) if key in order else 0
        nxt = order[(pos + direction) % len(order)]
        idxs = self._league_indices(nxt)
        if not idxs:
            return
        live = [i for i in idxs if self.universal[i]["live"]]
        started = [i for i in idxs if self._started(self.universal[i])]
        self.ucur = (live or started or idxs)[0]
        if self.detail is not None:
            self.detail = self.universal[self.ucur]["id"]
        self.hold = 0
        # Moving to a new league means the events panel is what you want.
        if self.PANEL_EVENTS in self.panels:
            self.panel_i = self.panels.index(self.PANEL_EVENTS)

    @staticmethod
    def _started(ev):
        """Has this event actually begun? Live or finished, not scheduled."""
        return (ev or {}).get("state") in ("in", "post")

    def _cycle_indices(self):
        """Indices the AUTO-cycle is allowed to visit.

        Scheduled games are deliberately excluded: a board that spends its
        time showing things that have not happened yet is a schedule, not a
        scoreboard. Manual browsing still reaches them (see _step), which
        is the case where looking up a start time is genuinely useful.

        Falls back to everything if nothing has started, so the panel shows
        the day's fixtures rather than going blank.
        """
        started = [i for i, e in enumerate(self.universal) if self._started(e)]
        return started or list(range(len(self.universal)))

    def _current_event(self):
        """The universal event currently on screen, or None."""
        if not self.universal:
            return None
        return self.universal[self.ucur % len(self.universal)]

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        # SELECT-TO-EXPAND. `rotate` is the select button on this hardware
        # (the phone remote's centre action), so it expands the event under
        # the cursor into the full-detail view and collapses back out of
        # it. `drop` is a second way back, and still toggles auto-advance
        # from the list, which is what it has always done there.
        if cmd == "rotate":
            if self.detail is not None:
                self.detail = None
            else:
                ev = self._current_event()
                if ev:
                    self.detail = ev["id"]
                    # Expanding is about an EVENT, so make sure the events
                    # panel is the one selected (the old `view = 1` here
                    # referred to the removed contested-slot scheme).
                    if self.PANEL_EVENTS in self.panels:
                        self.panel_i = self.panels.index(self.PANEL_EVENTS)
            self.hold = 0
            return
        if cmd == "drop":
            if self.detail is not None:
                self.detail = None       # back to the ticker, same position
            else:
                self.cycling = not self.cycling
            self.hold = 0

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        self.data = sports.FEED.get()
        u = sports.FEED.get_universal()
        self.universal = u.get("events") or []
        self.golf_pinned = u.get("golf_pinned")
        self.golf_event = u.get("golf_event")
        self.golf_move = u.get("golf_move")
        self.tennis_pinned = u.get("tennis_pinned")
        self.tennis_event = u.get("tennis_event")
        # Pulse keys on the MOVE, so it fires once per notable move rather
        # than continuously while the move is still being reported.
        self.golf_pulse.note(("golf", self.golf_move) if self.golf_move else None)
        if self.universal:
            self.ucur %= len(self.universal)
        else:
            self.ucur = 0
            self.detail = None       # nothing to expand
        # Rebuild the panel list, keeping the CURRENT panel selected across
        # the rebuild where possible -- otherwise a panel appearing or
        # disappearing would jump the view for no reason the viewer can see.
        was = self._panel()
        self.panels = self._build_panels()
        if was in self.panels:
            self.panel_i = self.panels.index(was)
        elif self.panels:
            self.panel_i %= len(self.panels)
        else:
            self.panel_i = 0
        games = self.data.get("games") or []
        # ENRICH the universal events with per-league detail the header
        # does not carry. The header has bases and outs but NOT the count
        # -- there are no balls/strikes fields anywhere in it, verified
        # across a full MLB slate -- while the per-league scoreboard has
        # them in `situation`. Event ids match exactly across the two feeds
        # (15/15 on today's slate), so this is a straight id join, not a
        # name guess. This is precisely why the per-league poll is NOT
        # redundant for configured leagues.
        if games and self.universal:
            by_id = {g.get("event_id"): g for g in games}
            for ev in self.universal:
                g = by_id.get(ev.get("id"))
                if g and g.get("situation"):
                    ev["situation"] = g["situation"]
        # Stay on the ticker when PINNED has nothing real to draw: either
        # no favorite is configured at all, or one is but that team has no
        # game today. The second case was previously missed, so with a
        # favorite set on an off day the mode spent half of every cycle
        # (and half of every 20s ambient slot) on a "NO <TEAM> GAME"
        # placeholder while real games sat one view away. Only forced when
        # the ticker actually has games -- otherwise the pinned view's
        # message is the more informative of two empty screens.
        # (The old `view = 1` force lived here. It is gone: _build_panels
        # only ever lists panels that HAVE data, so there is nothing to
        # force away from and nothing to strand.)
        if games:
            self.cur %= len(games)
        self.scroll += 0.5

        # Scoring alert: flash when the PINNED team's score just went up.
        # Tracked by event_id, not just score, so switching to a different
        # pinned team's game (or a new game entirely) can't misread a fresh
        # game's starting score as a "score" against the old baseline.
        fg = self.data.get("favorite_game")
        if fg:
            if fg["event_id"] != self._last_event_id:
                self._last_event_id = fg["event_id"]
                self._last_home_score = fg["home"]["score"]
                self._last_away_score = fg["away"]["score"]
            else:
                h, a = fg["home"]["score"], fg["away"]["score"]
                if (isinstance(h, (int, float)) and isinstance(self._last_home_score, (int, float)) and h > self._last_home_score) or \
                   (isinstance(a, (int, float)) and isinstance(self._last_away_score, (int, float)) and a > self._last_away_score):
                    self.score_flash = self.FLASH_TICKS
                self._last_home_score, self._last_away_score = h, a
        if self.score_flash > 0:
            self.score_flash -= 1

        self._detect_big_moments()

        # Auto-advance is suspended while EXPANDED: having deliberately
        # opened one event, having it slide away on a timer is the exact
        # push-only behaviour the browse control exists to escape.
        # Auto-advance is suspended while EXPANDED (see above).
        if self.cycling and self._browse_auto_ok and self.detail is None and self.panels:
            self.hold += 1
            on_events = self._panel() == self.PANEL_EVENTS
            limit = self.SPOTLIGHT_TICKS if on_events else self.VIEW_TICKS
            if self.hold >= limit:
                self.hold = 0
                if not on_events:
                    # A pinned panel shows once, then hands on.
                    self.panel_i = (self.panel_i + 1) % len(self.panels)
                else:
                    order = self._cycle_indices()
                    if order:
                        # Step to the next STARTED event; when the lap
                        # completes, hand the turn to the next panel.
                        nxt = [i for i in order if i > self.ucur]
                        if nxt:
                            self.ucur = nxt[0]
                        else:
                            self.ucur = order[0]
                            if len(self.panels) > 1:
                                self.panel_i = (self.panel_i + 1) % len(self.panels)
        self.score = len(self.universal) or len(games)

    # ---- render --------------------------------------------------------
    @staticmethod
    def _score_txt(v):
        return str(v) if isinstance(v, (int, float)) else "-"

    def _fit(self, s, max_px):
        # Drop whole trailing words first (ESPN's "shortDetail" strings are
        # space-separated: "8/6 - 8:00 PM EDT") -- a blind char-truncate
        # left dangling fragments like "...PM E" on screen, which reads as
        # broken rather than just abbreviated.
        while s and 4 * len(s) - 1 > max_px and " " in s:
            s = s.rsplit(" ", 1)[0]
        while s and 4 * len(s) - 1 > max_px:
            s = s[:-1]
        return s

    def _frame_empty(self, msg, sub):
        buf = blank()
        fill(buf, self.BG)
        draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 26, msg, self.INK_DIM)
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(sub) - 1)) // 2), 36, sub, self.INK_DIM)
        return bytes(buf)

    def _draw_game_block(self, buf, g, y, big=False, flash_col=None):
        """One game: away line over home line, matching the standard
        box-score convention (visitor listed first/on top).

        Each row gets a left-edge accent bar in that team's REAL color
        (from ESPN's own team.color field, see sports.py) -- broadcast-
        style color coding instead of a logo, same trademark reasoning as
        the flight tracker's plane icon: no logo, just the team's actual
        colors, which is common, unmistakable sports-broadcast visual
        language on its own."""
        scale = 2 if big else 1
        # Glyph height is 5*scale px; the gap between the away/home lines
        # must clear that or the two rows visibly bleed into each other.
        # (8 < 10 at scale=2 did exactly that -- caught by rendering an
        # actual frame and looking at it, not just eyeballing the number.)
        gap = 11 if big else 6
        bar_h = 5 * scale
        for i, (team, other) in enumerate(((g["away"], g["home"]), (g["home"], g["away"]))):
            txt = f"{team['abbr']} {self._score_txt(team['score'])}"
            txt = self._fit(txt, WIDTH - 4)
            w = (8 if big else 4) * len(txt) - (2 if big else 1)
            col = flash_col if flash_col else (
                self.WIN if team["winner"] else (self.INK if g["state"] != "post" else self.INK_DIM))
            row_y = y + i * gap
            bar_col = team.get("color") or self.INK_DIM
            for by in range(bar_h):
                put_px(buf, 1, row_y + by, bar_col)
                put_px(buf, 2, row_y + by, bar_col)
            draw_text3x5(buf, max(6, (WIDTH - w) // 2), row_y, txt, col, scale=scale)
            # Rank on the accent bar itself (NCAAF/NCAAB only -- ESPN's 99
            # "unranked" sentinel is filtered out in sports.py). Sits in
            # the 3px gutter beside the bar, so it costs no width from the
            # score line.
            if team.get("rank") and big:
                draw_text3x5(buf, 4, row_y + bar_h - 5, str(team["rank"]), self.RANK)

    # ---- live-state glyphs ----------------------------------------------
    # Thin delegates to the module-level functions, which is where these
    # actually live so GAME DAY renders the identical glyphs rather than a
    # second copy that could drift.
    def _draw_diamond(self, buf, x, y, bases):
        return draw_diamond(buf, x, y, bases, self.BASE_ON, self.BASE_OFF)

    def _draw_outs(self, buf, x, y, outs):
        return draw_outs(buf, x, y, outs, self.OUT_ON, self.OUT_OFF)

    def _situation_line(self, g):
        return situation_line(g)

    def _frame_pinned(self):
        buf = blank()
        fill(buf, self.BG)
        fg = self.data.get("favorite_game")
        fav = self.data.get("favorite") or {}

        if not fg:
            sub = f"NO {fav.get('team_abbr','')} GAME TODAY".strip()
            return self._frame_empty("PINNED TEAM", self._fit(sub, WIDTH - 4) or "NO GAME TODAY")

        lg_col = self.LEAGUE_COLOR.get(fg["league"], self.INK_DIM)
        # LIVE is the thing a sports fan is actually scanning for, so it
        # gets the header's right slot in the live colour rather than
        # being buried in the status line below.
        live = fg["state"] == "in"
        draw_header(buf, fg["league"], lg_col,
                    right_tag="LIVE" if live else ("FINAL" if fg["state"] == "post" else ""),
                    stale=bool(self.data.get("age") and self.data["age"] > 120),
                    icon=LEAGUE_ICON.get(fg["league"]))

        flash_col = self.FLASH if (self.score_flash > 0 and self.score_flash % 2 == 0) else None
        self._draw_game_block(buf, fg, 12, big=True, flash_col=flash_col)

        # Status line, plus live-state glyphs when the game is actually in
        # progress. Layout gives the glyphs the right edge and lets the
        # detail text keep the rest, rather than centring the detail and
        # letting the two collide -- "BOT 9TH" plus a diamond plus outs is
        # the whole point of this view, so it has to fit as a unit.
        sit = fg.get("situation") if live else None
        detail = fg["detail"] or ""
        if sit and (sit.get("bases") is not None or sit.get("outs") is not None):
            detail = fit_text(detail, WIDTH - 26)
            draw_text3x5(buf, 3, 36, detail, self.LIVE)
            self._draw_diamond(buf, WIDTH - 20, 34, sit.get("bases"))
            self._draw_outs(buf, WIDTH - 11, 38, sit.get("outs"))
        else:
            draw_text_centered(buf, 36, fit_text(detail, WIDTH - 4),
                               self.LIVE if live else (86, 94, 116))

        # One secondary line under the status, then the divider BELOW it.
        # (Drawing both at y=44 previously overlapped the text with the
        # rule -- caught by rendering a real non-live game and looking.)
        #
        # Which line: while a game is live the count / down-and-distance is
        # what has stakes; before first pitch or after the final, the
        # season records are. Editorial call rather than showing both,
        # because both at 64px cost legibility for no gain.
        second = self._situation_line(fg) if live else ""
        if not second and not live:
            ar, hr = fg["away"].get("record"), fg["home"].get("record")
            if ar and hr:
                second = f"{ar} / {hr}"
        if second:
            draw_text_centered(buf, 44, fit_text(second, WIDTH - 4),
                               (150, 160, 185) if live else (110, 118, 140))
            draw_divider(buf, 51)
        else:
            draw_divider(buf, 44)

        win_prob = self.data.get("win_prob")
        if win_prob is not None:
            is_home = fav.get("team_abbr") == fg["home"]["abbr"]
            pct = win_prob if is_home else (1.0 - win_prob)
            draw_text_centered(buf, 47, f"{fav.get('team_abbr','')} {pct * 100:.0f}% WIN", self.WIN)
            # A real two-tone probability bar: the favourite's share in
            # their win colour against the opponent's share, rather than a
            # bar against empty space -- at a glance the SPLIT is the
            # information, and an unfilled remainder reads as "loading"
            # instead of "the other team".
            bar_w, bx0, by = WIDTH - 12, 6, 55
            fill_w = int(bar_w * pct)
            for x in range(bar_w):
                c = self.WIN if x < fill_w else (150, 60, 70)
                put_px(buf, bx0 + x, by, c)
                put_px(buf, bx0 + x, by + 1, rim(c, 0.5))
            for dy in (-1, 0, 1, 2):
                put_px(buf, bx0 + fill_w, by + dy, (235, 240, 255))
        return bytes(buf)

    def _frame_ticker(self):
        games = self.data.get("games") or []
        if not games:
            msg = "NO GAMES" if self.data.get("err") else "LOADING"
            buf = blank()
            fill(buf, self.BG)
            draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 28, msg,
                         self.LOSE if self.data.get("err") else self.INK_DIM)
            dots = "." * (1 + (self.ticks // 12) % 3)
            draw_text3x5(buf, (WIDTH - 11) // 2, 38, dots, self.INK_DIM)
            return bytes(buf)

        buf = blank()
        fill(buf, self.BG)
        g = games[self.cur % len(games)]

        lg_col = self.LEAGUE_COLOR.get(g["league"], self.INK_DIM)
        live = g["state"] == "in"
        draw_header(buf, g["league"], lg_col,
                    right_tag="LIVE" if live else f"{self.cur + 1}/{len(games)}",
                    stale=bool(self.data.get("age") and self.data["age"] > 120),
                    icon=LEAGUE_ICON.get(g["league"]))

        self._draw_game_block(buf, g, 12, big=True)

        detail = fit_text(g["detail"] or "", WIDTH - 4)
        draw_text_centered(buf, 36, detail, self.LIVE if live else (86, 94, 116))

        draw_dots(buf, 44, len(games), self.cur, on=lg_col, cap=10)
        draw_divider(buf, 48)

        parts = [f"{r['away']['abbr']} {self._score_txt(r['away']['score'])} @ "
                 f"{r['home']['abbr']} {self._score_txt(r['home']['score'])} {r['detail']}"
                 for r in games]
        draw_marquee(buf, 53, "   ".join(parts), self.INK_DIM, self.scroll)
        return bytes(buf)

    AMBIENT_STYLE = "wipe_right"    # scoreboard wipe

    def ambient_weight(self):
        games = self.data.get("games") or []
        if any(g.get("state") == "in" for g in games):
            return 3.0            # a live game is the most "happening" thing here
        return 1.0 if games else 0.5


    # ---- universal ticker + expanded detail -----------------------------
    SPORT_ACCENT = {
        "baseball": (120, 200, 255), "basketball": (255, 140, 40),
        "football": (255, 90, 120), "hockey": (150, 200, 255),
        "soccer": (90, 220, 140), "golf": (140, 230, 120),
        "tennis": (230, 220, 90), "mma": (235, 45, 65),
        "lacrosse": (200, 150, 255), "racing": (255, 170, 40),
        "volleyball": (255, 190, 120), "cricket": (170, 220, 170),
    }

    def _sport_accent(self, ev):
        return self.SPORT_ACCENT.get(ev.get("sport"), self.INK)

    def _state_tag(self, ev):
        if ev["live"]:
            return "LIVE"
        return "FINAL" if ev["state"] == "post" else ""

    # ---- per-sport render dispatch ---------------------------------------
    # WHY THIS EXISTS. One generic renderer had to satisfy every sport at
    # once, so every layout decision was made for the WORST CASE across
    # seven of them -- and the worst case is what all of them then got.
    # The tell that it was under-serving: the "one renderer" rule was
    # already broken twice, for golf (a leaderboard, not a fixture) and
    # tennis (string scores too wide for the score slot). Those two broke
    # loudly enough to force an exception; the rest were quietly flattened
    # into two rows of ABBREV + score.
    #
    # So: a sport claims a renderer, or falls back to the generic one.
    # Adding a sport-specific renderer is purely additive -- anything
    # unclaimed renders exactly as it does today.
    #
    # Contract for a renderer: take (buf, ev), draw the WHOLE frame
    # including its own header, return None. The caller owns blank/fill
    # and returns the bytes.
    def _sport_renderer(self, ev):
        return self.SPORT_RENDERERS.get(ev.get("sport"))

    def _frame_universal(self):
        """One event from ANY sport, dispatched to that sport's renderer
        when it has one, else the generic two-row fallback."""
        ev = self._current_event()
        if not ev:
            return self._frame_empty("SPORTS", "NOTHING ON RIGHT NOW")
        fn = self._sport_renderer(ev)
        if fn:
            buf = blank()
            fill(buf, self.BG)
            fn(self, buf, ev)
            return bytes(buf)
        return self._frame_universal_generic()

    def _draw_league_rail(self, buf, ev):
        """Vertical position indicator down the RIGHT edge: one pip per
        league, the current one lit.

        The two axes need to be discoverable without instructions, so each
        gets its own visible affordance -- this rail for UP/DOWN, and the
        header's N/M counter for LEFT/RIGHT. A rail rather than text
        because it also shows HOW MANY leagues there are and roughly where
        you sit among them, which a "3/11" label does not convey at a
        glance.
        """
        order = self._league_order()
        if len(order) < 2:
            return
        key = self._league_key(ev)
        cur = order.index(key) if key in order else 0
        n = len(order)
        # Fit the rail to the panel height, capping the pip count so a
        # 20-league day still renders discrete pips rather than a smear.
        top, bottom = 10, HEIGHT - 6
        span = bottom - top
        step = max(2, min(4, span // max(1, n)))
        h = step * n
        y0 = top + max(0, (span - h) // 2)
        x = WIDTH - 2
        for i in range(n):
            y = y0 + i * step
            if y >= bottom:
                break
            on = (i == cur)
            c = self._sport_accent(ev) if on else (40, 46, 60)
            put_px(buf, x, y, c)
            if on:
                # The active pip is wider, so it reads as a position
                # marker rather than one lit dot in a column.
                put_px(buf, x - 1, y, c)
                if y + 1 < bottom:
                    put_px(buf, x, y + 1, c)
                    put_px(buf, x - 1, y + 1, c)

    def _league_position(self, ev):
        """(index within league, count in league) for the header counter."""
        idxs = self._league_indices(self._league_key(ev))
        if not idxs:
            return 1, 1
        pos = idxs.index(self.ucur) + 1 if self.ucur in idxs else 1
        return pos, len(idxs)

    def _frame_universal_generic(self):
        """The original shared two-row layout. Still the fallback for any
        sport without its own renderer -- deliberately unchanged.

        Deliberately renders whatever the sport actually provides rather
        than forcing every sport into a two-team score: golf gets its
        leader and score to par, tennis gets set scores, MMA gets the two
        fighters. Nothing is invented for a sport that doesn't have it.
        """
        buf = blank(); fill(buf, self.BG)
        ev = self._current_event()
        if not ev:
            return self._frame_empty("SPORTS", "NOTHING ON RIGHT NOW")
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or ev["league"], accent,
                    right_tag=f"{pos}/{total}",
                    stale=bool(self.data.get("age") and self.data["age"] > 300),
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)

        comps = ev["competitors"]
        if ev["leaderboard"]:
            # Golf: the top of the leaderboard IS the story.
            for i, c in enumerate(comps[:4]):
                y = 14 + i * 10
                pos = str(c["place"] or i + 1)
                draw_text3x5(buf, 3, y, fit_text(pos, 8), self.INK_DIM)
                draw_text3x5(buf, 13, y, fit_text(c["abbr"], 34), self.HERO_INK)
                sc = c["score"] or "-"
                draw_text3x5(buf, WIDTH - 3 - text_w(sc), y, sc,
                             self.WIN if str(sc).startswith("-") else self.INK)
            thru = comps[0].get("thru") if comps else None
            foot = ev["detail"]
            if thru:
                foot = f"THRU {thru}"
            draw_divider(buf, 54)
            draw_text_centered(buf, 57, fit_text(foot, WIDTH - 6), self.INK_DIM)
            return bytes(buf)

        # Head-to-head: two rows, real team colours where the sport has them.
        for i, c in enumerate(comps[:2]):
            y = 16 + i * 13
            bar = c.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (1, 2):
                    put_px(buf, bx, y + by, bar)
            sc = c["score"]
            sc_txt = str(sc) if sc is not None else ""
            col = self.WIN if c["winner"] else self.HERO_INK
            # A numeric score is short and belongs beside the name at
            # scale 2. Tennis scores are STRINGS like "6-7(5-7) 4-6" --
            # 126px at scale 2 on a 64px panel -- so those drop to scale 1
            # on their own line rather than overflowing.
            wide = bool(sc_txt) and text_w(sc_txt, 2) > 22
            if wide:
                draw_text3x5(buf, 6, y, fit_text(c["abbr"], WIDTH - 12), col)
                draw_text3x5(buf, 6, y + 6, fit_text(sc_txt, WIDTH - 10), self.INK)
            else:
                name = fit_text(c["abbr"], WIDTH - 12 - (text_w(sc_txt, 2) if sc_txt else 0) - 8, 2)
                draw_text3x5(buf, 6, y, name, col, scale=2)
                if sc_txt:
                    draw_text3x5(buf, WIDTH - 3 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)

        line = ev["detail"] or ""
        if ev["live"] and ev.get("clock"):
            line = f"{line} {ev['clock']}".strip()
        draw_divider(buf, 44)
        draw_text_centered(buf, 47, fit_text(line, WIDTH - 6),
                           self.LIVE if ev["live"] else self.INK_DIM)
        if ev.get("class_label"):
            draw_text_centered(buf, 56, fit_text(ev["class_label"], WIDTH - 6), self.INK_DIM)
        elif ev.get("series"):
            draw_text_centered(buf, 56, fit_text(ev["series"], WIDTH - 6), self.INK_DIM)
        return bytes(buf)

    def _frame_golf_pinned(self):
        """The pinned player, given the panel.

        Golf's whole question is "where is MY player and did they just do
        something", so this leads with position / score to par / through
        -- the three numbers that answer it -- rather than a leaderboard
        they'd have to scan.

        Flashes via the shared Pulse on a NOTABLE move only (eagle,
        birdie, bogey, taking or losing the lead). Routine holes do not
        flash; a flash that fires constantly is one you stop seeing.
        """
        buf = blank(); fill(buf, self.BG)
        c = self.golf_pinned
        ev = self.golf_event or {}
        accent = self.SPORT_ACCENT["golf"]
        move = self.golf_move
        flash = self.golf_pulse.on

        draw_event_frame(buf, 1.0 if flash else 0.45, accent, accent)
        head = ev.get("league") or "GOLF"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           (255, 255, 255) if flash else color_on_dark(accent), x_min=3)

        # Name, largest that fits -- never truncated, since a clipped
        # surname is the wrong player.
        name = c.get("abbr") or c.get("full") or "-"
        scale = 2 if text_w(name, 2) <= WIDTH - 8 else 1
        draw_text_centered(buf, 14, fit_text(name, WIDTH - 8, scale),
                           self.HERO_INK, scale=scale, x_min=3)

        # POSITION and SCORE side by side -- the two headline numbers.
        place = c.get("place")
        pos = f"T{place}" if place and self._golf_tied(place) else (str(place) if place else "-")
        par = c.get("score") or "-"
        y = 28 if scale > 1 else 24
        draw_text3x5(buf, 5, y, "POS", self.INK_DIM)
        draw_text3x5(buf, 5, y + 7, pos, self.HERO_INK, scale=2)
        pw = text_w(str(par), 2)
        draw_text3x5(buf, WIDTH - 5 - text_w("PAR"), y, "PAR", self.INK_DIM)
        draw_text3x5(buf, WIDTH - 5 - pw, y + 7, str(par),
                     self.WIN if str(par).startswith("-") else self.HERO_INK, scale=2)

        # THROUGH -- the piece that says how live this number is. A player
        # who has not teed off yet has thru 0, which is not "through 0
        # holes doing badly", so it says so instead.
        thru = c.get("thru")
        if c.get("player_state") == "pre" or not thru:
            foot = "NOT STARTED" if c.get("player_state") == "pre" else (ev.get("detail") or "")
        else:
            foot = f"THRU {thru}"
        draw_text_centered(buf, 48, fit_text(foot, WIDTH - 8), self.INK, x_min=3)

        # The move itself, when there is one, in place of the tournament name.
        if move:
            draw_text_centered(buf, 56, fit_text(move, WIDTH - 8),
                               (255, 255, 255) if flash else self.WIN, x_min=3)
        else:
            draw_text_centered(buf, 56, fit_text(ev.get("name") or "", WIDTH - 8),
                               self.INK_DIM, x_min=3)
        return bytes(buf)

    def _golf_tied(self, place):
        """Is anyone else on the same place? ESPN repeats the place number
        for ties (three players all shown as 2), so a 'T' prefix is real
        information recoverable from the leaderboard itself."""
        ev = self.golf_event or {}
        n = sum(1 for x in (ev.get("competitors") or []) if x.get("place") == place)
        return n > 1

    def _tennis_set_line(self, comps):
        """Combine both competitors' per-set `sets` lists into ONE
        real set-by-set score string -- "7-6(7-5) 3-6 6-1" -- matching how
        a real scoreboard/broadcast graphic shows a tennis match, and
        matching ESPN's own real finished-match `notes[]` text format
        exactly (verified live: both independently produce "7-6 (7-5)"
        for the same real set).

        Each `sets` entry is confirmed real per-competitor data: `games`
        is that player's OWN game count for the set, `tiebreak` (when
        present) is THAT player's own breaker points -- both sides carry
        their own tiebreak value, not a single shared one (confirmed
        against a real payload: winner tiebreak=7, loser tiebreak=5 on
        the same set). A set neither competitor has data for yet (not
        played) is simply not included -- never a guessed "0-0".
        """
        if len(comps) < 2:
            return ""
        a, b = (comps[0].get("sets") or []), (comps[1].get("sets") or [])
        n = min(len(a), len(b))    # only sets BOTH sides actually have real data for
        parts = []
        for i in range(n):
            sa, sb = a[i], b[i]
            s = f"{sa['games']}-{sb['games']}"
            ta, tb = sa.get("tiebreak"), sb.get("tiebreak")
            if ta is not None and tb is not None:
                s += f"({ta}-{tb})"
            elif ta is not None or tb is not None:
                s += f"({ta if ta is not None else tb})"
            parts.append(s)
        return " ".join(parts)

    def _frame_tennis_pinned(self):
        """The pinned player, given the panel -- same "where is MY player"
        framing golf's pinned view established, applied to a head-to-head
        sport instead of a leaderboard: name, opponent, real set-by-set
        score so far, real match state. No live in-game point score is
        drawn -- see sports.py's TENNIS section docstring for why: no
        live match was available this session to confirm a field name for
        one, and this project never guesses a field into existence.
        """
        buf = blank(); fill(buf, self.BG)
        c = self.tennis_pinned or {}
        ev = self.tennis_event or {}
        accent = self.SPORT_ACCENT["tennis"]
        live = ev.get("live")

        draw_event_frame(buf, 1.0 if live else 0.45, accent, accent)
        head = ev.get("league") or "TENNIS"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           color_on_dark(accent), x_min=3)

        comps = ev.get("competitors") or []
        opp = next((x for x in comps if x.get("id") != c.get("id")), None)

        name = c.get("abbr") or c.get("full") or "-"
        scale = 2 if text_w(name, 2) <= WIDTH - 8 else 1
        y = 14
        draw_text_centered(buf, y, fit_text(name, WIDTH - 8, scale),
                           self.WIN if c.get("winner") else self.HERO_INK,
                           scale=scale, x_min=3)
        y += 5 * scale + 2

        if opp:
            vs = f"VS {opp.get('abbr') or opp.get('full') or ''}"
            draw_text_centered(buf, y, fit_text(vs, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

        line = self._tennis_set_line(comps[:2]) if len(comps) >= 2 else ""
        y += 2
        if line:
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.HERO_INK, x_min=3)
            y += 7
        elif ev.get("state") == "pre":
            draw_text_centered(buf, y, "NOT STARTED", self.INK_DIM, x_min=3)
            y += 7

        foot = ev.get("detail") or ev.get("name") or ""
        draw_text_centered(buf, min(max(y, 48), 56), fit_text(foot, WIDTH - 8),
                           self.LIVE if live else self.INK, x_min=3)
        return bytes(buf)

    # ---- per-sport renderers ---------------------------------------------
    def _draw_inning_arrow(self, buf, x, y, top, color):
        """Half-inning as a triangle: up = top, down = bottom. Thin
        wrapper over the shared draw_trend_arrow -- see that docstring."""
        draw_trend_arrow(buf, x, y, top, color)

    def _draw_scoreline(self, buf, ev, y, accent):
        """Two team rows with real colours, abbreviation and score. Shared
        by the team-sport renderers so they stay visually consistent with
        each other even while their live-state areas differ."""
        for i, c in enumerate(ev["competitors"][:2]):
            row = y + i * 12
            bar = c.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (1, 2):
                    put_px(buf, bx, row + by, bar)
            sc = c.get("score")
            sc_txt = "" if sc is None else str(sc)
            col = self.WIN if c.get("winner") else self.HERO_INK
            avail = WIDTH - 12 - (text_w(sc_txt, 2) if sc_txt else 0)
            draw_text3x5(buf, 5, row, fit_text(c.get("abbr") or "", avail, 2), col, scale=2)
            if sc_txt:
                draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), row, sc_txt, col, scale=2)

    def _render_baseball(self, buf, ev):
        """MLB. The live state IS the story -- "bottom 9th, 2 outs, runner
        on third" is a moment, "3-2" is a number -- so the diamond, outs,
        count and half-inning live on the MAIN row rather than behind a
        rotate press.

        Data provenance, because it is split across two feeds:
          * bases  -- header `onFirst/onSecond/onThird`. These are ATHLETE
            IDs, not booleans (0 = base empty, an id = that player is on
            it), which is why truthiness is the right test.
          * outs   -- header `outsText` ("2 Outs"), parsed to an int.
          * count  -- NOT in the header at all. Comes from the per-league
            scoreboard's `situation`, joined by event id in tick().
        Anything missing is simply not drawn.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or "MLB", accent,
                    right_tag=f"{pos}/{total}",
                    stale=bool(self.data.get("age") and self.data["age"] > 300))
        self._draw_league_rail(buf, ev)
        self._draw_scoreline(buf, ev, 11, accent)

        draw_divider(buf, 36)
        live = ev["live"]
        if live:
            # Half-inning + number, then the diamond, then outs, then count.
            detail = ev.get("detail") or ""
            top = detail.startswith("TOP")
            inning = ev.get("period")
            x = 4
            if inning:
                self._draw_inning_arrow(buf, x, 40, top, self.LIVE)
                x += 5
                draw_text3x5(buf, x, 40, str(inning), self.LIVE)
                x += text_w(str(inning)) + 4

            draw_diamond(buf, x, 39, ev.get("bases"))
            x += 9
            outs = ev.get("outs")
            if outs is not None:
                draw_outs(buf, x, 43, outs)
                x += 10

            sit = ev.get("situation") or {}
            b, k = sit.get("balls"), sit.get("strikes")
            if isinstance(b, int) and isinstance(k, int):
                draw_text3x5(buf, WIDTH - 4 - text_w(f"{b}-{k}"), 40, f"{b}-{k}", self.INK)
        else:
            draw_text_centered(buf, 40, fit_text(ev.get("detail") or "", WIDTH - 6),
                               self.INK_DIM)

        # Bottom line: series context while live/final, records before.
        foot = ev.get("series") or ""
        if not foot:
            recs = [c.get("record") for c in ev["competitors"][:2] if c.get("record")]
            foot = " / ".join(recs) if len(recs) == 2 else ""
        if foot:
            draw_text_centered(buf, 50, fit_text(foot, WIDTH - 6), self.INK_DIM)
        bc = ev.get("broadcast")
        if bc:
            draw_text_centered(buf, 57, fit_text(bc, WIDTH - 6), self.INK_DIM)

    def _render_mma(self, buf, ev):
        """MMA. A fight is not a score, it is a MATCHUP -- who, at what
        weight, with what records behind them. So the weight class is a
        primary line rather than a footnote, and each fighter's record
        sits with their name instead of being dropped.

        Card position is real structure worth showing: `cardSegment`
        (MAIN / PRELIMS) plus `matchNumber`. Note the numbering here is
        the OPPOSITE of the UFC scoreboard used by GAME DAY -- on this
        feed match number 1 IS the main event, whereas there the main
        event is last in the list. Verified against a real PFL card.

        Round and finish time come from `period` and `clock`, which carry
        the same meaning as in mma.py: time ELAPSED in the final round.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or "MMA", accent, right_tag=f"{pos}/{total}",
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)

        # PRIMARY line: weight class, and whether this is the main event.
        seg = ev.get("card_segment") or ""
        num = ev.get("match_number")
        headline = ev.get("class_label") or "MMA"
        draw_text_centered(buf, 10, fit_text(headline, WIDTH - 6), color_on_dark(accent))
        tag = "MAIN EVENT" if (num == 1 and seg.startswith("MAIN")) else seg
        if tag:
            draw_text_centered(buf, 17, fit_text(tag, WIDTH - 6), self.INK_DIM)

        # Name on its own row, record beneath it. They shared a row at
        # first and collided; reserving the record's width instead
        # truncated "A. COLGAN" to "A.", which loses WHO -- the whole
        # point of the view. The record is the thing that can afford its
        # own dimmer line.
        y = 25
        for i, c in enumerate(ev["competitors"][:2]):
            won = c.get("winner")
            col = self.WIN if won else (self.INK if ev["state"] == "post" else self.HERO_INK)
            draw_text3x5(buf, 6, y, fit_person(c.get("abbr"), WIDTH - 10), col)
            if won:
                # A block beside the winner: colour alone is ambiguous on
                # a fight nobody has won yet, and a "W" column costs more
                # width at 64px than it earns.
                for dy in range(2):
                    for dx in range(2):
                        put_px(buf, 2 + dx, y + 1 + dy, self.WIN)
            rec = c.get("record")
            if rec:
                draw_text3x5(buf, 8, y + 6, rec, self.INK_DIM)
            y += 13
            if i == 0:
                draw_text_centered(buf, y, "VS", color_on_dark(accent))
                y += 8

        # Result: round and time when it is over, status otherwise.
        rnd, clk = ev.get("period"), ev.get("clock")
        if ev["state"] == "post" and rnd and clk:
            line = f"R{rnd}  {clk}"
        else:
            line = ev.get("detail") or ""
        if line:
            draw_text_centered(buf, 58, fit_text(line, WIDTH - 6),
                               self.LIVE if ev["live"] else self.INK)

    def _render_soccer(self, buf, ev):
        """Soccer. Three things a football scoreboard has that a generic
        renderer flattens away: FORM (recent results, not just today's
        score), the clock already carrying stoppage time ("90'+3'" --
        ESPN pre-formats this, it is not computed here), and a penalty
        shootout score when a match was decided that way.

        AGGREGATE SCORE (`ev["series"]`, from ESPN's `seriesSummary`) is
        wired through and rendered if present, but UNVERIFIED: no
        two-legged tie is live anywhere in today's feed to check it
        against, the same honest gap football is in for down-and-distance.
        It will render correctly the day a real one appears; nobody has
        seen it do so yet.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or "SOCCER", accent, right_tag=f"{pos}/{total}",
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)

        # Laid out with a CURSOR. Fixed offsets put the divider and the
        # clock straight through the second team's score row -- each team
        # block is 10px of score PLUS a form line, and the constants below
        # were written for a block without one. The render audit caught it
        # ("LOU" overlapping "90'+4'"); it was invisible in a spot check
        # because the overlapping elements sat in different columns for
        # the matches I happened to look at.
        y = 9
        for c in ev["competitors"][:2]:
            bar = c.get("color") or self.INK_DIM
            for by in range(9):
                for bx in (1, 2):
                    put_px(buf, bx, y + by, bar)
            sc = c.get("score")
            sc_txt = "" if sc is None else str(sc)
            col = self.WIN if c.get("winner") else self.HERO_INK
            avail = WIDTH - 12 - (text_w(sc_txt, 2) if sc_txt else 0)
            draw_text3x5(buf, 5, y, fit_text(c.get("abbr") or "", avail, 2), col, scale=2)
            if sc_txt:
                draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)
            y += 11
            # Form beneath the score row -- most recent result rightmost,
            # matching how a real match programme lists it.
            form = c.get("form")
            if form:
                draw_text3x5(buf, 5, y, form, self.INK_DIM)
                y += 6
            y += 2

        draw_divider(buf, y)
        y += 3

        shootouts = [c.get("shootout") for c in ev["competitors"][:2]]
        if all(s is not None for s in shootouts):
            # Decided on penalties: that IS the headline once it happens,
            # not a footnote under a tied scoreline.
            draw_text_centered(buf, y, "PENALTIES", self.LIVE)
            y += 7
            pk = "-".join(str(s) for s in shootouts)
            if y + 10 <= HEIGHT:
                draw_text_centered(buf, y, pk, self.HERO_INK, scale=2)
                y += 11
        else:
            clock = ev.get("clock") or ""
            if clock:
                draw_text_centered(buf, y, fit_text(clock, WIDTH - 6),
                                   self.LIVE if ev["live"] else self.INK_DIM)
                y += 7
            detail = ev.get("detail") or ""
            if detail and detail != clock and y + 5 <= HEIGHT:
                draw_text_centered(buf, y, fit_text(detail, WIDTH - 6), self.INK)
                y += 7

        # Aggregate when ESPN supplies one -- see docstring on verification
        # status -- else any match note. Only if a row is actually left.
        extra = ev.get("series") or ev.get("note") or ""
        if extra and y + 5 <= HEIGHT:
            draw_text_centered(buf, y, fit_text(extra, WIDTH - 6), self.INK_DIM)

    def _draw_movement(self, buf, x, y, move, color_up, color_dn):
        """Small arrow for leaderboard movement: negative = moved UP
        toward the lead (ESPN's sign convention, verified against a real
        final-round leaderboard -- a player who jumped from ~63rd to 3rd
        showed movement=-60). Zero draws nothing; a flat position is not
        worth a glyph."""
        if not move:
            return
        up = move < 0
        c = color_up if up else color_dn
        if up:
            pts = ((1, 0), (0, 2), (1, 1), (2, 2))
        else:
            pts = ((1, 2), (0, 0), (1, 1), (2, 0))
        for dx, dy in pts:
            put_px(buf, x + dx, y + dy, c)

    def _render_golf(self, buf, ev):
        """Golf. Deeper leaderboard than the generic fallback (6 rows
        instead of 4) with a movement arrow per player -- ESPN tracks
        exactly this (`movement`, verified negative = climbed the
        leaderboard) and it was being computed nowhere and shown nowhere.

        `thru` is holes completed in the CURRENT round, not the whole
        tournament -- a player showing "F" has finished today's round,
        which is what the leader's thru drives the footer from.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or ev["league"], accent,
                    right_tag=f"{pos}/{total}",
                    stale=bool(self.data.get("age") and self.data["age"] > 300),
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)

        comps = ev["competitors"]
        rows = 6
        for i, c in enumerate(comps[:rows]):
            y = 10 + i * 8
            pos_txt = str(c.get("place") or i + 1)
            sc = c.get("score") or "-"
            # Budget the name from the ACTUAL score width rather than a
            # guessed constant. The constant reserved 20px for a score
            # that is usually 11px, and cut "E. HENSELEIT" down to
            # "HENSEL" -- a damaged name, not an abbreviation. Columns are
            # tightened too, which buys the surname a few more characters.
            draw_text3x5(buf, 1, y, fit_text(pos_txt, 10), self.INK_DIM)
            self._draw_movement(buf, 12, y, c.get("movement"), self.WIN, self.LOSE)
            name_x = 16
            name_w = (WIDTH - 2 - text_w(sc)) - name_x - 2
            draw_text3x5(buf, name_x, y, fit_person(c.get("abbr"), name_w), self.HERO_INK)
            draw_text3x5(buf, WIDTH - 2 - text_w(sc), y,
                         sc, self.WIN if str(sc).startswith("-") else self.INK)

        leader_thru = comps[0].get("thru") if comps else None
        foot = f"THRU {leader_thru}" if leader_thru else (ev.get("detail") or "")
        # y=59 is the LAST row that fits a scale=1 glyph (5px tall on a
        # 64px panel) -- one row lower clipped the bottom pixel of every
        # descender, caught by the bounds sweep, not by eye.
        draw_divider(buf, 58)
        draw_text_centered(buf, 59, fit_text(foot, WIDTH - 6), self.INK_DIM)

    def _render_tennis(self, buf, ev):
        """Tennis. Task #19 -- the last sport still on the generic
        fallback, blocked since 2026-08-01 on "no live match to verify
        against" until this session pulled real live/finished matches
        from the dedicated per-tour scoreboard (see sports.py's TENNIS
        section for the real schema facts -- a completely different
        nested shape from every team sport this file otherwise parses).

        The generic renderer's whole reason for the tennis exception in
        the first place was STRING SCORES TOO WIDE FOR THE SLOT -- a set
        score drawn at scale 2 is 126px on a 64px panel. This renderer
        gives the set-by-set line its own row at scale 1, on a y-cursor,
        rather than trying to squeeze it beside a name.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or "TENNIS", accent, right_tag=f"{pos}/{total}",
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)

        comps = ev["competitors"][:2]
        y = 10
        for c in comps:
            col = self.WIN if c.get("winner") else self.HERO_INK
            draw_text3x5(buf, 4, y, fit_person(c.get("abbr") or c.get("full") or "", WIDTH - 8), col)
            y += 7
        y += 2

        # Set-by-set score, own line, scale 1 -- never scale 2 (see
        # docstring above and CLAUDE.md's own layout rule on this exact
        # sport). Real data only: an unplayed set is simply absent, never
        # a guessed "0-0".
        line = self._tennis_set_line(comps)
        if line:
            draw_text_centered(buf, y, fit_text(line, WIDTH - 6), self.INK)
            y += 7
        elif ev["state"] == "pre":
            draw_text_centered(buf, y, "NOT STARTED", self.INK_DIM)
            y += 7

        # The DRAW (Men's/Women's Singles or Doubles) -- real structure a
        # generic renderer would have had no room for.
        draw_label = ev.get("class_label") or ""
        if draw_label and y <= HEIGHT - 12:
            draw_text_centered(buf, y, fit_text(draw_label, WIDTH - 6), self.INK_DIM)
            y += 6

        tag = ev.get("detail") or ""
        if tag and y <= HEIGHT - 5:
            draw_text_centered(buf, y, fit_text(tag, WIDTH - 6),
                               self.LIVE if ev["live"] else self.INK_DIM)

    def _render_football(self, buf, ev):
        """NFL/NCAAF. Closes the last row of the MAIN-renderer table.

        Reuses `_draw_scoreline()` as-is (same two-row team block every
        other team-sport renderer uses) and the module-level
        `situation_line()` for down-and-distance -- both already existed
        and already worked, this just wires them into a dedicated
        renderer instead of leaving football on the generic fallback.

        `situation_line()` prefers `sit["down_distance"]`
        (`sports._situation()`'s `downDistanceText`, folded at the I/O
        boundary in sports.py) -- present on the event dict ONLY when
        `state == "in"` AND the per-league scoreboard join in tick() ran
        for this event's league (NFL/NBA are in DEFAULT_LEAGUES; NCAAF/
        NCAAB only if configured). No live NFL/NCAAF game existed this
        session to confirm the field actually renders -- see CLAUDE.md's
        FOOTBALL section for the honest verification status. If it's
        genuinely absent, `situation_line()` returns "" and nothing is
        drawn -- no fabricated down-and-distance.

        No possession indicator: no ESPN field for one was ever confirmed
        on a real payload, matching `_situation()`'s own documented NHL
        power-play precedent ("guessing one would be inventing the
        feature"). Not built.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or "FOOTBALL", accent,
                    right_tag=f"{pos}/{total}",
                    stale=bool(self.data.get("age") and self.data["age"] > 300),
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)
        self._draw_scoreline(buf, ev, 11, accent)

        draw_divider(buf, 36)
        y = 40
        if ev["live"]:
            period, clock = ev.get("period"), ev.get("clock") or ""
            line = f"Q{period} {clock}".strip() if period else clock
            if line:
                draw_text_centered(buf, y, fit_text(line, WIDTH - 6), self.LIVE)
                y += 7
            dd = situation_line(ev)
            if dd:
                draw_text_centered(buf, y, fit_text(dd, WIDTH - 6), self.INK)
                y += 7
        else:
            draw_text_centered(buf, y, fit_text(ev.get("detail") or "", WIDTH - 6),
                               self.INK_DIM)
            y += 7

        # Records when the live-state rows leave room -- same "series,
        # else records" fallback baseball's main row already established.
        foot = ev.get("series") or ""
        if not foot:
            recs = [c.get("record") for c in ev["competitors"][:2] if c.get("record")]
            foot = " / ".join(recs) if len(recs) == 2 else ""
        if foot and y <= HEIGHT - 5:
            draw_text_centered(buf, y, fit_text(foot, WIDTH - 6), self.INK_DIM)
            y += 7
        bc = ev.get("broadcast")
        if bc and y <= HEIGHT - 5:
            draw_text_centered(buf, y, fit_text(bc, WIDTH - 6), self.INK_DIM)

    def _render_basketball(self, buf, ev):
        """NBA/NCAAB. The more standard of this session's two sports --
        no bases/downs/sets, just teams, scores, records and a clock.

        What this ACTUALLY adds over `_frame_universal_generic()`: the
        generic fallback never shows a team's record at all (only
        detail/clock/class_label), and its two-row score block scales
        the team block for the WORST case across every sport (tennis'
        wide string scores) rather than basketball's own numeric one.
        This renderer is genuinely closer to "the same layout with a
        purpose-built header" than to a from-scratch design -- an honest
        outcome per this task's own instructions, not a forced one.

        Deliberately NOT built: a bonus/foul-count/timeouts display. No
        live NBA/NCAAB game existed this session to confirm a real ESPN
        field name for any of those, and no such field was found on the
        confirmed `situation`/header shapes this module already parses
        (see sports._situation()'s docstring: the only fields ever
        confirmed there are baseball's and NFL's). Guessing one would be
        inventing the feature, the same standing rule `_situation()`'s
        own NHL power-play precedent already documents.
        """
        accent = self._sport_accent(ev)
        pos, total = self._league_position(ev)
        draw_header(buf, ev["league_name"] or "BASKETBALL", accent,
                    right_tag=f"{pos}/{total}",
                    stale=bool(self.data.get("age") and self.data["age"] > 300),
                    icon=SPORT_ICONS.get(ev.get("sport")))
        self._draw_league_rail(buf, ev)
        self._draw_scoreline(buf, ev, 11, accent)

        draw_divider(buf, 36)
        y = 40
        if ev["live"]:
            period, clock = ev.get("period"), ev.get("clock") or ""
            line = f"Q{period} {clock}".strip() if period else clock
            if line:
                draw_text_centered(buf, y, fit_text(line, WIDTH - 6), self.LIVE)
                y += 7
        else:
            draw_text_centered(buf, y, fit_text(ev.get("detail") or "", WIDTH - 6),
                               self.INK_DIM)
            y += 7

        foot = ev.get("series") or ""
        if not foot:
            recs = [c.get("record") for c in ev["competitors"][:2] if c.get("record")]
            foot = " / ".join(recs) if len(recs) == 2 else ""
        if foot and y <= HEIGHT - 5:
            draw_text_centered(buf, y, fit_text(foot, WIDTH - 6), self.INK_DIM)
            y += 7
        bc = ev.get("broadcast")
        if bc and y <= HEIGHT - 5:
            draw_text_centered(buf, y, fit_text(bc, WIDTH - 6), self.INK_DIM)

    # Populated as each sport gets its own renderer. Empty here means
    # every sport still takes the generic path, so this step changes
    # nothing on screen -- it only creates the seam.
    SPORT_RENDERERS = {
        "baseball": _render_baseball,
        "mma": _render_mma,
        "soccer": _render_soccer,
        "golf": _render_golf,
        "tennis": _render_tennis,
        "football": _render_football,
        "basketball": _render_basketball,
    }

    # ---- per-sport EXPANDED-detail dispatch -------------------------------
    # Same seam as SPORT_RENDERERS, one level deeper. The generic detail
    # view below was fine when every sport's MAIN view was equally generic
    # -- but now baseball's main row shows inning/count/diamond, MMA's
    # shows weight class and records as the headline, soccer's shows form
    # and shootout scores, golf's shows a real leaderboard. The shared
    # detail view still only shows a plain two-row score plus whatever
    # generic "extra" fits -- for baseball specifically it shows LESS live
    # state (bases/outs, no count) than the compact main row already does,
    # which is backwards for a view whose whole point is "more detail".
    #
    # A sport claims its own expanded renderer, or falls back to the
    # generic one -- same additive, cannot-regress-another-sport contract
    # as SPORT_RENDERERS. Registry starts EMPTY so this commit is a pure
    # seam, verified byte-identical to the old behaviour before anything
    # is added to it.
    SPORT_DETAIL_RENDERERS = {}

    # ---- per-sport BIG-MOMENT detection -----------------------------------
    # Same additive seam as SPORT_RENDERERS/SPORT_DETAIL_RENDERERS, one
    # more level over: a sport registers a detector, called once per tick
    # with no arguments beyond self. Each detector is responsible for
    # finding its OWN relevant live game/state (from self.data/
    # self.universal) and deciding whether to fire -- deliberately not
    # handed a single "the live game" by the caller, because what counts
    # as the relevant game differs by sport (MLB/soccer check the pinned
    # favorite's live game; golf checks self.golf_move, already computed
    # by the feed). A detector that finds nothing simply returns.
    # Registry starts EMPTY -- adding a detector is purely additive and
    # cannot regress another sport, same contract as the two seams above.
    # Detectors that raise are swallowed (never let a bad one break sports
    # mode); each is timed for I/O the same as everything else in this
    # project -- see sports.py for the request-volume discipline any new
    # per-game polling here must follow.

    BIG_MOMENT_DETECTORS = {}

    def _detect_golf_big_moment(self):
        """Golf's detector -- the only one of the five that needs no new
        parsing: `sports.golfer_move()` already runs inside the feed and
        `tick()` already reads its result into `self.golf_move` every
        poll (see tick(), and `golf_pulse.note()` right next to it, which
        is the existing quiet flash this does NOT replace).

        Judgment call, made deliberately: `golfer_move()` can return
        EAGLE, BIRDIE, LEAD, LOST LEAD, or BOGEY. Only EAGLE/BIRDIE/LEAD
        fire the big celebration. BOGEY and LOST LEAD are negative or
        routine outcomes for the pinned player -- bursting a full-panel
        celebratory graphic over a bogey or a lost lead would be tonally
        backwards (there is nothing to celebrate), and the existing
        `Pulse` flash already surfaces them appropriately without
        implying "great news". This mirrors the project's existing
        "no badge for the mundane/negative case" rule (see flight phase
        CRUISE, or routine golf holes not flashing at all).

        One-shot firing: `self.golf_move` is read from the feed and
        stays the same value for up to GOLF_MOVE_TTL (20s in sports.py),
        which is many ticks at ambient's fast tick rate -- so this must
        NOT fire every tick the feed keeps reporting the same move, only
        once when it first appears. Uses the identical idiom
        `golf_pulse.note()` already uses one line above in tick()
        (compare against the last-seen value, act only on a real change)
        but with its OWN tracking variable rather than reusing
        `golf_pulse` -- that Pulse is consumed for the quiet flash's
        timing (`.on`/`.t`) and re-keying it here for a second purpose
        would make the two features silently interfere with each other's
        flash timing.
        """
        move = self.golf_move
        if move == self._last_golf_big_moment:
            return
        self._last_golf_big_moment = move
        if move not in ("EAGLE", "BIRDIE", "LEAD"):
            return
        c = self.golf_pinned or {}
        name = c.get("abbr") or c.get("full") or "GOLFER"
        par = c.get("score") or "-"
        # Neutral warm gold -- golf has no team color to draw on, same
        # choice draw_celebration()'s own docstring calls out as the
        # fallback for a sport without one (golf, MMA).
        color = (255, 200, 40)
        self._set_big_moment(move, name, f"{move} {par}", color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="golf")

    BIG_MOMENT_DETECTORS["golf_move"] = _detect_golf_big_moment

    def _detect_big_moments(self):
        for fn in self.BIG_MOMENT_DETECTORS.values():
            try:
                fn(self)
            except Exception:                    # noqa: BLE001 - never break sports mode
                pass


    def _detect_mlb_home_run(self):
        """MLB home-run detector -- first real plug-in for BIG_MOMENT_DETECTORS.

        Scoped EXACTLY like sports._fetch_win_prob(): only the pinned
        favorite's own game, and only while it is genuinely `state == "in"`
        -- never the whole universal feed. That is the same narrow scope
        CLAUDE.md's ESPN request-volume section requires for any new
        per-game polling; see sports._fetch_home_run_plays()'s own
        docstring for the payload facts (scoringPlay + alternativeType.text
        == "Home Run", confirmed live).

        Seen-play tracking lives on THIS instance (`_seen_home_runs`), same
        one-shot idiom as GameDayEngine's `_seen_done` for MMA finishes:
        the first read adopts the current set without firing (a game
        already in progress when the mode is opened must not replay every
        earlier home run), and only IDs not in that set are new.
        """
        favorite = self.data.get("favorite")
        fg = self.data.get("favorite_game")
        if not favorite or favorite.get("league") != "MLB":
            return
        if not fg or fg.get("state") != "in":
            return
        event_id = fg.get("event_id")
        if not event_id:
            return
        if not self._detector_due("mlb_hr"):
            return
        hrs = sports._fetch_home_run_plays("MLB", event_id)
        ids = {h["id"] for h in hrs}
        if self._seen_home_runs is None:
            self._seen_home_runs = ids           # first read: adopt, don't replay
            return
        new_ids = ids - self._seen_home_runs
        self._seen_home_runs = ids
        if not new_ids:
            return
        # Only the most recent new one matters -- _set_big_moment is a
        # one-slot queue anyway, so firing on more than one would just
        # overwrite itself.
        newest = max((h for h in hrs if h["id"] in new_ids), key=lambda h: h["id"])
        home, away = fg["home"], fg["away"]
        line1 = f"{away['abbr']} {away['score']}, {home['abbr']} {home['score']}"
        color = home.get("color") or away.get("color") or (255, 200, 40)
        self._set_big_moment("HOME RUN", line1, newest["text"], color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="baseball")

    BIG_MOMENT_DETECTORS["mlb_hr"] = _detect_mlb_home_run

    def _detect_nfl_touchdown(self):
        """NFL/NCAAF touchdown detector -- same shape as
        _detect_mlb_home_run(), different sport. Name kept as
        `_detect_nfl_touchdown`/`BIG_MOMENT_DETECTORS["nfl_touchdown"]`
        for stability (grepped for other references before widening the
        scope below -- this method and its registered key are the only
        places "nfl_touchdown" appears in code; CLAUDE.md's own writeup
        was updated to match), even though it now also covers NCAAF.

        Scoped EXACTLY like sports._fetch_win_prob(): only the pinned
        favorite's own game, and only while it is genuinely `state ==
        "in"` -- never the whole universal feed. See
        sports._fetch_touchdown_plays()'s own docstring for the payload
        facts (`scoringPlays` array, `type.text` contains "Touchdown",
        confirmed live against event 401873271, Panthers @ Cardinals, NFL,
        and event 401769072, Alabama @ Indiana, NCAAF -- same shape both
        leagues, checked live, not assumed).

        Seen-play tracking lives on THIS instance (`_seen_nfl_touchdowns`),
        same one-shot idiom as `_seen_home_runs`: the first read adopts
        the current set without firing (a game already in progress when
        the mode is opened must not replay every earlier touchdown), and
        only IDs not in that set are new.
        """
        favorite = self.data.get("favorite")
        fg = self.data.get("favorite_game")
        league = favorite.get("league") if favorite else None
        if not favorite or league not in ("NFL", "NCAAF"):
            return
        if not fg or fg.get("state") != "in":
            return
        event_id = fg.get("event_id")
        if not event_id:
            return
        if not self._detector_due("nfl_touchdown"):
            return
        tds = sports._fetch_touchdown_plays(league, event_id)
        ids = {t["id"] for t in tds}
        if self._seen_nfl_touchdowns is None:
            self._seen_nfl_touchdowns = ids       # first read: adopt, don't replay
            return
        new_ids = ids - self._seen_nfl_touchdowns
        self._seen_nfl_touchdowns = ids
        if not new_ids:
            return
        # Only the most recent new one matters -- _set_big_moment is a
        # one-slot queue anyway, so firing on more than one would just
        # overwrite itself.
        newest = max((t for t in tds if t["id"] in new_ids), key=lambda t: t["id"])
        home, away = fg["home"], fg["away"]
        line1 = f"{away['abbr']} {away['score']}, {home['abbr']} {home['score']}"
        color = home.get("color") or away.get("color") or (255, 100, 40)
        self._set_big_moment("TOUCHDOWN", line1, newest["text"], color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="football")

    BIG_MOMENT_DETECTORS["nfl_touchdown"] = _detect_nfl_touchdown

    def _detect_nhl_goal(self):
        """NHL goal detector -- same shape as _detect_mlb_home_run() /
        _detect_nfl_touchdown(), different sport.

        Scoped EXACTLY like sports._fetch_win_prob(): only the pinned
        favorite's own game, and only while it is genuinely `state ==
        "in"` -- never the whole universal feed.

        UNVERIFIED against real live data -- see
        sports._fetch_goal_plays()'s own docstring: no NHL game was
        `state == "in"` this session (every event in today's scoreboard
        was `pre`), so the "Goal" `type.text` match this relies on has
        not been confirmed against a real payload. Built correctly on the
        same `scoringPlays` shape confirmed live for NFL/MLB, shipped
        honestly unverified rather than blocked -- same precedent as
        `_detect_mma_finish()`.

        Seen-play tracking lives on THIS instance (`_seen_nhl_goals`),
        same one-shot idiom as `_seen_home_runs` / `_seen_nfl_touchdowns`.
        """
        favorite = self.data.get("favorite")
        fg = self.data.get("favorite_game")
        if not favorite or favorite.get("league") != "NHL":
            return
        if not fg or fg.get("state") != "in":
            return
        event_id = fg.get("event_id")
        if not event_id:
            return
        if not self._detector_due("nhl_goal"):
            return
        goals = sports._fetch_goal_plays("NHL", event_id)
        ids = {g["id"] for g in goals}
        if self._seen_nhl_goals is None:
            self._seen_nhl_goals = ids            # first read: adopt, don't replay
            return
        new_ids = ids - self._seen_nhl_goals
        self._seen_nhl_goals = ids
        if not new_ids:
            return
        # Only the most recent new one matters -- _set_big_moment is a
        # one-slot queue anyway, so firing on more than one would just
        # overwrite itself.
        newest = max((g for g in goals if g["id"] in new_ids), key=lambda g: g["id"])
        home, away = fg["home"], fg["away"]
        line1 = f"{away['abbr']} {away['score']}, {home['abbr']} {home['score']}"
        color = home.get("color") or away.get("color") or (40, 160, 255)
        self._set_big_moment("GOAL", line1, newest["text"], color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="hockey")

    BIG_MOMENT_DETECTORS["nhl_goal"] = _detect_nhl_goal

    def _detect_basketball_clutch_shot(self):
        """Basketball "clutch shot" detector for NBA/NCAAB -- same shape
        as the other big-moment detectors, different signal.

        Scoped EXACTLY like sports._fetch_win_prob(): only the pinned
        favorite's own game, and only while it is genuinely `state ==
        "in"` -- never the whole universal feed.

        "Any scoring play" is deliberately NOT the trigger here, unlike
        home runs/touchdowns/goals -- basketball scores constantly (93
        real scoring plays in one real game checked this session), so a
        celebration on every made basket would be noise, violating this
        project's own repeated "don't flash on the mundane case" rule
        (golf's EAGLE/BIRDIE/LEAD-only firing, soccer's real-goal-only
        firing). See sports._fetch_clutch_plays()'s own docstring for the
        full real-schema facts and the exact clutch definition: final
        period-or-later (league-specific convention, NBA quarters vs
        NCAAB halves), inside CLUTCH_WINDOW_SECONDS of real game clock,
        and a real tie-or-lead-change by comparing this play's score
        margin against the previous scoring play's margin.

        Seen-play tracking lives on THIS instance
        (`_seen_basketball_clutch`), same one-shot idiom as
        `_seen_home_runs`/`_seen_nfl_touchdowns`/`_seen_nhl_goals`: the
        first read adopts the current set without firing, and only IDs
        not already in that set are new.

        No live NBA/NCAAB game existed to verify the live-fire path
        against this session (both leagues showed only `pre` games) --
        the fetch/detection LOGIC was verified against a real live/
        finished WNBA game's real play-by-play as a schema/logic
        reference only (WNBA stays out of scope here, matching the
        `_fetch_clutch_plays()` league gate), same "ship correct but
        honestly unverified live-fire" precedent as `_detect_nhl_goal()`/
        `_detect_mma_finish()`.
        """
        favorite = self.data.get("favorite")
        fg = self.data.get("favorite_game")
        league = favorite.get("league") if favorite else None
        if not favorite or league not in ("NBA", "NCAAB"):
            return
        if not fg or fg.get("state") != "in":
            return
        event_id = fg.get("event_id")
        if not event_id:
            return
        if not self._detector_due("basketball_clutch"):
            return
        clutch = sports._fetch_clutch_plays(league, event_id)
        ids = {c["id"] for c in clutch}
        if self._seen_basketball_clutch is None:
            self._seen_basketball_clutch = ids     # first read: adopt, don't replay
            return
        new_ids = ids - self._seen_basketball_clutch
        self._seen_basketball_clutch = ids
        if not new_ids:
            return
        # Only the most recent new one matters -- _set_big_moment is a
        # one-slot queue anyway, so firing on more than one would just
        # overwrite itself.
        newest = max((c for c in clutch if c["id"] in new_ids), key=lambda c: c["id"])
        home, away = fg["home"], fg["away"]
        line1 = f"{away['abbr']} {away['score']}, {home['abbr']} {home['score']}"
        # Neutral fallback distinct from every other sport's fallback
        # already in use (NFL orange (255,100,40), NHL blue (40,160,255),
        # MLB gold (255,200,40)): a basketball is itself orange, which
        # would collide with NFL's fallback, so this deliberately picks a
        # cool violet/purple instead -- an arena-lights color association
        # (common NBA-team branding hue) that reads as basketball-
        # appropriate while staying visually distinct from every other
        # sport's fallback already in use here.
        color = home.get("color") or away.get("color") or (160, 60, 220)
        self._set_big_moment("CLUTCH", line1, newest["text"], color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="basketball")

    BIG_MOMENT_DETECTORS["basketball_clutch"] = _detect_basketball_clutch_shot

    def _detect_mma_finish(self):
        """MMA finish detector for the SHARED big-moment celebration.

        Do not confuse this with GameDayEngine's existing `_finish_round`/
        `_seen_done` mechanism -- that is a different, older, already-working
        system that drives GAME DAY's dedicated UFC-card RESULT takeover
        from `mma.FEED` (the dedicated card feed) and is untouched here.
        This detector instead fires the shared `draw_celebration()` graphic
        while `ambient` is showing, off whatever MMA/PFL event
        `sports.FEED.get_universal()` happens to surface -- a completely
        separate feed pathway from `mma.FEED`, confirmed non-interchangeable
        in an earlier session.

        One-shot per event id, same idiom as GameDayEngine._seen_done: the
        first read adopts whatever is already state=="post" without firing
        (an event already over before ambient started watching must not
        replay), and only a NEWLY post id fires.

        UNVERIFIED END-TO-END: as of this build, sports.FEED.get_universal()
        has ZERO mma/PFL events (checked live), and mma.FEED (GAME DAY's own
        card feed) also has no next card -- a real, current data gap, not a
        reason to fabricate test data (see CLAUDE.md's "never invent" rule).
        This is built from the already-verified type-ID facts in mma.py
        (20=submission, 21=KO/TKO, 22=decision -- see mma.METHOD_BY_ID) and
        wired the same way as the other detectors, but has never fired
        against a real finish. See sports._fetch_mma_finish_method()'s own
        docstring for the two specific open unknowns (whether a per-event
        summary endpoint even exists for a universal-feed MMA event id, and
        whether the guessed league slug is right) -- both are genuine
        blockers, not gaps papered over by guessing. If the fetch fails or
        returns nothing, `kind` falls back to "RESULT" rather than
        inventing a method, and the moment still fires (a fight ending is
        real and worth celebrating even if the HOW is unknown), matching
        this project's every-other-feed discipline of degrading one field
        at a time instead of hiding the whole event.
        """
        done_ids = {e["id"] for e in self.universal
                    if e.get("sport") == "mma" and e.get("state") == "post"}
        if self._seen_mma_done is None:
            self._seen_mma_done = done_ids       # first read: adopt, don't replay
            return
        new_ids = done_ids - self._seen_mma_done
        self._seen_mma_done = done_ids
        if not new_ids:
            return
        ev = next((e for e in self.universal if e["id"] in new_ids), None)
        if not ev:
            return
        method = sports._fetch_mma_finish_method(ev.get("league") or "", ev.get("id"))
        winner = next((c for c in (ev.get("competitors") or []) if c.get("winner")), None)
        name = (winner or {}).get("full") or (winner or {}).get("abbr") or "WINNER"
        kind = method or "RESULT"
        line2 = ev.get("class_label") or ""
        color = (winner or {}).get("color") or (255, 200, 40)
        self._set_big_moment(kind, name, line2, color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="mma")

    BIG_MOMENT_DETECTORS["mma_finish"] = _detect_mma_finish

    def _frame_event_detail(self, ev):
        fn = self.SPORT_DETAIL_RENDERERS.get(ev.get("sport"))
        if fn:
            buf = blank(); fill(buf, self.BG)
            fn(self, buf, ev)
            return bytes(buf)
        return self._frame_event_detail_generic(ev)

    def _frame_event_detail_generic(self, ev):
        """EXPANDED single event -- the same visual language as GAME DAY
        (draw_event_frame), because it is the same idea: one event given
        the whole panel, rather than a row in a list. Still the fallback
        for any sport without its own expanded renderer.

        Shows everything the sport genuinely provides and silently omits
        what it doesn't -- these payloads are NOT uniform (see sports.py).
        """
        buf = blank(); fill(buf, self.BG)
        accent = self._sport_accent(ev)
        intensity = 1.0 if ev["live"] else 0.35
        draw_event_frame(buf, intensity, accent, accent)

        tag = self._state_tag(ev)
        head = f"{ev['league_name'] or ev['league']}"
        if tag:
            head = f"{head}  {tag}"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           self.LIVE if ev["live"] else color_on_dark(accent), x_min=3)

        comps = ev["competitors"]
        if ev["leaderboard"]:
            for i, c in enumerate(comps[:5]):
                y = 15 + i * 8
                draw_text3x5(buf, 4, y, str(c["place"] or i + 1), self.INK_DIM)
                draw_text3x5(buf, 13, y, fit_text(c["abbr"], 30), self.HERO_INK)
                sc = c["score"] or "-"
                draw_text3x5(buf, WIDTH - 4 - text_w(sc), y, sc,
                             self.WIN if str(sc).startswith("-") else self.INK)
            lead = comps[0] if comps else {}
            foot = f"THRU {lead['thru']}" if lead.get("thru") else (ev["detail"] or "")
            draw_text_centered(buf, 56, fit_text(foot, WIDTH - 8), self.INK_DIM, x_min=3)
            return bytes(buf)

        # Laid out with a CURSOR rather than fixed offsets: what each sport
        # provides varies (tennis has long string scores, MMA has records,
        # some have neither), and fixed rows collided the moment content
        # changed -- the second competitor's record landed on the venue
        # line. Advancing a cursor makes overlap impossible by construction.
        y = 13
        for c in comps[:2]:
            bar = c.get("color") or self.INK_DIM
            sc = c["score"]
            sc_txt = str(sc) if sc is not None else ""
            col = self.WIN if c["winner"] else self.HERO_INK
            wide = bool(sc_txt) and text_w(sc_txt, 2) > 22
            block_top = y
            if wide:
                # Tennis: name, then the full set score beneath at scale 1.
                # Never truncated -- a clipped set score is wrong, not small.
                draw_text3x5(buf, 7, y, fit_text(c["abbr"], WIDTH - 14), col)
                y += 6
                draw_text3x5(buf, 7, y, fit_text(sc_txt, WIDTH - 11), self.INK)
                y += 6
            else:
                avail = WIDTH - 14 - (text_w(sc_txt, 2) if sc_txt else 0)
                draw_text3x5(buf, 7, y, fit_text(c["abbr"], avail, 2), col, scale=2)
                if sc_txt:
                    draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)
                y += 11
            sub = c.get("record") or (f"SEED {c['seed']}" if c.get("seed") else "")
            if sub:
                draw_text3x5(buf, 7, y, fit_text(sub, WIDTH - 14), self.INK_DIM)
                y += 6
            # Colour bar spans exactly the rows this competitor occupies.
            for by in range(block_top, min(y, HEIGHT - 3)):
                for bx in (3, 4):
                    put_px(buf, bx, by, bar)
            y += 3

        # Whatever is left goes below, in priority order, only while rows
        # remain. MLB baserunners come from the EVENT level on this endpoint.
        show_bases = bool(ev.get("bases")) or ev.get("outs") is not None
        if show_bases and y <= 44:
            draw_diamond(buf, WIDTH - 22, y, ev.get("bases"))
            draw_outs(buf, WIDTH - 13, y + 4, ev.get("outs"))
        extra = ev.get("class_label") or ev.get("series") or ev.get("venue") or ""
        if extra and y <= 46:
            draw_text3x5(buf, 4, y, fit_text(extra, (WIDTH - 28) if show_bases else (WIDTH - 8)),
                         self.INK_DIM)
            y += 7

        line = ev["detail"] or ""
        if ev.get("clock"):
            line = f"{line} {ev['clock']}".strip()
        if line:
            draw_text_centered(buf, min(max(y, 46), 56), fit_text(line, WIDTH - 8),
                               self.LIVE if ev["live"] else self.INK_DIM, x_min=3)
        return bytes(buf)

    def _render_baseball_detail(self, buf, ev):
        """Baseball's EXPANDED view. The main row already shows inning /
        diamond / outs / count -- the same size, just smaller. What this
        view adds is room: both teams' full records, venue, series
        status, and the SAME live-state glyphs drawn bigger and with
        actual breathing space, rather than a wholly different set of
        facts. Selecting a game you're already watching should feel like
        zooming in, not switching to a different display.
        """
        accent = self._sport_accent(ev)
        draw_event_frame(buf, 1.0 if ev["live"] else 0.35, accent, accent)

        tag = self._state_tag(ev)
        head = ev["league_name"] or "MLB"
        if tag:
            head = f"{head}  {tag}"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           self.LIVE if ev["live"] else color_on_dark(accent), x_min=3)

        # Real budget, not guessed: two team blocks (17px each: 10px
        # name/score + 1 gap + 5px record + 1 gap) leave room for the
        # live-state row PLUS at least one footer line. The first version
        # of this advanced the cursor by more than the live-state row
        # actually draws (+12 when the real ink only reaches +6-7), which
        # pushed y past the footer guard on EVERY live game -- series,
        # venue and broadcast never appeared, not because anything
        # overflowed, but because the cursor overshot its own content.
        # render_audit's put_px instrumentation (added specifically to
        # catch this CLASS of bug) found no clipped pixels here, which is
        # what proved it was a cursor-accounting bug, not a real overflow.
        comps = ev["competitors"]
        y = 11
        for c in comps[:2]:
            bar = c.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (3, 4):
                    put_px(buf, bx, y + by, bar)
            sc = c.get("score")
            sc_txt = "" if sc is None else str(sc)
            col = self.WIN if c.get("winner") else self.HERO_INK
            avail = WIDTH - 14 - (text_w(sc_txt, 2) if sc_txt else 0)
            draw_text3x5(buf, 8, y, fit_text(c.get("abbr") or "", avail, 2), col, scale=2)
            if sc_txt:
                draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)
            y += 11
            rec = c.get("record")
            if rec:
                draw_text3x5(buf, 8, y, rec, self.INK_DIM)
            y += 6

        y += 1
        if ev["live"]:
            detail = ev.get("detail") or ""
            top = detail.startswith("TOP")
            inning = ev.get("period")
            x = 6
            if inning:
                draw_trend_arrow(buf, x, y + 2, top, self.LIVE)
                x += 5
                draw_text3x5(buf, x, y + 2, str(inning), self.LIVE)
                x += text_w(str(inning)) + 6
            draw_diamond(buf, x, y, ev.get("bases"))
            x += 12
            outs = ev.get("outs")
            if outs is not None:
                draw_outs(buf, x, y + 5, outs)
            sit = ev.get("situation") or {}
            b, k = sit.get("balls"), sit.get("strikes")
            if isinstance(b, int) and isinstance(k, int):
                cnt = f"{b}-{k}"
                draw_text3x5(buf, WIDTH - 4 - text_w(cnt), y + 2, cnt, self.INK)
            y += 7          # diamond's real extent is 5px + outs' 1px + a gap
        else:
            draw_text_centered(buf, y, fit_text(ev.get("detail") or "", WIDTH - 8), self.INK_DIM)
            y += 8

        # HEIGHT-5, not HEIGHT-6: a scale=1 glyph is 5px tall, so the true
        # last valid start row is 59 (59+5=64). HEIGHT-6=58 rejected a
        # perfectly legal y=59 -- caught by the same accounting review
        # that found the cursor overshoot above.
        foot_lines = [x for x in (ev.get("series"), ev.get("venue"), ev.get("broadcast")) if x]
        for line in foot_lines:
            if y > HEIGHT - 5:
                break
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

    SPORT_DETAIL_RENDERERS["baseball"] = _render_baseball_detail

    def _render_soccer_detail(self, buf, ev):
        """Soccer's EXPANDED view. The main row already shows form and the
        real ESPN-formatted clock -- the incremental value here is the
        full RECORD (with points, which the compact row has no room for:
        "7-5-5, 26 PTS" is 14 characters), plus venue/broadcast/series and
        the shootout treatment at full size. Same y-cursor discipline as
        baseball's detail view, after that one proved fixed offsets don't
        survive real content here either.
        """
        accent = self._sport_accent(ev)
        draw_event_frame(buf, 1.0 if ev["live"] else 0.35, accent, accent)

        tag = self._state_tag(ev)
        head = ev["league_name"] or "SOCCER"
        if tag:
            head = f"{head}  {tag}"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           self.LIVE if ev["live"] else color_on_dark(accent), x_min=3)

        comps = ev["competitors"]
        y = 11
        for c in comps[:2]:
            bar = c.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (3, 4):
                    put_px(buf, bx, y + by, bar)
            sc = c.get("score")
            sc_txt = "" if sc is None else str(sc)
            col = self.WIN if c.get("winner") else self.HERO_INK
            avail = WIDTH - 14 - (text_w(sc_txt, 2) if sc_txt else 0)
            draw_text3x5(buf, 8, y, fit_text(c.get("abbr") or "", avail, 2), col, scale=2)
            if sc_txt:
                draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)
            y += 11
            rec = c.get("record")
            if rec:
                draw_text3x5(buf, 8, y, fit_text(rec, WIDTH - 12), self.INK_DIM)
            y += 6

        y += 1
        shootouts = [c.get("shootout") for c in comps[:2]]
        if all(s is not None for s in shootouts):
            draw_text_centered(buf, y, "PENALTIES", self.LIVE)
            y += 7
            pk = "-".join(str(s) for s in shootouts)
            draw_text_centered(buf, y, pk, self.HERO_INK, scale=2)
            y += 11
        else:
            clock = ev.get("clock") or ev.get("detail") or ""
            if clock:
                draw_text_centered(buf, y, fit_text(clock, WIDTH - 8),
                                   self.LIVE if ev["live"] else self.INK_DIM)
                y += 7

        foot_lines = [x for x in (ev.get("series"), ev.get("venue"),
                                  ev.get("broadcast"), ev.get("note")) if x]
        for line in foot_lines:
            if y > HEIGHT - 5:
                break
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

    SPORT_DETAIL_RENDERERS["soccer"] = _render_soccer_detail

    def _detect_soccer_goal(self):
        """GOAL big-moment for the pinned favorite's own LIVE soccer
        game -- see BIG_MOMENT_DETECTORS' docstring above for the shared
        contract this plugs into.

        Cost discipline: reuses the exact same narrow scope
        `sports._fetch_win_prob()` already uses (the pinned favorite's
        own game, only while it is genuinely live) -- this never polls
        `keyEvents` for any other soccer match in the universal feed,
        which is the ESPN request-volume risk this project has already
        had to mitigate twice (see CLAUDE.md).
        """
        fav = self.data.get("favorite")
        fg = self.data.get("favorite_game")
        if not fav or not fg or fav["league"] not in sports.SOCCER_LEAGUES \
                or fg["state"] != "in":
            # Not watching a live soccer game right now -- drop any
            # in-progress seen-set so returning to a live game later (or
            # a different one) starts clean rather than stale.
            self._soccer_goal_event_id = None
            self._soccer_goal_seen = set()
            return

        if fg["event_id"] != self._soccer_goal_event_id:
            # The game being watched changed (a new pinned-favorite game
            # went live, or the old one finished and a fresh one
            # started). fetch_new_soccer_goals() itself does NOT adopt a
            # baseline -- its own docstring says the CALLER must hand in
            # a fresh empty set and is responsible for not replaying
            # history. A plain `self._soccer_goal_seen = set()` here
            # does the "fresh set" half but not the "don't replay" half:
            # the very next call below would report every goal already
            # scored before this game was opened as brand new (a real,
            # confirmed misfire -- ambient landing on a favorite game
            # already 2-0 would fire two GOAL celebrations for goals
            # that happened minutes ago). Adopt the baseline first, same
            # "don't replay" rule as GameDayEngine._seen_done and
            # _detect_mlb_home_run's _seen_home_runs: call once to seed
            # `seen` with whatever already scored, discard that result,
            # and only report what's genuinely new after that.
            self._soccer_goal_event_id = fg["event_id"]
            self._soccer_goal_seen = set()
            sports.fetch_new_soccer_goals(fav["league"], fg["event_id"],
                                           self._soccer_goal_seen)
            self._detector_last_poll["soccer_goal"] = time.time()
            return

        if not self._detector_due("soccer_goal"):
            return

        goals = sports.fetch_new_soccer_goals(fav["league"], fg["event_id"],
                                               self._soccer_goal_seen)
        if not goals:
            return
        newest = goals[-1]

        home, away = fg["home"], fg["away"]
        fav_team = home if home.get("abbr") == fav["team_abbr"] else away
        color = fav_team.get("color") or (255, 200, 40)   # real team color, else neutral gold

        line1 = f"{away.get('abbr') or ''} {away.get('score')} - {home.get('score')} {home.get('abbr') or ''}"
        self._set_big_moment("GOAL", line1, newest["text"] or newest["type"], color,
                             tier=TIER_INTERRUPT, system=SYSTEM_SPORTS, sport="soccer")

    BIG_MOMENT_DETECTORS["soccer_goal"] = _detect_soccer_goal

    def _render_golf_detail(self, buf, ev):
        """Golf's EXPANDED view. The main ticker row already fits a solid
        6-row leaderboard with movement arrows -- more rows here is not
        the incremental value (a 64px panel cannot show meaningfully more
        of a 25-deep field either way). What it genuinely lacks room for:
        the full tournament NAME (the compact row only gets a short_name
        that can truncate), round status, venue and broadcast. Same
        "context the main view has no room for" pattern as baseball and
        soccer's detail views, not "more of the same list".
        """
        accent = self._sport_accent(ev)
        draw_event_frame(buf, 0.6 if ev["live"] else 0.3, accent, accent)

        name = ev.get("name") or ev.get("league_name") or "GOLF"
        scale = 2 if text_w(name, 2) <= WIDTH - 8 else 1
        draw_text_centered(buf, 6, fit_text(name, WIDTH - 8, scale),
                           color_on_dark(accent), scale=scale, x_min=3)
        y = 6 + 5 * scale + 2
        detail = ev.get("detail") or ""
        if detail:
            draw_text_centered(buf, y, fit_text(detail, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

        comps = ev["competitors"]
        rows = min(6, max(1, (HEIGHT - 5 - y - 8) // 6))
        for i, c in enumerate(comps[:rows]):
            pos_txt = str(c.get("place") or i + 1)
            draw_text3x5(buf, 2, y, fit_text(pos_txt, 12), self.INK_DIM)
            self._draw_movement(buf, 14, y, c.get("movement"), self.WIN, self.LOSE)
            sc = c.get("score") or "-"
            name_w = (WIDTH - 2 - text_w(sc)) - 19 - 2
            draw_text3x5(buf, 19, y, fit_person(c.get("abbr"), name_w), self.HERO_INK)
            draw_text3x5(buf, WIDTH - 2 - text_w(sc), y,
                         sc, self.WIN if str(sc).startswith("-") else self.INK)
            y += 6

        foot_lines = [x for x in (ev.get("venue"), ev.get("broadcast")) if x]
        for line in foot_lines:
            if y > HEIGHT - 5:
                break
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

    SPORT_DETAIL_RENDERERS["golf"] = _render_golf_detail

    def _render_tennis_detail(self, buf, ev):
        """Tennis' EXPANDED view. The main row already fits the set-by-set
        score on its own line -- the incremental value here is room for
        FULL names instead of "N. MEJIA" (fit_person on the main row still
        prefers the surname, but the compact row has no space for the
        given name at all), the real tournament name, the DRAW, real
        venue/court, and a finished match's real `notes[]` summary line
        ("NICOLAS MEJIA (COL) BT MARCO TRUNGELLITI (ARG) 7-6 (7-5) 3-6
        6-1" -- already panel_text()-folded in sports.py). Same y-cursor
        discipline as every other detail renderer: what a given match
        actually has varies (no notes pre-match, no venue for some
        events), and a fixed offset would collide the moment content
        changed, same lesson baseball/soccer/golf's own detail views
        already had to learn on this exact seam.
        """
        accent = self._sport_accent(ev)
        draw_event_frame(buf, 1.0 if ev["live"] else 0.35, accent, accent)

        tag = self._state_tag(ev)
        head = ev.get("league_name") or "TENNIS"
        if tag:
            head = f"{head}  {tag}"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           self.LIVE if ev["live"] else color_on_dark(accent), x_min=3)

        comps = ev["competitors"][:2]
        y = 13
        for c in comps:
            col = self.WIN if c.get("winner") else self.HERO_INK
            draw_text3x5(buf, 4, y, fit_person(c.get("full") or c.get("abbr") or "", WIDTH - 8), col)
            y += 7
        y += 1

        line = self._tennis_set_line(comps)
        if line:
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.INK, x_min=3)
            y += 7
        elif ev["state"] == "pre":
            draw_text_centered(buf, y, "NOT STARTED", self.INK_DIM, x_min=3)
            y += 7

        foot_lines = [x for x in (ev.get("class_label"), ev.get("name"), ev.get("venue")) if x]
        for fline in foot_lines:
            if y > HEIGHT - 5:
                break
            draw_text_centered(buf, y, fit_text(fline, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

        note = ev.get("note") or ""
        if note and y <= HEIGHT - 5:
            draw_text_centered(buf, y, fit_text(note, WIDTH - 8), self.INK_DIM, x_min=3)

    SPORT_DETAIL_RENDERERS["tennis"] = _render_tennis_detail

    def _render_football_detail(self, buf, ev):
        """Football's EXPANDED view. Same y-cursor discipline as every
        other detail renderer here, deliberately -- baseball's detail
        view had a real, verified cursor-overshoot bug on this exact
        seam (the footer never rendered because the cursor advanced more
        than the live-state row actually drew); a fixed offset here would
        repeat it the moment down-and-distance is present vs. absent.

        Adds over the main row: full records (not truncated to fit
        beside the score), venue, broadcast, and series/note when
        present -- the same "context the main view has no room for"
        pattern golf/soccer/baseball's own detail renderers established.
        """
        accent = self._sport_accent(ev)
        draw_event_frame(buf, 1.0 if ev["live"] else 0.35, accent, accent)

        tag = self._state_tag(ev)
        head = ev.get("league_name") or "FOOTBALL"
        if tag:
            head = f"{head}  {tag}"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           self.LIVE if ev["live"] else color_on_dark(accent), x_min=3)

        comps = ev["competitors"]
        y = 11
        for c in comps[:2]:
            bar = c.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (3, 4):
                    put_px(buf, bx, y + by, bar)
            sc = c.get("score")
            sc_txt = "" if sc is None else str(sc)
            col = self.WIN if c.get("winner") else self.HERO_INK
            avail = WIDTH - 14 - (text_w(sc_txt, 2) if sc_txt else 0)
            draw_text3x5(buf, 8, y, fit_text(c.get("abbr") or "", avail, 2), col, scale=2)
            if sc_txt:
                draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)
            y += 11
            rec = c.get("record")
            if rec:
                draw_text3x5(buf, 8, y, fit_text(rec, WIDTH - 12), self.INK_DIM)
            y += 6

        y += 1
        if ev["live"]:
            period, clock = ev.get("period"), ev.get("clock") or ""
            line = f"Q{period} {clock}".strip() if period else clock
            if line:
                draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.LIVE, x_min=3)
                y += 7
            dd = situation_line(ev)
            if dd:
                draw_text_centered(buf, y, fit_text(dd, WIDTH - 8), self.INK, x_min=3)
                y += 7
        else:
            draw_text_centered(buf, y, fit_text(ev.get("detail") or "", WIDTH - 8),
                               self.INK_DIM, x_min=3)
            y += 7

        foot_lines = [x for x in (ev.get("series"), ev.get("venue"),
                                  ev.get("broadcast"), ev.get("note")) if x]
        for line in foot_lines:
            if y > HEIGHT - 5:
                break
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

    SPORT_DETAIL_RENDERERS["football"] = _render_football_detail

    def _render_basketball_detail(self, buf, ev):
        """Basketball's EXPANDED view. Same shape as football's detail
        renderer minus down-and-distance -- full records, venue,
        broadcast, series/note. No bonus/foul/timeout display, same
        honest gap as the main renderer (see `_render_basketball()`'s
        docstring): no confirmed ESPN field name for any of those, and
        guessing one is exactly what this project's "never invent" rule
        forbids.
        """
        accent = self._sport_accent(ev)
        draw_event_frame(buf, 1.0 if ev["live"] else 0.35, accent, accent)

        tag = self._state_tag(ev)
        head = ev.get("league_name") or "BASKETBALL"
        if tag:
            head = f"{head}  {tag}"
        draw_text_centered(buf, 6, fit_text(head, WIDTH - 8),
                           self.LIVE if ev["live"] else color_on_dark(accent), x_min=3)

        comps = ev["competitors"]
        y = 11
        for c in comps[:2]:
            bar = c.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (3, 4):
                    put_px(buf, bx, y + by, bar)
            sc = c.get("score")
            sc_txt = "" if sc is None else str(sc)
            col = self.WIN if c.get("winner") else self.HERO_INK
            avail = WIDTH - 14 - (text_w(sc_txt, 2) if sc_txt else 0)
            draw_text3x5(buf, 8, y, fit_text(c.get("abbr") or "", avail, 2), col, scale=2)
            if sc_txt:
                draw_text3x5(buf, WIDTH - 4 - text_w(sc_txt, 2), y, sc_txt, col, scale=2)
            y += 11
            rec = c.get("record")
            if rec:
                draw_text3x5(buf, 8, y, fit_text(rec, WIDTH - 12), self.INK_DIM)
            y += 6

        y += 1
        if ev["live"]:
            period, clock = ev.get("period"), ev.get("clock") or ""
            line = f"Q{period} {clock}".strip() if period else clock
            if line:
                draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.LIVE, x_min=3)
                y += 7
        else:
            draw_text_centered(buf, y, fit_text(ev.get("detail") or "", WIDTH - 8),
                               self.INK_DIM, x_min=3)
            y += 7

        foot_lines = [x for x in (ev.get("series"), ev.get("venue"),
                                  ev.get("broadcast"), ev.get("note")) if x]
        for line in foot_lines:
            if y > HEIGHT - 5:
                break
            draw_text_centered(buf, y, fit_text(line, WIDTH - 8), self.INK_DIM, x_min=3)
            y += 7

    SPORT_DETAIL_RENDERERS["basketball"] = _render_basketball_detail

    def _frame_for_view(self):
        if self.detail is not None:
            ev = self._current_event()
            if ev:
                return self._frame_event_detail(ev)
            self.detail = None
        # A notable golf move preempts whatever else was showing -- same
        # reasoning as the scoring flash: the moment is the content. This
        # is the ONE precedence override, and it is time-limited by the
        # feed's move TTL rather than being a standing priority.
        if self.golf_pinned and self.golf_move:
            return self._frame_golf_pinned()
        panel = self._panel()
        if panel == self.PANEL_TEAM:
            return self._frame_pinned()
        if panel == self.PANEL_GOLF:
            return self._frame_golf_pinned()
        if panel == self.PANEL_TENNIS:
            return self._frame_tennis_pinned()
        if panel == self.PANEL_EVENTS:
            return self._frame_universal()
        # No panel has data: fall back to whichever empty state is most
        # informative rather than a blank screen.
        if self.data.get("favorite"):
            return self._frame_pinned()
        return self._frame_ticker()

    def frame(self):
        """Render the current panel, sliding when the PANEL changes OR
        when a game is entered/left via select-to-expand.

        The panel slide fires on a panel change (pinned team -> golfer ->
        events), not on stepping between games inside the events panel --
        browsing should feel immediate, while switching what KIND of thing
        you are looking at deserves the shared transition. Entering or
        leaving `self.detail` gets the SAME treatment for the same reason
        -- opening one specific game's detail view is a real change of
        what's being looked at, not a browse step -- but with its OWN
        distinct style (`iris_open` opening, `push_down` closing) rather
        than reusing the panel slide's `push_up`, so hitting select to
        open a game reads as its own kind of motion rather than a smaller
        copy of the panel transition. Stepping from one game's detail
        view directly to another's (left/right while already expanded)
        deliberately stays instant, same "browsing stays immediate" rule.

        The outgoing frame is captured here rather than in tick() because
        input() can change the panel/detail between ticks.
        """
        in_detail = self.detail is not None
        cur = (self._panel(), self.panel_i, in_detail)
        if self._last_view is not None and cur != self._last_view:
            # Slide FROM the frame actually last shown, which is exactly
            # what was on the panel. Re-rendering the old panel here would
            # need its state restored and could re-trigger side effects.
            self._trans_from = self._prev_frame
            self._trans_i = 0
            was_in_detail = self._last_view[2]
            if in_detail and not was_in_detail:
                self._trans_style = "iris_open"
                self._trans_ticks = self.DETAIL_TRANSITION_TICKS
            elif was_in_detail and not in_detail:
                self._trans_style = "push_down"
                self._trans_ticks = self.DETAIL_TRANSITION_TICKS
            else:
                self._trans_style = transitions.DEFAULT_STYLE
                self._trans_ticks = self.TRANSITION_TICKS
        self._last_view = cur

        frame = self._frame_for_view()
        self._prev_frame = frame
        if self._trans_from and self._trans_i < self._trans_ticks:
            self._trans_i += 1
            frame = transitions.blend(
                self._trans_from, frame, self._trans_i / float(self._trans_ticks),
                self._trans_style)
            if self._trans_i >= self._trans_ticks:
                self._trans_from = None
        return frame


class NewsEngine(Browsable):
    """RSS headline ticker.

    Same discipline as every other data mode: no I/O in this class, reads
    whatever news.FEED has already cached. Headlines are typically far
    wider than 64px even at scale=1 (a real headline can run 80+
    characters), so unlike the ticker/sports modes there's no sensible
    fixed-width truncation for the "current" headline -- it scrolls, the
    same way the bottom tape of every other ticker mode already does,
    just bigger and as the primary content instead of a secondary strip.

    Auto-advances through headlines on a fixed tick cadence (not "wait for
    the scroll to finish"), matching every other mode's cycling behavior
    so the pacing is predictable regardless of headline length.
    """

    name = "news"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    HEADLINE = (255, 226, 60)
    STALE = (255, 170, 40)
    LOSE = (255, 70, 80)

    SPOTLIGHT_TICKS = 260      # ~13s per headline before auto-advancing

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"headlines": [], "label": "NEWS", "age": None, "err": None}
        self.cur = 0
        self.hold = 0
        self.cycling = True
        self.ticks = 0
        self.scroll = 0.0
        self._last_headline_key = None
        self.pulse = Pulse()
        self._init_scroll()

    # ---- input -----------------------------------------------------------
    def has_content(self):
        return bool(self.data.get("headlines"))

    def _step(self, direction):
        n = len(self.data.get("headlines") or [])
        if n:
            self.cur = (self.cur + direction) % n
            self.hold = 0

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        if cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        self.data = news.FEED.get()
        n = len(self.data.get("headlines") or [])
        if n:
            self.cur %= n
        self.scroll += 0.5
        if self.cycling and n > 1 and self.browse.auto_ok:
            self.hold += 1
            if self.hold >= self.SPOTLIGHT_TICKS:
                self.hold = 0
                self.cur = (self.cur + 1) % n
        self.score = n

        heads = self.data.get("headlines") or []
        self.pulse.note(heads[0] if heads else None)   # newest headline arriving

        key = (self.cur, n)
        if key != self._last_headline_key:
            self._last_headline_key = key
            self.scroll = 0.0     # fresh scroll from the start on every headline change

    AMBIENT_STYLE = "push_up"       # ticker feel

    def ambient_weight(self):
        return 1.0


    # ---- render --------------------------------------------------------
    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        headlines = self.data.get("headlines") or []
        label = self.data.get("label") or "NEWS"

        accent = (225, 60, 70)
        stale = bool(self.data.get("age") and self.data["age"] > 1800)

        if not headlines:
            draw_header(buf, label, accent, stale=stale)
            draw_text_centered(buf, 30, "NO HEADLINES" if self.data.get("err") else "LOADING",
                               self.LOSE if self.data.get("err") else self.INK_DIM)
            draw_text_centered(buf, 40, "." * (1 + (self.ticks // 12) % 3), self.INK_DIM)
            return bytes(buf)

        draw_header(buf, label, accent,
                    right_tag=f"{self.cur + 1}/{len(headlines)}", stale=stale)

        # Spotlight: the current headline, big and scrolling. Headlines
        # run far longer than 64px even at scale=1, so there's no sensible
        # fixed truncation -- it scrolls instead, and the smaller tape
        # below runs the full set at a different rate so the two are
        # independent reads rather than a race.
        draw_marquee(buf, 18, headlines[self.cur % len(headlines)],
                     self.pulse.mix(self.HEADLINE), self.scroll, scale=2, gap="     ")

        draw_dots(buf, 34, len(headlines), self.cur, on=accent)
        draw_divider(buf, 38)

        draw_marquee(buf, 45, "   /   ".join(headlines), self.INK_DIM,
                     self.scroll * 0.6, gap="   /   ")

        # A second, dimmer rule below the tape closes the frame so it reads
        # as a contained band rather than text running off the panel's
        # bottom edge. The source name is deliberately NOT repeated here --
        # it's already in the header, and repeating it just spent 5 rows
        # saying nothing new.
        draw_divider(buf, 53)
        return bytes(buf)


class WeatherEngine:
    """NOAA/NWS current conditions + active severe alerts.

    Same discipline as every other data mode: no I/O here, reads whatever
    weather.FEED has cached.

    An active alert PREEMPTS the normal view entirely rather than taking
    a turn in the rotation -- a tornado warning that waits 10 seconds for
    the conditions view to finish cycling is a broken product. The alert
    view also pulses, because on a wall panel across a room the motion is
    what actually catches an eye that isn't already looking.

    NWS returns Celsius and km/h despite being a US agency; converted
    here at the render layer, same as every other mode.
    """

    name = "weather"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (86, 94, 116)
    ACCENT = (90, 190, 255)
    STALE = (255, 170, 40)

    # Shared with the global takeover so the two can never drift apart --
    # see ALERT_SEVERITY_COLOR / draw_alert_frame at module level.
    SEVERITY_COLOR = ALERT_SEVERITY_COLOR

    COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

    VIEW_TICKS = 220

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"conditions": None, "alerts": [], "place": "HOME",
                    "configured": False, "age": None, "err": None}
        self.cur_alert = 0
        self.hold = 0
        self.pulse = Pulse()
        self.cycling = True
        self.ticks = 0
        self.scroll = 0.0

    # ---- input -----------------------------------------------------------
    def has_content(self):
        """Conditions OR an active alert. An alert with no observation
        still absolutely counts."""
        return bool(self.data.get("configured")) and bool(
            self.data.get("conditions") or self.data.get("alerts"))

    def input(self, cmd):
        alerts = self.data.get("alerts") or []
        if cmd == "left" and alerts:
            self.cur_alert = (self.cur_alert - 1) % len(alerts)
            self.hold = 0
        elif cmd == "right" and alerts:
            self.cur_alert = (self.cur_alert + 1) % len(alerts)
            self.hold = 0
        elif cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self.data = weather.FEED.get()
        # Flash on the temperature actually changing. Rounded to whole
        # degrees first: flashing on raw float jitter would fire constantly
        # and mean nothing.
        c = (self.data.get("conditions") or {}).get("temp_c")
        self.pulse.note(round(c_to_f(c)) if isinstance(c, (int, float)) else None)
        self.scroll += 0.5
        alerts = self.data.get("alerts") or []
        if alerts:
            self.cur_alert %= len(alerts)
            if self.cycling and len(alerts) > 1:
                self.hold += 1
                if self.hold >= self.VIEW_TICKS:
                    self.hold = 0
                    self.cur_alert = (self.cur_alert + 1) % len(alerts)
        self.score = len(alerts)

    # ---- render --------------------------------------------------------
    @classmethod
    def _compass(cls, deg):
        if deg is None:
            return ""
        return cls.COMPASS[int((deg + 22.5) % 360 // 45)]

    def _frame_alert(self, alert):
        return draw_alert_frame(alert, self.ticks, place=self.data.get("place", ""),
                                n_alerts=len(self.data.get("alerts") or []),
                                cur_alert=self.cur_alert)

    def _frame_conditions(self):
        buf = blank()
        fill(buf, self.BG)
        cond = self.data.get("conditions")
        stale = bool(self.data.get("age") and self.data["age"] > 3600)

        if not self.data.get("configured"):
            draw_header(buf, "WEATHER", self.ACCENT)
            draw_text_centered(buf, 28, "SET LOCATION", self.INK)
            draw_text_centered(buf, 38, "TO SEE WEATHER", self.INK_DIM)
            return bytes(buf)

        if not cond:
            draw_header(buf, "WEATHER", self.ACCENT, stale=stale)
            msg = "NO DATA" if self.data.get("err") else "LOADING"
            draw_text_centered(buf, 28, msg, self.INK_DIM)
            if self.data.get("err"):
                draw_text_centered(buf, 38, fit_text(str(self.data["err"]).upper(), WIDTH - 4), self.INK_DIM)
            else:
                draw_text_centered(buf, 38, "." * (1 + (self.ticks // 12) % 3), self.INK_DIM)
            return bytes(buf)

        # "Feels like" rides in the header's top-right: it's the number
        # that actually drives decisions (what to wear, whether to go
        # outside), so it must always be on screen -- but it's a
        # reference value next to the headline temperature, not a
        # competitor for it, so it stays small rather than taking space
        # from the hero. draw_header measures this tag and gives the
        # place name whatever room is left.
        temp_c = cond.get("temp_c")
        fl_c = weather.feels_like_c(cond)
        fl_tag = None
        if fl_c is not None:
            fl_tag = f"FL {c_to_f(fl_c):.0f}F"
        draw_header(buf, self.data.get("place", "WEATHER"), self.ACCENT,
                    right_tag=fl_tag, stale=stale)

        # Actual temperature is the hero -- scale=2 and the only bright
        # colour on the screen.
        if temp_c is not None:
            draw_text_centered(buf, 12, f"{c_to_f(temp_c):.0f}F",
                               self.pulse.mix((255, 235, 180)), scale=2)
        else:
            draw_text_centered(buf, 14, "--F", self.INK_DIM, scale=2)

        # Today's high/low, straight from the NWS forecast periods. This
        # is the context that makes the current number mean something --
        # 82F alone is a fact, "82F, HI 88 LO 76" tells you where in the
        # day you are.
        hi, lo = self.data.get("high_f"), self.data.get("low_f")
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
            draw_text_centered(buf, 23, f"HI {hi:.0f}  LO {lo:.0f}", (150, 160, 185))

        text = cond.get("text") or ""
        if text:
            draw_text_centered(buf, 30, fit_text(text, WIDTH - 4), self.INK)

        draw_divider(buf, 37)

        wind = cond.get("wind_kmh")
        wtxt = ""          # also read below for the right block's collision check
        if wind is not None:
            d = self._compass(cond.get("wind_dir_deg"))
            wtxt = f"{kmh_to_mph(wind):.0f}MPH {d}".strip()
            draw_text3x5(buf, 2, 41, "WIND", self.INK_DIM)
            draw_text3x5(buf, 2, 47, wtxt, self.INK)

        # Humidity and gust are genuinely often null even on a healthy
        # station (confirmed live), so they only appear when real.
        hum = cond.get("humidity")
        gust = cond.get("gust_kmh")
        if gust is not None:
            label, val = "GUST", f"{kmh_to_mph(gust):.0f}MPH"
        elif hum is not None:
            # "HUM", not "HUMIDITY": the long form pushed its block left
            # far enough to collide with the wind value ("10MPH N62%").
            label, val = "HUM", f"{hum:.0f}%"
        else:
            label = val = None
        if label:
            # Right-align label and value independently against the right
            # edge, and only draw if there's a real gap from the wind
            # block -- a shared left-edge block for the widest of the two
            # is what let the narrow value creep into the wind text.
            lx = WIDTH - 2 - text_w(label)
            vx = WIDTH - 2 - text_w(val)
            if min(lx, vx) > 2 + text_w(wtxt) + 3:
                draw_text3x5(buf, lx, 41, label, self.INK_DIM)
                draw_text3x5(buf, vx, 47, val, self.INK)

        draw_divider(buf, 55)
        # Next sun event beats a static "NO ALERTS": the absence of an
        # alert is already implied by this view being on screen at all,
        # whereas "sunset in 2h" is a real, changing thing worth glancing
        # at. Falls back to the alert-state line if sun times are
        # unavailable (polar latitudes -- see weather.sun_times).
        sun = self._sun_line()
        if sun:
            draw_text_centered(buf, 58, sun, (255, 200, 120))
        else:
            draw_text_centered(buf, 58, "NO ALERTS", (60, 110, 70))
        return bytes(buf)

    AMBIENT_STYLE = "fade"          # atmospheric: weather dissolves in

    def ambient_weight(self):
        return 3.0 if (self.data.get("alerts") or []) else 1.0


    def _sun_line(self):
        """"SUNSET 8:16P" or "SUNRISE 6:26A" -- whichever comes next.

        NWS does not provide sun times at all (verified: no sunrise/sunset
        key anywhere in /points or the forecast payload), so these are
        computed locally by weather.sun_times() and checked against an
        independent source to within a minute."""
        sr, ss = self.data.get("sunrise"), self.data.get("sunset")
        now = time.time()
        nxt = None
        if isinstance(sr, (int, float)) and now < sr:
            nxt = ("SUNRISE", sr)
        elif isinstance(ss, (int, float)) and now < ss:
            nxt = ("SUNSET", ss)
        elif isinstance(sr, (int, float)):
            nxt = ("SUNRISE", sr + 86400)      # past both: tomorrow's sunrise
        if not nxt:
            return ""
        label, when = nxt
        t = time.localtime(when)
        hh = time.strftime("%I", t).lstrip("0") or "12"
        return f"{label} {hh}:{time.strftime('%M', t)}{time.strftime('%p', t)[0]}"

    def frame(self):
        alerts = self.data.get("alerts") or []
        if alerts:
            return self._frame_alert(alerts[self.cur_alert % len(alerts)])
        return self._frame_conditions()


class ClockEngine:
    """Clock + at-a-glance dashboard. The panel's resting state.

    This is what the service starts in and what it falls back to, so it is
    the mode most likely to be on the wall at any given moment. Designed
    accordingly: readable across a room, calm, and never showing anything
    it isn't sure about.

    NO clock.py feed module, deliberately. Every other data mode has one
    because it has network I/O to isolate; this one has none --
    time.localtime() is a local call, not I/O -- and it composes two feeds
    that are ALREADY pure-I/O modules (weather.FEED, satellite.FEED).
    Adding an empty feed module to match the pattern would be cargo-cult,
    not consistency.

    Degrades a field at a time: no weather -> no temperature line, no ISS
    prediction -> no countdown line, and the clock itself never depends on
    either. The time is the one thing on this panel that cannot be stale
    or wrong, so it must never be blocked by something that can.
    """

    name = "clock"
    tick_rate = 0.1          # 10Hz is plenty; only the colon blinks per second

    BG = (0, 0, 0)
    ACCENT = (120, 200, 255)
    TIME = (235, 242, 255)
    DATE = (120, 130, 158)
    TEMP = (255, 200, 90)
    ISS = (255, 226, 60)
    SUN = (255, 170, 90)
    DIM = (70, 76, 92)

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.ticks = 0
        self.show_seconds = False     # face button toggles a seconds readout
        self.wx = {}
        self.sky = {}

    def has_content(self):
        """Always true -- local time is always available. Present for
        contract uniformity (AmbientEngine._available calls this
        unguarded); see the note there about why clock is NOT in the
        rotation."""
        return True

    # ---- input -----------------------------------------------------------
    def input(self, cmd):
        if cmd in ("rotate", "drop"):
            self.show_seconds = not self.show_seconds

    def auto(self):
        pass          # a clock is already its own ambient state

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        # Reads the shared feeds the same way every other engine does.
        # Cheap cached reads; also keeps them warm while resting, so
        # switching to weather or sky from here shows data immediately
        # instead of a cold LOADING screen.
        self.wx = weather.FEED.get()
        lat, lon, _lbl = satellite.FEED.get_location()
        self.sky = skypass.FEED.get(lat, lon)

    # ---- render --------------------------------------------------------
    @staticmethod
    def _hhmm(t, blink_on):
        """12-hour, no leading zero, blinking separator.

        The two classic off-by-one traps live here and are both handled by
        strftime("%I") rather than by arithmetic on tm_hour: midnight
        (tm_hour 0) must read 12, and noon (tm_hour 12) must also read 12.
        Doing `hour % 12` gives 0 for both, which is the bug."""
        h = time.strftime("%I", t).lstrip("0") or "12"
        sep = ":" if blink_on else " "
        return f"{h}{sep}{time.strftime('%M', t)}"

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        t = time.localtime()

        # Blink on a real half-second boundary from the clock itself, not
        # a tick counter -- a tick-derived blink drifts against the actual
        # seconds and looks subtly wrong next to any other clock.
        blink_on = (t.tm_sec % 2) == 0

        # Shared header, like every other mode -- this was hand-rolling its
        # own accent rule, the one place in the product not using the
        # common chrome. The title carries the weather station's location,
        # which also fixes a real gap: the clock showed a temperature with
        # nothing saying where it was measured.
        place = (self.wx or {}).get("place") or ""
        draw_header(buf, place or "HENDERBURGH", self.ACCENT,
                    right_tag=time.strftime("%p", t).upper())

        # --- date ---
        date = f"{time.strftime('%a', t)} {time.strftime('%b', t)} {t.tm_mday}".upper()
        draw_text_centered(buf, 10, fit_text(date, WIDTH - 4), self.DATE)

        # --- time (hero) ---
        # AM/PM moved into the header's right tag, so the hero is purely
        # the digits and can be centred on the full panel width instead of
        # being offset by a suffix.
        hhmm = self._hhmm(t, blink_on)
        draw_text_centered(buf, 18, hhmm, self.TIME, scale=2)

        if self.show_seconds:
            draw_text_centered(buf, 28, time.strftime("%S", t), self.DIM)

        draw_divider(buf, 36)

        # --- temperature (only if the weather feed actually has one) ---
        y = 41
        cond = (self.wx or {}).get("conditions")
        temp_c = cond.get("temp_c") if cond else None
        if isinstance(temp_c, (int, float)):
            draw_text_centered(buf, y, f"{c_to_f(temp_c):.0f}F", self.TEMP)
            y += 9
        elif self.wx.get("configured"):
            # Configured but no reading yet: say so rather than leave a
            # gap that looks like the mode is broken.
            draw_text_centered(buf, y, "--F", self.DIM)
            y += 9

        # --- next ISS pass (only if the ISS is actually in the unified
        # sky-pass list -- retired satellite.py's own polluxlabs pass
        # predictor once skypass.py's SGP4 predictions were validated to
        # agree with it; this is the ISS entry from that ONE list now,
        # picked by NORAD id, same as SatelliteEngine's telemetry slot) --
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        iss = next((p for p in (self.sky.get("passes") or []) if p.get("is_iss")), None)
        if iss:
            secs = (iss["rise"] - now).total_seconds()
            if secs > 0:
                label = f"ISS {SatelliteEngine._fmt_countdown(secs)}"
                draw_text_centered(buf, y, fit_text(label, WIDTH - 4), self.ISS)
                y += 8

        # --- next sun event, reusing the weather feed (no new source) ---
        # A clock that also tells you how much daylight is left is doing
        # something a clock alone cannot. Costs nothing: weather.FEED is
        # already read every tick for the temperature.
        sun = self._sun_line()
        if sun and y <= HEIGHT - 6:
            draw_text_centered(buf, y, sun, self.SUN)

        return bytes(buf)

    def _sun_line(self):
        """Next sunrise/sunset, same computation the weather mode uses."""
        sr, ss = (self.wx or {}).get("sunrise"), (self.wx or {}).get("sunset")
        now = time.time()
        if isinstance(sr, (int, float)) and now < sr:
            label, when = "SUNRISE", sr
        elif isinstance(ss, (int, float)) and now < ss:
            label, when = "SUNSET", ss
        elif isinstance(sr, (int, float)):
            label, when = "SUNRISE", sr + 86400
        else:
            return ""
        t = time.localtime(when)
        hh = time.strftime("%I", t).lstrip("0") or "12"
        return f"{label} {hh}:{time.strftime('%M', t)}{time.strftime('%p', t)[0]}"


class BlogEngine(Browsable):
    """Calm idle mode showing the latest posts from the HENDERBURGH site.

    Deliberately the quietest mode in the project: no scrolling, no
    pulsing, no auto-advancing counters -- a Vestaboard-style "here is a
    thing someone wrote" that is pleasant to have on a wall for a long
    time. Everything else here earns attention; this one specifically
    should not.

    Because it never scrolls, text that doesn't fit is WRAPPED across
    lines and, only if it still doesn't fit, truncated at a whole word
    (fit_text) -- never mid-word, matching the precedent set by flights
    and sports.
    """

    name = "blog"
    tick_rate = 0.05

    BG = (0, 0, 0)
    ACCENT = (176, 96, 255)
    NAME = (255, 226, 60)
    BODY = (190, 198, 220)
    INK_DIM = (70, 76, 92)

    DWELL_TICKS = 500        # ~25s per post -- long, on purpose; this is idle art
    BODY_LINES = 4

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"posts": [], "age": None, "err": None}
        self.cur = 0
        self.hold = 0
        self.cycling = True
        self.ticks = 0
        self.pulse = Pulse()
        self._init_scroll()

    def has_content(self):
        return bool(self.data.get("posts"))

    # ---- input -----------------------------------------------------------
    def _step(self, direction):
        n = len(self.data.get("posts") or [])
        if n:
            self.cur = (self.cur + direction) % n
            self.hold = 0

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        if cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        self.data = blog.FEED.get()
        posts = self.data.get("posts") or []
        self.pulse.note(posts[0]["id"] if posts else None)   # newest post arriving
        n = len(posts)
        if n:
            self.cur %= n
        if self.cycling and n > 1 and self.browse.auto_ok:
            self.hold += 1
            if self.hold >= self.DWELL_TICKS:
                self.hold = 0
                self.cur = (self.cur + 1) % n
        self.score = n

    AMBIENT_STYLE = "fade"          # calm, like the mode itself

    def ambient_weight(self):
        return 0.8                  # the quiet one: present, but brief


    # ---- render --------------------------------------------------------
    @staticmethod
    def _wrap(text, max_px, max_lines):
        """Greedy word wrap. A single word longer than the line is the one
        case that must still be broken, so it is hard-split rather than
        dropped or allowed to overflow."""
        lines, cur = [], ""
        for w in text.split():
            trial = f"{cur} {w}".strip()
            if text_w(trial) <= max_px:
                cur = trial
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while text_w(w) > max_px:
                keep = max(1, len(w) - 1)
                while keep > 1 and text_w(w[:keep]) > max_px:
                    keep -= 1
                lines.append(w[:keep])
                w = w[keep:]
                if len(lines) >= max_lines:
                    break
            cur = w
            if len(lines) >= max_lines:
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        return lines[:max_lines]

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        posts = self.data.get("posts") or []
        stale = bool(self.data.get("age") and self.data["age"] > 1800)
        draw_header(buf, "HENDERBURGH", self.ACCENT, stale=stale)

        if not posts:
            # An empty or unreachable blog shows what is actually true --
            # never sample/placeholder content standing in for a real post.
            msg = "NO POSTS" if not self.data.get("err") else "OFFLINE"
            draw_text_centered(buf, 28, msg, self.INK_DIM)
            return bytes(buf)

        p = posts[self.cur % len(posts)]

        # Poster name as the heading. Wrapped to two lines rather than
        # truncated -- these are short and it is the more personal half.
        name_lines = self._wrap(p["name"], WIDTH - 6, 2)
        y = 8
        for ln in name_lines:
            draw_text_centered(buf, y, ln, self.pulse.mix(self.NAME))
            y += 7

        y += 2
        for x in range(8, WIDTH - 8):
            put_px(buf, x, y, (34, 38, 50))
        y += 4

        body_lines = self._wrap(p["text"], WIDTH - 6, self.BODY_LINES)
        for ln in body_lines:
            draw_text_centered(buf, y, ln, self.BODY)
            y += 7

        if len(posts) > 1:
            draw_dots(buf, HEIGHT - 4, len(posts), self.cur)
        return bytes(buf)


class AmbientEngine(Browsable):
    """Master rotation: flights -> ISS -> weather -> sports -> news, on a
    loop, skipping anything that has nothing to show right now.

    Composition, not reimplementation: this owns real instances of the
    other engines and delegates tick()/frame()/input() to whichever is
    current. Every sub-mode therefore looks and behaves in the rotation
    exactly as it does on its own, and a fix to any of them lands here for
    free. Nothing about their rendering is duplicated.

    CLOCK IS DELIBERATELY NOT IN SEQUENCE. Clock is the panel's *resting*
    state (what is on when nothing has been chosen); ambient is an active
    choice to watch live data cycle. Putting clock in the rotation would
    also break the rotation's own contract: has_content() is always true
    for a clock, so it could never be skipped and would consume a full
    dwell slot of every lap, displacing the data you actually selected
    this mode to see. It IS used as ambient's empty-state fallback, since
    a clock beats a "no data" screen.

    All five sub-engines are ticked every tick, not just the visible one.
    That is deliberate and load-bearing: each engine's tick() is what
    calls its FEED.get(), and a feed whose get() stops being called goes
    idle and stops polling (IDLE_STOP). Ticking only the visible one would
    mean every mode had cold, empty data at the moment it came up -- and
    has_content() would then skip it, so the rotation would collapse to
    whichever mode happened to be warm. Ticking is cheap (cached reads,
    no I/O on this thread); only the current engine's frame() is drawn.
    """

    name = "ambient"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)

    # Order is deliberate: the two "look up, something is happening right
    # now" modes lead, then weather, then the two reading-heavy tickers.
    SEQUENCE = ("flights", "satellite", "weather", "sports", "news", "blog")

    DWELL_TICKS = 400        # ~20s per mode at this tick rate
    DWELL_MIN = 200          # ~10s -- floor, so nothing flashes past
    DWELL_MAX = 900          # ~45s -- ceiling, so nothing hogs the rotation
    TRANSITION_TICKS = 8     # ~0.4s slide between sub-modes at this tick rate
    RECHECK_TICKS = 60       # ~3s before giving up on an all-empty rotation

    def __init__(self):
        self.score = 0
        self.engines = {n: ENGINES[n]() for n in self.SEQUENCE}
        # Not part of SEQUENCE -- see the class docstring for why clock is
        # excluded from the rotation but used as the empty-state fallback.
        self._fallback = ClockEngine()
        self.reset()

    def reset(self):
        for e in self.engines.values():
            e.reset()
        self.idx = 0
        self.hold = 0
        self.ticks = 0
        self.cycling = True
        self._trans_from = None
        self._trans_i = 0
        # BIG-MOMENT CELEBRATION state. Lives here, not on any sub-engine,
        # because the interrupt is an AMBIENT behaviour -- manual browsing
        # of the sports mode keeps its own routine scoring flash (Pulse)
        # and never sees this. See draw_celebration()'s module docstring
        # for the shared contract every sport plugs into.
        self._celebration = None
        self._celebration_t = 0
        self._init_scroll()

    @property
    def current(self):
        return self.engines[self.SEQUENCE[self.idx]]

    def _dwell_for(self, eng):
        """Dwell weighted by how much is actually going on in that mode
        right now, instead of a flat duration for everything.

        A live game or a bright ISS pass earns more of your attention than
        an empty scoreboard; the guestbook is deliberately brief. Clamped
        so nothing can starve or hog the rotation even if a weight is
        wrong."""
        try:
            w = float(eng.ambient_weight())
        except Exception:                  # noqa: BLE001
            w = 1.0
        w = max(0.5, min(3.0, w))
        return int(clamp(self.DWELL_TICKS * w, self.DWELL_MIN, self.DWELL_MAX))

    def _available(self):
        return [i for i, n in enumerate(self.SEQUENCE)
                if self.engines[n].has_content()]

    def _advance(self, step=1):
        avail = self._available()
        if not avail:
            return
        # Capture the outgoing sub-mode's last frame so frame() can slide
        # the next one in. Ambient advances internally rather than through
        # set_mode, so it would otherwise be the ONE place in the product
        # that still hard-cuts between screens.
        try:
            self._trans_from = self._render_current()
            self._trans_i = 0
        except Exception:                      # noqa: BLE001 - never break rotation
            self._trans_from = None
        # Move to the next available index in rotation order, wrapping --
        # not just "next in avail", so manual stepping stays predictable
        # when availability changes between presses.
        n = len(self.SEQUENCE)
        for k in range(1, n + 1):
            cand = (self.idx + step * k) % n
            if cand in avail:
                self.idx = cand
                return

    # ---- input -----------------------------------------------------------
    def _step(self, direction):
        self._advance(direction)
        self.hold = 0

    def input(self, cmd):
        # left/right browse the ROTATION itself (flights -> weather -> ...),
        # one level up from the sub-mode's own list. Same gesture, same
        # accelerating hold -- the level it acts on is just whichever is
        # showing.
        if self._browse_input(cmd):
            return
        if cmd in ("rotate", "drop"):
            self.cycling = not self.cycling
        else:
            self.current.input(cmd)     # anything else belongs to the sub-mode

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self._scroll_tick()
        for e in self.engines.values():
            e.tick()                   # keeps every feed warm; see class docstring

        # BIG-MOMENT CELEBRATION. Checked every tick regardless of which
        # sub-mode is currently showing -- a home run should interrupt the
        # news ticker exactly as readily as it interrupts a quiet sports
        # screen, same "the moment finds you" idea as the severe-weather
        # takeover, just scoped to ambient instead of every mode (severe
        # weather already outranks this for free: it is composited by
        # arcade_server AFTER this engine's frame() runs, so an alert
        # covers a celebration exactly as it covers anything else).
        #
        # HIGHEST TIER WINS, not first-found. Four systems can now each
        # have a moment pending in the same tick, and dict iteration
        # order is not a priority order -- PEEK every engine (without
        # consuming), keep the highest tier seen, and only POP the
        # winner. The loser(s) stay queued in their own engine for the
        # next tick rather than being silently discarded, since
        # _set_big_moment()'s own tier-gated overwrite already protects
        # each engine's single slot from a lesser moment clobbering it.
        #
        # A TIER_TAKEOVER may pre-empt an in-flight celebration; nothing
        # else may (TIER_INTERRUPT-vs-TIER_INTERRUPT keeps the original
        # never-interrupt rule, and TIER_TAKEOVER-vs-TIER_TAKEOVER keeps
        # it too -- the FIRST takeover to start gets its full hold).
        best_engine, best_moment = None, None
        for e in self.engines.values():
            peek = getattr(e, "peek_big_moment", None)
            m = peek() if callable(peek) else None
            if m and (best_moment is None
                     or m.get("tier", TIER_INTERRUPT) > best_moment.get("tier", TIER_INTERRUPT)):
                best_engine, best_moment = e, m

        started_or_preempted = False
        if best_moment is not None:
            tier = best_moment.get("tier", TIER_INTERRUPT)
            playing = self._celebration_t > 0 and self._celebration
            cur_tier = playing.get("tier", TIER_INTERRUPT) if playing else None
            may_start = not playing
            may_preempt = (cur_tier is not None and tier == TIER_TAKEOVER
                          and cur_tier < TIER_TAKEOVER)
            if may_start or may_preempt:
                best_engine.pop_big_moment()
                self._celebration = best_moment
                self._celebration_t = TIER_TICKS.get(tier, CELEBRATION_TICKS)
                started_or_preempted = True

        if not started_or_preempted and self._celebration_t > 0:
            self._celebration_t -= 1
            if self._celebration_t <= 0:
                self._celebration = None

        avail = self._available()
        if not avail:
            return                     # nothing anywhere yet; the frame() shows why

        if self.idx not in avail:
            # Whatever we were showing just went empty (last plane left the
            # sky, games ended). Move on immediately rather than dwelling on
            # a mode that now has nothing.
            self._advance(1)
            self.hold = 0

        # Dwell timing PAUSES during a celebration -- it is a full-panel
        # interrupt, not a sub-mode taking its normal turn, so it must not
        # eat into whatever mode's dwell was in progress when it fired.
        if self._celebration_t <= 0 and self.cycling and len(avail) > 1 and self.browse.auto_ok:
            self.hold += 1
            if self.hold >= self._dwell_for(self.current):
                self.hold = 0
                self._advance(1)

        self.score = self.current.score

    # ---- render --------------------------------------------------------
    def frame(self):
        if self._celebration_t > 0:
            total = TIER_TICKS.get(self._celebration.get("tier", TIER_INTERRUPT), CELEBRATION_TICKS)
            elapsed = total - self._celebration_t
            return draw_celebration(blank(), elapsed, self._celebration, total=total)

        avail = self._available()
        if not avail:
            # Fall back to the clock rather than a "NO DATA YET" screen.
            # Clock needs no network and cannot be empty, so the panel
            # still shows something true instead of admitting defeat --
            # and this is exactly the situation (everything unreachable)
            # where a wall display is most likely to be looked at.
            self._fallback.tick()
            return self._fallback.frame()
        frame = self._render_current()
        if self._trans_from and self._trans_i < self.TRANSITION_TICKS:
            self._trans_i += 1
            p = self._trans_i / float(self.TRANSITION_TICKS)
            # Each mode enters with its OWN transition style, so you know
            # what kind of thing arrived before you have read a word --
            # built on the shared system, not a second one.
            style = getattr(self.current, "AMBIENT_STYLE", transitions.DEFAULT_STYLE)
            frame = transitions.blend(self._trans_from, frame, p, style)
            if self._trans_i >= self.TRANSITION_TICKS:
                self._trans_from = None
        return frame

    def _render_current(self):
        """The sub-mode's REAL screen, exactly as it renders when selected
        manually.

        Ambient is a rotation CONTROLLER, not a second visual layer. An
        earlier pass gave each mode a separate "channel ident" layout here;
        that was removed deliberately. The manual screens are the designed
        ones, and having two layouts per mode meant every future change had
        to be made twice or they would drift -- with the ambient copy
        being the one nobody looks at while working on a mode.

        What ambient still owns: WHICH mode is showing, for HOW LONG
        (weighted dwell), the entrance transition between modes, and
        browsing between them. Not what any of them look like.
        """
        return self.current.frame()


class BootEngine:
    name = "boot"
    tick_rate = 0.045

    BG = (0, 0, 0)
    CURTAIN = (120, 18, 34)
    CURTAIN_FOLD = (90, 12, 26)
    CURTAIN_LIT = (168, 30, 48)
    GOLD = (255, 200, 60)
    SUB = (140, 150, 175)

    HOLD_DARK = 6
    PART_TICKS = 26
    HOLD_LOGO = 22
    FLASH_TICKS = 4
    TOTAL = HOLD_DARK + PART_TICKS + HOLD_LOGO + FLASH_TICKS

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.t = 0
        self.launch = None
        self._sparks = []

    def input(self, cmd):
        self.launch = "menu"        # any button skips the intro

    def auto(self):
        pass

    def tick(self):
        self.t += 1
        # A few sparkle twinkles seeded once the logo is fully visible.
        if self.t == self.HOLD_DARK + self.PART_TICKS:
            self._sparks = [
                (random.randint(6, WIDTH - 7), random.randint(14, 44), random.randint(0, 30))
                for _ in range(10)
            ]
        if self.t >= self.TOTAL:
            self.launch = "menu"

    def _draw_logo(self, buf, glow=1.0):
        sub, title = "HENDERBURGH", "ARCADE"
        tw1 = 4 * len(sub) - 1
        draw_text3x5(buf, (WIDTH - tw1) // 2, 24, sub,
                     tuple(int(v * glow) for v in self.SUB))
        tw2 = 2 * (4 * len(title) - 1)
        gold = tuple(min(255, int(v * glow)) for v in self.GOLD)
        draw_text3x5(buf, (WIDTH - tw2) // 2, 31, title, gold, scale=2)
        # Underline that grows with the reveal -- a marquee-style flourish.
        uw = int(tw2 * min(1.0, glow))
        ux = (WIDTH - tw2) // 2
        for x in range(ux, ux + uw):
            put_px(buf, x, 43, gold)

    def frame(self):
        buf = blank()
        fill(buf, self.BG)

        if self.t < self.HOLD_DARK:
            return bytes(buf)

        pt = self.t - self.HOLD_DARK
        if pt < self.PART_TICKS:
            # Ease-out parting: fast start, gentle settle -- reads as a real
            # curtain's weight rather than a linear slide.
            f = pt / self.PART_TICKS
            f = 1 - (1 - f) ** 2
        else:
            f = 1.0

        self._draw_logo(buf, glow=f)

        if self.t >= self.HOLD_DARK + self.PART_TICKS:
            for x, y, phase in self._sparks:
                age = (self.t + phase) % 26
                if age < 4:
                    b = 255 if age in (1, 2) else 140
                    put_px(buf, x, y, (b, b, b))

        # 40px of travel clears the 32px half-screen with a bit to spare, so
        # the curtains are fully gone right as parting finishes -- not, as
        # before, 60% of the way through it (that made most of "parting"
        # play out as an empty stage with the logo already lit, instead of
        # the widening-gap reveal it's supposed to be).
        left_edge = int(32 - f * 40)
        right_edge = int(32 + f * 40)
        for x in range(0, max(0, left_edge)):
            col = self.CURTAIN_LIT if x >= left_edge - 2 else (
                self.CURTAIN_FOLD if x % 4 == 0 else self.CURTAIN)
            for y in range(HEIGHT):
                put_px(buf, x, y, col)
        for x in range(min(WIDTH, right_edge), WIDTH):
            col = self.CURTAIN_LIT if x <= right_edge + 2 else (
                self.CURTAIN_FOLD if x % 4 == 0 else self.CURTAIN)
            for y in range(HEIGHT):
                put_px(buf, x, y, col)

        # Fixed scalloped valance across the very top -- frames the whole
        # scene like the top of a stage arch, independent of the curtain
        # parting happening below it.
        for x in range(WIDTH):
            depth = 3 if (x // 4) % 2 == 0 else 1
            for y in range(depth):
                put_px(buf, x, y, self.CURTAIN_FOLD if depth == 3 else self.CURTAIN)

        if self.t >= self.HOLD_DARK + self.PART_TICKS + self.HOLD_LOGO:
            flash_t = self.t - (self.HOLD_DARK + self.PART_TICKS + self.HOLD_LOGO)
            k = flash_t / max(1, self.FLASH_TICKS)
            if k < 1.0:
                fill(buf, tuple(int(255 * (1 - k) + c * k) for c in self.BG))
                self._draw_logo(buf, glow=1.0)

        return bytes(buf)


# =============================================================================
# MENU — the system's own home screen, drawn on the panel like a console
# dashboard. The phone is just a controller (dpad + SELECT/AUTO); browsing
# happens on the matrix, which is the whole point of this mode existing.
# =============================================================================
class MenuEngine:
    name = "menu"
    tick_rate = 0.09

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)

    COLS, ROWS_VISIBLE = 5, 2
    ICON = 10
    CELL_W, CELL_H = 12, 15
    X_OFF = (WIDTH - COLS * 12) // 2   # 2
    Y_OFF = 3

    # Cartridge cyan-gold -- distinct enough from the native games' palette
    # that a dropped-in cart still reads as its own kind of tile, but not so
    # different it feels like a bolted-on second app in the same flat list.
    CART_COLOR = (255, 191, 0)

    # id, label, accent -- same palette as the phone controller, so the two
    # screens read as one system rather than two different apps.
    NATIVE_GAMES = [
        ("snake",    "SNAKE",    (51, 255, 176)),
        ("tetris",   "TETRIS",   (0, 200, 255)),
        ("pong",     "PONG",     (143, 255, 199)),
        ("breakout", "BREAKOUT", (255, 90, 120)),
        ("tron",     "TRON",     (0, 255, 242)),
        ("flappy",   "FLAPPY",   (255, 226, 60)),
        ("invaders", "INVADERS", (255, 51, 200)),
        ("dodge",    "DODGE",    (255, 210, 60)),
        ("2048",     "2048",     (176, 96, 255)),
        ("life",     "LIFE",     (125, 255, 176)),
        ("brawler",  "BRAWLER",  (80, 235, 130)),
        ("chase",    "CHASE",    (255, 226, 60)),
        ("tunnel",   "TUNNEL",   (120, 110, 255)),
        ("powder",   "POWDER",   (230, 190, 90)),
        ("ticker",   "TICKER",   (60, 230, 110)),
        ("satellite", "ISS",     (255, 226, 60)),
        ("flights",  "FLIGHTS",  (120, 200, 255)),
        ("sports",   "SPORTS",   (255, 140, 40)),
        ("news",     "NEWS",     (255, 226, 60)),
        ("weather",  "WEATHER",  (90, 190, 255)),
        ("clock",    "CLOCK",    (120, 200, 255)),
        ("blog",     "BLOG",     (176, 96, 255)),
        ("ambient",  "AMBIENT",  (176, 96, 255)),
        ("gameday",  "GAME DAY", GAMEDAY_ACCENT),
    ]

    def __init__(self):
        self.score = 0
        # Cart library is scanned once at menu construction (server restart
        # picks up newly-dropped .tic files) and appended to the same flat
        # list the native games live in -- one list, one selection model.
        self.GAMES = list(self.NATIVE_GAMES) + [
            (f"tic:{p.name}", p.stem.upper()[:10], self.CART_COLOR)
            for p in scan_tic_carts()
        ]
        self.reset()

    def reset(self):
        self.cur = 0
        self.launch = None
        self.pulse = 0
        self.bump = 0
        self._auto_ticks = 0
        self.view_row = 0
        # Two-stage selection, like a console: browse the shelf, then decide
        # how to run the thing you picked. Previously SELECT and AUTO were
        # two different buttons on the grid, which meant the choice was
        # invisible until after you had already committed to a game.
        self.stage = "grid"        # "grid" -> browsing, "pick" -> play/watch
        self.pick = 0              # 0 = PLAY, 1 = WATCH
        # Floating cursor position (icon-grid pixel space) that tick() eases
        # toward the selected tile -- a snap-to-cell highlight reads as static
        # UI, but a cursor that glides between tiles is what makes browsing
        # feel alive rather than like paging through a list.
        vx, vy = self._cell_xy(self.cur)
        self.vx, self.vy = float(vx), float(vy)

    def _cell_xy(self, i):
        r, c = divmod(i, self.COLS)
        return self.X_OFF + c * self.CELL_W, self.Y_OFF + (r - self.view_row) * self.CELL_H

    def _select(self, resume_id=None):
        if resume_id:
            for i, (gid, _, _) in enumerate(self.GAMES):
                if gid == resume_id:
                    self.cur = i
                    self.vx, self.vy = (float(v) for v in self._cell_xy(i))
                    return

    def _row_bounds(self, i):
        n = len(self.GAMES)
        start = (i // self.COLS) * self.COLS
        return start, min(start + self.COLS, n)

    def _vert_move(self, d):
        n = len(self.GAMES)
        rows = -(-n // self.COLS)  # ceil -- library can end on a ragged row
        r, c = divmod(self.cur, self.COLS)
        nr = (r + d) % rows
        start = nr * self.COLS
        end = min(start + self.COLS, n)
        return start + min(c, end - start - 1)

    def _move(self, i):
        self.cur = i
        self.bump = 8
        r = self.cur // self.COLS
        if r < self.view_row:
            self.view_row = r
        elif r >= self.view_row + self.ROWS_VISIBLE:
            self.view_row = r - self.ROWS_VISIBLE + 1

    def input(self, cmd):
        if self.stage == "pick":
            gid = self.GAMES[self.cur][0]
            if cmd in ("left", "right"):
                # PLAY/WATCH are laid out side by side (see _frame_pick), so
                # only the horizontal axis should switch between them --
                # up/down used to flip it too, which read as a misfire to
                # anyone navigating vertically out of habit.
                self.pick ^= 1
                self.bump = 8
            elif cmd == "rotate":
                # Cartridges have no self-playing mode, so WATCH falls back
                # to a normal launch rather than a mode that cannot exist.
                self.launch = (gid + "-demo"
                               if self.pick == 1 and not gid.startswith("tic:")
                               else gid)
            elif cmd in ("drop", "hold"):
                self.stage = "grid"                       # back to the shelf
                self.bump = 8
            return

        if cmd == "left":
            start, end = self._row_bounds(self.cur)
            self._move(start + (self.cur - start - 1) % (end - start))
        elif cmd == "right":
            start, end = self._row_bounds(self.cur)
            self._move(start + (self.cur - start + 1) % (end - start))
        elif cmd == "up":
            self._move(self._vert_move(-1))
        elif cmd == "down":
            self._move(self._vert_move(1))
        elif cmd in ("rotate", "drop"):
            self.stage = "pick"                           # ask how to run it
            self.pick = 0
            self.bump = 8

    def auto(self):
        # Idle screensaver sweep when nobody's touched the controller --
        # never actually launches anything, just keeps the menu alive to look at.
        # If it was left sitting on the play/watch prompt, back out first:
        # an unattended panel should drift across the shelf, not sit on a
        # half-finished decision nobody is going to make.
        if self.stage == "pick":
            self.stage = "grid"
            return
        self._auto_ticks += 1
        if self._auto_ticks >= 14:
            self._auto_ticks = 0
            self._move((self.cur + 1) % len(self.GAMES))

    def tick(self):
        self.pulse = (self.pulse + 1) % 24
        if self.bump > 0:
            self.bump -= 1
        tx, ty = self._cell_xy(self.cur)
        # Critically-damped-ish ease: covers most of the gap in ~4 ticks
        # (~0.36s) but still visibly glides instead of teleporting.
        self.vx += (tx - self.vx) * 0.45
        self.vy += (ty - self.vy) * 0.45
        if abs(tx - self.vx) < 0.3: self.vx = float(tx)
        if abs(ty - self.vy) < 0.3: self.vy = float(ty)

    def _icon(self, buf, gid, x0, y0, color, dim):
        c = color if not dim else tuple(v // 3 for v in color)
        w = (255, 255, 255) if not dim else tuple(v // 3 for v in (255, 255, 255))

        def block(x, y, w_, h_, col):
            for yy in range(y, y + h_):
                for xx in range(x, x + w_):
                    put_px(buf, x0 + xx, y0 + yy, col)

        if gid == "snake":
            block(0, 1, 6, 2, c); block(4, 4, 6, 2, c); block(0, 7, 6, 2, c)
        elif gid == "tetris":
            block(1, 1, 4, 4, c); block(5, 5, 4, 4, dim and c or tuple(v * 2 // 3 for v in color))
        elif gid == "pong":
            block(1, 2, 2, 6, c); block(7, 2, 2, 6, c); put_px(buf, x0 + 5, y0 + 4, w)
        elif gid == "breakout":
            for row in range(3):
                for col_ in range(3):
                    block(1 + col_ * 3, 1 + row * 2, 2, 1, c)
            block(2, 8, 6, 1, c); put_px(buf, x0 + 5, y0 + 7, w)
        elif gid == "tron":
            block(4, 1, 2, 8, c); block(1, 4, 8, 2, c)
        elif gid == "flappy":
            for i, (xs, xe, y) in enumerate([(4, 5, 0), (3, 6, 1), (2, 7, 2), (1, 8, 3),
                                             (1, 8, 4), (2, 7, 5), (3, 6, 6), (4, 5, 7)]):
                block(xs, y + 1, xe - xs + 1, 1, c)
        elif gid == "invaders":
            for dy, row in enumerate((".X.X.", "XXXXX", "X.X.X", ".X.X.")):
                for dx, ch in enumerate(row):
                    if ch == "X":
                        put_px(buf, x0 + 2 + dx, y0 + 2 + dy, c)
        elif gid == "dodge":
            for i, w_ in enumerate((8, 6, 4, 2)):
                block((10 - w_) // 2, i, w_, 1, c)
            for i, w_ in enumerate((2, 4, 6, 8)):
                block((10 - w_) // 2, 6 + i, w_, 1, c)
        elif gid == "2048":
            for x in range(1, 9):
                put_px(buf, x0 + x, y0 + 1, c); put_px(buf, x0 + x, y0 + 8, c)
            for y in range(1, 9):
                put_px(buf, x0 + 1, y0 + y, c); put_px(buf, x0 + 8, y0 + y, c)
            block(3, 3, 4, 4, dim and c or tuple(v * 2 // 3 for v in color))
        elif gid == "life":
            for gx, gy in ((1, 0), (2, 1), (0, 2), (1, 2), (2, 2)):
                block(1 + gx * 3, 1 + gy * 3, 2, 2, c)
        elif gid == "brawler":
            # two ledges with a figure between them
            block(0, 2, 10, 1, c); block(0, 8, 10, 1, c)
            block(4, 4, 2, 4, w)
        elif gid == "chase":
            # maze corner + a dot trail
            block(1, 1, 8, 1, c); block(1, 1, 1, 8, c)
            block(4, 4, 5, 1, c)
            for gx in (3, 5, 7):
                put_px(buf, x0 + gx, y0 + 7, w)
        elif gid == "tunnel":
            # nested rings converging = corridor rushing at you
            for i, s in enumerate((0, 2, 4)):
                for xx in range(s, 10 - s):
                    put_px(buf, x0 + xx, y0 + s, c)
                    put_px(buf, x0 + xx, y0 + 9 - s, c)
                for yy in range(s, 10 - s):
                    put_px(buf, x0 + s, y0 + yy, c)
                    put_px(buf, x0 + 9 - s, y0 + yy, c)
        elif gid == "powder":
            # a falling stream piling into a heap
            for yy in range(0, 5):
                put_px(buf, x0 + 4, y0 + yy, w)
                put_px(buf, x0 + 5, y0 + yy, w)
            for i, wd in enumerate((2, 4, 6, 8)):
                block((10 - wd) // 2, 6 + i, wd, 1, c)
        elif gid == "ticker":
            # A rising bar chart -- reads as "markets" instantly, and the
            # shape survives at icon size where a "$" glyph would not.
            for bx, bh in ((1, 3), (4, 5), (7, 8)):
                for dy in range(bh):
                    block(bx, 9 - dy, 2, 1, c)
            put_px(buf, x0 + 8, y0 + 0, w)
            put_px(buf, x0 + 7, y0 + 1, w)
        elif gid == "satellite":
            # A little satellite silhouette: body + two solar panels + a
            # signal arc. Reads as "space/tracking" at a glance, distinct
            # from every other icon's boxy/bar-chart language.
            block(4, 4, 2, 2, c)
            block(1, 4, 2, 1, dim and c or tuple(v * 2 // 3 for v in color))
            block(7, 4, 2, 1, dim and c or tuple(v * 2 // 3 for v in color))
            put_px(buf, x0 + 6, y0 + 2, w)
            put_px(buf, x0 + 2, y0 + 7, c)
            put_px(buf, x0 + 3, y0 + 6, c)
            put_px(buf, x0 + 4, y0 + 5, w)
        elif gid == "flights":
            # A simple swept-wing silhouette pointed up-right, the classic
            # "flight tracker" glyph shape -- reads instantly as aviation,
            # distinct from the satellite's boxier body-and-panels shape.
            put_px(buf, x0 + 7, y0 + 1, c)
            put_px(buf, x0 + 6, y0 + 2, c)
            put_px(buf, x0 + 5, y0 + 3, c)
            block(1, 3, 4, 1, c)
            put_px(buf, x0 + 4, y0 + 4, c)
            block(3, 5, 3, 1, c)
            put_px(buf, x0 + 4, y0 + 6, c)
            put_px(buf, x0 + 3, y0 + 7, c)
            put_px(buf, x0 + 4, y0 + 3, w)
        elif gid == "sports":
            # A scoreboard: a frame with a divider and two blocky "score"
            # marks -- reads as "live score" distinct from the ticker's
            # bar-chart language and the flights/satellite silhouettes.
            block(1, 1, 8, 7, dim and c or tuple(v * 2 // 3 for v in color))
            for xx in range(1, 9):
                put_px(buf, x0 + xx, y0 + 4, self.BG)
            block(2, 2, 2, 1, w)
            block(6, 2, 2, 1, w)
            block(2, 5, 2, 1, c)
            block(6, 5, 2, 1, c)
        elif gid == "weather":
            # A cloud with a sun peeking over it -- the universal weather
            # glyph, distinct from every other icon's boxy/bar language.
            block(2, 1, 3, 2, w)
            block(1, 4, 7, 3, c)
            block(2, 3, 5, 1, c)
            put_px(buf, x0 + 1, y0 + 7, c)
            put_px(buf, x0 + 7, y0 + 7, c)
        elif gid == "clock":
            # A clock face: ring plus two hands. The most literal icon in
            # the set, deliberately -- this is the resting state and should
            # be instantly recognisable rather than clever.
            for xx in range(3, 7):
                put_px(buf, x0 + xx, y0 + 0, c)
                put_px(buf, x0 + xx, y0 + 9, c)
            for yy in range(3, 7):
                put_px(buf, x0 + 0, y0 + yy, c)
                put_px(buf, x0 + 9, y0 + yy, c)
            for dx, dy in ((1,1),(2,1),(1,2),(8,1),(7,1),(8,2),(1,8),(1,7),(2,8),(8,8),(8,7),(7,8)):
                put_px(buf, x0 + dx, y0 + dy, c)
            put_px(buf, x0 + 4, y0 + 3, w)      # hour hand (up)
            put_px(buf, x0 + 4, y0 + 4, w)
            put_px(buf, x0 + 5, y0 + 5, w)      # minute hand (right)
            put_px(buf, x0 + 6, y0 + 5, w)
            put_px(buf, x0 + 4, y0 + 5, w)      # centre pin
        elif gid == "blog":
            # A quote/speech bubble with a tail -- "someone wrote something",
            # distinct from the news tile's newspaper-column language.
            block(1, 1, 8, 5, c)
            block(3, 2, 4, 1, w)
            block(3, 4, 3, 1, w)
            put_px(buf, x0 + 2, y0 + 6, c)      # tail
            put_px(buf, x0 + 2, y0 + 7, c)
        elif gid == "gameday":
            # A full border with a filled centre -- the icon IS the mode's
            # identity (see GameDayEngine._occasion_frame): every other
            # mode's icon is a subject, this one is the FRAME, because a
            # takeover is about the whole panel rather than a subject.
            for xx in range(0, 10):
                put_px(buf, x0 + xx, y0 + 0, c)
                put_px(buf, x0 + xx, y0 + 9, c)
            for yy in range(0, 10):
                put_px(buf, x0 + 0, y0 + yy, c)
                put_px(buf, x0 + 9, y0 + yy, c)
            block(3, 3, 4, 4, w)
        elif gid == "ambient":
            # A rotation arrow around a dot: "these modes cycle" -- distinct
            # from every single-subject icon since this one IS the loop.
            for xx in range(2, 8):
                put_px(buf, x0 + xx, y0 + 1, c)
                put_px(buf, x0 + xx, y0 + 8, c)
            for yy in range(2, 8):
                put_px(buf, x0 + 1, y0 + yy, c)
                put_px(buf, x0 + 8, y0 + yy, c)
            block(4, 4, 2, 2, w)          # the "current" mode at the centre
            put_px(buf, x0 + 7, y0 + 0, w)   # arrowhead, so it reads as motion
            put_px(buf, x0 + 8, y0 + 1, w)
            put_px(buf, x0 + 7, y0 + 2, w)
        elif gid == "news":
            # A folded newspaper: masthead bar + column rules -- distinct
            # from the ticker's bar-chart language and the sports
            # scoreboard's grid, reads as "print/headlines" at a glance.
            block(1, 1, 8, 2, w)
            for yy in (4, 6, 8):
                block(1, yy, 8, 1, c)
        elif gid.startswith("tic:"):
            # Generic cartridge glyph -- a cart's own art lives inside the
            # emulator, not on the menu tile, so every dropped-in .tic gets
            # the same silhouette: a shell with a notch and a label stripe.
            block(1, 1, 8, 7, c)
            block(3, 0, 4, 1, dim and c or tuple(v * 2 // 3 for v in color))
            block(1, 6, 8, 1, w)

    def _frame_pick(self):
        """The play/watch prompt: the chosen game shown large, then the one
        decision left to make. Two options side by side, because the d-pad
        axis you use to choose should match the way they are laid out."""
        buf = blank()
        fill(buf, self.BG)
        gid, label, color = self.GAMES[self.cur]
        is_cart = gid.startswith("tic:")

        # The picked game's icon at 2x, so it is unmistakably the subject.
        icon = blank()
        self._icon(icon, gid, 0, 0, color, dim=False)
        for y in range(self.ICON):
            for x in range(self.ICON):
                i = (y * WIDTH + x) * 3
                px = (icon[i], icon[i + 1], icon[i + 2])
                if px == (0, 0, 0):
                    continue
                bx, by = 22 + x * 2, 5 + y * 2
                for dy in range(2):
                    for dx in range(2):
                        put_px(buf, bx + dx, by + dy, px)

        tw = 4 * len(label) - 1
        draw_text3x5(buf, (WIDTH - tw) // 2, 28, label, color)

        glow = 0.55 + 0.45 * abs((self.pulse % 24) - 12) / 12
        for idx, text in enumerate(("PLAY", "WATCH")):
            x0 = 3 + idx * 31
            x1 = x0 + 27
            y0, y1 = 38, 52
            on = (idx == self.pick)
            # WATCH is meaningless for a cartridge, so it is drawn muted --
            # still selectable, but it reads as the lesser option.
            muted = (idx == 1 and is_cart)
            base = color if on else self.INK_DIM
            edge = tuple(min(255, int(v * glow)) for v in base) if on else self.INK_DIM
            if muted and not on:
                edge = tuple(v // 2 for v in self.INK_DIM)
            for x in range(x0, x1 + 1):
                put_px(buf, x, y0, edge)
                put_px(buf, x, y1, edge)
            for y in range(y0, y1 + 1):
                put_px(buf, x0, y, edge)
                put_px(buf, x1, y, edge)
            tw2 = 4 * len(text) - 1
            ink = color if on else self.INK_DIM
            draw_text3x5(buf, x0 + (28 - tw2) // 2, y0 + 5, text, ink)

        hint = "BACK"
        draw_text3x5(buf, (WIDTH - (4 * len(hint) - 1)) // 2, HEIGHT - 7,
                     hint, self.INK_DIM)
        return bytes(buf)

    def frame(self):
        if self.stage == "pick":
            return self._frame_pick()
        buf = blank()
        fill(buf, self.BG)
        lo, hi = self.view_row * self.COLS, (self.view_row + self.ROWS_VISIBLE) * self.COLS
        for i in range(lo, min(hi, len(self.GAMES))):
            gid, label, color = self.GAMES[i]
            r, cidx = divmod(i, self.COLS)
            x0 = self.X_OFF + cidx * self.CELL_W
            y0 = self.Y_OFF + (r - self.view_row) * self.CELL_H
            self._icon(buf, gid, x0, y0, color, dim=(i != self.cur))

        # Cursor drawn at the eased (vx, vy), not the selected cell's real
        # coordinates -- this is what makes it glide between tiles instead of
        # jumping. A fresh "bump" briefly grows the frame by a pixel, like a
        # controller giving a little kick when the selection lands.
        color = self.GAMES[self.cur][2]
        x0, y0 = self.vx, self.vy
        pad = 1 + (1 if self.bump > 5 else 0)
        glow = 0.55 + 0.45 * abs((self.pulse % 24) - 12) / 12
        border = tuple(min(255, int(v * glow)) for v in color)
        ix0, iy0 = int(round(x0)) - pad, int(round(y0)) - pad
        ix1, iy1 = int(round(x0)) + self.ICON - 1 + pad, int(round(y0)) + self.ICON - 1 + pad
        for x in range(ix0, ix1 + 1):
            put_px(buf, x, iy0, border)
            put_px(buf, x, iy1, border)
        for y in range(iy0, iy1 + 1):
            put_px(buf, ix0, y, border)
            put_px(buf, ix1, y, border)

        gid, label, color = self.GAMES[self.cur]
        y_name = self.Y_OFF + self.ROWS_VISIBLE * self.CELL_H + 2
        tw = 4 * len(label) - 1
        draw_text3x5(buf, (WIDTH - tw) // 2, y_name, label, color)

        # Control legend. Both face buttons now open the play/watch prompt,
        # so advertising a separate AUTO button here would be a lie.
        hint = "SELECT"
        tw2 = 4 * len(hint) - 1
        draw_text3x5(buf, (WIDTH - tw2) // 2, y_name + 8, hint, self.INK_DIM)

        # Soft ambient bar in the selected game's colour -- a console home
        # screen always has some life to it even sitting still on one tile.
        bary = HEIGHT - 3
        bar_w = int(20 + 20 * (0.5 + 0.5 * ((self.pulse % 24) / 24)))
        bx0 = (WIDTH - bar_w) // 2
        for x in range(bx0, bx0 + bar_w):
            put_px(buf, x, bary, tuple(v // 4 for v in color))
        return bytes(buf)


class TunnelEngine:
    """
    A native reimagining of the downloaded "Skip Ahead" cart: a procedural
    pseudo-3D tunnel dive where a winding corridor rushes toward the
    viewer and you steer to stay inside it. The original computed this via
    a literal per-row hash-noise function and a gravity/jump-timed gate
    check; ported to the matrix as a continuous steering challenge instead
    (checked every tick at a fixed near-row, not gated behind a jump-arc
    quirk that only made sense at 30fps on a much taller screen).
    """
    name = "tunnel"
    tick_rate = 0.045

    BG = (0, 0, 0)
    WALL = (60, 40, 160)
    WALL_DIM = (35, 20, 100)
    PLAYER = (255, 226, 60)
    BOOST_GLOW = (255, 255, 255)
    DEAD = (255, 60, 60)

    PLAYER_ROW = 50          # near-bottom, matches the original's "gate" placement
    ROW_DEPTH_SCALE = 2.4    # how much depth separates each row (bigger = tunnel scrolls slower)

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.t = 0.0
        self.px = float(WIDTH // 2)
        self.boost = 0
        self.death_flash = 0
        self.ticks = 0
        # The corridor shape is a sum of sines, which is smooth and cheap --
        # but with fixed phases every run carves the IDENTICAL tunnel, so
        # the auto/ambient mode would replay the same dive forever. Random
        # per-run phases (and slightly jittered frequencies) keep the exact
        # same feel and difficulty curve while making each run its own.
        self._ph1 = random.uniform(0, math.tau)
        self._ph2 = random.uniform(0, math.tau)
        self._ph3 = random.uniform(0, math.tau)
        self._f1 = 0.045 * random.uniform(0.85, 1.15)
        self._f2 = 0.11 * random.uniform(0.85, 1.15)
        self._f3 = 0.03 * random.uniform(0.85, 1.15)

    def _difficulty(self):
        # Ramps like every other engine here: gentle for the first
        # stretch, then genuinely demanding.
        return min(1.0, self.score / 600.0)

    def _center(self, depth):
        d = self._difficulty()
        amp1 = 12 + 6 * d
        amp2 = 6 + 4 * d
        return (WIDTH / 2
                + amp1 * math.sin(depth * self._f1 + self._ph1)
                + amp2 * math.sin(depth * self._f2 + self._ph2))

    def _halfwidth(self, depth):
        d = self._difficulty()
        base = 15 - 6 * d
        wob = 7 * math.sin(depth * self._f3 + self._ph3)
        hw = base + wob
        if self.boost > 0:
            hw += 4  # a brief squeeze-through window, the "dash" payoff
        return max(6.0, hw)

    def input(self, cmd):
        if self.death_flash:
            return
        if cmd == "left":
            self.px = max(2.0, self.px - 4)
        elif cmd == "right":
            self.px = min(WIDTH - 3.0, self.px + 4)
        elif cmd == "rotate":
            self.boost = 12

    def auto(self):
        depth = self.t + (HEIGHT - 1 - self.PLAYER_ROW) * self.ROW_DEPTH_SCALE
        target = self._center(depth)
        self.px += (target - self.px) * 0.5
        if self.boost <= 0 and int(self.t) % 40 == 0:
            self.boost = 12

    def tick(self):
        if self.death_flash:
            self.death_flash -= 1
            if self.death_flash == 0:
                self.reset()
            return
        self.ticks += 1
        self.t += 1.6 + 1.4 * self._difficulty()
        if self.boost > 0:
            self.boost -= 1
        self.score += 1

        depth = self.t + (HEIGHT - 1 - self.PLAYER_ROW) * self.ROW_DEPTH_SCALE
        center = self._center(depth)
        hw = self._halfwidth(depth)
        if self.px < center - hw or self.px > center + hw:
            self.death_flash = 14

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        for y in range(HEIGHT):
            depth = self.t + (HEIGHT - 1 - y) * self.ROW_DEPTH_SCALE
            center = self._center(depth)
            hw = self._halfwidth(depth)
            left = int(center - hw)
            right = int(center + hw)
            # Depth-banded wall colour: gives the illusion of rings rushing
            # past even though each row is drawn once, statically, per frame.
            band = int(depth / 6) % 2
            col = self.WALL if band == 0 else self.WALL_DIM
            for x in range(0, max(0, left)):
                put_px(buf, x, y, col)
            for x in range(min(WIDTH, right + 1), WIDTH):
                put_px(buf, x, y, col)

        color = self.DEAD if self.death_flash and self.death_flash % 4 < 2 else self.PLAYER
        px, py = int(round(self.px)), self.PLAYER_ROW
        # A wake first, so the craft reads as travelling rather than parked.
        put_px(buf, px, py + 2, tuple(v // 2 for v in color))
        put_px(buf, px, py + 3, tuple(v // 4 for v in color))
        # Punch a dark socket before drawing the craft: the corridor floor is
        # black but the walls are purple, and when you are hugging an edge a
        # bare sprite melts into the wall. The socket guarantees separation
        # against either background.
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                put_px(buf, px + dx, py + dy, self.BG)
        for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
            put_px(buf, px + dx, py + dy, color)
        put_px(buf, px, py, (255, 255, 255))          # hot core
        if self.boost > 0:
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                put_px(buf, px + dx, py + dy, self.BOOST_GLOW)

        draw_text3x5(buf, 2, 2, str(self.score), (150, 160, 185))
        return bytes(buf)


class PowderEngine:
    """
    A native reimagining of the downloaded "Powder Game": a small falling-
    sand cellular automaton. Draw with the touchpad (finger position maps
    1:1 onto the grid, same touchpad control surface the fantasy-console
    layer already established for pointer-driven content), physics runs
    every tick as a simple cellular automaton -- sand/water fall and
    settle, stone is inert, fire consumes and spreads to flammable
    neighbours, and steam rises and fades.
    """
    name = "powder"
    tick_rate = 0.05

    BG = (0, 0, 0)
    VOID = 0
    SAND = 1
    WATER = 2
    STONE = 3
    FIRE = 4
    STEAM = 5
    OIL = 6
    PLANT = 7
    LAVA = 8

    COLORS = {
        SAND: (230, 190, 90),
        WATER: (60, 120, 255),
        STONE: (120, 120, 130),
        FIRE: (255, 110, 30),
        STEAM: (150, 160, 175),
        OIL: (150, 80, 190),
        PLANT: (70, 210, 90),
        LAVA: (255, 80, 20),
    }
    # Order the HOLD button cycles through. Fire sits after the two things
    # that actually burn, so the obvious experiment (lay fuel, then light
    # it) is the one the button order walks you into.
    MATERIALS = [SAND, WATER, OIL, PLANT, FIRE, LAVA, STONE]

    # Heavier sinks through lighter. Without this, water poured onto sand
    # just perched on top of it, which is the one behaviour everybody tests
    # first and the one that told you the simulation was fake.
    DENSITY = {SAND: 3, WATER: 2, OIL: 1, LAVA: 2}
    FLAMMABLE = {OIL, PLANT}

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.grid = [[self.VOID] * WIDTH for _ in range(HEIGHT)]
        self.material = self.SAND
        self.erase = False
        self.px, self.py = 0.5, 0.5
        self.pointer_down = False
        self.ticks = 0
        self.score = 0
        self._auto_cd = 0
        self._auto_hold = 0
        self._auto_x = WIDTH // 2
        self._auto_drift = 0

    def pointer(self, fx, fy, down):
        self.px, self.py = fx, fy
        self.pointer_down = down

    def mouse_right(self, held):
        self.erase = held

    def wheel(self, direction):
        # Repurposed as material cycling since a fixed 64x64 grid doesn't
        # need the original's pen-size control.
        i = self.MATERIALS.index(self.material)
        self.material = self.MATERIALS[(i + direction) % len(self.MATERIALS)]

    def input(self, cmd):
        if cmd == "hold":
            i = self.MATERIALS.index(self.material)
            self.material = self.MATERIALS[(i + 1) % len(self.MATERIALS)]
        elif cmd == "rotate":
            self.erase = not self.erase
        elif cmd == "drop":
            self.reset()

    def _place(self, cx, cy, radius=1):
        kind = self.VOID if self.erase else self.material
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy > radius * radius:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                    if self.erase or self.grid[y][x] == self.VOID:
                        self.grid[y][x] = kind

    def auto(self):
        # Sustained pours from one nozzle, not a fresh random x every few
        # ticks. A nozzle that teleports sprays loose pixels across the
        # whole box and never builds anything; holding position lets a real
        # stream form, pile up and slump, which is the entire appeal of
        # watching sand. A slow drift keeps the dunes from stacking into one
        # identical cone every time.
        self._auto_cd -= 1
        if self._auto_cd <= 0:
            self._auto_hold = random.randint(45, 110)
            self._auto_cd = self._auto_hold + random.randint(20, 60)
            self._auto_x = random.randint(6, WIDTH - 7)
            self._auto_drift = random.choice((-1, 0, 0, 1))
            # Only materials that actually fall. STONE is inert, so pouring
            # it from the top just paints a bar hanging in mid-air instead
            # of building anything.
            self.material = random.choice((self.SAND, self.SAND, self.WATER))
        if self._auto_hold > 0:
            self._auto_hold -= 1
            if self._auto_hold % 6 == 0:
                self._auto_x = max(4, min(WIDTH - 5,
                                          self._auto_x + self._auto_drift))
            # Single pixel, so it falls as a stream of grains rather than a
            # 5px clump that drops as one coherent blob.
            self._place(self._auto_x, 2, radius=0)

    def tick(self):
        self.ticks += 1
        if self.pointer_down:
            cx = int(self.px * WIDTH)
            cy = int(self.py * HEIGHT)
            self._place(cx, cy, radius=2)

        g = self.grid
        for y in range(HEIGHT - 2, -1, -1):
            for x in range(WIDTH):
                c = g[y][x]
                if c == self.VOID or c == self.STONE:
                    continue
                if c == self.SAND or c == self.WATER or c == self.OIL or c == self.LAVA:
                    dens = self.DENSITY[c]
                    below = g[y + 1][x]
                    # Fall into empty space, or sink through anything lighter
                    # (sand through water, water under oil).
                    if below == self.VOID:
                        g[y][x], g[y + 1][x] = self.VOID, c
                    elif self.DENSITY.get(below, 9) < dens:
                        g[y][x], g[y + 1][x] = below, c
                    elif c == self.SAND:
                        d = random.choice((-1, 1))
                        for nx in (x + d, x - d):
                            if 0 <= nx < WIDTH and g[y + 1][nx] == self.VOID:
                                g[y][x], g[y + 1][nx] = self.VOID, self.SAND
                                break
                    else:
                        # Liquids seek their own level: look a few tiles out
                        # so a pool actually flattens instead of stacking
                        # into lumps the way a single-step spread does.
                        d = random.choice((-1, 1))
                        moved = False
                        for step in (1, 2, 3):
                            nx = x + d * step
                            if not (0 <= nx < WIDTH) or g[y][nx] != self.VOID:
                                break
                            if y + 1 < HEIGHT and g[y + 1][nx] == self.VOID:
                                g[y][x], g[y + 1][nx] = self.VOID, c
                                moved = True
                                break
                        if not moved:
                            nx = x + d
                            if 0 <= nx < WIDTH and g[y][nx] == self.VOID:
                                g[y][x], g[y][nx] = self.VOID, c

                    if c == self.LAVA:
                        # Lava is the counterpart to fire: it sets solid when
                        # quenched and lights anything that burns.
                        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                            nx, ny = x + dx, y + dy
                            if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                                continue
                            n = g[ny][nx]
                            if n == self.WATER:
                                g[ny][nx] = self.STEAM
                                g[y][x] = self.STONE
                            elif n in self.FLAMMABLE and random.random() < 0.25:
                                g[ny][nx] = self.FIRE

                elif c == self.PLANT:
                    # Grows along water, which gives fire something worth
                    # burning and makes a pond slowly come alive.
                    if random.random() < 0.03:
                        dx, dy = random.choice(((0, 1), (0, -1), (1, 0), (-1, 0)))
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < WIDTH and 0 <= ny < HEIGHT and g[ny][nx] == self.WATER:
                            g[ny][nx] = self.PLANT

                elif c == self.FIRE:
                    burned = False
                    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        nx, ny = x + dx, y + dy
                        if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                            continue
                        n = g[ny][nx]
                        if n in self.FLAMMABLE and random.random() < 0.30:
                            g[ny][nx] = self.FIRE      # fire finally has fuel
                            burned = True
                        elif n == self.WATER:
                            g[y][x] = self.STEAM       # doused
                            burned = True
                            break
                    if g[y][x] == self.FIRE and random.random() < (0.10 if burned else 0.22):
                        g[y][x] = self.STEAM if random.random() < 0.3 else self.VOID

                elif c == self.STEAM:
                    if random.random() < 0.06:
                        g[y][x] = self.WATER if random.random() < 0.25 else self.VOID
                    elif y > 0:
                        nx = x + random.choice((-1, 0, 0, 1))
                        nx = nx if 0 <= nx < WIDTH else x
                        if g[y - 1][nx] == self.VOID:
                            g[y][x], g[y - 1][nx] = self.VOID, self.STEAM

        self.score = sum(1 for row in g for c in row if c not in (self.VOID,))

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        g = self.grid
        for y in range(HEIGHT):
            row = g[y]
            for x in range(WIDTH):
                c = row[x]
                if c != self.VOID:
                    put_px(buf, x, y, self.COLORS[c])
        # Cursor: an open crosshair with a lit centre, in the colour of the
        # material about to be laid down. Against a box that fills up with
        # busy sand and water, four lone pixels vanish -- the arms give it a
        # shape that survives a cluttered background, and tinting it by
        # material means you never have to guess what will pour.
        # Material selector, drawn as a swatch strip along the top with the
        # active one raised and boxed in white. Cycling with a button and
        # showing the result only as a faint cursor tint meant you had to
        # pour something to find out what you had picked -- the selector
        # existed but was effectively invisible.
        n = len(self.MATERIALS)
        sw, pad = 5, 2
        total = n * sw + (n - 1) * pad
        sx0 = (WIDTH - total) // 2
        for i, m in enumerate(self.MATERIALS):
            x0 = sx0 + i * (sw + pad)
            sel = (m == self.material and not self.erase)
            top = 1 if sel else 2
            col = self.COLORS[m]
            for dx in range(sw):
                for dy in range(3 if sel else 2):
                    put_px(buf, x0 + dx, top + dy, col)
            if sel:
                for dx in range(-1, sw + 1):
                    put_px(buf, x0 + dx, top - 1, (255, 255, 255))
                    put_px(buf, x0 + dx, top + 3, (255, 255, 255))
                put_px(buf, x0 - 1, top + 1, (255, 255, 255))
                put_px(buf, x0 + sw, top + 1, (255, 255, 255))
        if self.erase:
            for dx in range(6):
                put_px(buf, WIDTH - 8 + dx, 2, (255, 90, 90))

        cx, cy = int(self.px * WIDTH), int(self.py * HEIGHT)
        ring = (255, 90, 90) if self.erase else self.COLORS[self.material]
        for d in (2, 3):
            put_px(buf, cx - d, cy, ring)
            put_px(buf, cx + d, cy, ring)
            put_px(buf, cx, cy - d, ring)
            put_px(buf, cx, cy + d, ring)
        core = (255, 255, 255) if self.pointer_down else ring
        put_px(buf, cx, cy, core)
        return bytes(buf)


class BrawlerEngine:
    """
    Native platform-brawler built on the mechanic the downloaded Mario Bros
    cart is about: you never stomp enemies from above -- you punch the
    underside of the ledge they're standing on to flip them onto their
    back, then run into them to knock them off the stage. Studied from the
    cart's own source: slippery momentum floors (accel/friction rather
    than instant velocity), a bump that only registers on upward head
    contact, a flip that times out and comes back angrier, and enemies
    that wrap horizontally through the stage edges.

    Rebuilt for 64x64: four tiers instead of a tall scrolling map, tuned so
    a single jump exactly clears one tier.
    """
    name = "brawler"
    tick_rate = 0.033

    BG = (0, 0, 0)
    LEDGE = (55, 70, 165)
    LEDGE_HI = (105, 135, 240)
    YOU = (80, 235, 130)
    ENEMY = (235, 70, 90)
    ENEMY_ANGRY = (255, 120, 60)
    ENEMY_FLIP = (255, 215, 65)
    DEAD = (255, 45, 60)
    INK = (150, 160, 185)
    EYE = (245, 250, 255)

    GRAVITY = 0.34
    JUMP_V = -3.35            # v^2/2g ~= 16.5px -> just clears one 14px tier
    ACCEL = 0.17
    FRICTION = 0.09
    MAX_VX = 1.1
    BUMP_REACH = 9            # how far along the ledge a bump shakes enemies loose

    # (row, x0, x1) -- 1px-thick ledges, 14px apart
    PLATFORMS = [
        (58, 0, 63),
        (44, 3, 25), (44, 38, 60),
        (30, 0, 19), (30, 26, 37), (30, 44, 63),
        (16, 7, 29), (16, 34, 56),
    ]
    SPAWN_LEDGES = [(16, 7, 29), (16, 34, 56), (30, 0, 19), (30, 44, 63),
                    (44, 3, 25), (44, 38, 60)]

    # A shake block, floating where a ground-level jump can reach it. This
    # exists for the same reason the arcade original has one: an enemy on
    # the floor you are standing on cannot be bumped (there is no ledge
    # under it to punch), so without this the lowest tier is an unwinnable
    # dead end. Hitting it flips everything currently standing anywhere.
    POW_ROW, POW_X0, POW_X1 = 50, 28, 35
    POW_CHARGES = 3

    PW, PH = 3, 5             # player is 3 wide, 5 tall (y = feet row)
    EW, EH = 3, 3

    FLIP_TICKS = 150          # how long a flipped enemy stays kickable

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.lives = 3
        self.wave = 1
        self.score = 0
        self.death_flash = 0
        self._impulse = {}
        self._held = set()
        self._respawn()
        self._spawn_wave()

    def _respawn(self):
        self.x = float(WIDTH // 2)
        self.y = 58.0
        self.vx = 0.0
        self.vy = 0.0
        self.on_ground = True
        self.face = 1

    def _spawn_wave(self):
        self.pow_charges = self.POW_CHARGES
        self.pow_flash = 0
        self.enemies = []
        count = min(6, 2 + self.wave)
        speed = 0.22 + 0.05 * min(6, self.wave - 1)
        for i in range(count):
            row, x0, x1 = self.SPAWN_LEDGES[i % len(self.SPAWN_LEDGES)]
            self.enemies.append({
                "x": float(random.randint(x0 + 2, max(x0 + 2, x1 - 2))),
                "y": float(row),
                "x0": x0, "x1": x1,
                "dir": random.choice((-1, 1)),
                "speed": speed,
                "flip": 0,
                "angry": False,
                "falling": False,
                "vy": 0.0,
            })

    # ---- input ---------------------------------------------------------
    def press(self, cmd):
        self._held.add(cmd)
        if cmd == "rotate":
            self._try_jump()

    def release(self, cmd):
        self._held.discard(cmd)

    def input(self, cmd):
        # Tap-based clients (keyboard dashboard, repeat-fire) get a brief
        # hold so momentum still builds instead of stuttering.
        if cmd == "rotate":
            self._try_jump()
        else:
            self._impulse[cmd] = 5

    def _try_jump(self):
        if self.on_ground and not self.death_flash:
            self.vy = self.JUMP_V
            self.on_ground = False

    def _moving(self, cmd):
        return cmd in self._held or self._impulse.get(cmd, 0) > 0

    TIER_GAP = 14

    def auto(self):
        """Play the actual loop: cash in anything flipped on this tier, else
        stand under an enemy one tier up and punch the ledge. Deliberately
        does NOT try to climb -- enemies descend on their own, so waiting on
        a low tier and bumping upward is both the correct strategy and what
        reads clearly as intentional play on the panel."""
        if self.death_flash:
            return
        here = [e for e in self.enemies if abs(e["y"] - self.y) < 3 and not e["falling"]]
        # Only commit to a kick there's actually time to finish -- running at
        # a flip that expires on arrival is the main avoidable death.
        flipped = [e for e in here if e["flip"] > 25]
        if flipped:
            t = min(flipped, key=lambda e: abs(e["x"] - self.x))
            if abs(t["x"] - self.x) * 1.6 < t["flip"]:
                self._walk_toward(t["x"])
                return

        # Anything standing on the ledge directly overhead is bumpable from here.
        above = [e for e in self.enemies
                 if e["flip"] == 0 and not e["falling"]
                 and abs((self.y - self.TIER_GAP) - e["y"]) < 3]
        if above:
            t = min(above, key=lambda e: abs(e["x"] - self.x))
            self._walk_toward(t["x"])
            if abs(t["x"] - self.x) < 5 and self.on_ground:
                self._try_jump()
            return

        # An enemy on THIS tier can't be bumped (nothing under it to punch),
        # so use the shake block -- that's exactly what it's there for.
        threat = [e for e in here if e["flip"] == 0]
        if threat:
            t = min(threat, key=lambda e: abs(e["x"] - self.x))
            pow_cx = (self.POW_X0 + self.POW_X1) / 2
            # Don't walk into it on the way to the block -- retreat first if
            # it's already close, otherwise go shake everything loose.
            if abs(t["x"] - self.x) < 7:
                self._walk_toward(self.x - 12 if t["x"] > self.x else self.x + 12)
            elif self.pow_charges > 0:
                self._walk_toward(pow_cx)
                if abs(pow_cx - self.x) < 4 and self.on_ground:
                    self._try_jump()
            else:
                self._walk_toward(self.x - 12 if t["x"] > self.x else self.x + 12)
            return
        if self.enemies:
            t = min(self.enemies, key=lambda e: abs(e["x"] - self.x))
            self._walk_toward(t["x"])

    def _walk_toward(self, tx):
        if tx < self.x - 1.5:
            self._impulse["left"] = 2
        elif tx > self.x + 1.5:
            self._impulse["right"] = 2

    # ---- physics -------------------------------------------------------
    def _ledge_at(self, feet_from, feet_to, x):
        """The ledge a faller starting at feet_from and ending at feet_to lands on."""
        for row, x0, x1 in self.PLATFORMS:
            if x0 - 1 <= x <= x1 + 1 and feet_from <= row <= feet_to:
                return row, x0, x1
        return None

    def _ledge_overhead(self, head_from, head_to, x):
        for row, x0, x1 in self.PLATFORMS:
            if x0 - 1 <= x <= x1 + 1 and head_to <= row <= head_from:
                return row, x0, x1
        return None

    def tick(self):
        for k in list(self._impulse):
            self._impulse[k] -= 1
            if self._impulse[k] <= 0:
                del self._impulse[k]

        if self.death_flash:
            self.death_flash -= 1
            if self.death_flash == 0:
                if self.lives <= 0:
                    self.reset()
                else:
                    self._respawn()
            return

        # Horizontal: accelerate while held, coast on slick floors.
        if self._moving("left"):
            self.vx = max(-self.MAX_VX, self.vx - self.ACCEL)
            self.face = -1
        elif self._moving("right"):
            self.vx = min(self.MAX_VX, self.vx + self.ACCEL)
            self.face = 1
        else:
            if self.vx > 0:
                self.vx = max(0.0, self.vx - self.FRICTION)
            elif self.vx < 0:
                self.vx = min(0.0, self.vx + self.FRICTION)

        self.x += self.vx
        # Stage wraps horizontally, same as the enemies.
        self.x %= WIDTH

        prev_feet = self.y
        prev_head = self.y - self.PH + 1
        self.vy += self.GRAVITY
        self.y += self.vy
        feet, head = self.y, self.y - self.PH + 1

        if self.vy > 0:
            hit = self._ledge_at(prev_feet, feet, self.x)
            if hit:
                self.y = float(hit[0])
                self.vy = 0.0
                self.on_ground = True
        elif self.vy < 0:
            # The shake block sits below every ledge, so a rising player
            # reaches it first -- check it before the ledges.
            if (prev_head >= self.POW_ROW >= head
                    and self.POW_X0 - 1 <= self.x <= self.POW_X1 + 1):
                self.y = float(self.POW_ROW + self.PH)
                self.vy = 0.0
                self._hit_pow()
            else:
                over = self._ledge_overhead(prev_head, head, self.x)
                if over:
                    row, x0, x1 = over
                    self.y = float(row + self.PH)
                    self.vy = 0.0
                    self._bump(row, x0, x1)
        if self.vy != 0:
            self.on_ground = False

        if self.y > HEIGHT + 6:      # fell off the stage
            self._die()
            return

        self._move_enemies()
        self._collide_enemies()

        if not self.enemies:
            self.wave += 1
            self.score += 500
            self._spawn_wave()

    def _hit_pow(self):
        if self.pow_charges <= 0:
            return
        self.pow_charges -= 1
        self.pow_flash = 10
        for e in self.enemies:
            if not e["falling"] and e["flip"] == 0:
                e["flip"] = self.FLIP_TICKS
                e["dir"] = 1 if e["x"] >= self.x else -1

    def _bump(self, row, x0, x1):
        for e in self.enemies:
            if int(e["y"]) != row:
                continue
            if abs(e["x"] - self.x) <= self.BUMP_REACH:
                if e["flip"] == 0:
                    e["flip"] = self.FLIP_TICKS
                    e["dir"] = 1 if e["x"] >= self.x else -1

    def _move_enemies(self):
        for e in self.enemies:
            if e["flip"] > 0:
                e["flip"] -= 1
                if e["flip"] == 0:
                    e["angry"] = True          # comes back faster, like the original
                    e["speed"] += 0.12
                elif not e["falling"]:
                    # A flipped enemy slides the way it was knocked and
                    # tumbles off the ledge end -- so a flip on the tier
                    # above cascades DOWN to the player instead of being
                    # stranded somewhere they can't reach to kick it.
                    e["x"] += e["dir"] * 0.13
                    if e["x"] < e["x0"] or e["x"] > e["x1"]:
                        if e["x"] < 0 or e["x"] > WIDTH - 1:
                            e["x"] %= WIDTH
                            cont = self._ledge_on_row(int(e["y"]), e["x"])
                            if cont:
                                e["x0"], e["x1"] = cont[1], cont[2]
                            else:
                                self._start_fall(e)
                        else:
                            self._start_fall(e)
                    continue

            if e["falling"]:
                # Enemies traverse DOWN through the stage, like the arcade
                # original -- without this they'd loiter forever on the tier
                # they spawned on and never actually threaten the player.
                prev = e["y"]
                e["vy"] += self.GRAVITY
                e["y"] += e["vy"]
                e["x"] = (e["x"] + e["dir"] * e["speed"] * 0.5) % WIDTH
                land = self._ledge_at(prev, e["y"], e["x"])
                if land:
                    e["y"] = float(land[0])
                    e["x0"], e["x1"] = land[1], land[2]
                    e["vy"] = 0.0
                    e["falling"] = False
                elif e["y"] > HEIGHT + 4:
                    top = self.SPAWN_LEDGES[random.randrange(2)]
                    e["y"] = float(top[0])
                    e["x0"], e["x1"] = top[1], top[2]
                    e["x"] = float(random.randint(top[1] + 2, max(top[1] + 2, top[2] - 2)))
                    e["vy"] = 0.0
                    e["falling"] = False
                continue

            e["x"] += e["dir"] * e["speed"]
            if e["x1"] - e["x0"] >= WIDTH - 1:
                e["x"] %= WIDTH                # full-width floor wraps at the sides
                continue
            if e["x"] < e["x0"] or e["x"] > e["x1"]:
                if e["x"] < 0 or e["x"] > WIDTH - 1:
                    # Walked off the side of the stage: wrap through, and
                    # keep walking if this tier continues on the far side.
                    e["x"] %= WIDTH
                    cont = self._ledge_on_row(int(e["y"]), e["x"])
                    if cont:
                        e["x0"], e["x1"] = cont[1], cont[2]
                        continue
                    self._start_fall(e)
                elif random.random() < 0.45:
                    self._start_fall(e)        # step off the end and drop a tier
                else:
                    e["x"] = float(max(e["x0"], min(e["x1"], e["x"])))
                    e["dir"] = -e["dir"]

    def _ledge_on_row(self, row, x):
        for r, x0, x1 in self.PLATFORMS:
            if r == row and x0 <= x <= x1:
                return r, x0, x1
        return None

    def _start_fall(self, e):
        # Must clear BOTH the ledge row and its horizontal span before the
        # fall begins -- otherwise the very first landing check re-lands the
        # enemy on the ledge it is trying to step off of.
        e["falling"] = True
        e["vy"] = 0.0
        e["y"] = float(int(e["y"])) + 1.2
        e["x"] = (e["x"] + (2.0 if e["dir"] > 0 else -2.0)) % WIDTH

    def _collide_enemies(self):
        for e in list(self.enemies):
            if abs(e["x"] - self.x) < 3 and abs(e["y"] - self.y) < 4:
                if e["flip"] > 0:
                    self.enemies.remove(e)
                    self.score += 200 * self.wave
                else:
                    self._die()
                    return

    def _die(self):
        self.lives -= 1
        self.death_flash = 22

    # ---- render --------------------------------------------------------
    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        for row, x0, x1 in self.PLATFORMS:
            for x in range(x0, x1 + 1):
                put_px(buf, x, row, self.LEDGE_HI)
                put_px(buf, x, row + 1, self.LEDGE)

        if self.pow_flash > 0:
            self.pow_flash -= 1
        spent = self.pow_charges <= 0
        pc = ((255, 255, 255) if self.pow_flash > 0
              else (60, 60, 70) if spent else (255, 215, 65))
        for x in range(self.POW_X0, self.POW_X1 + 1):
            put_px(buf, x, self.POW_ROW, pc)
            put_px(buf, x, self.POW_ROW + 1, pc if self.pow_flash else
                   (40, 40, 48) if spent else (190, 150, 40))
        for i in range(self.pow_charges):
            put_px(buf, self.POW_X0 + 1 + i * 3, self.POW_ROW - 2, (255, 215, 65))

        for e in self.enemies:
            if e["flip"] > 0:
                # Blink faster as the flip is about to wear off.
                blink = e["flip"] < 40 and (e["flip"] // 4) % 2 == 0
                col = self.ENEMY if blink else self.ENEMY_FLIP
            else:
                col = self.ENEMY_ANGRY if e["angry"] else self.ENEMY
            ex, ey = int(e["x"]), int(e["y"])
            if e["flip"] > 0:
                # Belly-up with its legs kicking in the air. The flipped
                # state is the only window in which you can score, so it
                # gets its own silhouette rather than just a colour swap.
                put_px(buf, ex - 1, ey - 2, col)
                put_px(buf, ex + 1, ey - 2, col)
                for dx in (-1, 0, 1):
                    put_px(buf, ex + dx, ey - 1, col)
                    put_px(buf, ex + dx, ey, col)
            else:
                for dx in (-1, 0, 1):
                    put_px(buf, ex + dx, ey - 2, col)      # shell
                    put_px(buf, ex + dx, ey - 1, col)      # body
                put_px(buf, ex - 1, ey, col)               # feet, notched
                put_px(buf, ex + 1, ey, col)
                lead = 1 if e["dir"] >= 0 else -1          # eyes lead the walk
                put_px(buf, ex + (1 if lead > 0 else -1), ey - 1, self.EYE)
                put_px(buf, ex, ey - 1, self.EYE)

        # A figure, not a bar: narrow head offset toward the way you're
        # facing, wide shoulders, split legs. At 3x5 the silhouette is the
        # only thing that can carry "this one is me".
        col = self.DEAD if self.death_flash and self.death_flash % 4 < 2 else self.YOU
        px, py = int(self.x), int(self.y)
        put_px(buf, px, py - 4, col)
        put_px(buf, px + self.face, py - 4, col)            # head
        for dy in (-3, -2, -1):
            for dx in (-1, 0, 1):
                put_px(buf, px + dx, py + dy, col)          # shoulders + torso
        put_px(buf, px - 1, py, col)                        # legs, split
        put_px(buf, px + 1, py, col)
        put_px(buf, px + self.face, py - 4, self.EYE)       # face

        draw_text3x5(buf, 2, 2, str(self.score), self.INK)
        for i in range(self.lives):
            put_px(buf, WIDTH - 3 - i * 3, 3, self.YOU)
            put_px(buf, WIDTH - 4 - i * 3, 3, self.YOU)
        return bytes(buf)


class ChaseEngine:
    """
    Native maze-chase built on the mechanics the downloaded Pacman cart is
    about, read out of its own source: dot-clearing through a walled maze,
    four pursuers each with a *different* targeting rule rather than one
    shared chase, alternating scatter/chase phases on a timer, power
    pellets that invert the threat for a few seconds, and side tunnels that
    wrap you across the stage.

    Rebuilt for 64x64: a 21x21 cell lattice at 3px per cell, which is the
    largest grid where a corridor, a dot and a chaser are all still
    individually readable on the panel.
    """
    name = "chase"
    tick_rate = 0.045

    BG = (0, 0, 0)
    CELL = 3
    GW = GH = 21
    X_OFF, Y_OFF = 0, 1
    # An authored maze, not a generated lattice. A regular grid of corridors
    # is trivial to build but it makes every junction identical, which
    # removes the thing the arcade game is actually about: reading the
    # geometry. Real mazes give you long escape runs, corner pockets that
    # are worth the risk, and dead ends you must never be caught in. Cut to
    # 21x21 so it lands at 63x63 px, and kept left/right symmetric like the
    # original. '#' wall, '.' dot, 'o' power pellet, '-' ghost-house door,
    # 'G' house, 'P' player start; row 8 runs edge to edge as the tunnel.
    MAZE = (
        "#####################",
        "#........#.#........#",
        "#o##.###.#.#.###.##o#",
        "#.##.###.#.#.###.##.#",
        "#...................#",
        "#.##.#.#######.#.##.#",
        "#....#....#....#....#",
        "####.####.#.####.####",
        ".......#.....#.......",
        "#.####.#.#-#.#.####.#",
        "#.#......#G#......#.#",
        "#.####.#.###.#.####.#",
        "#......#..P..#......#",
        "####.####.#.####.####",
        "#....#....#....#....#",
        "#.##.#.#######.#.##.#",
        "#...................#",
        "#.##.###.#.#.###.##.#",
        "#o##.###.#.#.###.##o#",
        "#........#.#........#",
        "#####################",
    )
    TUNNEL_ROW = 8

    WALL = (35, 45, 140)
    WALL_HI = (65, 80, 200)
    DOT = (255, 205, 150)
    PELLET = (255, 255, 255)
    YOU = (255, 226, 60)
    # The fourth chaser is traditionally orange, which at 3px sat only 56
    # colour-units from the player's yellow -- every other chaser is 165+
    # away. Deepened toward true orange so no chaser can be mistaken for
    # you at a glance; the silhouettes differ too, but colour should not be
    # doing the opposite of what shape is saying.
    GHOST_COLS = [(255, 60, 60), (255, 145, 205), (80, 220, 255), (255, 125, 25)]
    EYE = (245, 250, 255)
    # Pale ice blue, not a saturated one. The maze walls are filled blue
    # blocks, and the obvious "scared ghosts are blue" choice landed at
    # luminance 84 against a wall highlight of 85 -- identical brightness,
    # so the one moment you are meant to give chase was the one moment the
    # ghosts stopped reading as actors. Every other actor here sits above
    # 210, so vulnerability is signalled by hue while staying legible.
    FRIGHT = (140, 185, 255)
    FRIGHT_END = (235, 240, 255)
    DEAD = (255, 45, 60)

    SCATTER_TARGETS = [(19, 1), (1, 1), (19, 19), (1, 19)]
    HOUSE = (10, 10)

    PLAYER_SPEED = 0.17
    GHOST_SPEED = 0.145
    FRIGHT_SPEED = 0.095
    EYES_SPEED = 0.34         # eyes outrun everything on the way home
    FRIGHT_TICKS = 140
    PHASES = [(155, "scatter"), (444, "chase")]   # ~7s / ~20s at this tick rate

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.lives = 3
        self.level = 1
        self.score = 0
        self.death_flash = 0
        self._build_dots()
        self._place_actors()

    def _wall(self, cx, cy):
        if cy < 0 or cy >= self.GH:
            return True
        if cx < 0 or cx >= self.GW:
            return cy != self.TUNNEL_ROW          # only the side tunnels leave the stage
        return self.MAZE[cy][cx] == "#"

    def _in_house(self, cx, cy):
        return abs(cx - self.HOUSE[0]) <= 1 and abs(cy - self.HOUSE[1]) <= 1

    def _build_dots(self):
        self.dots = set()
        self.pellets = set()
        for cy in range(self.GH):
            for cx in range(self.GW):
                if self._wall(cx, cy) or self._in_house(cx, cy):
                    continue
                self.dots.add((cx, cy))
        for p in ((1, 1), (self.GW - 2, 1), (1, self.GH - 2), (self.GW - 2, self.GH - 2)):
            if p in self.dots:
                self.dots.discard(p)
                self.pellets.add(p)

    def _place_actors(self):
        self.pcx, self.pcy = 10, 16
        self.dots.discard((self.pcx, self.pcy))
        self.pdir = (0, 0)
        self.pnext = (0, 0)
        self.pt = 0.0
        self.ghosts = []
        for i in range(4):
            self.ghosts.append({
                "cx": self.HOUSE[0], "cy": self.HOUSE[1],
                "dir": (0, -1), "t": 0.0, "kind": i,
                "fright": 0, "release": i * 45, "eaten": 0,
            })
        self.mode_i = 0
        self.mode_t = 0
        # Steady animation clock. mode_t resets on every scatter/chase flip,
        # so driving the chomp off it would stutter at phase boundaries.
        self.anim = 0
        self.combo = 0

    # ---- input ---------------------------------------------------------
    DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

    def press(self, cmd):
        d = self.DIRS.get(cmd)
        if d:
            self.pnext = d

    def release(self, cmd):
        pass   # a maze runner keeps going; direction is a latch, not a hold

    def input(self, cmd):
        self.press(cmd)

    def auto(self):
        """Flood out once, recording the first step toward the nearest of
        each KIND of prize separately, then choose between them.

        A plain nearest-goal search looks reasonable and plays badly: dots
        outnumber power pellets about fifty to one and the pellets sit in
        the far corners, so the closest goal is essentially always a dot.
        The result is that a pellet never gets eaten, frightened mode never
        fires, and an entire system of the game is invisible in ambient
        mode. Pellets therefore have to be worth a deliberate detour --
        a long one when something is closing in.
        """
        threats = [g for g in self.ghosts
                   if not g["eaten"] and g["fright"] <= 0 and g["release"] <= 0]
        nearest_ghost = min((abs(g["cx"] - self.pcx) + abs(g["cy"] - self.pcy)
                             for g in threats), default=99)

        def exits(cx, cy):
            return sum(1 for d in ((0, -1), (0, 1), (-1, 0), (1, 0))
                       if not self._wall(cx + d[0], cy + d[1]))

        def zone(radius, lookahead):
            """Tiles to treat as blocked: a diamond around each chaser plus a
            projection along its heading, since a chaser bearing down covers
            ground you are also trying to cross. With something close, stubs
            are added too -- a one-exit tile is a trap, and walking into one
            to grab a dot is the single most common way a run ends."""
            z = set()
            for g in threats:
                for dx in range(-radius, radius + 1):
                    for dy in range(-radius, radius + 1):
                        if abs(dx) + abs(dy) <= radius:
                            z.add((g["cx"] + dx, g["cy"] + dy))
                for k in range(1, lookahead + 1):
                    z.add((g["cx"] + g["dir"][0] * k, g["cy"] + g["dir"][1] * k))
            if radius >= 2 and nearest_ghost <= 7:
                for cy in range(self.GH):
                    for cx in range(self.GW):
                        if not self._wall(cx, cy) and exits(cx, cy) <= 1:
                            z.add((cx, cy))
            return z

        edible = {(g["cx"], g["cy"]) for g in self.ghosts
                  if g["fright"] > 0 and not g["eaten"]}
        # Shuffled expansion order breaks equal-distance ties differently
        # each run; a fixed order made every ambient run byte-identical.
        dirs = [(0, -1), (0, 1), (-1, 0), (1, 0)]
        random.shuffle(dirs)

        def search(avoid):
            best = {}
            start = (self.pcx, self.pcy)
            seen = {start}
            queue = deque([(start, None, 0)])
            while queue:
                (cx, cy), first, dist = queue.popleft()
                if first:
                    if (cx, cy) in edible:
                        best.setdefault("hunt", (dist, first))
                    elif (cx, cy) in self.pellets:
                        best.setdefault("pellet", (dist, first))
                    elif (cx, cy) in self.dots:
                        best.setdefault("dot", (dist, first))
                    if len(best) == 3:
                        break
                for d in dirs:
                    nx, ny = cx + d[0], cy + d[1]
                    if self._wall(nx, ny):
                        continue
                    nx %= self.GW
                    if (nx, ny) in seen or (nx, ny) in avoid:
                        continue
                    seen.add((nx, ny))
                    queue.append(((nx, ny), first or d, dist + 1))
            return best

        # Give ground reluctantly. Dropping straight from "avoid a wide
        # berth" to "ignore chasers entirely" is what made this walk into
        # ghosts and die every few seconds; each step here concedes a little
        # margin, and even the tightest still refuses to enter a chaser's
        # own tile.
        best = None
        for radius, lookahead in ((3, 3), (2, 2), (1, 1), (0, 0)):
            best = search(zone(radius, lookahead))
            if best:
                break
        if not best:
            # Boxed in. Take the legal move with the most clearance rather
            # than keeping a stale direction into a wall or a chaser.
            opts = [d for d in dirs if self._legal(self.pcx, self.pcy, d)]
            if opts and threats:
                def clearance(d):
                    nx, ny = (self.pcx + d[0]) % self.GW, self.pcy + d[1]
                    return min(abs(g["cx"] - nx) + abs(g["cy"] - ny)
                               for g in threats)
                self.pnext = max(opts, key=clearance)
            elif opts:
                self.pnext = opts[0]
            return

        if "hunt" in best:                      # edible chaser: worth the most
            self.pnext = best["hunt"][1]
            return

        # Something is right on top of us: stop collecting and open up the
        # gap. Goal-seeking alone will happily take a dot that costs a tile
        # of clearance, and at these speeds one tile is the whole margin.
        if nearest_ghost <= 3:
            opts = [d for d in dirs if self._legal(self.pcx, self.pcy, d)]
            if opts:
                def safety(d):
                    nx, ny = (self.pcx + d[0]) % self.GW, self.pcy + d[1]
                    room = min(abs(g["cx"] - nx) + abs(g["cy"] - ny)
                               for g in threats)
                    # Prefer keeping options open, and take a dot only when
                    # it costs nothing.
                    return (room, exits(nx, ny), (nx, ny) in self.dots)
                self.pnext = max(opts, key=safety)
                return
        pel, dot = best.get("pellet"), best.get("dot")
        # Detour for a pellet when the coast is clear. Under pressure take
        # it only if it is essentially on the way -- crossing the board for
        # a pellet with something on your tail is how runs used to end.
        detour = 3 if nearest_ghost <= 5 else 10
        if pel and (not dot or pel[0] <= dot[0] + detour):
            self.pnext = pel[1]
        else:
            self.pnext = (dot or pel)[1]

    # ---- simulation ----------------------------------------------------
    def _legal(self, cx, cy, d):
        return not self._wall(cx + d[0], cy + d[1])

    def tick(self):
        if self.death_flash:
            self.death_flash -= 1
            if self.death_flash == 0:
                if self.lives <= 0:
                    self.reset()
                else:
                    self._place_actors()
            return

        self._advance_mode()
        self._move_player()
        self._move_ghosts()
        self._resolve_contact()

        if not self.dots and not self.pellets:
            self.level += 1
            self.score += 1000
            self._build_dots()
            self._place_actors()

    def _advance_mode(self):
        self.anim += 1
        self.mode_t += 1
        if self.mode_t >= self.PHASES[self.mode_i % len(self.PHASES)][0]:
            self.mode_t = 0
            self.mode_i += 1
            # Every chaser turns around the instant the wave flips. This is
            # a signature rule of the original and it is what telegraphs the
            # switch to the player: the pack visibly breaking off is the cue
            # that a scatter has started, and vice versa.
            for g in self.ghosts:
                if not g["eaten"] and g["fright"] <= 0:
                    g["dir"] = (-g["dir"][0], -g["dir"][1])

    def _mode(self):
        return self.PHASES[self.mode_i % len(self.PHASES)][1]

    def _move_player(self):
        # Instant reversal is allowed mid-corridor, like the original --
        # it's what makes tunnel juking feel fair.
        if self.pnext and self.pdir and self.pnext == (-self.pdir[0], -self.pdir[1]):
            self.pcx = (self.pcx + self.pdir[0]) % self.GW
            self.pcy += self.pdir[1]
            self.pdir = self.pnext
            self.pt = 1.0 - self.pt
            # Reversing carries you into a new tile, so it has to eat what
            # is there. Without this, doubling back over a dot silently
            # skipped it -- the tile was entered but never consumed, and
            # the board could never be cleared from that square.
            self._eat()

        if self.pt == 0.0:
            if self.pnext and self._legal(self.pcx, self.pcy, self.pnext):
                self.pdir = self.pnext
            elif not (self.pdir and self._legal(self.pcx, self.pcy, self.pdir)):
                self.pdir = (0, 0)

        if self.pdir == (0, 0):
            return
        self.pt += self.PLAYER_SPEED
        while self.pt >= 1.0:
            self.pt -= 1.0
            self.pcx = (self.pcx + self.pdir[0]) % self.GW
            self.pcy += self.pdir[1]
            self._eat()
            if self.pnext and self._legal(self.pcx, self.pcy, self.pnext):
                self.pdir = self.pnext
            elif not self._legal(self.pcx, self.pcy, self.pdir):
                self.pdir = (0, 0)
                self.pt = 0.0
                break

    def _eat(self):
        cell = (self.pcx, self.pcy)
        if cell in self.dots:
            self.dots.discard(cell)
            self.score += 10
        elif cell in self.pellets:
            self.pellets.discard(cell)
            self.score += 50
            self.combo = 0
            for g in self.ghosts:
                if g["release"] <= 0:
                    g["fright"] = self.FRIGHT_TICKS
                    g["dir"] = (-g["dir"][0], -g["dir"][1])

    def _target(self, g):
        pcell = (self.pcx, self.pcy)
        if g["eaten"]:
            return self.HOUSE                # eyes navigate straight home
        if g["fright"] > 0:
            return None                      # frightened chasers wander
        if self._mode() == "scatter":
            return self.SCATTER_TARGETS[g["kind"]]
        k = g["kind"]
        if k == 0:
            return pcell
        if k == 1:
            return (self.pcx + self.pdir[0] * 4, self.pcy + self.pdir[1] * 4)
        if k == 2:
            red = self.ghosts[0]
            ax, ay = self.pcx + self.pdir[0] * 2, self.pcy + self.pdir[1] * 2
            return (2 * ax - red["cx"], 2 * ay - red["cy"])
        dist = abs(self.pcx - g["cx"]) + abs(self.pcy - g["cy"])
        return pcell if dist > 8 else self.SCATTER_TARGETS[3]

    def _move_ghosts(self):
        for g in self.ghosts:
            if g["release"] > 0:
                g["release"] -= 1
                continue
            if g["fright"] > 0:
                g["fright"] -= 1
            if g["eaten"]:
                speed = self.EYES_SPEED          # eyes hurry home
            elif g["fright"] > 0:
                speed = self.FRIGHT_SPEED
            else:
                speed = self.GHOST_SPEED
                if g["cy"] == self.TUNNEL_ROW:
                    # Chasers drag through the side tunnel; the player does
                    # not. That asymmetry is what makes the tunnel a genuine
                    # escape route rather than just another corridor.
                    speed *= 0.55
            g["t"] += speed
            while g["t"] >= 1.0:
                g["t"] -= 1.0
                g["cx"] = (g["cx"] + g["dir"][0]) % self.GW
                g["cy"] += g["dir"][1]
                if g["eaten"] and (g["cx"], g["cy"]) == self.HOUSE:
                    g["eaten"] = 0               # reassembled; wait to re-enter
                    g["release"] = 25
                    g["dir"] = (0, -1)
                    g["t"] = 0.0
                    break
                g["dir"] = self._choose_dir(g)

    def _choose_dir(self, g):
        back = (-g["dir"][0], -g["dir"][1])
        options = [d for d in ((0, -1), (-1, 0), (0, 1), (1, 0))
                   if self._legal(g["cx"], g["cy"], d) and d != back]
        if not options:
            return back if self._legal(g["cx"], g["cy"], back) else g["dir"]
        target = self._target(g)
        if target is None:
            return random.choice(options)
        def dist(d):
            nx, ny = g["cx"] + d[0], g["cy"] + d[1]
            return (nx - target[0]) ** 2 + (ny - target[1]) ** 2
        return min(options, key=dist)

    def _resolve_contact(self):
        for g in self.ghosts:
            if g["release"] > 0:
                continue
            if g["eaten"]:
                continue                        # a pair of eyes is harmless
            if g["cx"] == self.pcx and g["cy"] == self.pcy:
                if g["fright"] > 0:
                    self.combo += 1
                    # 200/400/800/1600 across one pellet, as in the original:
                    # the reward for chaining doubles rather than adding, which
                    # is what makes clearing all four worth the risk.
                    self.score += 200 * (2 ** (self.combo - 1))
                    g["fright"] = 0
                    g["eaten"] = 1              # send the eyes home, don't teleport
                    g["t"] = 0.0
                else:
                    self.lives -= 1
                    self.death_flash = 22
                    return

    # ---- render --------------------------------------------------------
    def _px(self, cx, cy, d, t):
        x = (cx + d[0] * t) * self.CELL + self.X_OFF + 1
        y = (cy + d[1] * t) * self.CELL + self.Y_OFF + 1
        return int(round(x)), int(round(y))

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        for cy in range(self.GH):
            for cx in range(self.GW):
                if not self._wall(cx, cy):
                    continue
                bx = cx * self.CELL + self.X_OFF
                by = cy * self.CELL + self.Y_OFF
                for dy in range(self.CELL):
                    for dx in range(self.CELL):
                        col = self.WALL_HI if dy == 0 else self.WALL
                        put_px(buf, bx + dx, by + dy, col)

        for (cx, cy) in self.dots:
            x, y = self._px(cx, cy, (0, 0), 0)
            put_px(buf, x, y, self.DOT)
        pulse = (self.mode_t // 6) % 2 == 0
        for (cx, cy) in self.pellets:
            x, y = self._px(cx, cy, (0, 0), 0)
            put_px(buf, x, y, self.PELLET)
            if pulse:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    put_px(buf, x + dx, y + dy, self.PELLET)

        # Ghosts: domed top, eyed body, notched "skirt" feet. The notch is
        # the whole point -- at 3px a plus-shape ghost and a plus-shape
        # player are the same silhouette in different hues, so from across
        # the room you cannot tell which one is you. Shape carries the
        # meaning here; colour only says *which* ghost.
        for g in self.ghosts:
            gx, gy = self._px(g["cx"], g["cy"], g["dir"], g["t"])
            if g["eaten"]:
                # Just a pair of eyes hurrying home. Drawing the body here
                # would read as a live chaser and send you running from
                # something that cannot hurt you.
                put_px(buf, gx - 1, gy, self.EYE)
                put_px(buf, gx + 1, gy, self.EYE)
                continue
            if g["fright"] > 0:
                ending = g["fright"] < 40 and (g["fright"] // 4) % 2 == 0
                col = self.FRIGHT_END if ending else self.FRIGHT
            else:
                col = self.GHOST_COLS[g["kind"]]
            for dx in (-1, 0, 1):
                put_px(buf, gx + dx, gy - 1, col)      # dome
                put_px(buf, gx + dx, gy, col)          # body
            put_px(buf, gx - 1, gy + 1, col)           # left foot
            put_px(buf, gx + 1, gy + 1, col)           # right foot
            if g["fright"] <= 0:
                # Eyes look the way it's travelling, so you can read a
                # ghost's intent a beat before it turns.
                ex = g["dir"][0]
                put_px(buf, gx - 1 + (1 if ex > 0 else 0), gy, self.EYE)
                put_px(buf, gx + 1 - (1 if ex < 0 else 0), gy, self.EYE)

        # Player: a solid round body with an animated wedge bite on the
        # side being faced -- a filled silhouette against notched ones.
        col = self.DEAD if self.death_flash and self.death_flash % 4 < 2 else self.YOU
        px, py = self._px(self.pcx, self.pcy, self.pdir, self.pt)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                put_px(buf, px + dx, py + dy, col)
        d = self.pdir if self.pdir != (0, 0) else (1, 0)
        if (self.anim // 4) % 2 == 0:
            perp = (d[1], d[0])
            put_px(buf, px + d[0], py + d[1], self.BG)
            put_px(buf, px + d[0] + perp[0], py + d[1] + perp[1], self.BG)

        for i in range(max(0, self.lives)):
            put_px(buf, 1 + i * 3, 0, self.YOU)
            put_px(buf, 2 + i * 3, 0, self.YOU)
        return bytes(buf)


PAUSE_ITEMS = ("RESUME", "RESTART", "MENU")


def draw_pause_overlay(frame, sel, pulse):
    """Dim the live game and lay the pause menu over it.

    The frozen game stays visible underneath on purpose: you pause to look
    at the board, so covering it with an opaque card would defeat the point.
    Dimming rather than blanking also makes it obvious at a glance that the
    panel is paused and not simply stuck on a still image.
    """
    buf = bytearray(frame)
    for i in range(0, len(buf), 3):
        buf[i] = (buf[i] * 45) >> 8
        buf[i + 1] = (buf[i + 1] * 45) >> 8
        buf[i + 2] = (buf[i + 2] * 45) >> 8

    x0, x1 = 5, WIDTH - 6
    y0, y1 = 9, HEIGHT - 10
    for x in range(x0, x1 + 1):          # card, so the text never fights the art
        for y in range(y0, y1 + 1):
            i = (y * WIDTH + x) * 3
            buf[i] = buf[i] // 3
            buf[i + 1] = buf[i + 1] // 3
            buf[i + 2] = buf[i + 2] // 3
    edge = (120, 130, 155)
    for x in range(x0, x1 + 1):
        put_px(buf, x, y0, edge)
        put_px(buf, x, y1, edge)
    for y in range(y0, y1 + 1):
        put_px(buf, x0, y, edge)
        put_px(buf, x1, y, edge)

    title = "PAUSED"
    draw_text3x5(buf, (WIDTH - (4 * len(title) - 1)) // 2, y0 + 4,
                 title, (235, 240, 255))

    glow = 0.55 + 0.45 * abs((pulse % 24) - 12) / 12
    for i, text in enumerate(PAUSE_ITEMS):
        ty = y0 + 14 + i * 9
        on = (i == sel)
        ink = (tuple(min(255, int(v * glow)) for v in (255, 226, 60))
               if on else (140, 148, 170))
        tw = 4 * len(text) - 1
        tx = (WIDTH - tw) // 2
        if on:
            for x in range(tx - 3, tx + tw + 3):
                put_px(buf, x, ty - 2, (70, 62, 20))
                put_px(buf, x, ty + 6, (70, 62, 20))
            put_px(buf, tx - 3, ty + 2, ink)
            put_px(buf, tx + tw + 2, ty + 2, ink)
        draw_text3x5(buf, tx, ty, text, ink)
    return bytes(buf)


def detect_cart_control_scheme(cart_bytes):
    """'gamepad' (d-pad + A/B/X) or 'pointer' (touchpad + right-click/wheel).

    TIC-80 carts don't carry structured input metadata we can rely on --
    the closest thing is the same convention TIC-80 itself uses for a
    handful of run modes: a "-- input: mouse" comment near the top of the
    code chunk. Most carts (including some that use the mouse) don't
    bother setting it, so this falls back to what the source actually
    calls: a cart with no btn()/btnp() calls but real mouse() calls is
    clearly built around a pointer, not a d-pad.
    """
    idx = cart_bytes.find(b"-- title:")
    text = cart_bytes[idx:idx + 8000].decode("latin1", "ignore") if idx != -1 else ""
    if "input: mouse" in text or "input:mouse" in text:
        return "pointer"
    if "mouse(" in text and "btn(" not in text and "btnp(" not in text:
        return "pointer"
    return "gamepad"


def scan_tic_carts():
    """Every .tic file dropped in carts/tic80/, sorted by name. The label
    shown in the menu is just the filename (upper-cased, extension off) --
    carts don't reliably self-describe a short display title, and the
    filename is the one thing you control when you drop a cart in."""
    if not CARTS_DIR.is_dir():
        return []
    return sorted(p for p in CARTS_DIR.glob("*.tic") if p.is_file())


class TicCartEngine:
    """
    Hosts one TIC-80 cart through the headless libretro core (tic80_core.py)
    and presents it through the exact same contract every native engine
    uses: reset() / input() / auto() / tick() / frame() / .score / .tick_rate.
    The rest of the system -- MenuEngine's launch hook, arcade_server's
    render loop, DDP push -- never needs to know a cart engine is any
    different from SnakeEngine.

    Held-button state (not just discrete taps) matters for arbitrary carts
    -- a platformer needs to see a jump button actually held down, not one
    pulse per phone tap -- so this is driven by press()/release() rather
    than input(cmd). arcade_server calls press/release when present and
    falls back to input() for engines that don't define them (i.e. every
    native game keeps working exactly as before).
    """

    tick_rate = 1 / 60  # TIC-80's own native frame rate; PANEL_FPS still
                         # caps what actually reaches the panel, same as
                         # every other engine.

    # Phone d-pad/action verbs -> TIC-80 joypad button ids.
    #
    # IMPORTANT: the libretro core deliberately SWAPS A/B and X/Y before
    # handing them to TIC-80 ("A/B and X/Y are switched in TIC-80" --
    # tic80_libretro.c, tic80_libretro_update_gamepad). TIC-80's own
    # scripting API numbers gamepad buttons 4=A, 5=B, 6=X, 7=Y, and that is
    # what carts actually check (e.g. Mario Bros checks btnp(4) for jump).
    # The core wires those up from the OPPOSITE libretro id:
    #   TIC-80 A (btn 4) <- RETRO_DEVICE_ID_JOYPAD_B
    #   TIC-80 B (btn 5) <- RETRO_DEVICE_ID_JOYPAD_A
    #   TIC-80 X (btn 6) <- RETRO_DEVICE_ID_JOYPAD_Y
    #   TIC-80 Y (btn 7) <- RETRO_DEVICE_ID_JOYPAD_X
    # So to make our on-screen "A" (rotate, the primary/most-used face
    # button) actually land on the cart's own A button, we must send
    # JOYPAD_B -- sending JOYPAD_A here would silently hit the cart's B
    # instead, and every cart's jump/confirm button would never fire.
    BUTTON_MAP = {
        "up": tic80_core.RETRO_DEVICE_ID_JOYPAD_UP,
        "down": tic80_core.RETRO_DEVICE_ID_JOYPAD_DOWN,
        "left": tic80_core.RETRO_DEVICE_ID_JOYPAD_LEFT,
        "right": tic80_core.RETRO_DEVICE_ID_JOYPAD_RIGHT,
        "rotate": tic80_core.RETRO_DEVICE_ID_JOYPAD_B,   # -> TIC-80 A
        "drop": tic80_core.RETRO_DEVICE_ID_JOYPAD_A,     # -> TIC-80 B
        "hold": tic80_core.RETRO_DEVICE_ID_JOYPAD_Y,     # -> TIC-80 X
    }

    _core = None  # one libretro core instance shared process-wide

    def __init__(self, cart_path):
        self.cart_path = Path(cart_path)
        self.score = 0
        self.launch = None
        self._held = {}
        self._pointer_fx, self._pointer_fy = 0.5, 0.5
        self._pointer_down = False
        self._mouse_right = False
        self._wheel = 0
        if TicCartEngine._core is None:
            TicCartEngine._core = tic80_core.TicCore()
        self._core = TicCartEngine._core
        cart_bytes = self.cart_path.read_bytes()
        self._core.load(cart_bytes, path=self.cart_path.name)
        self.control_scheme = detect_cart_control_scheme(cart_bytes)

    def reset(self):
        pass  # cart state resets by reloading a fresh TicCartEngine instance

    def press(self, cmd):
        btn = self.BUTTON_MAP.get(cmd)
        if btn is not None:
            self._held[btn] = True

    def release(self, cmd):
        btn = self.BUTTON_MAP.get(cmd)
        if btn is not None:
            self._held[btn] = False

    def input(self, cmd):
        # Fallback for callers that only know the tap-based contract (e.g.
        # the old /api/input path) -- treat a bare tap as a brief press.
        self.press(cmd)

    def pointer(self, fx, fy, down):
        """fx, fy: fraction (0..1) of the touchpad -- absolute position,
        matching the drawable canvas 1:1, for mouse-driven carts."""
        self._pointer_fx, self._pointer_fy, self._pointer_down = fx, fy, down

    def mouse_right(self, held):
        self._mouse_right = held

    def wheel(self, direction):
        self._wheel = direction

    def auto(self):
        pass  # no demo/AI mode for arbitrary carts

    def tick(self):
        self._core.set_buttons(self._held)
        self._core.set_pointer(self._pointer_fx, self._pointer_fy, self._pointer_down)
        self._core.set_mouse_right(self._mouse_right)
        if self._wheel:
            self._core.pulse_wheel(self._wheel)
            self._wheel = 0
        self._core.tick()

    def frame(self):
        w, h, pitch, buf = self._core.raw_frame()
        out = bytearray(WIDTH * HEIGHT * 3)
        if buf:
            sx, sy = w / WIDTH, h / HEIGHT
            for oy in range(HEIGHT):
                srow = (int(oy * sy)) * pitch
                orow = oy * WIDTH * 3
                for ox in range(WIDTH):
                    off = srow + int(ox * sx) * 4
                    o = orow + ox * 3
                    # XRGB8888 little-endian bytes are B,G,R,X
                    out[o] = buf[off + 2]
                    out[o + 1] = buf[off + 1]
                    out[o + 2] = buf[off]
        return bytes(out)



# =============================================================================
# GAME DAY -- the EVENT / TAKEOVER mode.
#
# A different CATEGORY of mode from everything above, and worth stating
# plainly because future modes should pick a side deliberately:
#
#   DATA MODES (flights, ISS, weather, sports, news, blog) are GLANCE
#   modes. They assume they are sharing the panel with a rotation, they
#   get a slice of attention, and ambient's ident layer deliberately
#   strips them back to one fact each.
#
#   GAME DAY assumes the opposite. It is opt-in, it is about ONE event,
#   and while it is on nothing else competes for the panel. So it is
#   allowed to be maximally detailed and maximally dramatic -- the things
#   a glance mode must not be. It does not appear in ambient's SEQUENCE
#   (ambient is a rotation; a takeover cannot take a turn), and it hands
#   the panel back on its own when the event is genuinely over.
#
# The ONLY thing that may interrupt it is the global severe-weather
# takeover, and that comes for free: arcade_server composites the alert
# over whatever the current mode drew, after this engine has run.
# =============================================================================
def draw_event_frame(buf, intensity=0.0, accent=GAMEDAY_ACCENT, edge_col=GAMEDAY_GOLD):
    """The EVENT visual language: the panel is FRAMED on all four edges
    rather than given a single top rule like the data modes.

    This is the signature shared by GAME DAY and by the sports mode's
    expanded single-event view -- one language for "this is a whole event,
    not a row in a list", rather than a third style invented per surface.

    `intensity` 0..1 brightens and thickens the frame. Cheap: a handful of
    edge writes, no per-pixel pass over the buffer.
    """
    k = 0.55 + 0.45 * max(0.0, min(1.0, intensity))
    c = rim(accent, k)
    edge = rim(edge_col, k)
    for x in range(WIDTH):
        put_px(buf, x, 0, edge)
        put_px(buf, x, 1, c)
        put_px(buf, x, HEIGHT - 1, edge)
        put_px(buf, x, HEIGHT - 2, c)
    for y in range(HEIGHT):
        put_px(buf, 0, y, edge)
        put_px(buf, WIDTH - 1, y, edge)
    # A second inner rule only at high intensity, so escalation is visible
    # as the frame literally closing in.
    if intensity > 0.6:
        for x in range(2, WIDTH - 2):
            put_px(buf, x, 2, rim(c, 0.5))
            put_px(buf, x, HEIGHT - 3, rim(c, 0.5))
    return buf


class GameDayEngine(Browsable):
    """One event, given the whole panel.

    Two targets, chosen in gameday_config.json:
      "ufc"  -- the next/current UFC card, from mma.FEED
      "team" -- the pinned favourite team's game, from sports.FEED

    Pure, like every other engine here: no I/O, reads whatever the feeds
    have already cached.

    UFC VIEWS. A card is a night with a shape, so the mode tracks where
    the night stands rather than showing one static thing:
      * CARD      -- how far through the card we are (N of M), always
                     available so there is a sense of the night's arc
      * UPCOMING  -- who is about to fight, with records. The main event
                     gets its own treatment.
      * RESULT    -- a fight just ended. This is the payoff and it
                     preempts everything else.

    RESULT PACING. A finish is the single most time-critical thing this
    project renders, so the moment a fight completes the view switches to
    it immediately and HOLDS for RESULT_TICKS (~22s) before returning to
    the rotation. The reasoning: a result is read in about two seconds but
    is worth sitting with -- cutting away fast makes it feel like a stat
    line scrolling past, which is exactly what the brief said not to
    build. Long enough to be an occasion, short enough that the card
    progress is not stale by the time it returns.

    Results only fire for fights that finish WHILE WATCHING. Loading a
    card that is already 9 fights deep must not replay nine finishes --
    same first-value rule as Pulse.
    """

    name = "gameday"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (86, 94, 116)
    HERO = (245, 248, 255)
    ACCENT = GAMEDAY_ACCENT
    GOLD = GAMEDAY_GOLD
    LIVE = (90, 230, 120)

    RESULT_TICKS = 440       # ~22s holding a finish
    VIEW_TICKS = 260         # ~13s per rotating view otherwise
    EXPIRE_TICKS = 1200      # ~60s on the final result, then hand the panel back

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"card": None, "next_label": None, "next_date": None,
                     "age": None, "err": None}
        self.sports_data = {}
        self.ticks = 0
        self.hold = 0
        self.view = 0             # index into the rotating views
        self.sel = None           # browsed fight index, None = follow live
        self.result_t = 0         # ticks left holding a finish
        self.result_fight = None
        self.done_t = 0           # ticks the whole event has been over
        self.scroll = 0.0         # card-name marquee offset
        self._seen_done = None    # ids of finished fights, None until first read
        self.pulse = Pulse(ticks=26)      # louder than the standard 14
        self.cfg = gameday.load_config()
        self.launch = None
        self._init_scroll()

    # ---- contract --------------------------------------------------------
    def has_content(self):
        """Never in ambient's rotation (a takeover cannot take a turn), but
        the contract is honoured so nothing has to special-case it."""
        return False

    def expired(self):
        """True once the event is genuinely over and has been shown as
        over for a while. arcade_server polls this to hand the panel back
        to the resting mode -- that is what makes this a takeover with an
        end rather than a mode you must remember to leave."""
        if not self.cfg.get("auto_exit", True):
            return False
        return self.done_t >= self.EXPIRE_TICKS

    # ---- browse ----------------------------------------------------------
    def _fights(self):
        card = self.data.get("card") or {}
        return card.get("fights") or []

    def _displayed_index(self):
        """Index of the fight actually ON SCREEN right now.

        Not the same as the live index: while a result is being held, the
        screen is showing the fight that just ENDED, not the one about to
        start. Browsing has to step from what you can see, or the first
        tap appears to skip a fight.
        """
        if self.result_t > 0 and self.result_fight:
            return self.result_fight["index"]
        return self.sel if self.sel is not None else self._live_index()

    def _step(self, direction):
        """Universal scroll control: step through the card fight by fight.
        Same tap/hold contract as every other browsable mode."""
        fights = self._fights()
        if not fights:
            return
        self.sel = (self._displayed_index() + direction) % len(fights)
        self.hold = 0
        self.result_t = 0            # browsing takes manual control

    def input(self, cmd):
        if self._browse_input(cmd):
            return
        if cmd in ("rotate", "drop"):
            # Snap back to following the live fight.
            self.sel = None
            self.hold = 0

    def auto(self):
        pass

    # ---- state -----------------------------------------------------------
    def _live_index(self):
        """The fight the night is currently ON: the one in progress, else
        the first not yet fought, else the last (card over)."""
        fights = self._fights()
        if not fights:
            return 0
        for f in fights:
            if f["state"] == "in":
                return f["index"]
        for f in fights:
            if f["state"] == "pre":
                return f["index"]
        return fights[-1]["index"]

    def _current(self):
        fights = self._fights()
        if not fights:
            return None
        i = self.sel if self.sel is not None else self._live_index()
        return fights[max(0, min(len(fights) - 1, i))]

    def tick(self):
        self.ticks += 1
        self.scroll += 0.35
        self._scroll_tick()
        self.cfg = gameday.load_config()

        if self.cfg.get("target") == "team":
            self.sports_data = sports.FEED.get()
            self._tick_team()
            return

        self.data = mma.FEED.get()
        card = self.data.get("card")
        fights = self._fights()
        self.score = (card or {}).get("done", 0)

        # New finishes -> the result view, but never on first read.
        done_ids = {f["id"] for f in fights if f["state"] == "post"}
        if self._seen_done is None:
            self._seen_done = done_ids          # first read: adopt, don't replay
        elif done_ids - self._seen_done:
            newest = max(done_ids - self._seen_done,
                         key=lambda i: next(f["index"] for f in fights if f["id"] == i))
            self.result_fight = next(f for f in fights if f["id"] == newest)
            self.result_t = self.RESULT_TICKS
            self.sel = None
            self._seen_done = done_ids
        else:
            self._seen_done = done_ids

        if self.result_t > 0:
            self.result_t -= 1
            self.pulse.note(("result", self.result_fight["id"]))
        else:
            self.pulse.note(("card", (card or {}).get("done")))

        # Declare fight-statistics interest for whatever fight is ON
        # SCREEN, and only while it's live or just finished -- a fight
        # that hasn't started yet has nothing but honest zeros ESPN
        # itself hasn't started counting, so there's no call worth making
        # for it. See mma.UfcFeed.want_stats().
        disp = self._displayed_index()
        df = next((x for x in fights if x["index"] == disp), None)
        if df and df["state"] in ("in", "post") and card:
            aids = [fr.get("id") for fr in df.get("fighters") or []]
            mma.FEED.want_stats(df["id"], card["id"], df["id"], aids,
                                live=(df["state"] == "in"))

        # Event over -> start the exit clock.
        if card and card.get("completed"):
            self.done_t += 1
        else:
            self.done_t = 0
        self._maybe_exit()

        # Rotate the non-result views on the normal cadence, unless the
        # viewer is browsing (universal scroll control pauses auto-advance).
        # Order is deliberate: UPCOMING first (who's fighting, the thing
        # you'd want on arrival), then STATS (how it's going, once there's
        # something to say), then CARD (where the night stands overall,
        # the "zoom out" view) -- a natural drill-down/pull-back rhythm
        # rather than an arbitrary index order.
        if self.result_t <= 0 and self.browse.auto_ok and self.sel is None:
            self.hold += 1
            if self.hold >= self.VIEW_TICKS:
                self.hold = 0
                self.view = (self.view + 1) % 3

    def _maybe_exit(self):
        """Hand the panel back once the event is over.

        Reuses the SAME `.launch` hand-off that BootEngine and MenuEngine
        already use -- the render loop picks it up and calls set_mode --
        so a takeover that ends needs no special case anywhere in the
        server. This is what makes GAME DAY a takeover with an end rather
        than a mode you have to remember to leave.
        """
        if self.expired():
            self.launch = RESTING_MODE

    def _tick_team(self):
        g = (self.sports_data or {}).get("favorite_game")
        self.score = 0
        if g:
            self.score = (g["home"].get("score") or 0) + (g["away"].get("score") or 0)
            self.pulse.note((g["home"].get("score"), g["away"].get("score")))
            self.done_t = self.done_t + 1 if g["state"] == "post" else 0
        else:
            self.done_t = 0
        self._maybe_exit()

    # ---- shared chrome ---------------------------------------------------
    def _occasion_frame(self, buf, intensity=0.0):
        return draw_event_frame(buf, intensity, self.ACCENT, self.GOLD)

    def _kicker(self, buf, text, color=None):
        draw_text_centered(buf, 6, fit_text(text, WIDTH - 8),
                           color or color_on_dark(self.ACCENT), x_min=3)

    def _name(self, buf, y, name, color, big_ok=True):
        """Fighter/team name at the largest scale that actually fits.
        Never truncates a name to keep a size -- losing characters loses
        WHO, losing size only loses emphasis."""
        name = str(name or "")
        scale = 2 if (big_ok and text_w(name, 2) <= WIDTH - 8) else 1
        draw_text_centered(buf, y, fit_text(name, WIDTH - 8, scale), color,
                           scale=scale, x_min=3)
        return scale

    # ---- UFC views -------------------------------------------------------
    def _frame_result(self, f):
        """A finish, as a highlight rather than a stat line.

        Hierarchy is the point: the winner and the METHOD dominate, and
        the round/time is supporting detail. "KO/TKO" reads from across a
        room; the timestamp is for when you walk over.

        The method is often the physically largest element rather than the
        name, and that is a deliberate consequence of never truncating a
        name: "M. ANKALAEV" does not fit at scale 2, so it drops to scale
        1 instead of losing letters. Losing size costs emphasis; losing
        letters costs WHO WON, which is the entire point of the view.
        """
        buf = blank(); fill(buf, self.BG)
        flashing = self.pulse.on
        self._occasion_frame(buf, 1.0 if flashing else 0.75)

        method = f.get("method")
        short = mma.METHOD_SHORT.get(method, method) if method else None
        self._kicker(buf, "WINNER", self.GOLD if flashing else color_on_dark(self.ACCENT))
        self._name(buf, 14, f.get("winner") or "-",
                   (255, 255, 255) if flashing else self.HERO)

        if short:
            # The method is the headline of a finish -- "KO/TKO" is six
            # glyphs and fits at scale 2, which is exactly why the short
            # forms exist in mma.METHOD_SHORT.
            draw_text_centered(buf, 30, fit_text(short, WIDTH - 8, 2),
                               self.GOLD, scale=2, x_min=3)
        else:
            # No method in the payload -- say nothing rather than guess.
            draw_text_centered(buf, 32, "WINS", self.GOLD)

        rnd, t = f.get("final_round"), f.get("final_time")
        if rnd and t:
            # Time is ELAPSED in the final round (see mma.py), so this
            # reads the way a finish is actually announced.
            draw_text_centered(buf, 46, f"R{rnd}  {t}", self.INK)
        loser = f.get("loser")
        if loser:
            # fit_person on the NAME, then prefix -- fitting the whole
            # "DEF D. RODRIGUEZ" string dropped trailing words and left
            # "DEF D.", losing the very thing the line exists to say.
            shown = fit_person(loser, WIDTH - 8 - text_w("DEF "))
            draw_text_centered(buf, 54, f"DEF {shown}", self.INK_DIM, x_min=3)
        return bytes(buf)

    def _frame_upcoming(self, f):
        """Who is about to fight. Given real weight: two names stacked
        with the records that give them stakes, and a main event announced
        as one."""
        buf = blank(); fill(buf, self.BG)
        card = self.data.get("card") or {}
        main = f.get("main_event")
        self._occasion_frame(buf, 0.7 if main else 0.3)

        if main:
            self._kicker(buf, "MAIN EVENT", self.GOLD)
        elif f.get("co_main"):
            self._kicker(buf, "CO-MAIN EVENT")
        else:
            self._kicker(buf, f"FIGHT {f['number']} OF {card.get('total', '?')}")

        fighters = f.get("fighters") or []
        y = 14
        for i, fr in enumerate(fighters[:2]):
            self._name(buf, y, fr.get("name"), self.HERO, big_ok=False)
            if fr.get("record"):
                draw_text_centered(buf, y + 7, fr["record"], self.INK_DIM)
            y += 16
            if i == 0:
                draw_text_centered(buf, y - 4, "VS", color_on_dark(self.ACCENT))
                y += 4

        wt = f.get("weight")
        if wt:
            draw_text_centered(buf, 54, fit_text(wt, WIDTH - 8), self.INK_DIM, x_min=3)
        return bytes(buf)

    def _frame_stats(self, f):
        """Fight statistics, laid out the way the real UFC broadcast
        graphic shows them: two named columns, one stat per row down the
        middle -- sig. strikes landed, takedowns landed, control time.

        Source is mma.FEED.want_stats()/get_stats(): a genuinely more
        expensive call than the rest of the card (two per-fighter
        requests, no batched form), which is why tick() only ever
        requests it for the fight actually on screen, and only once it's
        live or finished -- see the comment there.

        A fight with nothing fetched yet (just switched to it, or the
        background poll hasn't landed) shows the matchup instead of a row
        of zeros that would look exactly like a real 0-0 fight -- the
        same "never invent a number" rule as every other feed here.
        """
        buf = blank(); fill(buf, self.BG)
        self._occasion_frame(buf, 0.5 if f.get("state") == "in" else 0.3)

        fighters = f.get("fighters") or []
        stats = mma.FEED.get_stats(f.get("id")) if f.get("id") else {}
        left = stats.get(fighters[0]["id"]) if fighters else None
        right = stats.get(fighters[1]["id"]) if len(fighters) > 1 else None
        if not fighters or (left is None and right is None):
            # Bail out to the matchup view BEFORE drawing anything of our
            # own -- the kicker used to be drawn here first, which meant
            # "FIGHT STATS" got recorded (by render_audit.py's draw
            # instrumentation) immediately before _frame_upcoming's own
            # "MAIN EVENT"/"CO-MAIN EVENT" kicker, both centered at the
            # same y=6 box. The stray draw never reached a real panel (this
            # buf is discarded in favor of _frame_upcoming's own fresh
            # buffer), but it produced a real, intermittent COLLISION
            # report every time a fight had no stats fetched yet -- exactly
            # the "FIGHT STATS overlaps MAIN EVENT" failure seen in
            # render_audit.py gameday runs. Drawing the kicker only once we
            # know we're keeping this frame removes the phantom draw.
            return self._frame_upcoming(f)
        self._kicker(buf, "FIGHT STATS", self.GOLD)

        # Last name only -- both corners plus the row labels have to
        # share 64px, and the font can always fit a single surname.
        def surname(full):
            parts = str(full or "").split()
            return parts[-1] if parts else ""
        lname = fit_text(surname(fighters[0]["name"]), 28)
        rname = fit_text(surname(fighters[1]["name"]) if len(fighters) > 1 else "", 28)
        # y=13, NOT y=8: the kicker occupies rows 6-10, and names at y=8
        # collided with it -- visible only by rendering the frame and
        # looking at the pixels, which is why that check is mandatory here.
        draw_text3x5(buf, 3, 13, lname, self.HERO)
        draw_text3x5(buf, WIDTH - 3 - text_w(rname), 13, rname, self.HERO)

        def row(y, label, lv, rv):
            draw_text_centered(buf, y, fit_text(label, 30), self.INK_DIM)
            lt = "-" if lv is None else str(int(lv))
            rt = "-" if rv is None else str(int(rv))
            draw_text3x5(buf, 3, y + 7, lt, self.HERO)
            draw_text3x5(buf, WIDTH - 3 - text_w(rt), y + 7, rt, self.HERO)

        def val(d, k):
            return (d or {}).get(k)

        # Three 14px groups (label + values 7px below) stacked from y=20,
        # so the last value row ends at y=59 clear of the y=62 border.
        row(20, "SIG STR", val(left, "sig_landed"), val(right, "sig_landed"))
        row(34, "TAKEDOWNS", val(left, "td_landed"), val(right, "td_landed"))

        # Control time is already a formatted "M:SS" string (see
        # mma._parse_stats), not a number, so it gets its own row rather
        # than going through row()'s int() formatting.
        ctl = val(left, "control_time") or "-"
        ctr = val(right, "control_time") or "-"
        draw_text_centered(buf, 48, "CONTROL", self.INK_DIM)
        draw_text3x5(buf, 3, 55, ctl, self.HERO)
        draw_text3x5(buf, WIDTH - 3 - text_w(ctr), 55, ctr, self.HERO)
        return bytes(buf)

    def _frame_card(self):
        """Where the night stands. Always available, so there is never a
        moment where you cannot tell how far through the card you are."""
        buf = blank(); fill(buf, self.BG)
        card = self.data.get("card") or {}
        live = card.get("live")
        self._occasion_frame(buf, 0.6 if live else 0.25)

        total = card.get("total") or 0
        done = card.get("done") or 0
        self._kicker(buf, "LIVE NOW" if live else "CARD", self.LIVE if live else None)
        draw_text_centered(buf, 16, f"{done}/{total}", self.HERO, scale=2, x_min=3)
        draw_text_centered(buf, 32, "FIGHTS DONE", self.INK_DIM)

        # Progress bar -- the arc of the night in one glyph.
        bar_w, bx, by = WIDTH - 14, 7, 40
        filled = int(bar_w * (done / total)) if total else 0
        for x in range(bar_w):
            c = self.ACCENT if x < filled else (40, 44, 56)
            put_px(buf, bx + x, by, c)
            put_px(buf, bx + x, by + 1, rim(c, 0.5))

        # The night's identity is the BILLING ("MEDIC VS. RODRIGUEZ"),
        # not the series label. ESPN's own shortName is "UFC FIGHT NIGHT"
        # for every Fight Night, which identifies nothing, and the full
        # name truncates to a meaningless "UFC FIGHT" at 64px -- so take
        # the part after the colon, which is the actual matchup.
        name = card.get("name") or ""
        billing = name.split(":", 1)[1].strip() if ":" in name else name
        billing = billing or card.get("short") or ""
        if text_w(billing) <= WIDTH - 8:
            draw_text_centered(buf, 48, billing, self.INK, x_min=3)
        else:
            # Scroll rather than truncate: dropping words here loses who
            # is fighting, which is the one thing this line is for.
            draw_marquee(buf, 48, billing, self.INK, self.scroll, gap="   -   ")
        where = card.get("city")
        if where:
            draw_text_centered(buf, 55, fit_text(where, WIDTH - 8), self.INK_DIM, x_min=3)
        return bytes(buf)

    def _frame_waiting(self):
        """No card loaded, or the next one is days out."""
        buf = blank(); fill(buf, self.BG)
        self._occasion_frame(buf, 0.2)
        label = self.data.get("next_label")
        if self.data.get("err") and not label:
            self._kicker(buf, "GAME DAY")
            draw_text_centered(buf, 28, "NO DATA", self.INK_DIM)
            return bytes(buf)
        self._kicker(buf, "NEXT CARD", self.GOLD)
        if label:
            words = label.split()
            lines, cur = [], ""
            for w in words:
                trial = f"{cur} {w}".strip()
                if text_w(trial) <= WIDTH - 10:
                    cur = trial
                else:
                    if cur:
                        lines.append(cur)
                    cur = w
            if cur:
                lines.append(cur)
            y = 18
            for ln in lines[:4]:
                draw_text_centered(buf, y, ln, self.HERO, x_min=3)
                y += 8
        else:
            draw_text_centered(buf, 28, "LOADING", self.INK_DIM)
        when = self.data.get("next_date")
        if when:
            draw_text_centered(buf, 54, fit_text(str(when)[:10].replace("-", "/"), WIDTH - 8),
                               self.INK_DIM, x_min=3)
        return bytes(buf)

    # ---- team view -------------------------------------------------------
    def _intensity(self, g):
        """0..1 "how much this game matters RIGHT NOW", driving the frame.

        Two things make a game feel alive and both are real data already
        in the payload, not invented: how CLOSE it is, and how LATE it is.
        A 1-point game in the 4th is the maximum; a blowout in the 1st is
        the minimum. They are multiplied rather than averaged, because a
        blowout does not get tense just by being late, and an early game
        is not tense just for being tied -- it needs both.

        Deliberately NOT applied to a finished or unstarted game: the
        escalation has to mean "right now", or it is decoration.
        """
        if not g or g.get("state") != "in":
            return 0.0
        hs, as_ = g["home"].get("score"), g["away"].get("score")
        if not isinstance(hs, int) or not isinstance(as_, int):
            return 0.3
        margin = abs(hs - as_)
        close = max(0.0, 1.0 - margin / 12.0)      # 12+ points apart = not close
        period = g.get("period") or 0
        # ESPN gives the CURRENT period but never the total, so the total
        # comes from the sport's real structure (REGULATION_PERIODS), not
        # from a guessed constant -- 4 would be wrong for MLB's 9 innings,
        # NHL's 3 periods and college basketball's 2 halves alike.
        total = REGULATION_PERIODS.get(g.get("league"), 4)
        late = max(0.0, min(1.0, period / float(max(1, total))))
        return max(0.15, close * late)

    def _frame_team(self):
        buf = blank(); fill(buf, self.BG)
        data = self.sports_data or {}
        g = data.get("favorite_game")
        fav = data.get("favorite") or {}

        if not g:
            self._occasion_frame(buf, 0.15)
            self._kicker(buf, "GAME DAY")
            draw_text_centered(buf, 26, fit_text(fav.get("team_abbr") or "NO TEAM", WIDTH - 8),
                               self.HERO, x_min=3)
            draw_text_centered(buf, 38, "NO GAME TODAY", self.INK_DIM)
            return bytes(buf)

        live = g["state"] == "in"
        self._occasion_frame(buf, self._intensity(g))
        self._kicker(buf, "LIVE" if live else ("FINAL" if g["state"] == "post" else "TONIGHT"),
                     self.LIVE if live else None)

        # Full panel, maximum detail -- this is the one night it does not
        # have to share space. Both teams big, with their real colours.
        flash = (255, 255, 255) if self.pulse.on else None
        y = 14
        for team in (g["away"], g["home"]):
            col = flash or (self.HERO if team.get("winner") or live else self.INK)
            bar = team.get("color") or self.INK_DIM
            for by in range(10):
                for bx in (2, 3):
                    put_px(buf, bx, y + by, bar)
            txt = f"{team['abbr']} {team.get('score') if team.get('score') is not None else ''}".strip()
            draw_text_centered(buf, y, fit_text(txt, WIDTH - 12, 2), col, scale=2, x_min=6)
            y += 14

        detail = g.get("detail") or ""
        if detail:
            draw_text_centered(buf, 44, fit_text(detail, WIDTH - 8),
                               self.LIVE if live else self.INK_DIM, x_min=3)
        # Sport-specific live state, reusing the work from the context pass.
        sit = g.get("situation") if live else None
        if sit and (sit.get("bases") is not None or sit.get("outs") is not None):
            draw_diamond(buf, WIDTH - 20, 52, sit.get("bases"))
            draw_outs(buf, WIDTH - 11, 56, sit.get("outs"))
            line = situation_line(g)
            if line:
                draw_text3x5(buf, 4, 53, fit_text(line, WIDTH - 28), self.INK)
        else:
            line = situation_line(g) if live else ""
            if not line:
                ar, hr = g["away"].get("record"), g["home"].get("record")
                line = f"{ar} / {hr}" if ar and hr else ""
            if line:
                draw_text_centered(buf, 53, fit_text(line, WIDTH - 8), self.INK_DIM, x_min=3)
        return bytes(buf)

    # ---- render ----------------------------------------------------------
    def frame(self):
        if self.cfg.get("target") == "team":
            return self._frame_team()

        card = self.data.get("card")
        if not card or not card.get("fights"):
            return self._frame_waiting()

        # A finish preempts everything, including a browse-selected fight
        # is NOT true -- browsing cancels the hold (see _step), because a
        # viewer who has taken manual control should keep it.
        if self.result_t > 0 and self.result_fight:
            return self._frame_result(self.result_fight)

        f = self._current()
        if f is None:
            return self._frame_waiting()
        # A fight already fought shows its result; one still to come shows
        # the matchup. That makes browsing the card coherent: you scroll
        # back through results and forward into what is still to happen.
        if f["state"] == "post" and (self.sel is not None or self.view == 1):
            return self._frame_result(f)
        # STATS is part of the auto-rotation only (view == 2, not while
        # browsing): a manually browsed fight keeps the existing
        # post->result / pre->upcoming contract, since stats for a fight
        # several positions away from the live one is rarely what a
        # browsing viewer is after, and tick() only actually fetches
        # stats for the fight on screen -- browsing straight to view 2
        # would just show "no data yet" for most of the card.
        if self.sel is None and self.view == 2 and f["state"] in ("in", "post"):
            return self._frame_stats(f)
        if self.sel is not None or self.view == 0:
            return self._frame_upcoming(f)
        return self._frame_card()


ENGINES = {
    "snake": SnakeEngine,
    "tetris": TetrisEngine,
    "pong": PongEngine,
    "breakout": BreakoutEngine,
    "tron": TronEngine,
    "flappy": FlappyEngine,
    "invaders": InvadersEngine,
    "life": LifeEngine,
    "dodge": DodgeEngine,
    "2048": Game2048Engine,
    "tunnel": TunnelEngine,
    "powder": PowderEngine,
    "brawler": BrawlerEngine,
    "chase": ChaseEngine,
    "ticker": TickerEngine,
    "satellite": SatelliteEngine,
    "flights": FlightEngine,
    "sports": SportsEngine,
    "news": NewsEngine,
    "weather": WeatherEngine,
    "clock": ClockEngine,
    "blog": BlogEngine,
    "ambient": AmbientEngine,
    "gameday": GameDayEngine,
    # PLANE-IN-WINDOW takeover -- same shape as "gameday": registered so
    # arcade_server.set_mode("planewatch") can construct it with zero
    # args, deliberately NOT added to PLAYABLE/MenuEngine.NATIVE_GAMES/
    # AmbientEngine.SEQUENCE. It is force-triggered by arcade_server's
    # render loop (see PlaneWatchEngine's own docstring), never chosen
    # from a menu or a rotation.
    "planewatch": PlaneWatchEngine,
    # HOME ASSISTANT NOTIFY, urgent tier (task #8) -- same shape as
    # "planewatch"/"gameday": registered so arcade_server.set_mode
    # ("notify") can construct it with zero args, deliberately NOT added
    # to MenuEngine.NATIVE_GAMES/AmbientEngine.SEQUENCE. Force-triggered
    # only by /api/notify (see NotifyEngine's own docstring), never chosen
    # from a menu or a rotation.
    "notify": NotifyEngine,
    "menu": MenuEngine,
    "boot": BootEngine,
}

PLAYABLE = set(ENGINES) - {"menu", "boot"}
