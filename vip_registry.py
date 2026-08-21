"""
vip_registry.py -- a SELF-BUILT, permanent registry of real aircraft
this device has personally confirmed flying a real U.S. government VIP
callsign (AF1/AF2/MARINE1/MARINE2/SAM.../SPAR... -- see
flights._is_vip_callsign()). Same shape as hangar.py: a plain module
owning a local JSON-lines file, a lock-protected in-memory cache, get()
never blocks and never invents.

WHY THIS EXISTS, and why it is NOT a seeded tail-number/hex watchlist
(CLAUDE.md's own "Deliberately deferred" section already rejected that
category of feature -- asserting a specific real registration belongs
to a specific real government aircraft without a verified source would
be fabricating an identity claim). This registry never asserts anything
external: every entry here exists ONLY because THIS device's own ADS-B
feed personally witnessed that exact real registration broadcasting a
real, confirmed VIP callsign at least once. That is a genuinely
different epistemic category -- a real, first-hand observation, not an
assertion about a public list.

WHAT THIS BUYS: the sitting President is not aboard SAM28000/SAM29000
(the real VC-25A tail numbers) every time they fly -- ATC only calls it
"Air Force One" ON THE RADIO while he genuinely is aboard, and the
SAME real airframe flies plain "SAM" callsigns the rest of the time
(both real, well-documented facts -- see flights.py's own VIP_CALLSIGN_*
docstring). Once this device has seen a tail broadcast ANY real VIP
callsign once, recognizing that SAME tail again later -- even on a day
it happens to broadcast a plain, non-VIP-pattern callsign, or none at
all -- is honestly derivable: it is still the real, same, historically
notable airframe, confirmed by this device's own prior sighting, not a
new guess.

WRITER: flights.FlightFeed's own background poll thread, inside the
SAME real VIP-callsign adopt-then-diff cycle that already drives the
celebration/scope treatment and the events-log entry -- zero new I/O,
zero new poll cadence, pure composition of data already fetched.

READER: flights._notable() cannot call this directly (it is a pure,
I/O-free function reused everywhere in this module) -- instead
_fetch_positions() checks it once per aircraft, immediately after
computing the real callsign-based notable() result, and only USES a
registry hit when the real registration is present and the callsign
check itself found nothing this cycle. A real callsign match always
wins; the registry is the honest fallback for "we don't see a VIP
callsign THIS cycle, but we've confirmed this exact tail before."

IDENTITY -- keyed by REGISTRATION, same reasoning as hangar.py: an
aircraft broadcasting no real registration is simply never recorded or
matched here, an honest small gap rather than a hex-keyed placeholder
identity.

RETENTION -- bounded (VIP_REGISTRY_MAX), LRU by last-seen, same
discipline as HANGAR_MAX_ENTRIES. Real VIP sightings are inherently
rare, so this cap is not expected to bind in practice; it exists as a
hard ceiling on principle, matching every other bounded log-store in
this project.
"""
import json
import threading
import time
from pathlib import Path

VIP_REGISTRY_PATH = Path(__file__).parent / "vip_registry.jsonl"
VIP_REGISTRY_MAX = 25


class VipRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}   # reg -> {reg, label, type, first_seen, last_seen, times_seen}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        entries = {}
        if VIP_REGISTRY_PATH.exists():
            try:
                with VIP_REGISTRY_PATH.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue   # one corrupt line (a torn write) must not lose the rest
                        if isinstance(e, dict) and e.get("reg"):
                            entries[e["reg"]] = e
            except OSError:
                pass
        self._entries = entries
        self._loaded = True

    def _save(self):
        try:
            with VIP_REGISTRY_PATH.open("w") as f:
                for e in self._entries.values():
                    f.write(json.dumps(e) + "\n")
        except OSError:
            pass   # a failed write loses nothing already confirmed in memory this run

    def _evict_if_over_cap(self):
        if len(self._entries) <= VIP_REGISTRY_MAX:
            return
        oldest_reg = min(self._entries, key=lambda r: self._entries[r].get("last_seen", 0))
        del self._entries[oldest_reg]

    def record(self, reg, label, ac_type=None):
        """Called ONLY from flights.py's own real VIP-callsign
        adopt-then-diff cycle, i.e. only when a real callsign match just
        fired -- `reg`/`label` must already be real, non-empty, folded
        strings. A later real sighting with a genuinely different real
        label (e.g. first confirmed as a bare "GOVT VIP FLIGHT" SPAR
        callsign, later confirmed as the specific "AIR FORCE ONE" AF1
        callsign on the same tail) upgrades the stored label rather than
        keeping the earlier, less specific one -- the more specific real
        fact is the more useful one to remember."""
        if not reg or not label:
            return
        with self._lock:
            self._ensure_loaded()
            now = time.time()
            e = self._entries.get(reg)
            if e is None:
                self._entries[reg] = {
                    "reg": reg, "label": label, "type": ac_type or None,
                    "first_seen": now, "last_seen": now, "times_seen": 1,
                }
                self._evict_if_over_cap()
                self._save()
                return
            if label == "AIR FORCE ONE" or label == "AIR FORCE TWO" or label == "MARINE ONE":
                e["label"] = label   # a more specific real confirmation wins
            if ac_type and not e.get("type"):
                e["type"] = ac_type
            e["last_seen"] = now
            e["times_seen"] = e.get("times_seen", 1) + 1
            self._save()

    def is_known(self, reg):
        """Real stored label for a previously-confirmed real registration,
        or None -- never a guess, never fires for a registration this
        device has not itself already witnessed broadcasting a real VIP
        callsign at least once."""
        if not reg:
            return None
        with self._lock:
            self._ensure_loaded()
            e = self._entries.get(reg)
            return e["label"] if e else None

    def get(self):
        """Real snapshot of the whole registry, for control-panel/status
        surfacing -- a copy, never the live dicts, same mutation-safety
        convention every other FEED.get() in this project follows."""
        with self._lock:
            self._ensure_loaded()
            return [dict(e) for e in self._entries.values()]


LOG = VipRegistry()
