"""
home.py -- house glance for the Home Hub.

Same shape as weather.py / flights.py: ALL I/O lives here. A background
thread may poll Home Assistant if a token is configured; HA can also
PUSH a snapshot to POST /api/home/ingest. get() never blocks. Never
invent a sensor.

Honest gaps:
  * No ingest and no reachable HA -> rooms empty, err set, last-good kept.
  * Doorbell / door / leak entity ids empty -> those slots stay absent.
  * A light already on when we start watching: on_since is NOW. We do
    not invent how long it was on before the hub opened an eye.
  * Apollo M-1 is the glass, not a room lamp. Excluded from the count.

Story over telemetry. Durations become 3H / 45M. Counts become 3 ON.
Missing sensors are not drawn as errors.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import paneltext

CONFIG_PATH = Path(__file__).parent / "home_config.json"
HOME_LOG_PATH = Path(__file__).parent / "home_log.jsonl"
HOME_LOG_MAX = 80

CONFIG_CHECK = 10.0
POLL = 20.0
IDLE_STOP = 120.0
TIMEOUT = 6.0
LATE_MIN_ON = 15 * 60        # still-on after bedtime must be a real sit, not a blip
STORY_MIN_ON = 30 * 60       # room story chip only after a half hour

# Real HA areas in this house, mapped to radar slots. Labels are what
# the 3x5 font can hold. Entertainment Room is the game room.
DEFAULT_ROOMS = (
    {"id": "hallway", "label": "HALLWAY", "area": "Hallway"},
    {"id": "kitchen", "label": "KITCHEN", "area": "Kitchen"},
    {"id": "living", "label": "LIVING", "area": "Living Room"},
    {"id": "game", "label": "GAME", "area": "Entertainment Room"},
    {"id": "beds", "label": "BEDS", "area": "3rd Bedroom"},
)

DEFAULTS = {
    "ha_url": "http://127.0.0.1:8123",
    "ha_token": None,
    "bedtime_hour": 23,
    "rooms": [dict(r) for r in DEFAULT_ROOMS],
    "exclude_prefix": ["light.apollo_m_1"],
    "doorbell": None,
    "door": None,
    "leak": None,
}

_ENTITY_ROOM = (
    ("hallway", "hallway"),
    ("kitchen", "kitchen"),
    ("living_room", "living"),
    ("tv_lamp", "living"),
    ("nicole", "beds"),
    ("bedroom", "beds"),
    ("bookshelf", "game"),
    ("office", "game"),
    ("t_v_lights", "game"),
    ("desk", "game"),
)


def _fold(s):
    return paneltext.panel_text(s) if s else None


def load_config():
    cfg = dict(DEFAULTS)
    cfg["rooms"] = [dict(r) for r in DEFAULT_ROOMS]
    if not CONFIG_PATH.exists():
        return cfg
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    if raw.get("ha_url"):
        cfg["ha_url"] = str(raw["ha_url"]).rstrip("/")
    tok = raw.get("ha_token")
    cfg["ha_token"] = str(tok).strip() or None if tok else None
    try:
        bh = int(raw.get("bedtime_hour"))
        if 0 <= bh <= 23:
            cfg["bedtime_hour"] = bh
    except (TypeError, ValueError):
        pass
    rooms = raw.get("rooms")
    if isinstance(rooms, list) and rooms:
        cleaned = []
        for r in rooms:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip().lower()
            if not rid:
                continue
            cleaned.append({
                "id": rid,
                "label": _fold(r.get("label") or rid) or rid.upper(),
                "area": str(r.get("area") or "").strip() or None,
            })
        if cleaned:
            cfg["rooms"] = cleaned
    ex = raw.get("exclude_prefix")
    if isinstance(ex, list) and ex:
        cfg["exclude_prefix"] = [str(x) for x in ex if x]
    for k in ("doorbell", "door", "leak"):
        v = raw.get(k)
        cfg[k] = str(v).strip() or None if v else None
    return cfg


def save_config(patch):
    """Read-modify-write. Omitted keys stay."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(patch, dict):
        raise ValueError("config must be an object")
    for k in ("ha_url", "ha_token", "doorbell", "door", "leak"):
        if k in patch:
            v = patch[k]
            data[k] = (str(v).strip() or None) if v else None
    if "bedtime_hour" in patch:
        try:
            bh = int(patch["bedtime_hour"])
        except (TypeError, ValueError) as e:
            raise ValueError("bedtime_hour must be 0-23") from e
        if not 0 <= bh <= 23:
            raise ValueError("bedtime_hour must be 0-23")
        data["bedtime_hour"] = bh
    if "rooms" in patch:
        data["rooms"] = patch["rooms"]
    if "exclude_prefix" in patch:
        data["exclude_prefix"] = patch["exclude_prefix"]
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return load_config()


def _excluded(eid, prefixes):
    e = (eid or "").lower()
    return any(e.startswith(p.lower()) for p in (prefixes or []))


def _room_for(light, rooms, prefixes):
    eid = (light.get("id") or "").lower()
    if _excluded(eid, prefixes):
        return None
    area = (light.get("area") or "").strip().lower()
    for r in rooms:
        want = (r.get("area") or "").strip().lower()
        if want and area == want:
            return r["id"]
    for needle, rid in _ENTITY_ROOM:
        if needle in eid:
            return rid
    return None


def fmt_age(secs):
    """3H / 45M / 12S. None if not a real duration."""
    if not isinstance(secs, (int, float)) or secs < 0:
        return None
    secs = int(secs)
    if secs < 60:
        return "%dS" % secs
    if secs < 3600:
        return "%dM" % (secs // 60)
    return "%dH" % (secs // 3600)


class HomeFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._cfg = load_config()
        self._lights = []
        self._on_since = {}          # entity_id -> epoch, adopted on first ON
        self._updated = 0.0
        self._err = None
        self._last_read = 0.0
        self._last_cfg = 0.0
        self._last_poll = 0.0
        self._thread = None
        self._late_day = None        # local yyyy-mm-dd already announced
        self._late_pending = None
        self._stories = []
        self._loaded_log = False

    def get(self):
        now = time.time()
        with self._lock:
            self._last_read = now
            self._ensure_log()
            cfg = dict(self._cfg)
            lights = [dict(x) for x in self._lights]
            on_since = dict(self._on_since)
            age = (now - self._updated) if self._updated else None
            err = self._err
            stories = list(self._stories[-12:])
            late_day = self._late_day
        self._ensure_thread()
        rooms = self._rooms_from(cfg, lights, on_since, now)
        on_count = sum(r["on"] for r in rooms)
        total = sum(r["total"] for r in rooms)
        story = None
        longest = None
        for r in rooms:
            if r["on"] and isinstance(r.get("longest_s"), (int, float)):
                if longest is None or r["longest_s"] > longest["longest_s"]:
                    longest = r
        if longest and longest["longest_s"] >= STORY_MIN_ON:
            age_s = fmt_age(longest["longest_s"])
            if age_s:
                story = "%s  %s" % (longest["label"], age_s)
        rings_today = 0
        day0 = time.localtime(now)
        start = time.mktime((day0.tm_year, day0.tm_mon, day0.tm_mday,
                             0, 0, 0, 0, 0, -1))
        for e in stories:
            if e.get("kind") == "ring" and float(e.get("ts") or 0) >= start:
                rings_today += 1
        slots = {
            "doorbell": bool(cfg.get("doorbell")),
            "door": bool(cfg.get("door")),
            "leak": bool(cfg.get("leak")),
        }
        late = False
        try:
            bh = int(cfg.get("bedtime_hour"))
        except (TypeError, ValueError):
            bh = None
        if bh is not None and on_count and time.localtime(now).tm_hour >= bh:
            late = any((r.get("longest_s") or 0) >= LATE_MIN_ON for r in rooms)
        return {
            "configured": bool(cfg.get("ha_url")),
            "rooms": rooms,
            "on_count": on_count,
            "light_total": total,
            "story": story,
            "rings_today": rings_today,
            "stories": list(reversed(stories[-8:])),
            "slots": slots,
            "late": late,
            "late_today": late_day == time.strftime("%Y-%m-%d", time.localtime(now)),
            "bedtime_hour": bh,
            "age": age,
            "err": err,
        }

    def ingest(self, lights):
        """HA (or a test) pushed a real snapshot. Adopt ON times."""
        if not isinstance(lights, list):
            raise ValueError("lights must be a list")
        now = time.time()
        cleaned = []
        for raw in lights:
            if not isinstance(raw, dict):
                continue
            eid = str(raw.get("id") or raw.get("entity_id") or "").strip()
            if not eid:
                continue
            st = str(raw.get("state") or "").strip().lower()
            if st not in ("on", "off", "unavailable", "unknown"):
                st = "unknown"
            cleaned.append({
                "id": eid,
                "state": st,
                "name": _fold(raw.get("name") or eid) or eid,
                "area": raw.get("area") or None,
            })
        with self._lock:
            self._cfg = load_config()
            prefixes = self._cfg.get("exclude_prefix") or []
            kept = [x for x in cleaned if not _excluded(x["id"], prefixes)]
            self._adopt_on_since(kept, now)
            self._lights = kept
            self._updated = now
            self._err = None
            self._maybe_late_locked(now)
        return len(cleaned)

    def record_event(self, kind, summary):
        """Lasting house fact. kind is ring/door/leak/late. Summary folded by caller."""
        if kind not in ("ring", "door", "leak", "late") or not summary:
            return
        now = time.time()
        e = {"ts": now, "kind": kind, "summary": summary}
        with self._lock:
            self._ensure_log()
            self._stories.append(e)
            while len(self._stories) > HOME_LOG_MAX:
                self._stories.pop(0)
            try:
                with HOME_LOG_PATH.open("w") as f:
                    for row in self._stories:
                        f.write(json.dumps(row) + "\n")
            except OSError:
                pass
        return e

    def pop_late(self):
        """One-shot normal banner payload, or None."""
        with self._lock:
            pending, self._late_pending = self._late_pending, None
        return pending

    def _rooms_from(self, cfg, lights, on_since, now):
        rooms_cfg = cfg.get("rooms") or []
        prefixes = cfg.get("exclude_prefix") or []
        buckets = {r["id"]: [] for r in rooms_cfg}
        for lt in lights:
            rid = _room_for(lt, rooms_cfg, prefixes)
            if rid in buckets:
                buckets[rid].append(lt)
        out = []
        for r in rooms_cfg:
            items = buckets.get(r["id"]) or []
            if not items:
                continue
            ons = [x for x in items if x.get("state") == "on"]
            longest = None
            for x in ons:
                since = on_since.get(x["id"])
                if isinstance(since, (int, float)):
                    d = now - since
                    if longest is None or d > longest:
                        longest = d
            out.append({
                "id": r["id"],
                "label": r.get("label") or r["id"].upper(),
                "on": len(ons),
                "total": len(items),
                "longest_s": longest,
            })
        return out

    def _adopt_on_since(self, lights, now):
        live = {x["id"] for x in lights}
        for x in lights:
            eid, st = x["id"], x["state"]
            if st == "on":
                if eid not in self._on_since:
                    self._on_since[eid] = now
            else:
                self._on_since.pop(eid, None)
        for eid in list(self._on_since):
            if eid not in live:
                self._on_since.pop(eid, None)

    def _maybe_late_locked(self, now):
        cfg = self._cfg
        try:
            bh = int(cfg.get("bedtime_hour"))
        except (TypeError, ValueError):
            return
        local = time.localtime(now)
        if local.tm_hour < bh:
            return
        day = time.strftime("%Y-%m-%d", local)
        if self._late_day == day:
            return
        rooms = self._rooms_from(cfg, self._lights, self._on_since, now)
        on_count = sum(r["on"] for r in rooms)
        if not on_count:
            return
        # Need at least one light that has been on a real stretch.
        if not any((r.get("longest_s") or 0) >= LATE_MIN_ON for r in rooms):
            return
        self._late_day = day
        label = "LIGHTS  LATE"
        self._late_pending = {"title": "LIGHTS", "message": "LATE"}
        # Persist the fact here (lock already held).
        self._ensure_log()
        e = {"ts": now, "kind": "late", "summary": label}
        self._stories.append(e)
        while len(self._stories) > HOME_LOG_MAX:
            self._stories.pop(0)
        try:
            with HOME_LOG_PATH.open("w") as f:
                for row in self._stories:
                    f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def _ensure_log(self):
        if self._loaded_log:
            return
        rows = []
        if HOME_LOG_PATH.exists():
            try:
                with HOME_LOG_PATH.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(e, dict) and e.get("kind") and e.get("summary"):
                            rows.append(e)
            except OSError:
                pass
        self._stories = rows
        self._loaded_log = True

    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if not (self._cfg.get("ha_token") and self._cfg.get("ha_url")):
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read
            if idle > IDLE_STOP:
                return
            self._maybe_reload()
            self._poll_once()
            time.sleep(2.0)

    def _maybe_reload(self):
        now = time.time()
        if now - self._last_cfg < CONFIG_CHECK:
            return
        self._last_cfg = now
        cfg = load_config()
        with self._lock:
            self._cfg = cfg

    def _poll_once(self):
        now = time.time()
        with self._lock:
            if now - self._last_poll < POLL:
                return
            url, token = self._cfg.get("ha_url"), self._cfg.get("ha_token")
            self._last_poll = now
        if not (url and token):
            return
        try:
            req = urllib.request.Request(
                url.rstrip("/") + "/api/states",
                headers={"Authorization": "Bearer " + token,
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload = json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            with self._lock:
                self._err = type(e).__name__
            return
        except Exception as e:                    # noqa: BLE001
            with self._lock:
                self._err = type(e).__name__
            return
        lights = []
        if isinstance(payload, list):
            for st in payload:
                if not isinstance(st, dict):
                    continue
                eid = st.get("entity_id") or ""
                if not str(eid).startswith("light."):
                    continue
                attrs = st.get("attributes") or {}
                lights.append({
                    "id": eid,
                    "state": st.get("state"),
                    "name": attrs.get("friendly_name") or eid,
                    "area": attrs.get("area") or None,
                })
        try:
            self.ingest(lights)
        except ValueError:
            pass


FEED = HomeFeed()


def classify_notify(title, message, kind=None):
    """Map an HA notify into a house story kind, or None.

    Only real words we were sent. Do not guess a leak from 'wet paint'."""
    k = (kind or "").strip().lower()
    if k in ("doorbell", "ring"):
        return "ring"
    if k in ("door",):
        return "door"
    if k in ("leak", "water", "flood"):
        return "leak"
    blob = " ".join(x for x in (title, message) if x).upper()
    if re.search(r"\b(LEAK|FLOOD|WATER ALARM)\b", blob):
        return "leak"
    if re.search(r"\b(DOORBELL|RING)\b", blob):
        return "ring"
    if re.search(r"\b(DOOR OPEN|GARAGE|ENTRY)\b", blob):
        return "door"
    return None
