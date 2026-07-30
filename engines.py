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
from collections import deque
from pathlib import Path

import market
import satellite
import flights
import sports
import tic80_core

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
    "$": ("011", "110", "010", "011", "110"),
    "!": ("010", "010", "010", "000", "010"),
    "?": ("110", "001", "010", "000", "010"),
    "'": ("010", "010", "000", "000", "000"),
    ">": ("100", "010", "001", "010", "100"),
    "<": ("001", "010", "100", "010", "001"),
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
class TickerEngine:
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

    # ---- input ---------------------------------------------------------
    def input(self, cmd):
        if not self.rows:
            return
        if cmd == "left":
            self.cur = (self.cur - 1) % len(self.rows)
            self.hold = 0
        elif cmd == "right":
            self.cur = (self.cur + 1) % len(self.rows)
            self.hold = 0
        elif cmd in ("rotate", "drop"):
            self.cycling = not self.cycling      # park on one symbol

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation ----------------------------------------------------
    def tick(self):
        self.ticks += 1
        self.rows, self.age, self.err = market.FEED.get()
        if self.rows:
            self.cur %= len(self.rows)
        self.scroll += 0.5
        if self.cycling and self.rows:
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

    def frame(self):
        buf = blank()
        fill(buf, self.BG)

        if not self.rows:
            msg = "NO DATA" if self.err else "LOADING"
            tw = 4 * len(msg) - 1
            draw_text3x5(buf, (WIDTH - tw) // 2, 28, msg,
                         self.DOWN if self.err else self.INK_DIM)
            dots = "." * (1 + (self.ticks // 12) % 3)
            draw_text3x5(buf, (WIDTH - 11) // 2, 38, dots, self.INK_DIM)
            return bytes(buf)

        row = self.rows[self.cur]
        col = self._tint(row["pct"])

        # --- spotlight ---
        sym = row["sym"][:4]
        draw_text3x5(buf, (WIDTH - (8 * len(sym) - 2)) // 2, 6, sym, col, scale=2)

        price = self._fmt_price(row["price"])
        draw_text3x5(buf, (WIDTH - (4 * len(price) - 1)) // 2, 24, price, self.INK)

        pct = row["pct"]
        chg = f"{abs(pct):.2f}%"
        arrow_w = 7          # triangle is 5px wide; leave a clear gap after it
        tw = 4 * len(chg) - 1 + arrow_w
        ax = (WIDTH - tw) // 2
        # A triangle, not just a +/- sign: direction should survive being
        # glanced at from across the room even if the digits do not.
        # Row i widens downward, so a gain puts the apex at the TOP and a loss
        # puts it at the BOTTOM. Getting this backwards is silent and worse
        # than useless: colour and arrow would disagree, and the arrow is the
        # half that still reads from across the room.
        if pct > 0.05:                                   # gain: apex on top
            for i in range(3):
                for x in range(-i, i + 1):
                    put_px(buf, ax + 2 + x, 33 + i, col)
        elif pct < -0.05:                                # loss: apex on bottom
            for i in range(3):
                w = 2 - i
                for x in range(-w, w + 1):
                    put_px(buf, ax + 2 + x, 33 + i, col)
        else:
            for x in range(5):
                put_px(buf, ax + x, 34, col)
        draw_text3x5(buf, ax + arrow_w, 32, chg, col)

        # --- divider ---
        for x in range(4, WIDTH - 4):
            put_px(buf, x, 43, (30, 34, 44))

        # --- scrolling tape ---
        parts = []
        for r in self.rows:
            sign = "+" if r["pct"] >= 0 else "-"
            parts.append(f"{r['sym']} {self._fmt_price(r['price'])} "
                         f"{sign}{abs(r['pct']):.1f}%")
        tape = "   ".join(parts) + "   "
        tape_w = 4 * len(tape)
        if tape_w:
            off = int(self.scroll) % tape_w
            x = -off
            for ch in tape:
                if x > WIDTH:
                    break
                if x > -4:
                    draw_text3x5(buf, x, 49, ch, self.INK_DIM)
                x += 4
            x = -off + tape_w                    # second copy for a seamless wrap
            for ch in tape:
                if x > WIDTH:
                    break
                if x > -4:
                    draw_text3x5(buf, x, 49, ch, self.INK_DIM)
                x += 4

        # --- honesty indicator ---
        # Prices that stopped updating must never look live. A dot beats a
        # word here: it costs 1px and never crowds the numbers.
        if self.age is not None and self.age > 180:
            put_px(buf, WIDTH - 3, 2, self.STALE)
            put_px(buf, WIDTH - 2, 2, self.STALE)
            put_px(buf, WIDTH - 3, 3, self.STALE)
            put_px(buf, WIDTH - 2, 3, self.STALE)
        return bytes(buf)


class SatelliteEngine:
    """ISS tracker.

    Same discipline as TickerEngine: no I/O in this class at all. It reads
    whatever satellite.FEED has already cached on its own thread, so a slow
    pass-prediction API can never stall the render loop.

    Two views, because "next pass" and "right now" are both worth 4+ seconds
    of someone's attention but don't fit as one glanceable read: PASS is the
    hero view (PRODUCTION.md frames this whole feature as an "ISS countdown"),
    LIVE is the secondary one. Auto-cycles like the ticker's spotlight;
    left/right jumps between them manually.
    """

    name = "satellite"
    tick_rate = 0.05

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    VISIBLE = (60, 230, 110)
    NOT_VISIBLE = (170, 178, 200)
    STALE = (255, 170, 40)
    ORBIT = (40, 46, 66)
    ISS = (255, 226, 60)

    VIEW_TICKS = 160          # ~8s per view at this tick rate

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"configured": False, "label": "HOME", "pos": None,
                    "pos_age": None, "next_pass": None, "pass_age": None,
                    "seconds_to_rise": None, "err": None}
        self.view = 0             # 0 = PASS, 1 = LIVE
        self.hold = 0
        self.cycling = True
        self.ticks = 0
        self.orbit_phase = 0.0

    # ---- input -----------------------------------------------------------
    def input(self, cmd):
        if cmd == "left":
            self.view = (self.view - 1) % 2
            self.hold = 0
        elif cmd == "right":
            self.view = (self.view + 1) % 2
            self.hold = 0
        elif cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self.data = satellite.FEED.get()
        self.orbit_phase += 0.035
        if self.cycling:
            self.hold += 1
            if self.hold >= self.VIEW_TICKS:
                self.hold = 0
                self.view = (self.view + 1) % 2
        self.score = int(self.data.get("pos", {}).get("alt_km", 0)) if self.data.get("pos") else 0

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

    def _frame_unconfigured(self, buf):
        msg = "SET LOCATION"
        draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 24, msg, self.INK)
        sub = "TO TRACK ISS"
        draw_text3x5(buf, (WIDTH - (4 * len(sub) - 1)) // 2, 34, sub, self.INK_DIM)
        return bytes(buf)

    def _frame_pass(self):
        buf = blank()
        fill(buf, self.BG)
        nxt = self.data.get("next_pass")
        secs = self.data.get("seconds_to_rise")

        if not nxt or secs is None:
            msg = "NO PASS DATA" if self.data.get("err") else "LOADING"
            draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 28, msg, self.INK_DIM)
            dots = "." * (1 + (self.ticks // 12) % 3)
            draw_text3x5(buf, (WIDTH - 11) // 2, 38, dots, self.INK_DIM)
            return bytes(buf)

        visible = nxt.get("visible")
        col = self.VISIBLE if visible else self.NOT_VISIBLE

        label = "NEXT PASS"
        draw_text3x5(buf, (WIDTH - (4 * len(label) - 1)) // 2, 4, label, self.INK_DIM)

        cd = self._fmt_countdown(secs)
        draw_text3x5(buf, (WIDTH - (8 * len(cd) - 2)) // 2, 12, cd, col, scale=2)

        # "IN DAYLIGHT/SHADOW" (the fully accurate reason) doesn't fit at
        # 64px; NOT VISIBLE keeps the honest meaning -- this pass happens,
        # you just will not see it with your eyes -- in a width that fits.
        tag = "VISIBLE" if visible else "NOT VISIBLE"
        draw_text3x5(buf, (WIDTH - (4 * len(tag) - 1)) // 2, 26, tag, col)

        detail = f"{nxt['compass']} {nxt['max_elev']:.0f}DEG {nxt['duration_s']}S"
        if 4 * len(detail) - 1 > WIDTH - 4:
            detail = f"{nxt['compass']} {nxt['max_elev']:.0f}D {nxt['duration_s']}S"
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(detail) - 1)) // 2), 36, detail, self.INK_DIM)

        for x in range(4, WIDTH - 4):
            put_px(buf, x, 44, (30, 34, 44))
        loc = self.data.get("label", "HOME")[:10]
        draw_text3x5(buf, (WIDTH - (4 * len(loc) - 1)) // 2, 50, loc, self.INK_DIM)

        if self.data.get("pass_age") and self.data["pass_age"] > 3600 * 2:
            put_px(buf, WIDTH - 3, 2, self.STALE)
            put_px(buf, WIDTH - 2, 2, self.STALE)
        return bytes(buf)

    def _frame_live(self):
        buf = blank()
        fill(buf, self.BG)
        pos = self.data.get("pos")

        if not pos:
            msg = "NO SIGNAL" if self.data.get("err") else "LOADING"
            draw_text3x5(buf, (WIDTH - (4 * len(msg) - 1)) // 2, 28, msg, self.INK_DIM)
            dots = "." * (1 + (self.ticks // 12) % 3)
            draw_text3x5(buf, (WIDTH - 11) // 2, 38, dots, self.INK_DIM)
            return bytes(buf)

        label = "ISS LIVE"
        draw_text3x5(buf, (WIDTH - (4 * len(label) - 1)) // 2, 3, label, self.ISS)

        # A simple orbit ring with a moving marker -- not a literal map (a
        # real ground-track projection is not something 64x64 can show
        # meaningfully), just a glanceable "it's moving" flourish.
        cx, cy, r = WIDTH // 2, 26, 13
        for i in range(48):
            a = i / 48 * 2 * math.pi
            put_px(buf, cx + int(r * math.cos(a)), cy + int(r * 0.45 * math.sin(a)), self.ORBIT)
        a = self.orbit_phase
        mx = cx + int(r * math.cos(a))
        my = cy + int(r * 0.45 * math.sin(a))
        for dx in (-1, 0, 1):
            put_px(buf, mx + dx, my, self.ISS)
        put_px(buf, mx, my - 1, self.ISS)
        put_px(buf, mx, my + 1, self.ISS)

        alt = f"{pos['alt_km']:.0f}KM"
        draw_text3x5(buf, 3, 40, alt, self.INK)
        vel = f"{pos['vel_kmh']:.0f}KM/H"
        if 4 * len(vel) - 1 <= WIDTH - 6:
            draw_text3x5(buf, WIDTH - 3 - (4 * len(vel) - 1), 40, vel, self.INK_DIM)

        if self.data.get("configured") and "distance_km" in pos:
            dist = f"{pos['distance_km']:.0f}KM FROM {self.data.get('label','HOME')[:8]}"
            if 4 * len(dist) - 1 > WIDTH - 4:
                dist = f"{pos['distance_km']:.0f}KM AWAY"
            draw_text3x5(buf, max(2, (WIDTH - (4 * len(dist) - 1)) // 2), 50, dist, self.INK_DIM)
        else:
            draw_text3x5(buf, (WIDTH - (4 * 12 - 1)) // 2, 50, "SET LOCATION", self.INK_DIM)

        if self.data.get("pos_age") and self.data["pos_age"] > 120:
            put_px(buf, WIDTH - 3, 2, self.STALE)
            put_px(buf, WIDTH - 2, 2, self.STALE)
        return bytes(buf)

    def frame(self):
        buf = blank()
        fill(buf, self.BG)
        if not self.data.get("configured"):
            return self._frame_unconfigured(buf)
        return self._frame_pass() if self.view == 0 else self._frame_live()


class FlightEngine:
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

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"aircraft": [], "age": None, "home_label": "HOME",
                    "configured": False, "err": None}
        self.cur = 0
        self.hold = 0
        self.cycling = True
        self.ticks = 0

    # ---- input -----------------------------------------------------------
    def input(self, cmd):
        n = len(self.data.get("aircraft") or [])
        if not n:
            return
        if cmd == "left":
            self.cur = (self.cur - 1) % n
            self.hold = 0
        elif cmd == "right":
            self.cur = (self.cur + 1) % n
            self.hold = 0
        elif cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self.data = flights.FEED.get()
        n = len(self.data.get("aircraft") or [])
        if n:
            self.cur %= n
        if self.cycling and n > 1:
            self.hold += 1
            if self.hold >= self.VIEW_TICKS:
                self.hold = 0
                self.cur = (self.cur + 1) % n
        self.score = n

    # ---- render --------------------------------------------------------
    @staticmethod
    def _compass(deg):
        if deg is None:
            return ""
        i = int((deg + 22.5) % 360 // 45)
        return FlightEngine.COMPASS[i]

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

    def frame(self):
        if not self.data.get("configured"):
            buf = blank()
            fill(buf, self.BG)
            return self._frame_unconfigured(buf)

        aircraft = self.data.get("aircraft") or []
        if not aircraft:
            return self._frame_idle()

        buf = blank()
        fill(buf, self.BG)
        ac = aircraft[self.cur % len(aircraft)]

        ident = (ac.get("ident") or "UNKNOWN")[:8]
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(ident) - 1)) // 2), 4, ident, self.PLANE)

        alt = ac.get("alt_ft")
        # .upper() defensively: aircraft type codes are conventionally
        # uppercase in ADS-B data, but "conventionally" is exactly the
        # word that just burned the airline-name field above.
        typ = (ac.get("type") or "").upper()
        line2 = f"{typ} {alt:.0f}FT".strip() if isinstance(alt, (int, float)) else (typ or "-")
        if 4 * len(line2) - 1 > WIDTH - 4:
            line2 = f"{alt:.0f}FT" if isinstance(alt, (int, float)) else typ
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(line2) - 1)) // 2), 16, line2, self.INK)

        gs = ac.get("gs_kt")
        dist = ac.get("dist_nm")
        compass = self._compass(ac.get("dir_deg"))
        parts = []
        if isinstance(gs, (int, float)):
            parts.append(f"{gs:.0f}KT")
        if isinstance(dist, (int, float)):
            parts.append(f"{dist:.0f}NM {compass}".strip())
        line3 = " ".join(parts) if parts else "-"
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(line3) - 1)) // 2), 26, line3, self.INK_DIM)

        for x in range(4, WIDTH - 4):
            put_px(buf, x, 34, (30, 34, 44))

        route = ac.get("route")
        if route and route.get("origin") and route.get("dest"):
            rline = f"{route['origin']}>{route['dest']}".upper()
            draw_text3x5(buf, max(2, (WIDTH - (4 * len(rline) - 1)) // 2), 40, rline, self.ROUTE)
            # adsbdb returns mixed-case names ("United Airlines"), but the
            # font is uppercase-only -- draw_text3x5 silently skips glyphs
            # it doesn't have, so lowercase letters vanished and "United
            # Airlines" rendered as just the two capitals, "U" and "A",
            # nothing else. Every other display string in this codebase
            # was already uppercase at the source; this was the one place
            # that wasn't.
            airline = (route.get("airline") or "").upper()[:16]
            if airline:
                draw_text3x5(buf, max(2, (WIDTH - (4 * len(airline) - 1)) // 2), 48, airline, self.INK_DIM)
        else:
            draw_text3x5(buf, (WIDTH - (4 * 10 - 1)) // 2, 44, "NO ROUTE DATA", self.INK_DIM)

        # Position dots: which aircraft (of how many) is on screen right
        # now, so cycling reads as "stepping through a list" rather than
        # an unexplained change every few seconds.
        n = min(len(aircraft), 8)
        dot_w = n * 4 - 2
        dx0 = (WIDTH - dot_w) // 2
        for i in range(n):
            col = self.DOT_ON if i == (self.cur % n) else self.DOT_OFF
            put_px(buf, dx0 + i * 4, HEIGHT - 3, col)

        if self.data.get("age") and self.data["age"] > 60:
            put_px(buf, WIDTH - 3, 2, self.STALE)
            put_px(buf, WIDTH - 2, 2, self.STALE)
        return bytes(buf)


class SportsEngine:
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

    BG = (0, 0, 0)
    INK = (150, 160, 185)
    INK_DIM = (70, 76, 92)
    WIN = (60, 230, 110)
    LOSE = (255, 70, 80)
    LIVE = (255, 226, 60)
    STALE = (255, 170, 40)
    FLASH = (255, 255, 255)

    LEAGUE_COLOR = {
        "NFL": (255, 90, 120), "NBA": (255, 140, 40),
        "MLB": (120, 200, 255), "NHL": (150, 200, 255),
    }

    VIEW_TICKS = 200          # ~10s per view at this tick rate
    SPOTLIGHT_TICKS = 90      # ~4.5s per game in the ticker view
    FLASH_TICKS = 14

    def __init__(self):
        self.score = 0
        self.reset()

    def reset(self):
        self.data = {"games": [], "favorite": None, "favorite_game": None,
                    "win_prob": None, "age": None, "err": None}
        self.view = 0             # 0 = PINNED, 1 = TICKER
        self.hold = 0
        self.cur = 0              # index into games, for TICKER view
        self.cycling = True
        self.ticks = 0
        self.scroll = 0.0
        self._last_home_score = None
        self._last_away_score = None
        self._last_event_id = None
        self.score_flash = 0

    # ---- input -----------------------------------------------------------
    def input(self, cmd):
        games = self.data.get("games") or []
        if cmd == "left":
            if self.view == 1 and games:
                self.cur = (self.cur - 1) % len(games)
                self.hold = 0
            elif self.data.get("favorite"):
                self.view = (self.view - 1) % 2
                self.hold = 0
        elif cmd == "right":
            if self.view == 1 and games:
                self.cur = (self.cur + 1) % len(games)
                self.hold = 0
            elif self.data.get("favorite"):
                self.view = (self.view + 1) % 2
                self.hold = 0
        elif cmd in ("rotate", "drop"):
            self.cycling = not self.cycling

    def auto(self):
        pass          # already self-cycling; ambient and manual look the same

    # ---- simulation --------------------------------------------------------
    def tick(self):
        self.ticks += 1
        self.data = sports.FEED.get()
        if not self.data.get("favorite"):
            self.view = 1          # nothing to pin: stay on the ticker
        games = self.data.get("games") or []
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

        if self.cycling:
            self.hold += 1
            limit = self.VIEW_TICKS if self.view == 0 else self.SPOTLIGHT_TICKS
            if self.hold >= limit:
                self.hold = 0
                if self.data.get("favorite"):
                    self.view = (self.view + 1) % 2
                elif games:
                    self.cur = (self.cur + 1) % len(games)
        self.score = len(games)

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
        box-score convention (visitor listed first/on top)."""
        scale = 2 if big else 1
        # Glyph height is 5*scale px; the gap between the away/home lines
        # must clear that or the two rows visibly bleed into each other.
        # (8 < 10 at scale=2 did exactly that -- caught by rendering an
        # actual frame and looking at it, not just eyeballing the number.)
        gap = 11 if big else 6
        for i, (team, other) in enumerate(((g["away"], g["home"]), (g["home"], g["away"]))):
            txt = f"{team['abbr']} {self._score_txt(team['score'])}"
            txt = self._fit(txt, WIDTH - 4)
            w = (8 if big else 4) * len(txt) - (2 if big else 1)
            col = flash_col if flash_col else (
                self.WIN if team["winner"] else (self.INK if g["state"] != "post" else self.INK_DIM))
            draw_text3x5(buf, max(2, (WIDTH - w) // 2), y + i * gap, txt, col, scale=scale)

    def _frame_pinned(self):
        buf = blank()
        fill(buf, self.BG)
        fg = self.data.get("favorite_game")
        fav = self.data.get("favorite") or {}

        if not fg:
            sub = f"NO {fav.get('team_abbr','')} GAME TODAY".strip()
            return self._frame_empty("PINNED TEAM", self._fit(sub, WIDTH - 4) or "NO GAME TODAY")

        lg_col = self.LEAGUE_COLOR.get(fg["league"], self.INK_DIM)
        draw_text3x5(buf, (WIDTH - (4 * len(fg["league"]) - 1)) // 2, 2, fg["league"], lg_col)

        flash_col = self.FLASH if (self.score_flash > 0 and self.score_flash % 2 == 0) else None
        self._draw_game_block(buf, fg, 11, big=True, flash_col=flash_col)

        detail = self._fit(fg["detail"] or "", WIDTH - 4)
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(detail) - 1)) // 2), 35, detail, self.LIVE if fg["state"] == "in" else self.INK_DIM)

        for x in range(4, WIDTH - 4):
            put_px(buf, x, 43, (30, 34, 44))

        win_prob = self.data.get("win_prob")
        if win_prob is not None:
            is_home = fav.get("team_abbr") == fg["home"]["abbr"]
            pct = win_prob if is_home else (1.0 - win_prob)
            label = f"{fav.get('team_abbr','')} {pct * 100:.0f}%"
            draw_text3x5(buf, (WIDTH - (4 * len(label) - 1)) // 2, 48, label, self.WIN)
            # A literal probability bar under the text -- the number alone
            # reads fine up close, but the bar is what survives a glance
            # from across the room the way the ticker's arrow does.
            bar_w = WIDTH - 12
            bx0 = 6
            fill_w = int(bar_w * pct)
            for x in range(bar_w):
                col = self.WIN if x < fill_w else (30, 34, 44)
                put_px(buf, bx0 + x, 55, col)

        if self.data.get("age") and self.data["age"] > 120:
            put_px(buf, WIDTH - 3, 2, self.STALE)
            put_px(buf, WIDTH - 2, 2, self.STALE)
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
        draw_text3x5(buf, (WIDTH - (4 * len(g["league"]) - 1)) // 2, 3, g["league"], lg_col)

        self._draw_game_block(buf, g, 11, big=True)

        detail = self._fit(g["detail"] or "", WIDTH - 4)
        draw_text3x5(buf, max(2, (WIDTH - (4 * len(detail) - 1)) // 2), 35, detail,
                     self.LIVE if g["state"] == "in" else self.INK_DIM)

        for x in range(4, WIDTH - 4):
            put_px(buf, x, 43, (30, 34, 44))

        # --- scrolling tape of every other game, ESPN-style ---
        parts = []
        for r in games:
            sign = f"{r['away']['abbr']} {self._score_txt(r['away']['score'])} @ " \
                   f"{r['home']['abbr']} {self._score_txt(r['home']['score'])} {r['detail']}"
            parts.append(sign)
        tape = "   ".join(parts) + "   "
        tape_w = 4 * len(tape)
        if tape_w:
            off = int(self.scroll) % tape_w
            x = -off
            for ch in tape:
                if x > WIDTH:
                    break
                if x > -4:
                    draw_text3x5(buf, x, 50, ch, self.INK_DIM)
                x += 4
            x = -off + tape_w
            for ch in tape:
                if x > WIDTH:
                    break
                if x > -4:
                    draw_text3x5(buf, x, 50, ch, self.INK_DIM)
                x += 4

        n = min(len(games), 8)
        dot_w = n * 4 - 2
        dx0 = (WIDTH - dot_w) // 2
        for i in range(n):
            col = self.INK if i == (self.cur % n) else self.INK_DIM
            put_px(buf, dx0 + i * 4, HEIGHT - 3, col)

        if self.data.get("age") and self.data["age"] > 120:
            put_px(buf, WIDTH - 3, 2, self.STALE)
            put_px(buf, WIDTH - 2, 2, self.STALE)
        return bytes(buf)

    def frame(self):
        if self.view == 0 and self.data.get("favorite"):
            return self._frame_pinned()
        return self._frame_ticker()


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
    "menu": MenuEngine,
    "boot": BootEngine,
}

PLAYABLE = set(ENGINES) - {"menu", "boot"}
