"""
detector_throttle_audit.py -- prove SportsEngine._detector_due() actually
throttles, not just that today's traffic happened not to trip it.

    .venv/bin/python detector_throttle_audit.py

WHY THIS EXISTS. Found during the 2026-08-08 "cut redundant per-league
polling" audit (task #21): every per-game big-moment detector (MLB HR,
NFL/NCAAF touchdown, NHL goal, basketball clutch, soccer goal) called a
real network fetch against SUMMARY_URL from tick(), gated only on "the
favorite's game is live" -- not on time. tick() fires every tick_rate
(0.05s), so a live favorite game was re-hitting ESPN's summary endpoint
up to 20x/second, unthrottled, until _detector_due() was added to gate
each detector to at most once per sports.WINPROB_REFRESH (20s).

That fix was verified once, live, this session, via an ad hoc script --
not checked into either standing audit. If _detector_due()'s gate is
ever accidentally removed, or its comparison inverted, or a future
detector added without calling it, nothing would catch the regression
back to a real production risk (rate-limiting/blocking by ESPN) until
someone happened to notice a live favorite game hammering the network
again.

Pure logic, no network: _detector_due() only touches a dict and
time.time(), so this exercises the real function directly against a
throwaway SportsEngine instance (never engines.SportsEngine() through
its real __init__ -- that would try to read sports.FEED's live state;
constructed via __new__ instead, same technique this session's other
audits/verification scripts already use for force-triggered engines).
"""
import sys
import time

import engines
import sports


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print("  %-58s %s%s" % (name, status, ("  " + detail) if detail and not cond else ""))
    return 0 if cond else 1


def main():
    bad = 0
    eng = engines.SportsEngine.__new__(engines.SportsEngine)
    eng._detector_last_poll = {}

    # ---- first call for a brand new key is always due -------------------
    bad += check("first call for a new detector key is due",
                 eng._detector_due("mlb_hr") is True)

    # ---- an immediate second call for the SAME key is throttled ---------
    bad += check("immediate re-call of the same key is NOT due",
                 eng._detector_due("mlb_hr") is False)

    # ---- a DIFFERENT key is completely independent -- one detector being
    # due/not-due must never gate a different detector. This is the exact
    # shape of bug that would silently re-introduce partial unthrottling
    # (e.g. a shared key by copy-paste mistake across two detectors). ----
    bad += check("a different detector key is independent (due on its own first call)",
                 eng._detector_due("nfl_touchdown") is True)
    bad += check("mlb_hr is STILL throttled after nfl_touchdown's own call",
                 eng._detector_due("mlb_hr") is False)

    # ---- simulate real elapsed time by rewriting the stored timestamp
    # (not mocking time.time() globally -- keeps this test isolated from
    # anything else that might read the real clock). Just under the
    # refresh window: still throttled. -----------------------------------
    eng._detector_last_poll["mlb_hr"] = time.time() - (sports.WINPROB_REFRESH - 1.0)
    bad += check("still throttled just under WINPROB_REFRESH (%.0fs) elapsed"
                 % (sports.WINPROB_REFRESH - 1.0),
                 eng._detector_due("mlb_hr") is False)

    # ---- just over the refresh window: due again, and this call must
    # reset the stored timestamp to now (proven by immediately checking
    # it's throttled again right after). ---------------------------------
    eng._detector_last_poll["mlb_hr"] = time.time() - (sports.WINPROB_REFRESH + 1.0)
    bad += check("due again just OVER WINPROB_REFRESH (%.0fs) elapsed"
                 % (sports.WINPROB_REFRESH + 1.0),
                 eng._detector_due("mlb_hr") is True)
    bad += check("becoming due resets the gate -- immediately throttled again",
                 eng._detector_due("mlb_hr") is False)

    # ---- real regression check: simulate the ORIGINAL bug (tick() firing
    # every 0.05s, unthrottled) and prove the REAL _detector_due() caps
    # the actual call rate, not just individual before/after comparisons.
    # Monkeypatches engines.time.time (not the global time module -- only
    # the reference _detector_due() itself reads) so the real function
    # runs unmodified against a fast-forwarded fake clock instead of this
    # test needing to sleep 20 real seconds.
    eng._detector_last_poll = {}
    due_count = 0
    sim_now = [time.time()]
    real_time_time = engines.time.time
    engines.time.time = lambda: sim_now[0]
    try:
        for _ in range(400):                  # 400 * 0.05s tick_rate = ~20s of simulated ticks
            if eng._detector_due("sim"):
                due_count += 1
            sim_now[0] += engines.SportsEngine.tick_rate
    finally:
        engines.time.time = real_time_time
    bad += check("~20s of 0.05s ticks (the ORIGINAL bug's real cadence) yields ~1 due call, not 400",
                 due_count <= 2, "got %d due call(s)" % due_count)

    print("\n%d check(s) failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
