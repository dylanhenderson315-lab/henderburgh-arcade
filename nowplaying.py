"""
nowplaying.py -- "NOW PLAYING": the real track currently playing, via
Last.fm's scrobbling API. Same shape as every other feed module here: all
I/O lives in this file, a background-thread poller with a last-good
cache, get() never blocks, never invents a track.

WHY LAST.FM AND NOT THE PANEL'S MIC. The M-1 has a real digital mic and
audio_sync.py already receives WLED-MM's on-device analysis of it -- but
that analysis is VOLUME + a 16-band FFT (see audio_sync.py's own packet
layout documentation, confirmed against the real firmware source). Those
are the inputs for a VISUALIZER; there is no song IDENTITY anywhere in
them. Acoustic fingerprinting (Shazam-style) would be a whole additional
class of dependency and a paid or heavily-rate-limited service, so
"now playing" has to come from a metadata source, not the mic. Stated
explicitly because the mic's existence makes it look like this feature
should be nearly free, and it isn't.

WHY LAST.FM AND NOT SPOTIFY. Checked both live before choosing:
  * Last.fm (ws.audioscrobbler.com) -- a free API key, instant signup,
    NO OAuth flow at all for reading a user's own now-playing track, one
    plain GET. Critically it is PLAYER-AGNOSTIC: it reports whatever the
    owner actually scrobbles, so Spotify, Apple Music, iTunes, YouTube
    Music and anything else with a scrobbler all work through the one
    integration.
  * Spotify Web API -- requires app registration plus a full OAuth2
    authorization-code flow with refresh tokens and a registered
    redirect URI, which is genuinely awkward on a headless LAN device,
    and only ever covers Spotify.
Last.fm is the smaller, more useful integration. If Spotify-exclusive
fields (album art, exact playback position) are ever wanted, that is a
real, separate follow-up, not a reason to start there.

HONESTLY UNVERIFIED AGAINST A REAL PAYLOAD. Every other feed in this
project was built against a real live response. This one could not be:
Last.fm requires an API key, none is configured yet, and the real
endpoint correctly refuses without one (confirmed live -- it returns a
clean {"message": "Invalid API key...", "error": 10}, which is why the
error path below is the one part that IS verified against real
behaviour). The response PARSING below is written against Last.fm's
documented user.getRecentTracks shape and is therefore correct-but-
unverified until a real key exists -- same "ship correct, flag honestly"
precedent this project already set for the NHL goal detector and the MMA
finish detector. The first real key is what proves it; re-check the
field names then rather than trusting this note.

NO KEY CONFIGURED IS A NORMAL STATE, NOT AN ERROR. With no api_key/user
set, get() reports that plainly and the engine says so on screen -- the
same honest-empty-state discipline as "NO HOME AIRPORT CONFIGURED" or
"NOT CURRENTLY AIRBORNE", never a placeholder track.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paneltext

CONFIG_PATH = Path(__file__).parent / "nowplaying_config.json"

API_ROOT = "https://ws.audioscrobbler.com/2.0/"

# A track changes on the order of minutes; 20s is responsive enough that
# the panel isn't visibly behind the music without being wasteful against
# a free third-party API. Same reasoning as sports.WINPROB_REFRESH's own
# 20s choice, and the same IDLE_STOP-gated lifecycle every feed here has
# (this stops polling entirely once nothing has read it for 120s).
REFRESH = 20.0
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def load_config():
    """{"api_key": str|None, "user": str|None}. Both empty is the honest
    default (nothing configured yet), never a guessed account."""
    cfg = {"api_key": None, "user": None}
    if not CONFIG_PATH.exists():
        return cfg
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict):
            for k in ("api_key", "user"):
                v = raw.get(k)
                cfg[k] = str(v).strip() or None if v else None
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass          # keep honest defaults; never crash a render over a bad file
    return cfg


def save_config(api_key, user):
    """Read-modify-write, preserving any key this function doesn't own --
    built in from the start per the 2026-08-09 lesson about save_*()
    functions that construct a fresh dict instead."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    data["api_key"] = (str(api_key).strip() or None) if api_key else None
    data["user"] = (str(user).strip() or None) if user else None
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return {"api_key": data["api_key"], "user": data["user"]}


def _parse_now_playing(data):
    """Real currently-playing track from a user.getRecentTracks response,
    or None if nothing is playing right now.

    Last.fm marks the in-progress track with `@attr.nowplaying == "true"`
    on the FIRST recenttracks entry; without that attribute every entry
    is just a past scrobble, and reporting the most recent past scrobble
    as "now playing" would be inventing a fact the API didn't state --
    so a missing nowplaying flag correctly returns None.

    All display strings are folded through paneltext.panel_text() HERE,
    at this module's I/O boundary, per the project-wide rule (real artist
    and track names are exactly the kind of text that carries accents and
    curly quotes -- e.g. "Sigur Rós", "Beyoncé" -- which the 3x5 font
    silently drops unfolded).
    """
    tracks = ((data or {}).get("recenttracks") or {}).get("track")
    if isinstance(tracks, dict):          # a single-track response isn't wrapped in a list
        tracks = [tracks]
    if not isinstance(tracks, list) or not tracks:
        return None
    first = tracks[0]
    if not isinstance(first, dict):
        return None
    attrs = first.get("@attr") or {}
    if str(attrs.get("nowplaying", "")).lower() != "true":
        return None                        # nothing playing right now -- an honest None
    artist = first.get("artist") or {}
    album = first.get("album") or {}
    return {
        "track": paneltext.panel_text(first.get("name")) or None,
        # Last.fm nests these as {"#text": ...} objects on this endpoint.
        "artist": paneltext.panel_text(
            artist.get("#text") if isinstance(artist, dict) else artist) or None,
        "album": paneltext.panel_text(
            album.get("#text") if isinstance(album, dict) else album) or None,
    }


class NowPlayingFeed:
    """Background poller with a last-good cache -- same contract as every
    other FEED in this project."""

    def __init__(self):
        self._lock = threading.Lock()
        cfg = load_config()
        self._api_key, self._user = cfg["api_key"], cfg["user"]
        self._track = None
        self._updated = 0.0
        self._last_try = 0.0
        self._last_read = 0.0
        self._last_config_check = 0.0
        self._thread = None
        self._err = None

    def get_config(self):
        with self._lock:
            return {"api_key": self._api_key, "user": self._user}

    def set_config(self, api_key, user):
        cfg = save_config(api_key, user)
        with self._lock:
            self._api_key, self._user = cfg["api_key"], cfg["user"]
            # A new account's track is a different real fact -- never show
            # the previous account's last track against the new one.
            self._track = None
            self._updated = 0.0
            self._last_try = 0.0
            self._err = None
        return cfg

    def get(self):
        """{configured, playing, track, age, err}. Never blocks.

        `playing` is a real tri-state matching the honest states this can
        actually be in: None (not configured -- nothing to look up),
        False (configured, a real lookup ran, genuinely nothing playing
        right now), True (a real current track is cached)."""
        now = time.time()
        with self._lock:
            self._last_read = now
            configured = bool(self._api_key and self._user)
            track = dict(self._track) if self._track else None
            updated, err = self._updated, self._err
        self._ensure_thread()
        return {
            "configured": configured,
            "playing": (track is not None) if configured else None,
            "track": track,
            "age": (now - updated) if updated else None,
            "err": err,
        }

    # ---- polling -----------------------------------------------------
    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read
            if idle > IDLE_STOP:
                return
            self._maybe_reload_config()
            self._refresh_once()
            time.sleep(2.0)

    def _maybe_reload_config(self):
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        cfg = load_config()
        with self._lock:
            if (cfg["api_key"], cfg["user"]) != (self._api_key, self._user):
                self._api_key, self._user = cfg["api_key"], cfg["user"]
                self._track = None
                self._updated = 0.0

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < REFRESH:
                return
            self._last_try = now
            api_key, user = self._api_key, self._user

        if not (api_key and user):
            with self._lock:
                self._track = None
                self._err = None
            return

        params = urllib.parse.urlencode({
            "method": "user.getrecenttracks", "user": user,
            "api_key": api_key, "format": "json", "limit": 1,
        })
        try:
            data = _get_json(f"{API_ROOT}?{params}")
        except Exception as e:                        # noqa: BLE001 - never die
            with self._lock:
                self._err = type(e).__name__
            return

        # Last.fm reports auth/permission problems as a 200 with an
        # `error` field, NOT an HTTP error -- confirmed live (an invalid
        # key returns {"message": "Invalid API key...", "error": 10}).
        # Surfaced as a real error rather than silently looking like
        # "nothing playing", so a bad key is diagnosable instead of
        # indistinguishable from a quiet evening.
        if isinstance(data, dict) and data.get("error"):
            with self._lock:
                self._err = paneltext.panel_text(data.get("message")) or "API ERROR"
                self._track = None
            return

        track = _parse_now_playing(data)
        with self._lock:
            self._track = track
            self._updated = time.time()
            self._err = None


FEED = NowPlayingFeed()
