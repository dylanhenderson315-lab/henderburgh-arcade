"""
news.py -- free RSS headline feed for the news ticker mode.

Same shape as market.py/satellite.py/flights.py/sports.py, deliberately:
all I/O lives here so the mode that draws it stays pure.

One data source, but a GENERIC one: any standard RSS 2.0 feed URL, set
via config (news_config.json) -- not hardcoded to one outlet. Verified
live against real feeds before writing this:
  * Fox News (moxie.foxnews.com/google-publisher/latest.xml) -- real,
    working, standard RSS 2.0. Used as the shipped default only because
    the user mentioned it as an option, not an endorsement.
  * BBC (feeds.bbci.co.uk/news/rss.xml) -- real, working, standard RSS 2.0.
  * AP's and Reuters' commonly-referenced public RSS URLs were tried
    live and BOTH currently redirect to their homepage / return nothing
    parseable -- those outlets appear to have discontinued or moved
    those specific feeds. Not included as a default for that reason; if
    you have a current AP/Reuters/other RSS URL, any valid RSS 2.0 feed
    works here unmodified -- this module never assumes an outlet.

Headlines only, on purpose: this reads <title> from each <item> and
nothing else. It deliberately never reads <description> or
<content:encoded> (both present in real feeds, e.g. Fox's), because
displaying more than a headline crosses from "ticker" into
"redistributing article content," which is out of scope here.

Same two rules as every other feed in this project:
  1. Never block the render loop -- background thread, last-good cache.
  2. Never invent a headline -- if the feed is down or empty, the engine
     gets an empty list and an error, not stale-but-unlabeled or
     fabricated text.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "news_config.json"

DEFAULT_FEED_URL = "https://moxie.foxnews.com/google-publisher/latest.xml"
DEFAULT_LABEL = "FOX NEWS"

MAX_HEADLINES = 15        # keep the tape a reasonable length, not a whole feed dump
REFRESH = 300.0            # headlines don't need to update faster than every 5 min
CONFIG_CHECK = 10.0
IDLE_STOP = 120.0
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"

_WS_RE = re.compile(r"\s+")


def _clean_title(raw):
    """RSS titles can carry HTML entities (already decoded by
    ElementTree) and odd whitespace/line breaks from the source feed --
    collapse to one clean line. Does not alter the actual words, only
    whitespace."""
    return _WS_RE.sub(" ", (raw or "").strip())


def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            url = str(data.get("feed_url", DEFAULT_FEED_URL)).strip() or DEFAULT_FEED_URL
            label = str(data.get("label", "")).strip()
            return url, label
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            pass
    save_config(DEFAULT_FEED_URL, "")
    return DEFAULT_FEED_URL, ""


def save_config(feed_url, label):
    CONFIG_PATH.write_text(json.dumps(
        {"feed_url": feed_url, "label": label}, indent=2))


def _fetch_headlines(feed_url):
    req = urllib.request.Request(feed_url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    channel_title = _clean_title(root.findtext(".//channel/title") or "")
    headlines = []
    for item in root.findall(".//item")[:MAX_HEADLINES]:
        title = _clean_title(item.findtext("title") or "")
        if title:
            headlines.append(title.upper())     # font is uppercase-only, see engines.py
    return headlines, channel_title


class NewsFeed:
    """Background poller with a last-good cache -- same contract as every
    other FEED in this project."""

    def __init__(self):
        self._lock = threading.Lock()
        self.feed_url, self._label_override = load_config()
        self._headlines = []
        self._channel_title = ""
        self._updated = 0.0
        self._last_try = 0.0
        self._last_config_check = 0.0
        self._last_read = 0.0
        self._thread = None
        self._err = None

    def get(self):
        """Returns {headlines: [...], label, age, err}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            headlines = list(self._headlines)
            # RSS <channel><title> is often a full sentence ("Latest &
            # Breaking News on Fox News"), not a short label -- fine as a
            # fallback for a custom feed URL, but for the shipped default
            # feed a real short name reads far better than a truncated
            # sentence, so prefer the known short name for that one case.
            if self._label_override:
                label = self._label_override
            elif self.feed_url == DEFAULT_FEED_URL:
                label = DEFAULT_LABEL
            else:
                label = self._channel_title or "NEWS"
            updated, err = self._updated, self._err
        self._ensure_thread()
        age = (now - updated) if updated else None
        return {"headlines": headlines, "label": label.upper()[:24], "age": age, "err": err}

    def get_config(self):
        with self._lock:
            return self.feed_url, self._label_override

    def set_source(self, feed_url, label=""):
        feed_url = str(feed_url).strip()
        label = str(label).strip()
        if not feed_url:
            return False
        with self._lock:
            self.feed_url = feed_url
            self._label_override = label
            self._last_try = 0.0       # refresh immediately
        save_config(feed_url, label)
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
        url, label = load_config()
        with self._lock:
            if url != self.feed_url or label != self._label_override:
                self.feed_url, self._label_override = url, label
                self._last_try = 0.0

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < REFRESH:
                return
            self._last_try = now
            url = self.feed_url
        try:
            headlines, channel_title = _fetch_headlines(url)
        except Exception as e:                     # noqa: BLE001 - never die
            with self._lock:
                self._err = f"{type(e).__name__}"
            return
        with self._lock:
            if headlines:
                self._headlines = headlines
                self._channel_title = channel_title
                self._updated = time.time()
                self._err = None
            else:
                self._err = "no headlines"


FEED = NewsFeed()
