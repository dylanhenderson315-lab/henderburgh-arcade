"""
transitions.py -- one shared transition system for every mode.

Why this exists: a board that snaps between states reads as a monitor; one
with considered motion reads as an object someone made. This is applied
centrally in arcade_server's render loop, so all nine modes get the SAME
transition rather than each inventing its own -- consistency is the point.

COST IS THE HARD CONSTRAINT. This runs at render-loop frequency on a 12KB
buffer, and arcade_server documents that flooding the panel or stalling
the loop is a real failure mode. So every transition here is built from
byte SLICING and CONCATENATION, or from the cached 256-byte translate
table in brightness.py -- all of which run in C over the whole buffer.
There is deliberately no per-pixel Python loop anywhere in this file; a
naive cross-fade blending two 12288-byte frames in Python would be about
three orders of magnitude more expensive and is exactly what this avoids.

That rules out a true A-over-B cross-fade (it needs per-pixel arithmetic
on two sources). Fading through black gets the same "soft" feel using
only translate(), so that is what `fade` does.
"""
import brightness

WIDTH = HEIGHT = 64
ROW = WIDTH * 3
FRAME = WIDTH * HEIGHT * 3
BLACK = bytes(FRAME)


def _ease(p):
    """Ease-in-out. Linear motion reads mechanical; this is what makes a
    slide feel like it has weight rather than like a scrollbar."""
    p = 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)
    return 2 * p * p if p < 0.5 else 1 - ((-2 * p + 2) ** 2) / 2


def push_up(old, new, p):
    """New content slides up from below, pushing the old off the top.
    Two slices and a concat -- the cheapest possible real motion."""
    k = int(_ease(p) * HEIGHT)
    if k <= 0:
        return old
    if k >= HEIGHT:
        return new
    return old[k * ROW:] + new[: k * ROW]


def push_down(old, new, p):
    k = int(_ease(p) * HEIGHT)
    if k <= 0:
        return old
    if k >= HEIGHT:
        return new
    return new[(HEIGHT - k) * ROW:] + old[: (HEIGHT - k) * ROW]


def wipe_right(old, new, p):
    """Vertical edge sweeps across. Costs one slice pair per row (64), so
    still no per-pixel work."""
    k = int(_ease(p) * WIDTH)
    if k <= 0:
        return old
    if k >= WIDTH:
        return new
    out = bytearray(FRAME)
    kb = k * 3
    for y in range(HEIGHT):
        o = y * ROW
        out[o:o + kb] = new[o:o + kb]
        out[o + kb:o + ROW] = old[o + kb:o + ROW]
    return bytes(out)


def fade(old, new, p):
    """Fade out to black, then in. Uses brightness's cached translate
    tables, so each half is a single C-level pass over the buffer.

    A true cross-fade (A blended over B) is deliberately NOT offered: it
    requires per-pixel arithmetic over two 12KB sources every frame, which
    is the one thing this module exists to avoid."""
    if p <= 0.0:
        return old
    if p >= 1.0:
        return new
    if p < 0.5:
        return brightness.apply(old, 1.0 - p * 2.0)
    return brightness.apply(new, (p - 0.5) * 2.0)


STYLES = {
    "push_up": push_up,
    "push_down": push_down,
    "wipe_right": wipe_right,
    "fade": fade,
}
DEFAULT_STYLE = "push_up"


def blend(old, new, p, style=DEFAULT_STYLE):
    """One frame of a transition. `p` is 0..1 progress."""
    if not old or len(old) != FRAME:
        return new
    if not new or len(new) != FRAME:
        return old
    return STYLES.get(style, push_up)(old, new, p)


# ---- first light -------------------------------------------------------
# Coming up from dark is the emotional peak of a display like this, and it
# is cheap to do well: a brightness ramp on the real first frame, so the
# content resolves out of black instead of appearing fully formed. Reuses
# brightness.apply, so it costs one translate pass per frame.
WAKE_FRAMES = 18          # ~0.75s at 24fps


def wake(frame, i, total=WAKE_FRAMES):
    """Frame `i` of the panel coming alive. Eased so it blooms up rather
    than ramping linearly."""
    if i >= total:
        return frame
    return brightness.apply(frame, _ease(max(0.0, i / float(total))))
