"""
ownernote.py -- today's work note. One sticky on the wall.

This is not a notebook and not a notification:
  * `/api/notify` is HA-pushed and ephemeral.
  * `blog.py` is the public guestbook.
  * This is the owner's one active line — typed from the phone,
    folded at save, held on the panel until they change or clear it.

The panel can draw 6 lines × 14 glyphs (~84 characters). Anything past
that stays on disk but is not a lie on the glass: the capture UI shows
the budget before save. Empty is None, never a quote.

No network I/O. Disk is the write boundary. Readers use an in-memory
cache so AmbientEngine's 20 Hz tick does not open the file every frame.
"""
import json
from pathlib import Path

import paneltext

CONFIG_PATH = Path(__file__).parent / "ownernote_config.json"

# 64×64 math: wrap width is WIDTH-6 = 58px; 3×5 + 1px gap = 4px/glyph.
COLS = 14
LINES = 6
BUDGET = COLS * LINES

_cache = None
_mtime = None


def _read_disk():
    if not CONFIG_PATH.exists():
        return {"text": None}
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict):
            v = raw.get("text")
            return {"text": (str(v).strip() or None) if v else None}
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass
    return {"text": None}


def load_config():
    """{"text": str|None}. Cached; re-reads only when the file mtime changes."""
    global _cache, _mtime
    try:
        mt = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
    except OSError:
        return dict(_cache) if _cache is not None else {"text": None}
    if _cache is not None and mt == _mtime:
        return dict(_cache)
    data = _read_disk()
    _cache = data
    _mtime = mt
    return dict(data)


def preview(text):
    """What the panel will actually hold. Folded, counted, never invented."""
    folded = paneltext.panel_text(text) if text else ""
    folded = folded.strip()
    chars = len(folded)
    return {
        "folded": folded or None,
        "chars": chars,
        "budget": BUDGET,
        "fits": chars <= BUDGET,
        "cols": COLS,
        "lines": LINES,
    }


def save_config(text):
    """Fold at the write boundary. Empty/whitespace clears the note."""
    global _cache, _mtime
    folded = paneltext.panel_text(text) if text else None
    data = {"text": folded or None}
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    _cache = dict(data)
    try:
        _mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        _mtime = None
    return data
