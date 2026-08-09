"""
dnd.py -- a real Do Not Disturb / focus mode: a manual owner toggle that
suppresses DISCRETIONARY interrupts (the plane-in-window takeover,
sports-celebration flashes/interrupts, normal-priority HA banners) while
never touching the two things that are never optional:

  * severe-weather takeover (arcade_server._severe_alert_frame()) --
    life-safety, completely untouched, no DND check anywhere near it.
  * urgent-priority HA notifications -- the owner explicitly marked these
    urgent at the source (a real Home Assistant automation choosing
    priority: "urgent"), the same reasoning iOS/Android Focus modes use
    for their own "still get through" exception: DND suppresses ambient
    noise, not something a real automation deliberately escalated.

Deliberately a plain manual toggle, not a schedule -- night/quiet-hours
BRIGHTNESS scheduling already exists (brightness.py, time-based dimming),
a genuinely separate concern from "am I actively trying not to be
interrupted right now" (working, a call, asleep with the panel still lit
as a lamp). If a scheduled DND is ever wanted, it is a real, separate
follow-up (see CLAUDE.md), not folded into this file preemptively.

Same shape as every other tiny config module here: a JSON file, safe
defaults on first run, read-modify-write (though this file currently owns
exactly one key -- built in from the start per the 2026-08-09 lesson on
save_*() functions that construct a fresh dict instead).
"""
import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "dnd_config.json"

_lock = threading.Lock()
_cache = {"cfg": None, "mtime": None}


def load_config():
    """{"enabled": bool}. Cached on file mtime, same pattern
    brightness.load_config() already uses -- this is read every render
    tick by more than one call site (the plane-in-window trigger check
    AND the sports-celebration tier gate), so a cheap cached read matters
    here the same way it did there."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return {"enabled": False}

    with _lock:
        if _cache["cfg"] is not None and _cache["mtime"] == mtime:
            return dict(_cache["cfg"])

    cfg = {"enabled": False}
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict):
            cfg["enabled"] = bool(raw.get("enabled", False))
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass          # keep the honest default; never crash the render loop over a bad file

    with _lock:
        _cache["cfg"], _cache["mtime"] = dict(cfg), mtime
    return cfg


def save_config(enabled):
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    data["enabled"] = bool(enabled)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    with _lock:
        _cache["cfg"], _cache["mtime"] = None, None      # force reload
    return {"enabled": data["enabled"]}


def is_enabled():
    return bool(load_config().get("enabled"))
