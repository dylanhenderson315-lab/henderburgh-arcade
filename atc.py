"""
atc.py -- shared config/constants for the ATC transcription log.

PERSONAL-RIG-ONLY FEATURE. See CLAUDE.md's "ATC transcription log" section
for the full reasoning; the short version: LiveATC.net's own current terms
("Audio streams may not be used in any third-party products") make this
fine for this one Mac mini feeding this one owner's panel, and NOT fine
for PRODUCTION.md's sellable-device path. Same category of restriction as
the flight tracker's plane icon deliberately not being a real airline
logo -- this one is a data-source restriction rather than an IP one, but
the "personal rig only" boundary is identical. Do not add this to
PRODUCTION.md's feature scope.

ARCHITECTURE: a genuinely separate OS PROCESS (`atc_transcribe.py`, run
standalone), not a thread inside arcade_server -- confirmed reasoning
during feasibility research: transcription is real, non-trivial compute,
and even though mlx-whisper's actual work happens in Metal/native code
(which releases the GIL, same as numpy), proving that has zero impact on
real frame timing under real concurrent load was not worth the risk when
process isolation sidesteps the question entirely. Same discipline as the
existing hard rule against `import arcade_server` from a script -- keep
anything that isn't the render loop OUT of the render loop's process.

The worker process communicates with the rest of the project purely by
writing to LOG_PATH -- a JSON-lines file, one transcript segment per
line, each `{"ts": <unix epoch, chunk START time>, "text": "...",
"duration": <seconds>}`. The engine-side reader (built in a later phase)
only ever READS this file; it never talks to the worker directly. This
is the same "background thread writes, engine reads a snapshot" shape
every other feed in this project already uses, just with a file instead
of an in-memory FEED object, because the writer is a separate process.
"""
from pathlib import Path

# Confirmed live and reachable during feasibility research (2026-08-02):
# a real, continuous 16kbps mono MP3 stream, not a placeholder or dead
# mount. LiveATC's own site is Cloudflare-protected and can't be scraped
# for the current feed list, so this URL was found by testing known
# direct-mount host/feed-name patterns against MYR specifically, not
# guessed generically.
STREAM_URL = "https://s1-bos.liveatc.net/kmyr"

# Where the worker process writes, and the (future) engine-side reader
# reads from. Lives next to the other *_config.json-style local state
# this project already keeps beside the code.
LOG_PATH = Path(__file__).parent / "atc_log.jsonl"

# Real-time factor measured on this Mac mini (M4 Pro) during feasibility
# research: a 71.68s real MYR clip transcribed in 1.76s, a 40.8x realtime
# factor with the "small.en" model -- comfortable headroom for a 20s
# rolling chunk to always finish well before the next one is ready.
CHUNK_SECONDS = 20
MODEL_REPO = "mlx-community/whisper-small.en-mlx"

# How long a log entry stays worth showing at all. Matches the "2-5
# minute eviction window" the log-store phase will use -- defined here,
# not duplicated, since both the writer (for trimming the file so it
# cannot grow forever) and the reader (for "how stale is this") need the
# same number.
LOG_MAX_AGE_SECONDS = 300

try:
    import mlx_whisper                  # noqa: F401 - presence check only
    HAVE_MLX_WHISPER = True
except ImportError:                     # degrade honestly, never guess
    HAVE_MLX_WHISPER = False


# ---- engine-side reader (phase 2) ---------------------------------------
import json
import threading
import time


class AtcLogFeed:
    """Reads LOG_PATH -- never writes it; only atc_transcribe.py's
    separate process does that. Same shape as every other FEED in this
    project (a cache, a cheap get(), never blocks the caller), except the
    thing being polled is a local file instead of a network endpoint, so
    there is no background thread: a stat()+parse is already fast enough
    to do inline, throttled the same way a network feed would be.

    Deliberately reads the WHOLE small file rather than tailing/seeking --
    the file is bounded by the worker's own LOG_MAX_AGE_SECONDS trim (a
    few KB at most), so simplicity wins over incremental-read complexity
    that would only pay for itself at a scale this file never reaches.
    """

    REFRESH = 2.0    # seconds between re-reads -- plenty responsive for
                      # an "N seconds ago" caption, cheap either way

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []       # newest-first
        self._last_refresh_try = 0.0   # when _refresh() last actually RAN
        self._last_mtime = None

    def get(self):
        """Every entry currently in the log, newest first. Never blocks;
        never invents an entry -- an unreadable/missing/corrupt file is
        an empty list, not a guess.

        REAL BUG, FOUND ON THE LIVE PANEL, NOT IN A UNIT TEST: this used
        to track a single `_last_read` timestamp, bumped on every call to
        get(). On an engine ticking every 0.05s, get() is called every
        0.05s too, so `now - _last_read` was ALWAYS ~0.05s -- it could
        never accumulate to REFRESH's 2.0s, because the timestamp being
        compared against was itself being reset by the very call doing
        the comparing. The throttle never elapsed after the very first
        read, so the live panel showed the SAME first-ever entry for
        14+ real minutes while the file kept growing underneath it --
        every standalone test (a fresh short-lived process, get() called
        only a few times seconds apart) read fresh data every time and
        never exposed it. Fixed the same way flights.py's FlightFeed
        already does it correctly: a SEPARATE `_last_refresh_try` that is
        only touched by the refresh path itself, never by the read path.
        """
        now = time.time()
        with self._lock:
            due = now - self._last_refresh_try >= self.REFRESH
        if due:
            self._refresh()
        # Age-filter on every read, not just when the file changes.
        # The worker trims LOG_PATH on its own cadence; if that process
        # is dead the file sits untouched and a mtime short-circuit
        # would keep serving week-old radio as if it were still live.
        # LOG_MAX_AGE_SECONDS is "how long an entry stays worth showing".
        cutoff = now - LOG_MAX_AGE_SECONDS
        with self._lock:
            return [e for e in self._entries if e.get("ts", 0) >= cutoff]

    def _refresh(self):
        # Set UNCONDITIONALLY, on every path out of this method (even the
        # early-return ones) -- this is the timestamp get()'s throttle
        # checks, and it must reflect "a refresh attempt happened", not
        # "a refresh attempt happened AND found new content", or a
        # missing/unchanged file would retry every single call instead
        # of respecting REFRESH.
        with self._lock:
            self._last_refresh_try = time.time()
        if not LOG_PATH.exists():
            with self._lock:
                self._entries = []
                self._last_mtime = None
            return
        try:
            mtime = LOG_PATH.stat().st_mtime
        except OSError:
            return                       # transient FS error: keep last-good, try again next cycle
        with self._lock:
            unchanged = mtime == self._last_mtime
        if unchanged:
            return
        entries = []
        try:
            with LOG_PATH.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue         # one corrupt line (e.g. a torn write) must not lose the rest
                    if isinstance(entry, dict) and "ts" in entry and "text" in entry:
                        entries.append(entry)
        except OSError:
            return                       # transient FS error: keep last-good, try again next cycle
        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        with self._lock:
            self._entries = entries
            self._last_mtime = mtime


FEED = AtcLogFeed()


# ---- callsign matching (phase 3) ----------------------------------------
# CONFIDENCE-GATED HIGHLIGHT ONLY, NEVER A SILENT FILTER. Per the
# feasibility research's own finding, GA tail numbers (spoken digit/
# phonetic sequences like "one whiskey") cannot be reliably reconstructed
# from ASR text -- this module does not attempt them, full stop. It only
# ever matches AIRLINE NAME + FLIGHT NUMBER, and only ever REPORTS a match
# when the reconstructed callsign is an EXACT match against a real
# currently-tracked ADS-B ident. A same-airline-different-flight-number
# candidate (confirmed against real data: "SOUTHWEST 1437" spoken
# repeatedly while the only real Southwest aircraft in range was
# SWA2587) is correctly reported as NO match, not a fuzzy near-miss --
# exactly the honesty this feature exists to guarantee.
import re

# Real FAA-registered radio callsigns -> real ICAO 3-letter airline codes,
# same category of static reference data as flights.ICAO_TYPE_NAMES (a
# public, bounded fact table, not invented). Limited to carriers actually
# plausible in US airspace generally and the Myrtle Beach/Grand Strand
# area specifically -- confirmed several of these appear VERBATIM in real
# transcribed MYR audio during this feature's own development (DELTA,
# SOUTHWEST, FRONTIER, AMERICAN all observed live).
AIRLINE_ICAO = {
    "AMERICAN": "AAL", "DELTA": "DAL", "UNITED": "UAL", "SOUTHWEST": "SWA",
    "FRONTIER": "FFT", "JETBLUE": "JBU", "SPIRIT": "NKS", "ALASKA": "ASA",
    "ENVOY": "ENY", "REPUBLIC": "RPA", "SKYWEST": "SKW", "ENDEAVOR": "EDV",
    "ALLEGIANT": "AAY", "HAWAIIAN": "HAL",
}

# An airline word followed (within a short run of digits/punctuation --
# Whisper renders a flight number as anything from "2214" to "22, 14" to
# "22 14") by 2-5 digit groups. Built and tuned against REAL transcript
# output captured during this feature's own live testing, not assumed:
# confirmed to correctly parse "DELTA 2327," -> DAL2327 and "SOUTHWEST
# 14, 37," -> SWA1437 from real MYR audio.
_CALLSIGN_RE = {
    name: re.compile(re.escape(name) + r"[,.]?\s+((?:\d[,.\s]*){2,5})")
    for name in AIRLINE_ICAO
}


def match_callsign(text, real_idents):
    """The first real, currently-tracked aircraft ident this transcript
    text names by airline+flight-number, or None.

    `real_idents` must be the CALLER's actual current aircraft list (e.g.
    FlightEngine's `flights.FEED.get()` idents) -- this function invents
    nothing and trusts nothing beyond an exact string match against that
    real list. Only the FIRST match is returned; a transcript naming
    multiple real aircraft is rare and "highlight the first one found" is
    a reasonable, honest simplification rather than a scored ranking this
    feature does not need yet.
    """
    if not text or not real_idents:
        return None
    idents = set(real_idents)
    for name, code in AIRLINE_ICAO.items():
        for m in _CALLSIGN_RE[name].finditer(text):
            digits = re.sub(r"\D", "", m.group(1))
            if not digits:
                continue
            candidate = code + digits
            if candidate in idents:
                return candidate
    return None
