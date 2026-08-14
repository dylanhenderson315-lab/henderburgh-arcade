"""
hangar_audit.py -- prove HANGAR_MAX_ENTRIES eviction actually works at the
boundary, not just in prose.

    .venv/bin/python hangar_audit.py

WHY THIS EXISTS. hangar.py's own docstring asserts a real, load-bearing
claim: "bounded to HANGAR_MAX_ENTRIES from the start, LRU by last-seen --
the least-recently-seen entry is evicted the moment a NEW distinct
aircraft would exceed the cap." Found during the 2026-08-09 full-project
gap audit: nothing in this codebase ever demonstrated that claim. A real
collection won't hit 500 distinct tail numbers for a long time, so a
regression here (off-by-one in the cap check, evicting the WRONG entry,
evicting on an UPDATE instead of only a new insert) would sit silent for
months before anyone noticed the collection wasn't actually bounded.

NEVER touches the real singleton (hangar.LOG) or the real hangar_log.jsonl
file -- that file holds a real, live collection (hundreds of real logged
aircraft as of this session). Every check here constructs a FRESH
HangarLog() against a temp path, verified to be a real temp file (never
the real HANGAR_PATH) before any write happens.
"""
import sys
import tempfile
import time
from pathlib import Path

import hangar

_REAL_HANGAR_PATH = hangar.HANGAR_PATH   # captured at import time, before any patching


def _fresh_log(tmp_path):
    """A HangarLog() instance pointed at a throwaway file -- never the
    real hangar_log.jsonl. Patches hangar.HANGAR_PATH for the duration
    since HangarLog._save()/_ensure_loaded() read the module-level
    constant directly, not an instance attribute."""
    assert tmp_path != _REAL_HANGAR_PATH, "refusing to touch the real hangar log"
    log = hangar.HangarLog()
    return log


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print("  %-42s %s%s" % (name, status, ("  " + detail) if detail and not cond else ""))
    return 0 if cond else 1


def main():
    bad = 0
    real_path = hangar.HANGAR_PATH
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td) / "hangar_audit_test.jsonl"
        hangar.HANGAR_PATH = tmp_path
        try:
            log = _fresh_log(tmp_path)

            # ---- fills to exactly the cap, no eviction yet -------------
            # Staggered last_seen (ascending with insertion order) so
            # "oldest" is deterministic -- record_sighting() stamps
            # time.time() itself, and 500 real calls in a tight loop could
            # theoretically tie at the same float on a fast machine, which
            # would make the eviction target ambiguous and this test
            # flaky. Stamp last_seen directly after each insert instead of
            # relying on wall-clock spacing.
            for i in range(hangar.HANGAR_MAX_ENTRIES):
                reg = "N%04dTEST" % i
                log.record_sighting(reg, "C172", None)
                log._entries[reg]["last_seen"] = float(i)
            bad += check("fills to cap with no eviction",
                         len(log._entries) == hangar.HANGAR_MAX_ENTRIES,
                         "got %d entries" % len(log._entries))
            bad += check("oldest entry (N0000TEST) still present at cap",
                         "N0000TEST" in log._entries)

            # ---- one more distinct aircraft: must evict exactly one, the
            # least-recently-seen one -----------------------------------
            log.record_sighting("N0500TEST", "B738", "TEST AIRLINES")
            log._entries["N0500TEST"]["last_seen"] = 1000.0
            bad += check("still exactly at cap after over-cap insert",
                         len(log._entries) == hangar.HANGAR_MAX_ENTRIES,
                         "got %d entries" % len(log._entries))
            bad += check("real LRU victim (N0000TEST, oldest last_seen) evicted",
                         "N0000TEST" not in log._entries)
            bad += check("next-oldest (N0001TEST) survived -- only ONE evicted",
                         "N0001TEST" in log._entries)
            bad += check("the new arrival (N0500TEST) is present",
                         "N0500TEST" in log._entries)

            # ---- a REPEAT sighting of an existing entry must NOT evict
            # anything -- eviction only triggers on a genuinely NEW
            # distinct registration (record_sighting's `if e is None`
            # branch), never on an update to times_seen/last_seen for an
            # aircraft already in the collection. -----------------------
            before = set(log._entries)
            # last_seen was stamped to the insertion index (a 1970-era
            # unix time) so LRU eviction is deterministic. A same-visit
            # poll needs a FRESH last_seen or the visit-gap logic would
            # treat 50 years of silence as a return trip.
            log._entries["N0001TEST"]["last_seen"] = time.time()
            log.record_sighting("N0001TEST", "C172", None)   # same visit
            bad += check("repeat sighting does not change collection size",
                         len(log._entries) == hangar.HANGAR_MAX_ENTRIES)
            bad += check("repeat sighting evicts nothing (same key set)",
                         set(log._entries) == before)
            bad += check("same-visit poll does not increment times_seen",
                         log._entries["N0001TEST"]["times_seen"] == 1,
                         "got %r" % log._entries["N0001TEST"]["times_seen"])
            # A real return after VISIT_GAP_S is a new visit.
            log._entries["N0001TEST"]["last_seen"] = (
                time.time() - hangar.VISIT_GAP_S - 1)
            log.record_sighting("N0001TEST", "C172", None)
            bad += check("return after visit gap increments times_seen",
                         log._entries["N0001TEST"]["times_seen"] == 2,
                         "got %r" % log._entries["N0001TEST"]["times_seen"])

            # ---- LRU actually tracks last_seen, not insertion order:
            # touching an OLD entry (bumping its last_seen forward) must
            # protect it from being the next eviction victim. -----------
            log._entries["N0002TEST"]["last_seen"] = 2000.0   # now the freshest
            log.record_sighting("N0501TEST", "A320", None)
            log._entries["N0501TEST"]["last_seen"] = 2001.0
            bad += check("recently-touched entry (N0002TEST) survives eviction",
                         "N0002TEST" in log._entries)
            bad += check("the actual next-oldest (N0003TEST) was evicted instead",
                         "N0003TEST" not in log._entries)

            # ---- persistence round-trip: a fresh HangarLog() against the
            # SAME temp file reloads exactly what was saved, nothing
            # silently dropped or duplicated by the write path. ---------
            reloaded = hangar.HangarLog()
            reloaded._ensure_loaded()
            bad += check("persisted file reloads to the same entry count",
                         len(reloaded._entries) == len(log._entries),
                         "wrote %d, reloaded %d" % (len(log._entries), len(reloaded._entries)))
        finally:
            hangar.HANGAR_PATH = real_path

    print("\n%d check(s) failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
