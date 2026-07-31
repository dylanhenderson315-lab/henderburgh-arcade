"""
brightness.py -- time-of-day brightness scheduling for the panel.

Why this exists: the panel now RESTS on the clock (see arcade_server's
DEFAULT_MODE), so it is lit 24 hours a day. A full-brightness 64x64 LED
matrix at 3am is the first thing that becomes unpleasant about an
always-on display.

PRODUCTION.md assumes an ambient light sensor on the sellable device;
this development rig has none (confirmed -- the Apollo M-1 exposes no
lux sensor), so scheduling is time-based. When a sensor does exist, only
`level_at()` needs to change: everything above it takes a 0..1 scalar and
does not care where it came from.

Applied at the RENDER layer (scaling the RGB buffer before it is sent),
deliberately, rather than by setting WLED's own `bri` over HTTP:
  * It is renderer-agnostic. The future Pi/Bonnet path (display.py) gets
    dimming for free; a WLED-specific HTTP call would not port.
  * It needs no network round-trip per change and cannot fail or lag.
  * It is deterministic and unit-testable without hardware.

This module is pure: config file access plus arithmetic, no network, no
panel. `level_at()` is a pure function of (minutes-since-midnight, cfg)
so every edge -- midnight wrap, fade boundaries, degenerate windows -- is
directly testable.
"""
import json
import threading
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "brightness_config.json"

DEFAULTS = {
    "enabled": True,
    "day_brightness": 1.0,       # 0..1 scale applied to every pixel
    "night_brightness": 0.28,    # dim but still readable across a dark room
    "night_start": "22:00",      # local time
    "night_end": "07:00",
    "fade_minutes": 30,          # 0 = instant switch
}

_lock = threading.Lock()
_cache = {"cfg": None, "mtime": None}


def _parse_hhmm(s, fallback_min):
    """'22:00' -> 1320. Returns fallback on anything malformed rather than
    raising: a bad config value must never be able to stop the panel."""
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError, TypeError):
        pass
    return fallback_min


def _clamp01(v, fallback):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, v))


def load_config():
    """Read brightness_config.json, seeding defaults on first run. Cached
    on file mtime so the render loop can call this every frame without
    hitting the filesystem each time."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        save_config(DEFAULTS)
        with _lock:
            _cache["cfg"], _cache["mtime"] = dict(DEFAULTS), None
        return dict(DEFAULTS)

    with _lock:
        if _cache["cfg"] is not None and _cache["mtime"] == mtime:
            return dict(_cache["cfg"])

    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict):
            cfg["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
            cfg["day_brightness"] = _clamp01(raw.get("day_brightness"), DEFAULTS["day_brightness"])
            cfg["night_brightness"] = _clamp01(raw.get("night_brightness"), DEFAULTS["night_brightness"])
            cfg["night_start"] = str(raw.get("night_start", DEFAULTS["night_start"]))
            cfg["night_end"] = str(raw.get("night_end", DEFAULTS["night_end"]))
            try:
                cfg["fade_minutes"] = max(0, int(raw.get("fade_minutes", DEFAULTS["fade_minutes"])))
            except (TypeError, ValueError):
                cfg["fade_minutes"] = DEFAULTS["fade_minutes"]
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass          # keep defaults; never crash the render loop over a bad file

    with _lock:
        _cache["cfg"], _cache["mtime"] = dict(cfg), mtime
    return cfg


def save_config(cfg):
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    CONFIG_PATH.write_text(json.dumps(merged, indent=2))
    with _lock:
        _cache["cfg"], _cache["mtime"] = None, None      # force reload
    return merged


def level_at(minutes, cfg=None):
    """Brightness scalar 0..1 for a given minute-of-day (0..1439).

    Pure function -- no clock read, no file access -- so every boundary is
    directly testable.

    The night window may WRAP past midnight (22:00 -> 07:00 is the normal
    case), which is the classic off-by-one here: naive `start <= t < end`
    is false for every minute of a wrapping window. All arithmetic is done
    as "minutes since night began, modulo 1440" so wrapping needs no
    special case at all.
    """
    cfg = cfg or load_config()
    day = cfg["day_brightness"]
    night = cfg["night_brightness"]
    if not cfg["enabled"]:
        return day

    ns = _parse_hhmm(cfg["night_start"], _parse_hhmm(DEFAULTS["night_start"], 1320))
    ne = _parse_hhmm(cfg["night_end"], _parse_hhmm(DEFAULTS["night_end"], 420))
    span = (ne - ns) % 1440          # length of the night window
    if span == 0:
        return day                    # start == end: no night window at all

    fade = int(cfg["fade_minutes"])
    d = (minutes - ns) % 1440         # minutes since night began

    if d < span:
        # Inside the night window, fading down from day.
        f = min(fade, span)           # a fade longer than the window is clamped
        if f > 0 and d < f:
            return day + (night - day) * (d / f)
        return night
    # Inside the day window, fading up from night.
    dd = d - span
    day_span = 1440 - span
    f = min(fade, day_span)
    if f > 0 and dd < f:
        return night + (day - night) * (dd / f)
    return day


def level_now(cfg=None, now=None):
    t = now or time.localtime()
    return level_at(t.tm_hour * 60 + t.tm_min + t.tm_sec / 60.0, cfg)


# ---- frame scaling ----------------------------------------------------
# A 256-entry byte table applied with bytes.translate() -- that runs in C
# over the whole 12KB frame, so dimming costs microseconds rather than a
# Python loop over 12288 bytes at 24fps. Tables are cached per quantised
# level so a smooth fade doesn't rebuild one every frame.
_TABLES = {}
_QUANT = 64          # distinct brightness steps; finer than the eye resolves here


def _table(level):
    q = max(0, min(_QUANT, int(round(level * _QUANT))))
    tbl = _TABLES.get(q)
    if tbl is None:
        k = q / _QUANT
        tbl = bytes(min(255, int(i * k + 0.5)) for i in range(256))
        _TABLES[q] = tbl
    return tbl


def apply(frame, level):
    """Scale every channel of an RGB frame by `level` (0..1). Returns the
    frame unchanged at full brightness so the common daytime path costs
    nothing at all."""
    if level >= 0.999:
        return frame
    return bytes(frame).translate(_table(level))
