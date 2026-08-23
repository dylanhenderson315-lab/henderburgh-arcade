"""A date invitation for Nicole, served at /date/<secret>.

Tonight is movies + dinner. She picks both -- the movie's vibe (not the
title, she's the one with taste; five vibe cards + surprise) and how
bougie the dinner is (four tiers from gas-station-taquitos to steakhouse).

Personal-rig-only, same scope precedent as atc.py's transcription feature.
Not general-purpose, not config-driven: one specific letter to one
specific person, and the copy lives in date.html where it can be edited
without touching Python.

Two things here are deliberate:

  * SECRET. henderburgh.com may be publicly reachable. A love letter and
    a notification trigger sitting on a guessable path is not something
    to find out about later, so the page is behind a word only she has.
    Obscurity, not auth -- appropriate for the stakes (embarrassment,
    not compromise) and consistent with the project's trusted-LAN
    posture everywhere except /api/notify.

  * Picks are APPENDED, never overwritten. She may open the page twice,
    change her mind, or resubmit with a different dinner tier -- every
    one of those is a real event worth keeping. Same jsonl discipline
    as events_log.py and hangar_log.jsonl. The last row wins for the
    banner; the earlier rows are still there when he wants to look.
"""

import json
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "date_log.jsonl"

# The word only she has. Change it here; the URL follows.
SECRET = "lawdog"

# Movie vibes. Not titles -- she's the one who actually decides what plays,
# this is just a nudge toward what mood she's in. UPPERCASE at rest so the
# panel banner can pass them through paneltext.panel_text() unchanged.
MOVIES = {
    "comfort":  "A COMFORT REWATCH",
    "dumb":     "SOMETHING DUMB AND FUNNY",
    "cry":      "SOMETHING THAT WILL MAKE ME CRY",
    "boom":     "SOMETHING WITH EXPLOSIONS",
    "you":      "YOU PICK, SURPRISE ME",
}

# Dinner tiers. She picks how bougie tonight is. HELI is its own tier
# because the running joke is worth its own tier.
DINNERS = {
    "gas":     "GAS STATION LEGAL MINIMUM",
    "takeout": "TAKEOUT IN SWEATS",
    "nice":    "A NICE SIT DOWN",
    "boujie":  "FULL BOUJIE, REAL NAPKINS",
}

HELI_LABEL = "THE 30 DOLLAR HELICOPTER RIDE"

_lock = threading.Lock()


def label_movie(mid):
    return MOVIES.get(mid)


def label_dinner(did):
    return DINNERS.get(did)


def summary(movie=None, dinner=None, heli=False):
    """One-line summary suitable for the panel banner. Never fabricates -
    unknown ids drop rather than become fake text."""
    parts = []
    m = label_movie(movie) if movie else None
    d = label_dinner(dinner) if dinner else None
    if m:
        parts.append("MOVIE: " + m)
    if d:
        parts.append("DINNER: " + d)
    if heli:
        parts.append("+ " + HELI_LABEL)
    return " / ".join(parts) if parts else "SHE OPENED IT"


def _clean_viewer(v):
    """Trim a `?v=` value down to a tag safe for the panel font and jsonl.
    Empty / non-str returns None so `viewer is None` is the "real Nicole
    open" case everywhere downstream."""
    if not isinstance(v, str):
        return None
    keep = "".join(c for c in v if c.isalnum() or c in " _.-")
    keep = keep.strip()[:40]
    return keep or None


def record(movie=None, dinner=None, heli=False, note=None, viewer=None):
    """Append one real pick. Returns the stored row.

    Accepts partial submits (just a movie, just a dinner, just heli) so
    the page can send progress if he wants that later; today it sends
    the whole thing at once from the final confirm.

    `viewer` is the `?v=` tag from the page URL. Present means a friend
    testing the flow: the row is marked test=True and the banner is
    prefixed so a test pick never masquerades as her real answer. Absent
    means the real Nicole open."""
    v = _clean_viewer(viewer)
    m = label_movie(movie) if movie else None
    d = label_dinner(dinner) if dinner else None
    row = {
        "ts": time.time(),
        "movie": movie if m else None,
        "movie_label": m,
        "dinner": dinner if d else None,
        "dinner_label": d,
        "heli": bool(heli),
        "viewer": v,
        "test": v is not None,
    }
    if isinstance(note, str) and note.strip():
        row["note"] = note.strip()[:500]
    banner = summary(movie, dinner, heli)
    if v:
        # Uppercase for the panel font -- paneltext.panel_text() would
        # do it anyway, doing it here keeps the stored banner string
        # matching what the panel actually draws.
        banner = ("TEST [" + v.upper() + "] ") + banner
    row["banner"] = banner
    with _lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return row


def picks(limit=50):
    """Every real pick, newest last. Missing log file is the normal
    before-state, not an error."""
    if not LOG_PATH.exists():
        return []
    out = []
    with LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:]
