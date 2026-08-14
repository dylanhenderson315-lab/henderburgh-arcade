"""
skins.py -- a real, LOCAL, config-driven visual-theme system.

SCOPE NOTE, stated up front because the feature this was inspired by
(ChuckBuilds/LEDMatrix's real plugin store) explicitly lets a user
install plugins fetched from arbitrary GitHub repos. This project does
NOT build that: downloading and executing remote code on this device is
out of scope regardless of the request that prompted this file -- no
exception, no opt-in flag. What ChuckBuilds actually ships as TWO
separate concepts (plugins own data/scheduling/caching; skins restyle
the screens a plugin's data feeds) maps cleanly onto this project's own
existing feed/engine split -- feed modules already own all data, engine
classes already own all rendering -- so the only genuinely new, safe
piece is a skin: a named set of CHROME color overrides (header/divider/
ink/accent-adjacent colors), never the real, meaningful data-driven
colors (a team's own real ESPN color, an altitude band, a severity
color) -- those stay real and untouched by any skin, the same "never
invent a color, only recolor the frame around it" discipline this
project's brightness.py and every unit-conversion helper already follow
for a different axis (brightness, units) of the same rule.

PILOT SCOPE, EXPANDED (2026-08-10): wired into SportsEngine, WeatherEngine
and ClockEngine -- still not blanket-applied to every mode (flights'
scope, satellite's dome, and the news/blog tickers keep their own fixed
palettes this pass; extend the same `_apply_skin()` idiom to any of
those in a future pass rather than guessing at scope not asked for).
Skins are pure UI-chrome overrides read once per reset()/tick() (a cheap
local file read, same cost class as dnd.py's own is_enabled() -- no new
I/O, no per-frame cost beyond an already-cached dict lookup).

Same shape as dnd.py: one JSON config file, safe defaults on first run,
read-modify-write, cached on file mtime.
"""
import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "skins_config.json"

_lock = threading.Lock()
_cache = {"cfg": None, "mtime": None}

# Each skin overrides a small, deliberately limited set of CHROME colors
# -- never a real data-driven color (team colors, altitude bands, alert
# severity). "classic" is the product's own existing default palette,
# kept as real named entries here (not just an implicit fallback) so
# switching back to it is a real, symmetric choice, not "off".
#
# `accent` is new (2026-08-10, skin expansion pass) -- WeatherEngine and
# ClockEngine both lead with one dominant ACCENT color rather than
# SportsEngine's INK/INK_DIM pairing being their primary chrome color, so
# a skin needs to carry both to mean anything on those two modes. Every
# skin below was re-picked for real HUE separation from its neighbors
# (not just a brightness/saturation nudge on the same hue), so switching
# skins reads as a genuinely different mode, not a subtle tint change.
SKINS = {
    "classic": {
        "label": "CLASSIC",
        "accent": (120, 200, 255),
        "ink": (150, 160, 185), "ink_dim": (70, 76, 92),
        "win": (60, 230, 110), "lose": (255, 70, 80),
        "live": (255, 226, 60), "stale": (255, 170, 40),
        "hero_ink": (235, 242, 255),
    },
    "midnight": {
        "label": "MIDNIGHT",
        "accent": (110, 130, 255),
        "ink": (140, 160, 220), "ink_dim": (55, 62, 90),
        "win": (80, 200, 255), "lose": (255, 90, 140),
        "live": (150, 180, 255), "stale": (120, 140, 200),
        "hero_ink": (220, 230, 255),
    },
    "sunset": {
        "label": "SUNSET",
        "accent": (255, 130, 60),
        "ink": (255, 190, 140), "ink_dim": (110, 70, 50),
        "win": (255, 180, 60), "lose": (255, 70, 90),
        "live": (255, 140, 60), "stale": (255, 110, 40),
        "hero_ink": (255, 235, 220),
    },
    "mono": {
        "label": "MONO",
        "accent": (210, 210, 210),
        "ink": (200, 200, 200), "ink_dim": (90, 90, 90),
        "win": (255, 255, 255), "lose": (120, 120, 120),
        "live": (230, 230, 230), "stale": (150, 150, 150),
        "hero_ink": (255, 255, 255),
    },
    "forest": {
        "label": "FOREST",
        "accent": (90, 200, 110),
        "ink": (170, 210, 160), "ink_dim": (60, 90, 60),
        "win": (140, 230, 90), "lose": (230, 120, 60),
        "live": (210, 230, 90), "stale": (200, 170, 70),
        "hero_ink": (225, 245, 220),
    },
    "neon": {
        "label": "NEON",
        "accent": (255, 60, 220),
        "ink": (100, 245, 235), "ink_dim": (60, 90, 95),
        "win": (60, 255, 170), "lose": (255, 60, 130),
        "live": (255, 230, 60), "stale": (180, 90, 255),
        "hero_ink": (220, 255, 250),
    },
}
DEFAULT_SKIN = "classic"


def load_config():
    """{"skin": name}. Cached on file mtime, same pattern dnd.py and
    brightness.py both already use."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return {"skin": DEFAULT_SKIN}

    with _lock:
        if _cache["cfg"] is not None and _cache["mtime"] == mtime:
            return dict(_cache["cfg"])

    cfg = {"skin": DEFAULT_SKIN}
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict) and raw.get("skin") in SKINS:
            cfg["skin"] = raw["skin"]
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass          # keep the honest default; never crash the render loop over a bad file

    with _lock:
        _cache["cfg"], _cache["mtime"] = dict(cfg), mtime
    return cfg


def save_config(skin):
    if skin not in SKINS:
        raise ValueError(f"unknown skin: {skin!r}")
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    data["skin"] = skin
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    with _lock:
        _cache["cfg"], _cache["mtime"] = None, None      # force reload
    return {"skin": data["skin"]}


def get_active():
    """The active skin's real color dict, always a valid entry (falls
    back to DEFAULT_SKIN if the config names one that no longer exists
    -- e.g. after a code change removed it -- rather than crashing)."""
    name = load_config().get("skin", DEFAULT_SKIN)
    return SKINS.get(name, SKINS[DEFAULT_SKIN])


def list_skins():
    return [{"id": k, "label": v["label"]} for k, v in SKINS.items()]
