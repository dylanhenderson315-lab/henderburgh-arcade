"""
events_log.py -- a persistent "recent events" memory: what just happened
across three real sources this project already has -- planes that
entered the configured window, big sports moments, and Home Assistant
notifications received. Same shape as hangar.py: a plain module owns a
local JSON-lines file, a lock-protected in-memory list, lazy-loaded on
first use, get() never blocks and never invents.

OWNER'S OWN WORDS: "A simple recent events / log view (last planes that
came through the window, last big sports moments, last HA notifications).
People like being able to look back."

RETENTION -- COUNT-BASED, reusing hangar.py's exact proven LRU-eviction
shape (see hangar_audit.py, which proved that pattern correct at the
boundary this same session) rather than age-based. Chosen over age-based
because this log's natural write rate is bursty and unpredictable (a
quiet week produces zero events, an active evening might produce a dozen
plane-in-window entries in a few minutes) -- a fixed count cap is a
simple, honest ceiling on real disk/memory use regardless of how bursty
the real event rate turns out to be, and it is the exact shape already
proven correct this session rather than a second, unverified eviction
policy. EVENTS_MAX_ENTRIES = 500, same generous ceiling hangar.py uses,
not expected to bind in practice for a personal device's real event rate.

WRITER DISCIPLINE -- each entry's `summary` is a short, ALREADY-FOLDED
(paneltext.panel_text()) display string, folded by the CALLER at the
point of recording -- same "fold at the boundary" discipline as every
other feed in this project (see fold_audit.py's own docstring on why a
per-module fold is not enough, and CLAUDE.md's 2026-08-09 fold-coverage
gap audit). This module never folds anything itself; it only stores and
returns exactly what it was given.

`kind` is a plain string tag ("plane"/"sports"/"notify"), not an enum
class -- matches this codebase's existing style (hangar.py's own plain
dict entries, sports.py's plain string tiers/kinds).
"""
import json
import threading
import time
from pathlib import Path

EVENTS_LOG_PATH = Path(__file__).parent / "events_log.jsonl"
EVENTS_MAX_ENTRIES = 500


class EventsLog:
    """Reads/writes EVENTS_LOG_PATH. Lazy-loaded on first use (not at
    import time -- keeps import side-effect-free like every other module
    here), then kept as an in-memory list for the life of the process;
    every write is also flushed to disk immediately so a restart doesn't
    lose the log."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []    # [{ts, kind, summary}, ...], oldest first
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        entries = []
        if EVENTS_LOG_PATH.exists():
            try:
                with EVENTS_LOG_PATH.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue    # one corrupt line (a torn write) must not lose the rest
                        if isinstance(e, dict) and e.get("summary") and e.get("kind"):
                            entries.append(e)
            except OSError:
                pass
        self._entries = entries
        self._loaded = True

    def record(self, kind, summary, detail=None):
        """Append one real event. `summary` must already be a real,
        non-empty, folded display string -- the caller folds it, this
        function trusts it and never invents one. `detail` is optional,
        stored verbatim (real data only, e.g. a real aircraft dict or
        real ESPN play text) for a future richer view; never required by
        get()."""
        if not summary:
            return
        with self._lock:
            self._ensure_loaded()
            e = {"ts": time.time(), "kind": kind, "summary": summary}
            if detail is not None:
                e["detail"] = detail
            self._entries.append(e)
            self._evict_if_over_cap()
            self._save()

    def _evict_if_over_cap(self):
        # Same LRU-by-recency shape as hangar.py's eviction, specialized
        # for an append-only log: the oldest entry is simply the first
        # one in insertion order, so eviction is a pop from the front --
        # no separate "last_seen" bookkeeping needed since nothing here
        # is ever updated in place, only appended.
        while len(self._entries) > EVENTS_MAX_ENTRIES:
            self._entries.pop(0)

    def _save(self):
        try:
            with EVENTS_LOG_PATH.open("w") as f:
                for e in self._entries:
                    f.write(json.dumps(e) + "\n")
        except OSError:
            pass    # degrade honestly: an unwritable disk loses persistence, not the session

    def get(self, limit=None):
        """Most-recent-first list of real events. Never blocks (a pure
        in-memory read after the one-time lazy load); never invents an
        entry -- an unreadable/missing/corrupt file is an empty log, not
        a guess. `limit` caps how many are returned, most recent first;
        None returns everything."""
        with self._lock:
            self._ensure_loaded()
            out = list(reversed(self._entries))
        if limit is not None:
            out = out[:limit]
        return out


LOG = EventsLog()
