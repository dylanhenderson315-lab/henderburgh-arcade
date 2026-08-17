"""
ambient.py -- owner config for AmbientEngine's channels.

Ambient is a DIRECTOR, not a playlist. The owner picks a channel;
the engine decides what is worth the glass right now.

  auto   -- default show. Sky, then what's happening, then one page.
            Live sports walk every live game in DETAIL. New storm /
            new song take the glass (not mid-slate). Night or a quiet
            world rests on the gallery.
            Clock is a quiet-day breath, not a slot every lap.
  world  -- data only (flights, ISS, sports, weather, ...).
  arcade -- self-playing games as living art.
  mix    -- the owner's own shortlist, in the order they saved.

Read-modify-write, same shape as ownernote/dnd. Never invents a channel
name; a bad file falls back to AUTO and an empty mix (AUTO does not
need a mix).
"""
import json
from pathlib import Path

import catalog

CONFIG_PATH = Path(__file__).parent / "ambient_config.json"

CHANNELS = ("auto", "world", "arcade", "mix")
DEFAULT_CHANNEL = "auto"
# Default MIX is the full visual world -- every glance mode that is
# honest to put on a wall. Owner can trim this list. AUTO/WORLD ignore
# it and already walk the full SEQUENCE.
DEFAULT_MIX = catalog.sequence()


def load_config():
    cfg = {"channel": DEFAULT_CHANNEL, "mix": list(DEFAULT_MIX)}
    if not CONFIG_PATH.exists():
        return cfg
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    ch = str(raw.get("channel") or "").strip().lower()
    if ch in CHANNELS:
        cfg["channel"] = ch
    mix = raw.get("mix")
    if isinstance(mix, list):
        cleaned = []
        for n in mix:
            if isinstance(n, str) and n.strip() and n.strip() not in cleaned:
                cleaned.append(n.strip())
        cfg["mix"] = cleaned
    return cfg


def save_config(channel=None, mix=None):
    """Omitted fields are preserved. Empty mix is allowed (mix channel
    then honestly has nothing and AUTO/WORLD still work)."""
    data = load_config()
    if channel is not None:
        ch = str(channel).strip().lower()
        if ch not in CHANNELS:
            raise ValueError("channel must be one of " + ", ".join(CHANNELS))
        data["channel"] = ch
    if mix is not None:
        if not isinstance(mix, list):
            raise ValueError("mix must be a list of engine names")
        cleaned = []
        for n in mix:
            if isinstance(n, str) and n.strip() and n.strip() not in cleaned:
                cleaned.append(n.strip())
        data["mix"] = cleaned
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return data
