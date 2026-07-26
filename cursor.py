"""
cursor.py — draw a pointer marker big enough to survive the downscale.

At 8:1 (and worse at 16:1) the real macOS cursor is sub-pixel on a 64x64 panel:
it averages away to nothing, so you cannot tell where you are pointing. The fix
is not to capture the cursor but to draw our own, sized as a fraction of the
SOURCE frame so it lands on the panel at a deliberate few pixels across.

Drawn onto the full-size image BEFORE imgproc runs, so the existing crop/scale
path applies to it automatically rather than the mapping being re-derived here
(and drifting out of sync the first time framing changes).

The marker is a white core inside a black ring: on a 64x64 panel the ring is
what makes it readable over light content, and the core over dark content.
Neither alone survives on both.
"""
import ctypes
import ctypes.util

from PIL import ImageDraw

_cg = None
_cf = None


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load():
    global _cg, _cf
    if _cg is None:
        _cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        _cg.CGEventCreate.restype = ctypes.c_void_p
        _cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        _cg.CGEventGetLocation.restype = _CGPoint
        _cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _cg, _cf


def position():
    """Global cursor position in the same coordinate space mss reports.

    Origin is the top-left of the main display, so displays placed to the left
    give negative x — which is exactly where the panel's virtual screen lives.
    """
    try:
        cg, cf = _load()
        ev = cg.CGEventCreate(None)
        if not ev:
            return None
        try:
            p = cg.CGEventGetLocation(ev)
            return (p.x, p.y)
        finally:
            cf.CFRelease(ev)          # called every frame; must not leak
    except Exception:
        return None


def draw(img, area, scale=0.055, style="ring"):
    """Draw the cursor onto a full-size capture. `area` is the grabbed rect.

    scale is the marker RADIUS as a fraction of the short side, so the marker is
    the same apparent size on the panel whatever resolution the source runs at.
    """
    pos = position()
    if not pos:
        return img
    x = pos[0] - area["left"]
    y = pos[1] - area["top"]
    w, h = img.size
    if not (0 <= x < w and 0 <= y < h):
        return img                                  # pointer is on another screen

    r = max(4.0, min(w, h) * scale)
    d = ImageDraw.Draw(img)
    if style == "crosshair":
        # Thin lines vanish at 64x64, so the arms are drawn thick on purpose.
        arm, t = r * 2.4, max(2.0, r * 0.34)
        d.line([(x - arm, y), (x + arm, y)], fill=(0, 0, 0), width=int(t * 2.4))
        d.line([(x, y - arm), (x, y + arm)], fill=(0, 0, 0), width=int(t * 2.4))
        d.line([(x - arm, y), (x + arm, y)], fill=(255, 255, 255), width=int(t))
        d.line([(x, y - arm), (x, y + arm)], fill=(255, 255, 255), width=int(t))
    d.ellipse([x - r, y - r, x + r, y + r], fill=(0, 0, 0))
    d.ellipse([x - r * 0.92, y - r * 0.92, x + r * 0.92, y + r * 0.92],
              outline=(255, 255, 255), width=max(1, int(r * 0.30)))
    d.ellipse([x - r * 0.34, y - r * 0.34, x + r * 0.34, y + r * 0.34],
              fill=(255, 255, 255))
    return img
