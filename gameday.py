"""
gameday.py -- config for GAME DAY, the event/takeover mode.

Tiny on purpose. GAME DAY needs exactly one piece of persistent state --
what tonight is about -- and its actual data comes from feeds that already
exist (mma.FEED for a UFC card, sports.FEED for a team game). This module
exists so that one setting lives with the other *_config.json files
instead of being hand-edited into a mode.

Same mtime-cached pattern as brightness.py so the render loop can read it
every tick without touching the filesystem each time.

WHY THIS MODE IS A DIFFERENT CATEGORY (see CLAUDE.md):
every other data mode is a GLANCE mode -- it assumes it is sharing the
panel, gets a slice of attention, and must stay legible in a rotation.
GAME DAY assumes the opposite: it is the only thing happening tonight,
nothing else competes for the panel, and it should therefore be as
detailed and as dramatic as the hardware allows. It is opt-in, it takes
over, and it hands the panel back when the event is genuinely over.
"""
import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "gameday_config.json"

TARGETS = ("ufc", "team")
DEFAULTS = {
    "target": "ufc",        # "ufc" -> next/current UFC card; "team" -> pinned team
    "auto_exit": True,      # hand the panel back when the event ends
}

_lock = threading.Lock()
_cache = {"cfg": None, "mtime": None}


def load_config():
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        save_config(DEFAULTS)
        return dict(DEFAULTS)

    with _lock:
        if _cache["cfg"] is not None and _cache["mtime"] == mtime:
            return dict(_cache["cfg"])

    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict):
            t = str(raw.get("target", DEFAULTS["target"])).strip().lower()
            cfg["target"] = t if t in TARGETS else DEFAULTS["target"]
            cfg["auto_exit"] = bool(raw.get("auto_exit", DEFAULTS["auto_exit"]))
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass          # a bad config file must never stop the panel

    with _lock:
        _cache["cfg"], _cache["mtime"] = dict(cfg), mtime
    return cfg


def save_config(cfg):
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    if merged["target"] not in TARGETS:
        merged["target"] = DEFAULTS["target"]
    merged["auto_exit"] = bool(merged["auto_exit"])
    CONFIG_PATH.write_text(json.dumps(merged, indent=2))
    with _lock:
        _cache["cfg"], _cache["mtime"] = None, None
    return merged
