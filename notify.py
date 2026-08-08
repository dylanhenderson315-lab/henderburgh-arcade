"""
notify.py -- Home Assistant notification pass-through (task #8).

WHY THIS IS ONE MODULE, NOT A "FEED": every other module here polls an
upstream source on a background thread (weather.py hits NWS every N
minutes, flights.py hits an ADS-B receiver, etc.) and an engine reads
whatever the feed last cached. A notification has nothing to poll -- Home
Assistant PUSHES to `POST /api/notify` in `arcade_server.py` when it wants
one shown, so there is no `FEED`/poll-thread/cache shape to mirror. What
IS shared with every other module here is the "config file, not a code
edit" pattern (`load_config`/`save_config`, safe defaults, never raises
into the caller) -- so this module holds exactly that: config load/save,
the auth check, and the one-shot hand-off slot an urgent notification
needs to survive a real mode swap. Everything else (text folding, the
banner's on-screen lifetime, which visual tier to use) is boundary logic
that belongs in arcade_server.py next to the endpoint that receives the
real HTTP request, the same way `/api/window`'s validation lives in
arcade_server.py rather than satellite.py.

AUTH is a real, targeted exception to this project's usual no-auth
posture. Every other `/api/*` endpoint here is unauthenticated -- the
server binds 0.0.0.0 on a trusted LAN, and CLAUDE.md's standing posture is
that anyone who can reach the Mac can already do far more damage than
POST a game mode. A notify endpoint is different: it puts arbitrary,
convincing-looking TEXT on a physical panel in the house ("GARAGE: Left
open" or, spoofed, something meant to alarm or mislead someone who trusts
the panel) with zero cost to the sender and zero corroborating context.
That is worth a shared-secret check even though nothing else here has
one. `hmac.compare_digest` (constant-time) is used deliberately -- a plain
`==` short-circuits on the first mismatched byte, which leaks a timing
signal an attacker on the same LAN could use to guess the token
character-by-character.
"""
import hmac
import json
import secrets
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "notify_config.json"

_lock = threading.Lock()
_pending = None   # one-shot: payload dict for the fresh NotifyEngine to consume


def load_config():
    """Read notify_config.json, generating a real random token on first
    run. Unlike brightness.py's load_config(), this is only called once
    per incoming request (not once per render frame), so there is no need
    for the mtime cache that module needs to survive being polled 24x/sec."""
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict) and raw.get("token"):
            return {"token": str(raw["token"])}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    # Missing, unreadable, or malformed -- (re)seed a fresh real token
    # rather than ever falling back to a guessable default. A predictable
    # fallback would defeat the entire point of gating this endpoint.
    cfg = {"token": secrets.token_hex(16)}
    save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps({"token": str(cfg["token"])}, indent=2))
    return {"token": cfg["token"]}


def check_token(presented):
    """Constant-time compare against the configured token. Missing or
    malformed input compares against the real token and simply fails,
    same as any other mismatch -- no special-cased short path that could
    itself leak timing information."""
    real = load_config()["token"]
    return hmac.compare_digest(str(presented or ""), real)


def push_pending(payload):
    """One-shot hand-off for an URGENT notification: arcade_server.py's
    /api/notify handler calls this right before `set_mode("notify")`, the
    same idiom flights.py's push_pending_detail()/pop_pending_detail()
    already established for PlaneWatchEngine's hand-off to FlightEngine
    (see engines.PlaneWatchEngine). set_mode() always constructs
    ENGINES[base]() with ZERO args, so this slot is the only way to carry
    title/message/color/return-mode across a real mode switch."""
    global _pending
    with _lock:
        _pending = dict(payload)


def pop_pending():
    """Consume (not peek) the pending notification payload, or None. Same
    "pop, don't peek-forever" discipline as flights.pop_pending_detail()."""
    global _pending
    with _lock:
        payload, _pending = _pending, None
        return payload
