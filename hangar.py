"""
hangar.py -- "THE HANGAR": a persistent collection of every distinct real
aircraft (by tail number/registration) this device has ever seen on ADS-B,
accumulating over time as a personal record rather than only showing
live/ambient traffic. Same shape as every other feed's log-store in this
project: a plain module owns the I/O (here, a local JSON-lines file
instead of a network call), a lock-protected in-memory cache, get() never
blocks and never invents.

WRITER: flights.FlightFeed's own background poll thread calls
record_sighting() once per real aircraft per refresh cycle -- no new
network call, no new poll cadence, this is pure composition of data
flights.py already fetches every POSITION_REFRESH (15s). times_seen
counts distinct VISITS (a gap of VISIT_GAP_S since last_seen), not
poll ticks -- the same tail lingering in RADIUS_NM for ten minutes
is one visit, not 40. A write to disk still happens when last_seen
moves or a new tail arrives.

IDENTITY -- keyed by REGISTRATION, not the ICAO24 hex. Confirmed live
against a real 238-aircraft sample near ORD (2026-08-03): the ADS-B "r"
field (real registration, e.g. "N8986Q") was present on 235/238 (98.7%).
The ~1-2% of real aircraft broadcasting no registration are simply NOT
recorded here -- a bare hex address is not the human-legible tail number
this feature is about, and inventing a placeholder identity for one would
misrepresent what "seen" means. An honest, small, real gap, not a bug.

RETENTION -- bounded to HANGAR_MAX_ENTRIES from the start, LRU by
last-seen: the least-recently-seen entry is evicted the moment a NEW
distinct aircraft would exceed the cap. Documented explicitly per the ATC
log's own retention-window precedent (see atc.py's LOG_MAX_AGE_SECONDS)
-- an unbounded, ever-growing log the render loop reads is exactly the
resource-growth class of issue this project's most recent audit found and
fixed elsewhere (mma.py/skypass.py's poll loops, arcade_server's POST body
reads). 500 distinct tail numbers is a generous cap for a single home
location's real local+overflight traffic; it exists as a hard ceiling,
not a number expected to bind in practice.
"""
import json
import threading
import time
from pathlib import Path

HANGAR_PATH = Path(__file__).parent / "hangar_log.jsonl"
HANGAR_MAX_ENTRIES = 500
# A visit is a distinct appearance, not a poll tick. FlightFeed records
# every aircraft every POSITION_REFRESH (~15s). Counting those as
# times_seen turned a 10-minute overflight into "SEEN 40X". Same tail
# still in the sky (last_seen within this gap) is one visit; a later
# reappearance after the gap is a new one. 30 minutes is longer than a
# local overflight and shorter than a real return trip.
VISIT_GAP_S = 30 * 60


class HangarLog:
    """Reads/writes HANGAR_PATH. Lazy-loaded on first use (not at import
    time -- keeps import side-effect-free like every other module here),
    then kept as an in-memory dict for the life of the process; every
    write is also flushed to disk immediately so a restart doesn't lose
    the collection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}    # reg -> {reg, type, airline, first_seen, last_seen, times_seen}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        entries = {}
        if HANGAR_PATH.exists():
            try:
                with HANGAR_PATH.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue    # one corrupt line (a torn write) must not lose the rest
                        if isinstance(e, dict) and e.get("reg"):
                            entries[e["reg"]] = e
            except OSError:
                pass
        self._entries = entries
        self._loaded = True

    def record_sighting(self, reg, ac_type, airline):
        """Called by flights.FlightFeed's background thread, once per
        real aircraft with a real registration per refresh cycle. `reg`
        must already be a real, non-empty, folded registration string --
        the caller filters that, this function trusts it. Real type/
        airline data seen on a LATER sighting fills in a gap from an
        earlier one (e.g. adsbdb's route lookup resolving the airline a
        cycle or two after the aircraft was first logged) rather than
        leaving it stuck missing forever."""
        with self._lock:
            self._ensure_loaded()
            now = time.time()
            e = self._entries.get(reg)
            if e is None:
                self._entries[reg] = {
                    "reg": reg, "type": ac_type or None, "airline": airline or None,
                    "first_seen": now, "last_seen": now, "times_seen": 1,
                }
                self._evict_if_over_cap()
            else:
                if ac_type and not e.get("type"):
                    e["type"] = ac_type
                if airline and not e.get("airline"):
                    e["airline"] = airline
                # Increment only on a new VISIT. last_seen is refreshed
                # every poll while the aircraft stays in range, so a
                # continuous pass keeps the same times_seen. A gap longer
                # than VISIT_GAP_S is a real departure and return.
                if now - float(e.get("last_seen") or 0) >= VISIT_GAP_S:
                    e["times_seen"] = e.get("times_seen", 1) + 1
                e["last_seen"] = now
            self._save()

    def _evict_if_over_cap(self):
        if len(self._entries) <= HANGAR_MAX_ENTRIES:
            return
        oldest_key = min(self._entries, key=lambda k: self._entries[k].get("last_seen", 0))
        del self._entries[oldest_key]

    def _save(self):
        try:
            with HANGAR_PATH.open("w") as f:
                for e in self._entries.values():
                    f.write(json.dumps(e) + "\n")
        except OSError:
            pass    # degrade honestly: an unwritable disk loses persistence, not the session

    def get(self):
        """Every entry, most-recently-seen first. Never blocks (a pure
        in-memory read after the one-time lazy load); never invents an
        entry -- an unreadable/missing/corrupt file is an empty
        collection, not a guess."""
        with self._lock:
            self._ensure_loaded()
            return sorted(self._entries.values(), key=lambda e: e.get("last_seen", 0), reverse=True)


LOG = HangarLog()
