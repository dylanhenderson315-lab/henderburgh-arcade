"""
market.py -- free market data (crypto + stocks) for the ticker mode.

All I/O lives here so the mode that draws it stays pure: TickerEngine is
handed plain numbers and renders pixels, which is what lets the identical
mode run on the Mac panel today and a Pi-driven HUB75 panel later.

Sources, both free and keyless:
  * CoinGecko simple/price      -- crypto, includes 24h change directly
  * Yahoo Finance v8 chart      -- stocks, unofficial but long-stable

Two rules this module exists to enforce:
  1. NEVER block the render loop. Fetching happens on a background thread;
     the mode reads whatever is currently cached and never waits.
  2. NEVER invent numbers. If the network is down we keep serving the last
     good values and mark them stale, so the panel can say so honestly
     rather than quietly showing a price that stopped being true an hour
     ago. A wrong price shown confidently is worse than no price.
"""
import json
import threading
import time
import urllib.error
import urllib.request

# (display symbol, coingecko id)
CRYPTO = [("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana")]
STOCKS = ["AAPL", "NVDA", "TSLA"]

REFRESH = 60.0          # seconds between refreshes
IDLE_STOP = 120.0       # stop polling if nobody has read for this long
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _fetch_crypto():
    ids = ",".join(cid for _, cid in CRYPTO)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true")
    data = _get_json(url)
    out = []
    for sym, cid in CRYPTO:
        row = data.get(cid)
        if not row or row.get("usd") is None:
            continue
        out.append({"sym": sym,
                    "price": float(row["usd"]),
                    "pct": float(row.get("usd_24h_change") or 0.0)})
    return out


def _fetch_stocks():
    out = []
    for sym in STOCKS:
        try:
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{sym}?range=1d&interval=1d")
            meta = _get_json(url)["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None or not prev:
                continue
            out.append({"sym": sym,
                        "price": float(price),
                        "pct": (float(price) - float(prev)) / float(prev) * 100.0})
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
                IndexError, TypeError, ValueError, OSError):
            continue      # one bad symbol must not lose the whole refresh
    return out


class MarketFeed:
    """Background poller with a last-good cache.

    Self-limiting: the thread starts on first read and exits once nothing has
    read from it for IDLE_STOP, so leaving the ticker mode does not leave a
    thread polling the internet forever.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._updated = 0.0        # when we last got real data
        self._last_try = 0.0
        self._last_read = 0.0
        self._thread = None
        self._err = None

    # ---- reading (called from the render loop; must never block) --------
    def get(self):
        """Returns (rows, age_seconds_or_None, error_or_None)."""
        now = time.time()
        with self._lock:
            self._last_read = now
            # Copy the dicts, not just the list. A shallow copy hands out the
            # cache's own dicts, so a caller writing to a row would silently
            # rewrite the stored price for everyone -- which is exactly how a
            # panel ends up confidently showing a number nothing produced.
            rows = [dict(r) for r in self._rows]
            updated, err = self._updated, self._err
        self._ensure_thread()
        age = (now - updated) if updated else None
        return rows, age, err

    # ---- polling -------------------------------------------------------
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
                return                     # nobody is watching; stand down
            self._refresh_once()
            time.sleep(2.0)

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < REFRESH:
                return
            self._last_try = now
        rows, err = [], None
        try:
            rows += _fetch_crypto()
        except Exception as e:                     # noqa: BLE001 - never die
            err = f"{type(e).__name__}"
        try:
            rows += _fetch_stocks()
        except Exception as e:                     # noqa: BLE001
            err = err or f"{type(e).__name__}"
        with self._lock:
            if rows:
                # Only replace the cache when we actually got something, so a
                # blip keeps the last good prices on screen instead of blanking.
                self._rows = rows
                self._updated = time.time()
                self._err = None
            else:
                self._err = err or "no data"


FEED = MarketFeed()
