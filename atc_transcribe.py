"""
atc_transcribe.py -- standalone ATC transcription worker. Run it as its
own process, NEVER imported by arcade_server or engines.py:

    .venv/bin/python atc_transcribe.py

Same isolation reasoning as the hard "never import arcade_server from a
script" rule (see CLAUDE.md), applied in the opposite direction: this
process must never be allowed anywhere near the render loop's process
either. Real, non-trivial compute (Whisper transcription) belongs in its
own OS process so a slow or stuck transcription can never stall a frame,
full stop -- no amount of "it's probably fine because MLX releases the
GIL" is worth betting the render loop on.

PIPELINE, each cycle:
  1. curl a CHUNK_SECONDS-long slice of the real live MYR stream (same
     technique verified during feasibility research -- LiveATC serves a
     continuous MP3, so a plain time-limited GET is a real rolling
     window, not a hack).
  2. Transcribe it with mlx-whisper (small.en, ~40x realtime on this
     Mac mini -- comfortable headroom before the next chunk is due).
  3. Append any NON-EMPTY result to LOG_PATH as one JSON line, with the
     REAL wall-clock time the chunk started (not when transcription
     finished -- "14s ago" should mean 14s since the transmission
     happened, not 14s since the CPU got around to it).
  4. Trim the log file to LOG_MAX_AGE_SECONDS so it can never grow
     unbounded.

Whisper already returns an empty string for a silent/no-speech chunk
(confirmed during feasibility testing on a genuinely quiet stretch) --
that is used directly as the "nothing happened" signal rather than
running a separate silence-detection pass.

NEVER INVENTS A TRANSCRIPT. A network failure, a stream drop, or a
transcription error is logged to stderr and retried with backoff -- the
log file simply gets no new entry for that cycle, same "degrade
honestly, never guess" contract every other feed in this project follows.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import atc
import paneltext

ERROR_BACKOFF_BASE = 5.0
ERROR_BACKOFF_MAX = 60.0


def _fetch_chunk(seconds):
    """One real CHUNK_SECONDS-long slice of the live stream, as raw MP3
    bytes, or None on any failure. curl's own --max-time is what bounds
    the capture -- the stream never ends on its own, so this is what
    turns a continuous broadcast into a discrete rolling window."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(seconds), atc.STREAM_URL,
             "-o", path],
            timeout=seconds + 10, check=False)
        # curl exit 28 = CURLE_OPERATION_TIMEDOUT, and here that is the
        # EXPECTED, SUCCESSFUL outcome, not a failure -- the stream is
        # continuous and never ends on its own, so --max-time cutting it
        # off after exactly `seconds` is how a bounded chunk is captured
        # at all. Confirmed live: a real 200 response with real audio
        # bytes still exits 28 once the timer fires. Only a genuinely
        # empty/tiny file (a real connection failure, no data at all)
        # counts as a failure -- checked by content size, not exit code.
        if result.returncode not in (0, 28):
            return None
        data = Path(path).read_bytes()
        return data if len(data) > 1000 else None   # a near-empty file is a failed fetch, not silence
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        Path(path).unlink(missing_ok=True)


def fold_transcript(raw_text):
    """The ENTIRE fold boundary, extracted as a pure function so
    fold_audit.py can canary-test it directly WITHOUT needing mlx_whisper
    installed or a real model loaded -- same reason flights._ident is a
    standalone function rather than inlined into the fetch call.

    FOLDED THROUGH paneltext.panel_text() HERE, AT THE I/O BOUNDARY --
    same rule every other feed in this project follows, and the one this
    project's own docstring tally exists to keep from being missed again.
    Caught live, in this codebase's own newest code, in this same
    session: Whisper's raw output is natural mixed-case English ("A
    bunch of them...") and the panel's 3x5 font is uppercase-only --
    unfolded, "A bunch of them" rendered as literally just "A", every
    lowercase letter silently dropped, discovered by looking at the
    actual rendered frame, not by reading this function. Folding here,
    once, at the write boundary, is what lets every future reader of
    LOG_PATH trust the text is already drawable -- the same reason
    news._clean_title/blog._clean/flights._ident fold at their own
    boundaries instead of leaving it to whoever renders them. This
    instance is now also covered by fold_audit.py, not just this
    docstring's word.

    Also treats a result with NO alphanumeric content as empty. Confirmed
    live: Whisper occasionally hallucinates a bare "!" or similar stray
    punctuation on near-silent audio rather than returning "" -- a real,
    observed quirk during the first stability run, not a hypothetical.
    That is noise, not a transmission, and would otherwise clutter the
    log with meaningless entries.
    """
    text = paneltext.panel_text(raw_text)
    return text if any(c.isalnum() for c in text) else ""


def _transcribe(mp3_bytes):
    """Real text, or "" for a silent/no-speech chunk -- both are
    legitimate outcomes, only an exception is a failure. See
    fold_transcript() for the fold boundary itself."""
    import mlx_whisper
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        path = f.name
    try:
        result = mlx_whisper.transcribe(path, path_or_hf_repo=atc.MODEL_REPO)
        return fold_transcript(result.get("text"))
    finally:
        Path(path).unlink(missing_ok=True)


def _append_log(ts, text, duration):
    entry = {"ts": ts, "text": text, "duration": duration}
    with atc.LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _trim_log(max_age):
    """Keep the file bounded -- drop anything older than max_age rather
    than letting it grow forever. Rewrites the whole (small) file; this
    runs once per CHUNK_SECONDS, not per frame, so the cost is trivial."""
    if not atc.LOG_PATH.exists():
        return
    cutoff = time.time() - max_age
    kept = []
    try:
        with atc.LOG_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue                # a corrupt line is dropped, not fatal
                if entry.get("ts", 0) >= cutoff:
                    kept.append(line)
    except OSError:
        return
    atc.LOG_PATH.write_text("\n".join(kept) + ("\n" if kept else ""))


def main():
    if not atc.HAVE_MLX_WHISPER:
        print("mlx_whisper not installed -- see requirements.txt", file=sys.stderr)
        return 1

    print("ATC transcription worker starting -- stream: %s" % atc.STREAM_URL)
    print("Log: %s" % atc.LOG_PATH)
    fails = 0
    while True:
        chunk_start = time.time()
        try:
            mp3 = _fetch_chunk(atc.CHUNK_SECONDS)
            if mp3 is None:
                raise RuntimeError("empty or failed fetch")
            text = _transcribe(mp3)
            fails = 0
            if text:
                _append_log(chunk_start, text, atc.CHUNK_SECONDS)
                print("[%s] %s" % (time.strftime("%H:%M:%S", time.localtime(chunk_start)), text),
                      flush=True)
            _trim_log(atc.LOG_MAX_AGE_SECONDS)
        except Exception as e:                       # noqa: BLE001 - never die, this is a long-running daemon
            fails += 1
            wait = min(ERROR_BACKOFF_MAX, ERROR_BACKOFF_BASE * (2 ** (fails - 1)))
            print("!! %s: %s (retry in %.0fs)" % (type(e).__name__, e, wait), file=sys.stderr, flush=True)
            time.sleep(wait)
            continue

        # Pace to the real chunk cadence, not "however long transcription
        # took" -- if a chunk finished in 2s (typical, ~40x realtime),
        # there's no reason to fetch the next one immediately; waiting
        # for the rest of the real CHUNK_SECONDS window keeps chunks from
        # overlapping or leaving gaps.
        elapsed = time.time() - chunk_start
        remaining = atc.CHUNK_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    sys.exit(main())
