"""
blog.py -- latest posts from the HENDERBURGH site, for the calm idle mode.

Same shape as every other feed module: all I/O here, background thread,
last-good cache, never blocks the render loop.

Source: the site's OWN existing public endpoint,
https://henderburgh.com/api/messages -- verified live before writing this.
No new endpoint was needed on the site and none was added: the blog logic
stays in one place (oura-dashboard) and this is a read-only consumer of
it. It needs no auth, and returns newest-first JSON objects shaped:

    {id, name, text, parent_id, timestamp}

WHAT THIS ACTUALLY IS (checked, not assumed): the "blog" on the site is a
guestbook / message board, not titled articles. There is no "title" field
-- `name` is who posted and `text` is the message body, so this renders
name as the heading and text as the body. Two consequences worth knowing:

  * Replies (parent_id set) are threaded under a post. Only top-level
    entries (parent_id null) are shown, since "a new post went up" means
    a new top-level message, not a reply to an old one.
  * Entries are visitor-submitted, not exclusively the site owner's own
    writing. Nothing here ships any bundled quote/lyric library -- it only
    ever mirrors what is already published on the owner's own site -- but
    "your own content" is worth reading as "your own site's content",
    which includes what guests posted to it.

Polled slowly on purpose (POLL_INTERVAL): this hits a live public website
rather than local config, and "did a new post go up" does not need to be
answered more than a few times an hour.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

import paneltext
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "blog_config.json"

DEFAULT_API_URL = "https://henderburgh.com/api/messages"

MAX_POSTS = 8            # newest N; the mode cycles them, it isn't an archive
POLL_INTERVAL = 300.0    # 5 min -- plenty for "did a new post go up"
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

_WS_RE = re.compile(r"\s+")


def _clean(s):
    """Collapse newlines/runs of whitespace to single spaces, and uppercase.

    Uppercasing happens HERE, at the I/O boundary, because the panel's 3x5
    font is uppercase-only and silently drops glyphs it lacks -- the exact
    bug that hit ticker, flights and sports. Real posts are free-form user
    text and are the most mixed-case source in the whole project, so this
    is the single most likely place for that bug to recur.
    """
    # Visitor-submitted text: the least predictable source here, so it
    # goes through the shared fold rather than a bare .upper().
    return _WS_RE.sub(" ", paneltext.panel_text(s))


def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            url = str(data.get("api_url", DEFAULT_API_URL)).strip() or DEFAULT_API_URL
            return url
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            pass
    save_config(DEFAULT_API_URL)
    return DEFAULT_API_URL


def save_config(api_url):
    """Read-modify-write, not a fresh dict -- same preemptive fix as
    market.py/news.py/notify.py's save_config (2026-08-09): a fresh-dict
    save is the exact bug shape that has already wiped a sibling key in
    location_config.json/sports_config.json twice this project's life."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            data = {}
    data["api_url"] = api_url
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def _fetch_posts(api_url):
    req = urllib.request.Request(api_url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    if not isinstance(data, list):
        return []
    out = []
    for m in data:
        if not isinstance(m, dict):
            continue
        if m.get("parent_id"):
            continue                     # a reply, not a new post
        name, text = _clean(m.get("name")), _clean(m.get("text"))
        if not (name or text):
            continue                     # nothing renderable; never pad it out
        out.append({
            "id": str(m.get("id") or ""),
            "name": name,
            "text": text,
            "timestamp": str(m.get("timestamp") or ""),
        })
        if len(out) >= MAX_POSTS:
            break
    return out


class BlogFeed:
    """Background poller with a last-good cache -- same contract as every
    other FEED in this project."""

    def __init__(self):
        self._lock = threading.Lock()
        self.api_url = load_config()
        self._posts = []
        self._updated = 0.0
        self._last_try = 0.0
        self._last_config_check = 0.0
        self._last_read = 0.0
        self._thread = None
        self._err = None

    def get(self):
        """Returns {posts: [...], age, err}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            posts = [dict(p) for p in self._posts]
            updated, err = self._updated, self._err
        self._ensure_thread()
        age = (now - updated) if updated else None
        return {"posts": posts, "age": age, "err": err}

    def get_config(self):
        with self._lock:
            return self.api_url

    def set_api_url(self, api_url):
        api_url = str(api_url).strip()
        if not api_url:
            return False
        with self._lock:
            self.api_url = api_url
            self._last_try = 0.0
        save_config(api_url)
        return True

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
        url = load_config()
        with self._lock:
            if url != self.api_url:
                self.api_url = url
                self._last_try = 0.0

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < POLL_INTERVAL:
                return
            self._last_try = now
            url = self.api_url
        try:
            posts = _fetch_posts(url)
        except Exception as e:                     # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"
            return
        with self._lock:
            # An empty blog is a real, valid answer -- cache it as such
            # rather than holding stale posts forever or inventing filler.
            self._posts = posts
            self._updated = time.time()
            self._err = None


FEED = BlogFeed()
