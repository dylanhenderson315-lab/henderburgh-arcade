"""
ownernote.py -- TODAY, the owner's work note. A real checklist on the wall.

Reframed 2026-08-17 from a single 84-char "sticky" into a proper
work-note tool -- the kind of thing you actually run a workday on: a
short list of today's line-items, each one checkable, an optional
heading, held on the glass until you change it. Still HONEST: only ever
what the owner typed, folded through paneltext.panel_text() at the write
boundary, never invented.

Not a notebook and not a notification:
  * `/api/notify` is HA-pushed and ephemeral.
  * `blog.py` is the public guestbook.
  * This is the owner's own active list -- typed from the phone, one task
    per line, `x ` in front to check one off, folded at save, held on the
    panel until they change or clear it.

DATA MODEL (backward-compatible):
    {"title": str|None,
     "items": [{"text": str, "done": bool}, ...],
     "saved_at": float|None}
An OLD single-`text` config ({"text": "..."}) still loads -- its lines
become items so nothing the owner ever typed is lost.

The panel draws a heading + up to LIST_ROWS checkable rows at ~13 glyphs
each. More items than fit stay on disk and are counted honestly on the
glass ("+N MORE"), never a lie. An empty note is None-shaped (no items),
never a quote.

No network I/O. Disk is the write boundary. Readers use an in-memory
cache so AmbientEngine's tick does not open the file every frame.
"""
import json
import time
from pathlib import Path

import paneltext

CONFIG_PATH = Path(__file__).parent / "ownernote_config.json"

# 64x64 math: a checkable row is a 5px box + gap + text, so the text
# budget is WIDTH-11 = 53px = ~13 glyphs at the 4px/glyph pitch.
COLS = 13
# The panel is a glance surface, not a document -- a checklist longer
# than this stops being scannable from across the room. Extra items are
# kept on disk and counted ("+N MORE"), never dropped silently.
MAX_ITEMS = 12
TITLE_COLS = 14

# Frictionless "check one off" markers a line can start with, phone-typed.
_DONE_MARKERS = ("x ", "[x]", "[x] ", "* ")

_cache = None
_mtime = None


# ---------------------------------------------------------------------------
# parsing helpers -- turn any typed shape into a clean item list
# ---------------------------------------------------------------------------
def _parse_line(line):
    """One raw text line -> (text, done). A leading `x ` / `[x]` / `* `
    (case-insensitive) checks the item off; everything else is the task."""
    s = str(line).strip()
    done = False
    low = s.lower()
    for m in _DONE_MARKERS:
        if low.startswith(m):
            done = True
            s = s[len(m):].strip()
            break
    return s, done


def _lines_to_items(raw):
    """A raw multi-line blob (phone textarea, or a legacy sticky) -> items.
    Blank lines are dropped; each real line is one task."""
    items = []
    for line in str(raw).splitlines():
        s, done = _parse_line(line)
        if s:
            items.append({"text": s, "done": done})
    return items


def _items_from_any(value):
    """Accept whatever the caller has: None, a raw text blob, a list of
    strings (each parsed for its own done-marker), or a list of item
    dicts. Returns a clean, UNFOLDED [{'text','done'}] list."""
    if value is None:
        return []
    if isinstance(value, str):
        return _lines_to_items(value)
    if isinstance(value, list):
        out = []
        for it in value:
            if isinstance(it, dict):
                t = str(it.get("text") or "").strip()
                if t:
                    out.append({"text": t, "done": bool(it.get("done"))})
            elif isinstance(it, str):
                s, done = _parse_line(it)
                if s:
                    out.append({"text": s, "done": done})
        return out
    return []


def _blank():
    return {"title": None, "items": [], "saved_at": None}


def _coerce(raw):
    """Normalize any on-disk shape into the current model. Back-compat:
    an old {"text": "..."} sticky becomes items so it is never lost."""
    if not isinstance(raw, dict):
        return _blank()
    title = raw.get("title")
    title = (str(title).strip() or None) if title else None
    saved = raw.get("saved_at")
    saved = saved if isinstance(saved, (int, float)) else None
    if isinstance(raw.get("items"), list):
        items = _items_from_any(raw["items"])[:MAX_ITEMS]
        return {"title": title, "items": items, "saved_at": saved}
    # Old single-`text` sticky -> items (each line a task), nothing lost.
    v = raw.get("text")
    if v:
        return {"title": title, "items": _lines_to_items(v)[:MAX_ITEMS],
                "saved_at": saved}
    return {"title": title, "items": [], "saved_at": saved}


def _clone(d):
    return {"title": d.get("title"),
            "items": [dict(it) for it in d.get("items", [])],
            "saved_at": d.get("saved_at")}


# ---------------------------------------------------------------------------
# load / save -- disk is the write boundary; fold happens on save
# ---------------------------------------------------------------------------
def _read_disk():
    if not CONFIG_PATH.exists():
        return _blank()
    try:
        return _coerce(json.loads(CONFIG_PATH.read_text()))
    except (json.JSONDecodeError, OSError, AttributeError, TypeError, ValueError):
        return _blank()


def load_config():
    """The current note. Cached; re-reads only when the file mtime changes."""
    global _cache, _mtime
    try:
        mt = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
    except OSError:
        return _clone(_cache) if _cache is not None else _blank()
    if _cache is not None and mt == _mtime:
        return _clone(_cache)
    data = _read_disk()
    _cache = data
    _mtime = mt
    return _clone(data)


def save_config(items=None, title=None):
    """Fold everything at the WRITE boundary and persist.

    `items` may be a raw text blob (one task per line, `x ` to check),
    a list of strings, or a list of {'text','done'} dicts -- read-modify
    happens above this in _items_from_any. An empty result clears the
    note (items == [], the one real "remove it" path). `title` is an
    optional heading; empty/whitespace means no heading.
    """
    global _cache, _mtime
    parsed = _items_from_any(items)
    folded = []
    for it in parsed:
        ft = paneltext.panel_text(it["text"]).strip() if it["text"] else ""
        if ft:
            folded.append({"text": ft, "done": bool(it["done"])})
        if len(folded) >= MAX_ITEMS:
            break
    ftitle = None
    if title:
        ftitle = paneltext.panel_text(str(title)).strip() or None
    data = {"title": ftitle, "items": folded,
            "saved_at": time.time() if folded else None}
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    _cache = _clone(data)
    try:
        _mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        _mtime = None
    return _clone(data)


# ---------------------------------------------------------------------------
# preview -- what the panel will actually hold, folded/counted, for the UIs
# ---------------------------------------------------------------------------
def to_body(cfg):
    """Reconstruct the phone textarea from a config: one task per line,
    checked items prefixed `x `. Round-trips through save_config()."""
    lines = []
    for it in cfg.get("items", []):
        lines.append(("x " if it.get("done") else "") + it["text"])
    return "\n".join(lines)


def preview(cfg):
    """Derived facts the phone/control-panel show without re-deriving the
    fold: per-item fit, done/total counts, the reconstructed textarea.
    Never invents an item -- only ever mirrors what is on disk."""
    items = cfg.get("items") or []
    done = sum(1 for it in items if it.get("done"))
    return {
        "cols": COLS,
        "title_cols": TITLE_COLS,
        "max_items": MAX_ITEMS,
        "count": len(items),
        "done": done,
        "open": len(items) - done,
        "items_fit": [len(it["text"]) <= COLS for it in items],
        "body": to_body(cfg),
    }
