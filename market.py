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

Symbols are config-driven, not hardcoded -- the production device needs
per-owner watchlists, and a single Mac install shouldn't need a code edit
to swap AAPL for something else either. See CONFIG_PATH.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "market_config.json"

DEFAULT_CRYPTO = ["BTC", "ETH", "SOL"]
DEFAULT_STOCKS = ["AAPL", "NVDA", "TSLA"]

# Config lists plain ticker symbols (BTC, not "bitcoin") because that's what
# an owner actually types -- this is the map that turns one into the other.
# Covers the coins someone is likely to actually type in; an unrecognized
# symbol is dropped with a clear reason rather than silently ignored (see
# MarketFeed.set_symbols), not the end of a search feature this doesn't need.
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOT": "polkadot", "MATIC": "matic-network",
    "LTC": "litecoin", "BCH": "bitcoin-cash", "SHIB": "shiba-inu",
    "UNI": "uniswap", "ATOM": "cosmos", "XLM": "stellar", "ETC": "ethereum-classic",
    "FIL": "filecoin", "APT": "aptos", "ARB": "arbitrum", "OP": "optimism",
    "NEAR": "near", "ICP": "internet-computer", "SUI": "sui", "TON": "the-open-network",
}

REFRESH = 60.0          # seconds between refreshes
CONFIG_CHECK = 10.0     # seconds between checking whether the config changed
IDLE_STOP = 120.0       # stop polling if nobody has read for this long
TIMEOUT = 8.0
_UA = "Mozilla/5.0 (HenderburghArcade)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def load_config():
    """Read market_config.json, seeding it with sane defaults on first run
    so there's always a real, editable file on disk -- an owner (or the
    production device's setup flow) should never need to hand-construct
    this from nothing."""
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            crypto = [str(s).upper() for s in data.get("crypto", DEFAULT_CRYPTO)]
            stocks = [str(s).upper() for s in data.get("stocks", DEFAULT_STOCKS)]
            if crypto or stocks:
                return crypto, stocks
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            pass          # fall through to defaults; never crash the feed over a bad file
    save_config(DEFAULT_CRYPTO, DEFAULT_STOCKS)
    return list(DEFAULT_CRYPTO), list(DEFAULT_STOCKS)


def save_config(crypto, stocks):
    CONFIG_PATH.write_text(json.dumps(
        {"crypto": list(crypto), "stocks": list(stocks)}, indent=2))


def _fetch_crypto(symbols):
    pairs = [(s, SYMBOL_TO_COINGECKO_ID[s]) for s in symbols if s in SYMBOL_TO_COINGECKO_ID]
    if not pairs:
        return []
    ids = ",".join(cid for _, cid in pairs)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true")
    data = _get_json(url)
    out = []
    for sym, cid in pairs:
        row = data.get(cid)
        if not row or row.get("usd") is None:
            continue
        out.append({"sym": sym,
                    "price": float(row["usd"]),
                    "pct": float(row.get("usd_24h_change") or 0.0)})
    return out


def _fetch_stocks(symbols):
    out = []
    for sym in symbols:
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
        self._last_config_check = 0.0
        self._thread = None
        self._err = None
        self.crypto, self.stocks = load_config()

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

    def get_symbols(self):
        with self._lock:
            return list(self.crypto), list(self.stocks)

    def set_symbols(self, crypto, stocks):
        """Update the watchlist and force an immediate refresh. Returns
        (accepted_crypto, rejected_crypto) so a caller (e.g. an HTTP
        endpoint) can tell an owner "TESLA isn't a coin" instead of the
        symbol just silently never appearing."""
        crypto = [str(s).strip().upper() for s in crypto if str(s).strip()]
        stocks = [str(s).strip().upper() for s in stocks if str(s).strip()]
        accepted = [s for s in crypto if s in SYMBOL_TO_COINGECKO_ID]
        rejected = [s for s in crypto if s not in SYMBOL_TO_COINGECKO_ID]
        with self._lock:
            self.crypto, self.stocks = accepted, stocks
            self._last_try = 0.0       # let the next loop tick refresh immediately
        save_config(accepted, stocks)
        return accepted, rejected

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
            self._maybe_reload_config()
            self._refresh_once()
            time.sleep(2.0)

    def _maybe_reload_config(self):
        # Picks up hand-edits to market_config.json (or a future setup-page
        # write) without needing a service restart.
        now = time.time()
        if now - self._last_config_check < CONFIG_CHECK:
            return
        self._last_config_check = now
        crypto, stocks = load_config()
        with self._lock:
            if crypto != self.crypto or stocks != self.stocks:
                self.crypto, self.stocks = crypto, stocks
                self._last_try = 0.0

    def _refresh_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_try < REFRESH:
                return
            self._last_try = now
            crypto, stocks = list(self.crypto), list(self.stocks)
        rows, err = [], None
        try:
            rows += _fetch_crypto(crypto)
        except Exception as e:                     # noqa: BLE001 - never die
            err = f"{type(e).__name__}"
        try:
            rows += _fetch_stocks(stocks)
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
