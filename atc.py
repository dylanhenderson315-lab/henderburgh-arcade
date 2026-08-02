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
