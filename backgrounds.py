"""
backgrounds.py — Apollo M-1 WLED effects as arcade backgrounds.

Correctness rule (no exceptions)
--------------------------------
We never invent what an effect looks like. Every pixel comes from the Apollo:

1. Ambient (mode=background)
   - Stop DDP → POST exact fx id → panel runs firmware FX.
   - UI = WebSocket liveview peek of the real LED buffer.
   - Those peeks are also recorded into a loop cache (free “port” of the effect).

2. Game + background
   - DDP owns the strip, so we composite a *recorded real loop* under sprites.
   - Cache miss → pause DDP, apply fx, peek N frames from liveview, resume.
   - Until the real loop exists, underlay is pure black — never a fake pattern.

3. Software generators in this file are DEAD CODE for display paths (kept only
   as offline experiment helpers). `frame()` will not call them.
"""
from __future__ import annotations

import base64
import json
import math
import os
import random
import socket
import struct
import threading
import time
import urllib.request
import zlib
from pathlib import Path

WIDTH = HEIGHT = 64
N = WIDTH * HEIGHT * 3
PEEK_HDR = 4  # L, ver, w, h
PEEK_PAYLOAD = WIDTH * HEIGHT * 3
# How many real frames to keep for a game underlay loop
LOOP_TARGET = 48
LOOP_TARGET_SPARSE = 72

_effects_cache: list[str] | None = None
_effects_at = 0.0
_CACHE_TTL = 30.0
_palettes_cache: list[str] | None = None

# Capture cache: fx -> {"speed": int, "frames": list[bytes], "at": float}
_loop_cache: dict[int, dict] = {}
_loop_lock = threading.Lock()
_capture_lock = threading.Lock()  # only one capture at a time (WLED allows 1 lv client)
_capturing = False
_capture_status = "idle"  # idle | capturing | ok | error
_capture_error = ""
# Ambient peeks accumulate here until LOOP_TARGET, then promote to _loop_cache
_building: dict[int, dict] = {}

# Captured loops persisted to disk. A capture costs ~10s with DDP released,
# during which a running game is *off the panel* — so paying it again for the
# same effect after every server restart was the single worst thing about
# backgrounds. Effects are deterministic for a given (fx, speed), so a recorded
# loop stays valid; version + TTL guard against firmware changes.
LOOP_DIR = Path(__file__).parent / ".bg-loops"
LOOP_FILE_VERSION = 1
LOOP_DISK_TTL = 30 * 24 * 3600
_last_fx_verify_at = 0.0
_last_fx_verify_val: int | None = None
_FX_VERIFY_TTL = 0.9  # seconds — avoid 503 from hammering /json/state

# Live peek for ambient UI
_peek_lock = threading.Lock()
_peek_frame: bytes | None = None
_peek_at = 0.0
_peek_thread: threading.Thread | None = None
_peek_stop = threading.Event()
_peek_ip: str | None = None


def _panel_ip(default="192.168.40.24"):
    try:
        from wled_ddp import DEFAULT_IP
        return DEFAULT_IP
    except Exception:
        return default


# ── Effect list ────────────────────────────────────────────────────────────

def fetch_effects(panel_ip=None, force=False):
    global _effects_cache, _effects_at
    now = time.time()
    if not force and _effects_cache is not None and now - _effects_at < _CACHE_TTL:
        return _effects_cache
    ip = panel_ip or _panel_ip()
    try:
        with urllib.request.urlopen(f"http://{ip}/json/effects", timeout=3) as r:
            names = json.load(r)
        if isinstance(names, list) and names:
            _effects_cache = [
                (str(n).strip() if n and str(n).strip() else f"Effect {i}")
                for i, n in enumerate(names)
            ]
            _effects_at = now
            return _effects_cache
    except Exception:
        pass
    return _effects_cache or []


def fetch_palettes(panel_ip=None):
    global _palettes_cache
    if _palettes_cache is not None:
        return _palettes_cache
    ip = panel_ip or _panel_ip()
    try:
        with urllib.request.urlopen(f"http://{ip}/json/palettes", timeout=3) as r:
            pals = json.load(r)
        if isinstance(pals, list):
            _palettes_cache = [str(p) for p in pals]
            return _palettes_cache
    except Exception:
        pass
    return _palettes_cache or ["Default"]


def list_modes(panel_ip=None):
    """Every Apollo effect + None. id is the WLED fx index as string."""
    names = fetch_effects(panel_ip, force=False)
    if not names:
        names = fetch_effects(panel_ip, force=True)
    out = [{"id": "none", "label": "None (black)", "group": "—", "fx": -1}]
    for i, name in enumerate(names):
        out.append({
            "id": str(i),
            "label": f"{i:03d} · {name}",
            "group": _group_for(name),
            "fx": i,
        })
    return out


def _group_for(name: str) -> str:
    n = name.lower().strip()
    raw = name.strip()
    if n.startswith("rsvd") or n == "rsvd":
        return "Reserved"
    if n.startswith("ps "):
        return "Particle System"
    if raw.startswith("Y💡") or "💡" in raw[:4] or raw.startswith("Y "):
        return "Yves 2D"
    if any(k in n for k in (
        "audio", "geq", "freq", "grav", "sound", "volume", "jerk",
        "noisemeter", "freqwave", "freqmatrix", "freqmap", "rocktaves",
        "puddlepeak", "ripple peak", "noisefire",
    )):
        return "Audio Reactive"
    if any(k in n for k in (
        "2d", "matrix", "matripix", "dna", "metaball", "game of life", "julia",
        "plasma ball", "black hole", "drift", "soap", "octopus", "waving cell",
        "noise2d", "akemi", "hiphotic", "tartan", "lissajous", "sindots",
        "frizzles", "squared swirl", "spaceship", "crazy bee", "ghost rider",
        "blobs", "pulser", "blurz", "waverly", "sun radiation", "colored burst",
        "swirl", "flow stripe", "wavesins", "pixelwave", "pixels", "juggles",
        "gravcenter", "gravcentric", "dj light", "funky plank",
    )):
        return "2D / Matrix"
    if "rainbow" in n or "pride" in n or "colorwave" in n or "colorloop" in n:
        return "Color / Rainbow"
    if any(k in n for k in (
        "fire", "candle", "meteor", "sparkle", "twinkle", "firework", "lightning",
        "aurora", "pacifica", "lake", "rain", "snow", "sunrise", "ripple",
        "plasma", "fairy", "glitter", "drip", "heartbeat", "sinelon",
    )):
        return "Nature / FX"
    if any(k in n for k in (
        "chase", "scan", "wipe", "theater", "running", "android", "comet",
        "sweep", "lighthouse", "saw", "icu", "oscillate", "railway", "spots",
        "loading", "tri wipe", "tri fade", "two dots", "multi comet",
    )):
        return "Chase / Scan"
    if any(k in n for k in (
        "solid", "blink", "breathe", "fade", "gradient", "palette", "percent",
        "dynamic", "random colors", "colorful", "blends", "phased", "sine",
    )):
        return "Basic"
    return "Effects"


def parse_fx(mode_id):
    if mode_id is None or mode_id == "" or str(mode_id).lower() in ("none", "off", "black"):
        return None
    try:
        v = int(mode_id)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        names = fetch_effects()
        low = str(mode_id).lower().strip()
        for i, n in enumerate(names):
            if n.lower().strip() == low or _clean_name(n) == low:
                return i
        return None


def effect_name(fx):
    names = fetch_effects()
    if fx is None or fx < 0 or fx >= len(names):
        return "none"
    return names[fx]


def _clean_name(name: str) -> str:
    """Strip WLED decorations → 'polar_waves', 'yves', 'fire_2012'."""
    s = str(name)
    for ch in ("💡", "☾", "⚙️", "☆", "★", "✦"):
        s = s.replace(ch, " ")
    # Leading Yves marker: "Y Polar_Waves" / "Y💡…"
    s = s.strip()
    if len(s) >= 2 and s[0] in ("Y", "y") and not s[1].isalnum():
        s = s[1:]
    s = s.strip().lower()
    out = []
    for c in s:
        if c.isalnum() or c == "_":
            out.append(c)
        elif c in (" ", "-", "/", ".", "+"):
            out.append("_")
        # drop other punctuation
    joined = "".join(out)
    while "__" in joined:
        joined = joined.replace("__", "_")
    return joined.strip("_")


# ── Native WLED apply (exact fx, no cosmetic mutation) ─────────────────────

def panel_fx(panel_ip=None, use_cache=False):
    """Current firmware effect id on segment 0, or None."""
    global _last_fx_verify_at, _last_fx_verify_val
    if use_cache and (time.time() - _last_fx_verify_at) < _FX_VERIFY_TTL:
        return _last_fx_verify_val
    ip = panel_ip or _panel_ip()
    try:
        with urllib.request.urlopen(f"http://{ip}/json/state", timeout=2.0) as r:
            st = json.load(r)
        seg = (st.get("seg") or [{}])[0]
        val = int(seg.get("fx", -1))
        _last_fx_verify_at = time.time()
        _last_fx_verify_val = val
        return val
    except Exception:
        return _last_fx_verify_val if use_cache else None


def panel_live(panel_ip=None):
    """True/False if WLED reports realtime (DDP) override, None if unreachable."""
    ip = panel_ip or _panel_ip()
    try:
        with urllib.request.urlopen(f"http://{ip}/json/info", timeout=1.5) as r:
            info = json.load(r)
        v = info.get("live")
        return bool(v) if v is not None else None
    except Exception:
        return None


def wait_realtime_released(panel_ip=None, max_wait=2.7, poll=0.4) -> float:
    """Block until WLED has actually left realtime mode. Returns seconds waited.

    WLED drops realtime `live.timeout` (2.5s) after the last packet, so the old
    code slept a flat 2.7s on every single capture. Polling `info.live` lets a
    panel that has already released carry on immediately, which is most of the
    win on a back-to-back background change. If the panel never reports (older
    firmware, field absent, offline) this still waits the full max_wait, so the
    behaviour degrades exactly to what it was before.
    """
    t0 = time.time()
    deadline = t0 + max_wait
    while time.time() < deadline:
        if panel_live(panel_ip) is False:
            return time.time() - t0
        time.sleep(poll)
    return time.time() - t0


def apply_wled(panel_ip, fx, speed=50, brightness=None, palette=None):
    """
    Set the real Apollo effect by exact fx id.
    Full 64×64 segment, unfrozen. Does not invent reverse/mirror.
    Palette only if caller passes it (otherwise leave panel palette alone).
    """
    if fx is None or int(fx) < 0:
        return False
    fx = int(fx)
    sx = max(0, min(255, int(round(int(speed) * 2.55))))
    ip = panel_ip or _panel_ip()
    # Only patch what we must. Do NOT force ix/rev/mi/palette — that mutates
    # exact Apollo character (and made effects look "wrong" vs the panel UI).
    seg = {
        "id": 0,
        "start": 0,
        "stop": 64,
        "startY": 0,
        "stopY": 64,
        "fx": fx,
        "sx": sx,
        "on": True,
        "frz": False,
        "sel": True,
    }
    if palette is not None:
        seg["pal"] = int(palette)
    payload = {
        "on": True,
        "transition": 0,
        "mainseg": 0,
        "seg": [seg],
    }
    if brightness is not None:
        payload["bri"] = int(brightness)
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://{ip}/json/state", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2.5) as r:
            r.read()
        return True
    except Exception:
        return False


def is_capturing() -> bool:
    return _capturing


def capture_status() -> dict:
    with _loop_lock:
        cached = sorted(_loop_cache.keys())
    return {
        "capturing": _capturing,
        "status": _capture_status,
        "error": _capture_error,
        "cached_fx": cached,
        "restored_from_disk": _loops_restored,
    }


# ── Minimal WebSocket client (stdlib only) ─────────────────────────────────

def _ws_connect(ip: str, path="/ws", timeout=3.0):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {ip}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    s = socket.create_connection((ip, 80), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(req)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    header, rest = data.split(b"\r\n\r\n", 1)
    if b"101" not in header.split(b"\r\n", 1)[0]:
        s.close()
        raise RuntimeError(f"ws handshake failed: {header[:120]!r}")
    return s, rest


def _ws_send_text(s, text: str):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header) + masked)


def _ws_recv_message(s, leftover=b"", timeout=3.0):
    """Read one full (possibly fragmented) websocket message. Returns (opcode, payload, leftover)."""
    s.settimeout(timeout)
    buf = leftover

    def need(n):
        nonlocal buf
        while len(buf) < n:
            chunk = s.recv(max(8192, n - len(buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    full = bytearray()
    opcode = None
    while True:
        b0 = need(1)[0]
        b1 = need(1)[0]
        op = b0 & 0x0F
        fin = bool(b0 & 0x80)
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", need(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", need(8))[0]
        mask = need(4) if masked else None
        payload = bytearray(need(ln))
        if mask:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
        if op == 0x8:  # close
            return 8, bytes(payload), buf
        if op == 0x9:  # ping — ignore, keep reading
            continue
        if op == 0xA:  # pong
            continue
        if opcode is None:
            opcode = op
        full += payload
        if fin:
            return opcode, bytes(full), buf


def _parse_peek_frame(msg: bytes) -> bytes | None:
    """WLED liveview binary: 'L', ver, w, h, then w*h*3 RGB."""
    if not msg or msg[0] != ord("L") or len(msg) < PEEK_HDR:
        return None
    w, h = msg[2], msg[3]
    need = PEEK_HDR + w * h * 3
    if len(msg) < need:
        return None
    raw = msg[PEEK_HDR:need]
    if w == WIDTH and h == HEIGHT:
        return raw
    # unexpected size — nearest-neighbour resample to 64x64
    out = bytearray(N)
    for y in range(HEIGHT):
        sy = min(h - 1, y * h // HEIGHT)
        for x in range(WIDTH):
            sx = min(w - 1, x * w // WIDTH)
            si = (sy * w + sx) * 3
            di = (y * WIDTH + x) * 3
            out[di:di + 3] = raw[si:si + 3]
    return bytes(out)


def peek_frames(panel_ip, count=1, timeout_each=2.0) -> list[bytes]:
    """Grab `count` real LED frames via WLED liveview. Stops lv when done."""
    ip = panel_ip or _panel_ip()
    frames: list[bytes] = []
    s = None
    leftover = b""
    try:
        s, leftover = _ws_connect(ip)
        try:
            _ws_recv_message(s, leftover, timeout=1.5)
            leftover = b""
        except Exception:
            leftover = b""
        _ws_send_text(s, '{"lv":true}')
        deadline = time.time() + max(3.0, timeout_each * count + 2.0)
        while len(frames) < count and time.time() < deadline:
            try:
                op, msg, leftover = _ws_recv_message(s, leftover, timeout=timeout_each)
            except Exception:
                break
            if op == 2:
                fr = _parse_peek_frame(msg)
                if fr is not None:
                    frames.append(fr)
            elif op == 1:
                # {"success":true} etc — ignore
                continue
            elif op == 8:
                break
        try:
            _ws_send_text(s, '{"lv":false}')
        except Exception:
            pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    return frames


def peek_one(panel_ip=None) -> bytes | None:
    got = peek_frames(panel_ip, count=1)
    return got[0] if got else None


# ── Ambient live peek thread (UI preview = exact panel) ────────────────────

def start_live_peek(panel_ip=None):
    """Background thread: keep `_peek_frame` equal to the real panel buffer."""
    global _peek_thread, _peek_ip
    ip = panel_ip or _panel_ip()
    _peek_ip = ip
    _peek_stop.clear()
    if _peek_thread and _peek_thread.is_alive():
        return

    def _run():
        global _peek_frame, _peek_at
        while not _peek_stop.is_set():
            if _capturing:
                _peek_stop.wait(0.2)
                continue
            try:
                frames = peek_frames(_peek_ip or ip, count=2, timeout_each=1.2)
                if frames:
                    with _peek_lock:
                        _peek_frame = frames[-1]
                        _peek_at = time.time()
            except Exception:
                pass
            _peek_stop.wait(0.35)
        # mark dead so next start_live_peek can respawn
        global _peek_thread
        _peek_thread = None

    _peek_thread = threading.Thread(target=_run, daemon=True, name="wled-peek")
    _peek_thread.start()


def stop_live_peek():
    _peek_stop.set()


def live_frame(max_age=1.5) -> bytes | None:
    with _peek_lock:
        if _peek_frame is None:
            return None
        if time.time() - _peek_at > max_age:
            return None
        return _peek_frame


# ── Capture real effect loops for game underlays ───────────────────────────

def _cache_key_speed(speed: int) -> int:
    # bucket speed so minor slider noise doesn't thrash recaptures
    return int(round(max(1, min(100, speed)) / 10.0) * 10)


def _loop_path(fx: int, sk: int) -> Path:
    return LOOP_DIR / f"fx{int(fx)}-s{int(sk)}.loop"


def _save_loop_to_disk(fx: int, ent: dict) -> bool:
    """Write one captured loop: a JSON metadata line, then zlib'd raw frames."""
    try:
        LOOP_DIR.mkdir(exist_ok=True)
        frames = ent.get("frames") or []
        if not frames:
            return False
        sk = int(ent["speed"])
        meta = {
            "v": LOOP_FILE_VERSION, "fx": int(fx), "speed": sk,
            "n": len(frames), "name": ent.get("name", ""),
            "sparse": bool(ent.get("sparse")),
            "busy": ent.get("busy"),
            "at": ent.get("at") or time.time(),
            "source": ent.get("source", ""),
        }
        blob = zlib.compress(b"".join(frames), 6)
        dst = _loop_path(fx, sk)
        tmp = dst.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(json.dumps(meta).encode() + b"\n")
            f.write(blob)
        tmp.replace(dst)         # atomic: a torn file would poison the underlay
        return True
    except Exception:
        return False


def _read_loop_file(p: Path):
    """Parse one .loop file -> (fx, entry), or None if unusable."""
    try:
        with open(p, "rb") as f:
            meta = json.loads(f.readline().decode())
            if int(meta.get("v", -1)) != LOOP_FILE_VERSION:
                return None
            if time.time() - float(meta.get("at") or 0) > LOOP_DISK_TTL:
                return None
            raw = zlib.decompress(f.read())
        cnt = int(meta["n"])
        if cnt <= 0 or len(raw) != cnt * N:
            return None               # truncated / wrong geometry — ignore it
        frames = [raw[i * N:(i + 1) * N] for i in range(cnt)]
        return int(meta["fx"]), {
            "speed": int(meta["speed"]),
            "frames": frames,
            "at": float(meta.get("at") or 0),
            "name": meta.get("name", ""),
            "sparse": bool(meta.get("sparse")),
            "busy": meta.get("busy"),
            "source": (meta.get("source") or "capture") + "+disk",
        }
    except Exception:
        return None


def _load_loops_from_disk() -> int:
    """Repopulate the loop cache at import so restarts don't re-capture."""
    loaded = 0
    try:
        if not LOOP_DIR.is_dir():
            return 0
        for p in sorted(LOOP_DIR.glob("fx*.loop")):
            got = _read_loop_file(p)     # one bad file must not stop the rest
            if not got:
                continue
            fx, ent = got
            with _loop_lock:
                _loop_cache[fx] = ent
            loaded += 1
    except Exception:
        pass
    return loaded


def _rehydrate(fx: int, sk: int):
    """Pull one loop back from disk after it was evicted from memory."""
    got = _read_loop_file(_loop_path(fx, sk))
    if not got:
        return None
    _fx, ent = got
    if int(ent["speed"]) != int(sk):
        return None
    with _loop_lock:
        _loop_cache[int(fx)] = ent
    return ent


_loops_restored = _load_loops_from_disk()


def get_cached_loop(fx: int, speed: int = 50):
    sk = _cache_key_speed(speed)
    with _loop_lock:
        ent = _loop_cache.get(int(fx))
        hit = bool(ent) and ent.get("speed") == sk
        frames = (ent.get("frames") or []) if hit else None
    if frames:
        return frames
    # Memory miss: the durable copy on disk still counts as a hit. Switching
    # backgrounds evicts loops from memory, and without this every switch back
    # would pay another ~10s capture with the game off the panel.
    ent = _rehydrate(int(fx), sk)
    return (ent.get("frames") or None) if ent else None


UNDERLAY_TARGET = 26.0   # mean 0–255 level a game underlay should sit at
UNDERLAY_PEAK = 88.0     # ceiling for its hottest pixels, so sprites still win
BUSY_THRESHOLD = 26.0    # adjacent-pixel delta above which a loop is "speckle"


def _busyness(frames) -> float:
    """Mean absolute difference between horizontally adjacent pixels.

    Smooth effects (Rainbow gradients, Running stripes, Breathe) score low;
    per-pixel noise (Colortwinkles) scores high.
    """
    if not frames:
        return 0.0
    tot = 0
    cnt = 0
    for f in frames[:12]:
        for y in range(0, HEIGHT, 2):
            ro = y * WIDTH * 3
            for x in range(0, (WIDTH - 1) * 3, 6):
                tot += abs(f[ro + x] - f[ro + x + 3])
                cnt += 1
    return tot / max(1, cnt)


def _blur_loop(frames, passes=1):
    """Separable 3-tap box blur over a whole loop, applied once at cache time.

    Speckled effects are unusable as a game underlay at *any* gain: they carry
    the same spatial frequency as the sprites, so Life's cells and the Invaders
    simply dissolve into them (verified on the panel at 0.46 and at 0.22).
    Defocusing turns the speckle into a soft colour field while keeping the
    effect's real captured colours — the pixels are still the Apollo's, just
    out of focus. Only the game underlay uses this cache; ambient mode peeks
    the panel live and is untouched.
    """
    out = []
    for f in frames:
        buf = bytearray(f)
        for _ in range(passes):
            tmp = bytearray(len(buf))
            for y in range(HEIGHT):
                ro = y * WIDTH * 3
                for x in range(WIDTH):
                    xm, xp = ro + ((x - 1) % WIDTH) * 3, ro + ((x + 1) % WIDTH) * 3
                    o = ro + x * 3
                    tmp[o] = (buf[xm] + buf[o] + buf[xp]) // 3
                    tmp[o + 1] = (buf[xm + 1] + buf[o + 1] + buf[xp + 1]) // 3
                    tmp[o + 2] = (buf[xm + 2] + buf[o + 2] + buf[xp + 2]) // 3
            buf2 = bytearray(len(tmp))
            for y in range(HEIGHT):
                ym, yp = ((y - 1) % HEIGHT) * WIDTH * 3, ((y + 1) % HEIGHT) * WIDTH * 3
                ro = y * WIDTH * 3
                for x in range(WIDTH):
                    xo = x * 3
                    for c in range(3):
                        buf2[ro + xo + c] = (tmp[ym + xo + c] + tmp[ro + xo + c]
                                             + tmp[yp + xo + c]) // 3
            buf = buf2
        out.append(bytes(buf))
    return out


def soften_if_busy(frames):
    """Blur a freshly captured loop if it is speckle. Returns (frames, busyness)."""
    b = _busyness(frames)
    if b <= BUSY_THRESHOLD:
        return frames, b
    passes = 2 if b > BUSY_THRESHOLD * 2 else 1
    return _blur_loop(frames, passes), b


def loop_level(fx: int, speed: int = 50):
    """(mean, p95) channel level of a cached loop, memoised on the entry.

    Two numbers because they control different failure modes: the mean says how
    much the effect floods the panel, the 95th percentile says how bright its
    hottest pixels get. A speckled effect like Colortwinkles has a low mean but
    individual pixels near 255, which is what made Life's cells disappear into
    it even at a "safe" average brightness.
    """
    sk = _cache_key_speed(speed)
    with _loop_lock:
        ent = _loop_cache.get(int(fx))
        if not ent or ent.get("speed") != sk:
            return None
        lv = ent.get("level")
        if lv is not None:
            return lv
        frames = ent.get("frames") or []
    if not frames:
        return None
    total = sum(sum(f) for f in frames)
    mean = total / float(len(frames) * N)
    # Strided sample for the percentile — exact sorting of 72x12288 bytes every
    # time would cost far more than it is worth for a tuning constant.
    sample = sorted(b for f in frames for b in f[::37])
    p95 = sample[int(len(sample) * 0.95)] if sample else 0
    lv = (mean, float(p95))
    with _loop_lock:
        ent = _loop_cache.get(int(fx))
        if ent is not None and ent.get("speed") == sk:
            ent["level"] = lv
    return lv


def underlay_gain(fx: int, speed: int = 50, cap: float = 0.55,
                  floor: float = 0.10) -> float:
    """Composite gain that lands any effect at a usable underlay brightness.

    Effects differ enormously in how much of the panel they light. Measured off
    the board: Rainbow and Chase Rainbow cover 100% at a mean level near
    120/255, Running 97%, while Lightning is mostly black (12% peak coverage,
    mean 0.1) and Colortwinkles sits at 52%. One fixed gain cannot serve both —
    at a flat 0.36 the dense ones washed the sprites out (purple invaders on
    saturated magenta, almost no contrast) while the sparse ones barely showed.
    Scaling to a target mean keeps every effect readable behind a game.
    """
    lv = loop_level(fx, speed)
    if not lv:
        return cap
    mean, p95 = lv
    g = cap
    if mean > 0:
        g = min(g, UNDERLAY_TARGET / mean)
    if p95 > 0:
        g = min(g, UNDERLAY_PEAK / p95)
    return max(floor, g)


def loop_frame(fx: int, t=None, speed=50) -> bytes | None:
    """Return one frame from a captured real-effect loop, or None."""
    if fx is None:
        return None
    frames = get_cached_loop(fx, speed)
    if not frames:
        return None
    if t is None:
        t = time.time()
    # Advance through the loop proportional to speed
    rate = 10.0 + (max(1, min(100, int(speed))) / 100.0) * 14.0  # ~10–24 fps feel
    idx = int(t * rate) % len(frames)
    return frames[idx]


def ingest_ambient_frame(fx: int, speed: int, frame: bytes, panel_ip=None,
                         verify_fx: bool = True) -> int:
    """
    While ambient peeks real LEDs, record them into a game-underlay loop.
    Pure firmware pixels only. Default verify_fx=True so Solid/wrong FX
    never poisons the cache for ICU/Polar/etc.
    Returns number of frames buffered for this fx (0 if skipped/cached).
    """
    if fx is None or int(fx) < 0 or not frame or len(frame) != N:
        return 0
    fx = int(fx)
    # Refuse to port the wrong effect (e.g. HA snaps to Solid mid-record).
    # Cached panel_fx (TTL) so ambient peek rate doesn't 503 the ESP.
    if verify_fx:
        cur = panel_fx(panel_ip, use_cache=True)
        if cur is None or cur != fx:
            with _loop_lock:
                if fx in _building:
                    _building.pop(fx, None)
            return 0
    # Reject near-full solid fills for non-solid effects (wrong capture)
    if fx != 0:
        nz = sum(1 for i in range(0, N, 3) if frame[i] | frame[i + 1] | frame[i + 2])
        if nz >= N // 3 - 8:  # almost every pixel lit → almost certainly Solid
            return 0
    sk = _cache_key_speed(speed)
    target = LOOP_TARGET_SPARSE if _is_sparse_effect(fx) else LOOP_TARGET
    with _loop_lock:
        ent = _loop_cache.get(fx)
        if ent and ent.get("speed") == sk and ent.get("frames"):
            return len(ent["frames"])
        b = _building.get(fx)
        if not b or b.get("speed") != sk:
            b = {"speed": sk, "frames": []}
            _building[fx] = b
        frames = b["frames"]
        # Dedup identical consecutive peeks (static holds)
        if not frames or frames[-1] != frame:
            frames.append(bytes(frame))
        if len(frames) >= target:
            ent = {
                "speed": sk,
                "frames": list(frames[:target]),
                "at": time.time(),
                "name": effect_name(fx),
                "source": "ambient-peek",
            }
            _loop_cache[fx] = ent
            _building.pop(fx, None)
            promoted = ent
        else:
            promoted = None
    if promoted is not None:
        _save_loop_to_disk(fx, promoted)   # outside the lock: this does file I/O
        return target
    with _loop_lock:
        b = _building.get(fx)
        return len(b["frames"]) if b else 0


def _is_sparse_effect(fx: int) -> bool:
    """Particle / burst FX need longer captures so the loop includes explosions."""
    key = _clean_name(effect_name(fx))
    return any(k in key for k in (
        "firework", "starburst", "popcorn", "sparkle", "twinkle", "meteor",
        "drip", "rain", "snow", "glitter", "fairy", "candle", "lightning",
        "ps_fireworks", "ps_sparkler", "ps_starburst",
    ))


def capture_effect_loop(
    panel_ip,
    fx: int,
    speed: int = 50,
    n_frames: int = 40,
    settle_s: float = 0.45,
    realtime_release_s: float = 2.7,
    wait_for_ddp_release: bool = True,
) -> list[bytes]:
    """
    Capture a loop of the *real* WLED effect.

    Caller must stop sending DDP before this (or set wait_for_ddp_release so we
    wait for WLED realtime timeout). Only one capture runs at a time.
    """
    global _capturing, _capture_status, _capture_error
    ip = panel_ip or _panel_ip()
    fx = int(fx)
    sk = _cache_key_speed(speed)

    if not _capture_lock.acquire(blocking=False):
        # another capture in progress — wait for it if same fx ends up cached
        with _capture_lock:
            pass
        cached = get_cached_loop(fx, speed)
        return list(cached) if cached else []

    try:
        _capturing = True
        _capture_status = "capturing"
        _capture_error = ""

        if wait_for_ddp_release:
            # WLED live.timeout is 25 (=2.5s). Poll rather than sleep the whole
            # thing — see wait_realtime_released; falls back to the full wait.
            wait_realtime_released(ip, max_wait=realtime_release_s)

        if not apply_wled(ip, fx, speed):
            _capture_status = "error"
            _capture_error = "apply_wled failed"
            return []

        sparse = _is_sparse_effect(fx)
        # Fireworks / particles: longer settle + more frames so bursts appear
        if sparse:
            n_frames = max(n_frames, 72)
            settle_s = max(settle_s, 1.0)

        time.sleep(settle_s)
        # Must actually be running the requested effect before we port pixels
        cur = panel_fx(ip)
        if cur is not None and cur != fx:
            apply_wled(ip, fx, speed)
            time.sleep(0.35)
            cur = panel_fx(ip)
        if cur is not None and cur != fx:
            _capture_status = "error"
            _capture_error = f"panel fx={cur} != requested {fx}; not caching"
            return []

        frames = peek_frames(ip, count=n_frames, timeout_each=1.2)

        # Sparse FX sometimes stream near-empty frames between bursts — keep
        # sampling a bit longer if we barely saw any lit pixels.
        if sparse and frames:
            def _nz(fr):
                return sum(1 for i in range(0, len(fr), 3) if fr[i] | fr[i + 1] | fr[i + 2])
            peak = max(_nz(f) for f in frames)
            if peak < 8:
                extra = peek_frames(ip, count=48, timeout_each=1.2)
                frames = frames + extra
                peak = max((_nz(f) for f in frames), default=0)
            # Still dead (capture raced DDP / empty bursts): don't cache garbage
            if peak < 3:
                _capture_status = "error"
                _capture_error = f"sparse FX peak_nz={peak}; black underlay until retry"
                return []

        if len(frames) < 3:
            _capture_status = "error"
            _capture_error = f"only got {len(frames)} peek frames"
            return frames

        under, busy = soften_if_busy(frames)
        ent = {
            "speed": sk,
            "frames": under,
            "at": time.time(),
            "name": effect_name(fx),
            "sparse": sparse,
            "busy": round(busy, 1),
            "source": "capture",
        }
        with _loop_lock:
            _loop_cache[fx] = ent
        _save_loop_to_disk(fx, ent)
        _capture_status = "ok"
        return frames
    except Exception as e:
        _capture_status = "error"
        _capture_error = str(e)
        return []
    finally:
        _capturing = False
        _capture_lock.release()


def ensure_capture_async(panel_ip, fx, speed=50, on_done=None):
    """Kick off capture in a daemon thread if cache miss."""
    if fx is None or int(fx) < 0:
        return False
    fx = int(fx)
    if get_cached_loop(fx, speed) is not None:
        return False
    if _capturing:
        return False

    def _job():
        capture_effect_loop(panel_ip, fx, speed)
        if on_done:
            try:
                on_done()
            except Exception:
                pass

    threading.Thread(target=_job, daemon=True, name=f"capture-fx-{fx}").start()
    return True


def invalidate_cache(fx=None, disk=True):
    """Drop cached loops.

    `disk=True` means the recording is genuinely stale (the effect or its speed
    bucket changed) so the persisted copy must go too, or the next restart
    would resurrect exactly what we just rejected. `disk=False` is a pure
    memory eviction — used when merely *switching away* from an effect, where
    the recording is still perfectly valid and we want to be able to rehydrate
    it instantly instead of re-capturing.
    """
    with _loop_lock:
        if fx is None:
            _loop_cache.clear()
            _building.clear()
        else:
            _loop_cache.pop(int(fx), None)
            _building.pop(int(fx), None)
    if not disk:
        return
    try:
        if not LOOP_DIR.is_dir():
            return
        pats = "fx*.loop" if fx is None else f"fx{int(fx)}-s*.loop"
        for p in LOOP_DIR.glob(pats):
            p.unlink(missing_ok=True)
    except Exception:
        pass


# ── Public frame() used by arcade ──────────────────────────────────────────

def frame(mode_id, t=None, speed=50, prefer_live=True):
    """
    Background RGB for compositing / UI.

    ONLY real Apollo pixels: live peek and/or captured loop.
    Never synthesizes Fire/ICU/etc. Missing data → pure black.
    """
    if t is None:
        t = time.time()
    speed = max(1, min(100, int(speed)))
    fx = parse_fx(mode_id)
    if fx is None:
        return bytes(N)

    if prefer_live:
        live = live_frame(max_age=2.0)
        if live is not None:
            return live

    looped = loop_frame(fx, t, speed)
    if looped is not None:
        return looped

    # No fake stand-in. Black until a real capture/peek exists.
    return bytes(N)


def composite(bg, fg, threshold=14, bg_gain=0.42):
    if bg is None or len(bg) != N:
        return fg
    if fg is None or len(fg) != N:
        return bg
    g = 0.0 if bg_gain < 0 else 1.0 if bg_gain > 1 else float(bg_gain)
    out = bytearray(N)
    for i in range(0, N, 3):
        r, gch, b = fg[i], fg[i + 1], fg[i + 2]
        if r <= threshold and gch <= threshold and b <= threshold:
            out[i] = int(bg[i] * g)
            out[i + 1] = int(bg[i + 1] * g)
            out[i + 2] = int(bg[i + 2] * g)
        else:
            out[i] = min(255, r + (r >> 3))
            out[i + 1] = min(255, gch + (gch >> 3))
            out[i + 2] = min(255, b + (b >> 3))
    return bytes(out)


# ── Software fallback (name-exact, Yves-aware) ─────────────────────────────

def _software_frame(fx: int, t: float, speed: int) -> bytes:
    name = effect_name(fx)
    key = _clean_name(name)
    rate = 0.10 + (speed / 100.0) * 1.7
    tt = t * rate

    if key.startswith("rsvd"):
        return _pat_noise(tt, fx, gain=0.12)

    # --- Exact Yves 2D modes (ids 228–254 on this firmware) ---
    yves_exact = {
        "polar_waves": _yves_polar_waves,
        "yves": _yves_yves,
        "waves": _yves_waves,
        "hot_blob": _yves_hot_blob,
        "rotating_blob": _yves_rotating_blob,
        "rgb_blobs": _yves_rgb_blobs,
        "rgb_blobs2": lambda tt, fx: _yves_rgb_blobs(tt, fx, n=2),
        "rgb_blobs3": lambda tt, fx: _yves_rgb_blobs(tt, fx, n=3),
        "rgb_blobs4": lambda tt, fx: _yves_rgb_blobs(tt, fx, n=4),
        "rgb_blobs5": lambda tt, fx: _yves_rgb_blobs(tt, fx, n=5),
        "spiralus": _yves_spiralus,
        "spiralus2": lambda tt, fx: _yves_spiralus(tt, fx, arms=5),
        "chasing_spirals": _yves_chasing_spirals,
        "big_caleido": _yves_caleido,
        "caleido1": lambda tt, fx: _yves_caleido(tt, fx, slices=6),
        "caleido2": lambda tt, fx: _yves_caleido(tt, fx, slices=8),
        "caleido3": lambda tt, fx: _yves_caleido(tt, fx, slices=12),
        "slow_fade": _yves_slow_fade,
        "zoom": _yves_zoom,
        "lava1": _yves_lava,
        "scaledemo1": _yves_zoom,
        "distance_experiment": _yves_distance,
        "center_field": _yves_center_field,
        "sm1": lambda tt, fx: _yves_sm(tt, fx, tier=1),
        "sm2": lambda tt, fx: _yves_sm(tt, fx, tier=2),
        "sm3": lambda tt, fx: _yves_sm(tt, fx, tier=3),
        "sm4": lambda tt, fx: _yves_sm(tt, fx, tier=4),
    }
    if key in yves_exact:
        return yves_exact[key](tt, fx)
    # Yves family prefix leftovers
    if key.startswith("sm") and key[2:].isdigit():
        return _yves_sm(tt, fx, tier=int(key[2:]))
    if "caleido" in key:
        return _yves_caleido(tt, fx)
    if "polar_wave" in key or key == "polar_waves":
        return _yves_polar_waves(tt, fx)
    if key.startswith("rgb_blob"):
        return _yves_rgb_blobs(tt, fx)

    # --- Core WLED effects by cleaned name (substring only when unambiguous) ---
    if key in ("fire_2012", "fire_flicker", "ps_fire", "ps_fire_1d") or (
        "fire" in key and "firework" not in key and "noisefire" not in key
    ):
        return _pat_fire(tt, fx)
    if key in ("matrix", "matripix") or key.endswith("_matrix") or key == "freqmatrix":
        return _pat_matrix(tt, fx)
    if key == "rain" or key == "snow_fall":
        return _pat_rain(tt, fx, snow=("snow" in key))
    if "rainbow" in key or key in ("pride_2015", "colorloop", "colorwaves"):
        return _pat_rainbow(tt, fx)
    if key in ("pacifica", "lake") or key.startswith("ripple"):
        return _pat_ripple(tt, fx)
    if "plasma" in key or key in ("soap", "hiphotic"):
        return _pat_plasma(tt, fx)
    if any(k in key for k in ("twinkle", "sparkle", "glitter", "fairy")):
        return _pat_twinkle(tt, fx)
    if "strobe" in key or key in ("blink",) or "lightning" in key:
        return _pat_strobe(tt, fx)
    if key in ("breathe", "fade", "solid", "solid_pattern", "solid_pattern_tri"):
        return _pat_solid(tt, fx)
    if "aurora" in key or "polar_light" in key:
        return _pat_aurora(tt, fx)
    if key in ("game_of_life", "tetrix"):
        return _pat_life(tt, fx)
    if any(k in key for k in ("dna", "julia", "lissajous", "spiral")):
        return _pat_spiral(tt, fx)
    if any(k in key for k in ("chase", "scan", "wipe", "theater", "running", "sweep")):
        return _pat_chase(tt, fx)
    if "tv" in key or key in ("dynamic", "dynamic_smooth"):
        return _pat_tv(tt, fx)
    # Fireworks family — NEVER twinkle (that was the visible bug)
    if "firework" in key or "starburst" in key:
        style = "1d" if "1d" in key else ("starburst" if "starburst" in key else "classic")
        return _pat_fireworks(tt, fx, style=style)
    if "popcorn" in key:
        return _pat_fireworks(tt, fx, style="popcorn")
    if "candle" in key or "volcano" in key or key == "lava1":
        return _pat_fire(tt, fx)
    if "noise" in key or "perlin" in key:
        return _pat_noise(tt, fx, gain=0.45)
    if "blob" in key or "black_hole" in key or "akemi" in key:
        return _yves_rgb_blobs(tt, fx, n=3)
    if "wave" in key:
        return _yves_waves(tt, fx)

    # Deterministic non-fire fallback from fx id (bands/plasma/ripple/noise only)
    gens = (_pat_bands, _pat_diagonal, _pat_plasma, _pat_ripple,
            _pat_noise, _pat_twinkle, _pat_chase, _pat_spiral,
            _pat_checker, _pat_radial, _pat_aurora, _pat_party)
    return gens[fx % len(gens)](tt, fx)


# ── Yves-style generators (visual character match; capture overrides) ──────

def _hsv(h, s, v):
    h = h % 1.0
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    if i == 0: r, g, b = v, t, p
    elif i == 1: r, g, b = q, v, p
    elif i == 2: r, g, b = p, v, t
    elif i == 3: r, g, b = p, q, v
    elif i == 4: r, g, b = t, p, v
    else: r, g, b = v, p, q
    return (int(r * 255), int(g * 255), int(b * 255))


def _put(buf, x, y, c):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        i = (y * WIDTH + x) * 3
        buf[i], buf[i + 1], buf[i + 2] = c


def _dim(c, k):
    k = 0 if k < 0 else 1 if k > 1 else k
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def _yves_polar_waves(tt, fx):
    """Cool cyan/white radial standing waves — NOT fire."""
    buf = bytearray(N)
    cx = cy = 31.5
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx, dy = x - cx, y - cy
            r = math.hypot(dx, dy)
            ang = math.atan2(dy, dx)
            # multi-frequency polar interference
            v = (
                math.sin(r * 0.45 - tt * 2.4)
                + 0.55 * math.sin(ang * 3.0 + tt * 1.1)
                + 0.35 * math.sin(r * 0.18 + ang * 2 - tt * 0.7)
            )
            bri = max(0.0, v * 0.42 + 0.15)
            # cool palette: cyan → white hot center lobes
            if bri < 0.04:
                continue
            cool = min(1.0, bri * 1.4)
            r8 = int(40 + 180 * cool * cool)
            g8 = int(80 + 170 * cool)
            b8 = int(120 + 135 * cool)
            if bri > 0.75:
                u = (bri - 0.75) / 0.25
                r8 = int(r8 + (255 - r8) * u)
                g8 = int(g8 + (255 - g8) * u)
                b8 = int(b8 + (255 - b8) * u)
            _put(buf, x, y, (min(255, r8), min(255, g8), min(255, b8)))
    return bytes(buf)


def _yves_yves(tt, fx):
    """Warm orange/red Yves signature field (sampled character from panel)."""
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            n = (
                math.sin(x * 0.12 + tt * 0.9)
                * math.cos(y * 0.10 - tt * 0.7)
                + 0.5 * math.sin((x + y) * 0.08 + tt)
            )
            bri = max(0.0, 0.35 + 0.55 * n)
            if bri < 0.12:
                continue
            # orange-red, no blue (matches live peek)
            r8 = int(80 + 140 * bri)
            g8 = int(20 + 90 * bri * bri)
            b8 = 0
            _put(buf, x, y, (min(255, r8), min(255, g8), b8))
    return bytes(buf)


def _yves_waves(tt, fx):
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = math.sin(x * 0.22 + tt * 1.8) * math.sin(y * 0.18 - tt * 1.3)
            bri = max(0.0, v)
            if bri < 0.05:
                continue
            # magenta/red wave (matches live peek of Y Waves)
            r8 = int(200 * bri)
            g8 = 0
            b8 = int(180 * bri + 40 * math.sin(tt + x * 0.1))
            _put(buf, x, y, (min(255, r8), g8, min(255, max(0, b8))))
    return bytes(buf)


def _yves_hot_blob(tt, fx):
    buf = bytearray(N)
    cx = 32 + 6 * math.sin(tt * 0.6)
    cy = 32 + 5 * math.cos(tt * 0.5)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - cx, y - cy)
            bri = max(0.0, 1.0 - d / (14 + 4 * math.sin(tt)))
            bri = bri ** 1.6
            if bri < 0.05:
                continue
            # yellow-hot center
            r8 = int(255 * bri)
            g8 = int(240 * bri * bri)
            b8 = int(40 * max(0, bri - 0.7))
            _put(buf, x, y, (r8, g8, b8))
    return bytes(buf)


def _yves_rotating_blob(tt, fx):
    buf = bytearray(N)
    ang = tt * 1.2
    cx = 32 + 12 * math.cos(ang)
    cy = 32 + 12 * math.sin(ang)
    return _blob_at(buf, cx, cy, tt, hue=0.08)


def _blob_at(buf, cx, cy, tt, hue=0.55):
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - cx, y - cy)
            bri = max(0.0, 1.0 - d / 16.0) ** 1.4
            if bri > 0.04:
                _put(buf, x, y, _dim(_hsv(hue + tt * 0.02, 0.85, 1), bri))
    return bytes(buf)


def _yves_rgb_blobs(tt, fx, n=3):
    buf = bytearray(N)
    hues = [0.0, 0.33, 0.66, 0.16, 0.5]
    n = max(1, min(5, n))
    acc = [[0.0, 0.0, 0.0] for _ in range(WIDTH * HEIGHT)]
    for i in range(n):
        cx = 32 + 18 * math.sin(tt * (0.5 + i * 0.17) + i * 2.1)
        cy = 32 + 16 * math.cos(tt * (0.4 + i * 0.13) + i * 1.7)
        h = hues[i % len(hues)]
        cr, cg, cb = _hsv(h, 0.9, 1)
        for y in range(HEIGHT):
            for x in range(WIDTH):
                d = math.hypot(x - cx, y - cy)
                bri = max(0.0, 1.0 - d / (12.0 + i)) ** 1.5
                j = y * WIDTH + x
                acc[j][0] += cr * bri
                acc[j][1] += cg * bri
                acc[j][2] += cb * bri
    for j, (r, g, b) in enumerate(acc):
        if r + g + b < 1:
            continue
        i = j * 3
        buf[i] = min(255, int(r))
        buf[i + 1] = min(255, int(g))
        buf[i + 2] = min(255, int(b))
    return bytes(buf)


def _yves_spiralus(tt, fx, arms=3):
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx, dy = x - 32, y - 32
            ang = math.atan2(dy, dx)
            r = math.hypot(dx, dy)
            v = 0.5 + 0.5 * math.sin(ang * arms + r * 0.22 - tt * 2.0)
            fade = max(0, 1 - r / 40)
            if v * fade < 0.35:
                continue
            h = (ang / (2 * math.pi) + tt * 0.05 + fx * 0.01) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), (v * fade - 0.2) * 1.1))
    return bytes(buf)


def _yves_chasing_spirals(tt, fx):
    return _yves_spiralus(tt * 1.4, fx, arms=4)


def _yves_caleido(tt, fx, slices=8):
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx, dy = x - 31.5, y - 31.5
            ang = math.atan2(dy, dx)
            r = math.hypot(dx, dy)
            # fold angle into kaleidoscope wedge
            wedge = (2 * math.pi) / slices
            a = abs((ang + tt * 0.3) % wedge - wedge / 2)
            v = 0.5 + 0.5 * math.sin(a * 8 + r * 0.35 - tt)
            h = (a / wedge + r * 0.01 + tt * 0.04) % 1.0
            fade = max(0, 1 - r / 38)
            if v * fade > 0.3:
                _put(buf, x, y, _dim(_hsv(h, 0.9, 1), (v * fade) * 0.7))
    return bytes(buf)


def _yves_slow_fade(tt, fx):
    h = (tt * 0.04 + fx * 0.01) % 1.0
    c = _dim(_hsv(h, 0.55, 1), 0.45 + 0.2 * math.sin(tt * 0.5))
    out = bytearray(N)
    r, g, b = c
    for i in range(0, N, 3):
        out[i], out[i + 1], out[i + 2] = r, g, b
    return bytes(out)


def _yves_zoom(tt, fx):
    buf = bytearray(N)
    z = 0.5 + 0.5 * math.sin(tt * 0.4)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx, dy = (x - 32) * (0.7 + z), (y - 32) * (0.7 + z)
            r = math.hypot(dx, dy)
            v = 0.5 + 0.5 * math.sin(r * 0.3 - tt * 2)
            h = (r * 0.02 + tt * 0.06) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), max(0, v * 0.5)))
    return bytes(buf)


def _yves_lava(tt, fx):
    # true lava — fire-like OK
    return _pat_fire(tt, fx)


def _yves_distance(tt, fx):
    buf = bytearray(N)
    cx = 32 + 10 * math.sin(tt * 0.5)
    cy = 32 + 10 * math.cos(tt * 0.4)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - cx, y - cy)
            v = abs(math.sin(d * 0.4 - tt * 1.5))
            _put(buf, x, y, _dim(_hsv((d * 0.02 + tt * 0.03) % 1, 0.8, 1), v * 0.55))
    return bytes(buf)


def _yves_center_field(tt, fx):
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - 32, y - 32)
            v = max(0, 1 - d / 30) * (0.6 + 0.4 * math.sin(tt * 2 + d * 0.2))
            _put(buf, x, y, _dim(_hsv(0.55 + tt * 0.02, 0.7, 1), v * 0.7))
    return bytes(buf)


def _yves_sm(tt, fx, tier=1):
    """SM1–SM4 scale demos — denser / faster with tier."""
    buf = bytearray(N)
    scale = 0.08 + tier * 0.05
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = math.sin(x * scale + tt * tier) * math.cos(y * scale * 1.1 - tt)
            h = (0.1 * tier + v * 0.1 + tt * 0.03) % 1.0
            bri = 0.3 + 0.4 * abs(v)
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), bri))
    return bytes(buf)


# ── Generic patterns ───────────────────────────────────────────────────────

def _pat_solid(tt, fx):
    k = 0.30 + 0.50 * (0.5 + 0.5 * math.sin(tt * 2.2 + fx))
    c = _dim(_hsv((fx * 0.07) % 1, 0.35, 1), k * 0.7)
    out = bytearray(N)
    r, g, b = c
    for i in range(0, N, 3):
        out[i], out[i + 1], out[i + 2] = r, g, b
    return bytes(out)


def _pat_bands(tt, fx):
    buf = bytearray(N)
    h0 = (fx * 0.618) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            u = x * 0.12 + tt * 0.8
            h = (h0 + u * 0.15) % 1.0
            v = 0.35 + 0.45 * (0.5 + 0.5 * math.sin(u * 3))
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), v))
    return bytes(buf)


def _pat_diagonal(tt, fx):
    buf = bytearray(N)
    h0 = (fx * 0.618) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            u = (x + y) * 0.15 + tt
            h = (h0 + (x - y) * 0.01 + tt * 0.05) % 1.0
            v = 0.3 + 0.5 * abs(math.sin(u * 2.5))
            _put(buf, x, y, _dim(_hsv(h, 0.9, 1), v))
    return bytes(buf)


def _pat_plasma(tt, fx):
    buf = bytearray(N)
    h0 = (fx * 0.618) % 1.0
    s = 0.12
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = (math.sin(x * s + tt) + math.sin(y * s * 1.1 - tt * 0.7)
                 + math.sin((x + y) * s * 0.7 + tt * 0.5)
                 + math.sin(math.hypot(x - 32, y - 32) * s * 0.9 - tt))
            h = (v * 0.08 + h0 + tt * 0.03) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), 0.48))
    return bytes(buf)


def _pat_fire(tt, fx):
    buf = bytearray(N)
    heat = [0.0] * (WIDTH * HEIGHT)
    for x in range(WIDTH):
        base = 0.35 + 0.65 * abs(math.sin(x * 0.28 + tt * 3 + fx))
        heat[(HEIGHT - 1) * WIDTH + x] = base
        heat[(HEIGHT - 2) * WIDTH + x] = base * 0.88
    cool = 0.025
    for y in range(HEIGHT - 3, -1, -1):
        for x in range(WIDTH):
            a = heat[(y + 1) * WIDTH + x]
            b = heat[(y + 1) * WIDTH + max(0, x - 1)]
            c = heat[(y + 1) * WIDTH + min(WIDTH - 1, x + 1)]
            n = abs(math.sin(x * 12.9 + y * 78.2 + tt * 9 + fx))
            heat[y * WIDTH + x] = max(0.0, (a + b + c) / 3.15 - cool - n * 0.02)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            h = heat[y * WIDTH + x]
            if h < 0.08:
                col = (0, 0, 0)
            else:
                t = min(1.0, h)
                if t < 0.45:
                    col = (int(40 + 200 * t / 0.45), int(20 * t / 0.45), 0)
                elif t < 0.75:
                    u = (t - 0.45) / 0.3
                    col = (255, int(40 + 140 * u), int(10 + 30 * u))
                else:
                    u = (t - 0.75) / 0.25
                    col = (255, int(180 + 50 * u), int(40 + 160 * u))
            _put(buf, x, y, _dim(col, 0.65))
    return bytes(buf)


def _pat_matrix(tt, fx):
    buf = bytearray(N)
    for x in range(0, WIDTH, 1 + fx % 2):
        head = int((tt * (8 + (x + fx) % 9) + x * 11) * 3.2) % (HEIGHT + 16)
        length = 8 + fx % 8
        for trail in range(length):
            y = head - trail
            if 0 <= y < HEIGHT:
                g = max(0, 240 - trail * 18)
                if trail == 0:
                    c = (200, 255, 200)
                else:
                    c = (0, g, 40)
                _put(buf, x, y, c)
    return bytes(buf)


def _pat_ripple(tt, fx):
    buf = bytearray(N)
    cx = 32 + 10 * math.sin(tt * 0.35)
    cy = 32 + 8 * math.cos(tt * 0.28)
    h0 = (fx * 0.07) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - cx, y - cy)
            v = 0.5 + 0.5 * math.sin(d * 0.35 - tt * 3.2)
            h = (h0 + d * 0.012 + tt * 0.02) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.8, 1), v * 0.5))
    return bytes(buf)


def _pat_noise(tt, fx, gain=0.45):
    buf = bytearray(N)
    h0 = (fx * 0.618) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            n = abs(math.sin(x * 0.22 + tt + fx) * math.cos(y * 0.2 - tt * 0.8)
                    + 0.4 * math.sin((x + y) * 0.09 + tt * 1.1))
            h = (h0 + n * 0.25) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.75, 0.95), gain * (0.5 + 0.5 * n)))
    return bytes(buf)


def _pat_twinkle(tt, fx):
    buf = bytearray(N)
    h0 = (fx * 0.618) % 1.0
    n_stars = 40 + fx % 30
    for i in range(n_stars):
        seed = (i * 1103515245 + fx * 12345) & 0x7FFFFFFF
        x = seed % WIDTH
        y = (seed // WIDTH) % HEIGHT
        phase = math.sin(tt * (2.5 + (i % 5) * 0.4) + i * 1.7 + fx)
        if phase > 0.1:
            bri = 0.25 + 0.75 * phase
            _put(buf, x, y, _dim(_hsv(h0 + i * 0.03, 0.45, 1), bri))
    return bytes(buf)


def _pat_fireworks(tt, fx, style="classic"):
    """
    Real fireworks character: dark sky, rising shells, radial bursts with gravity.
    Deterministic from time so every frame is coherent without stored state.
    style: classic | starburst | 1d | popcorn
    """
    buf = bytearray(N)
    # Party-like hues
    hues = (0.0, 0.08, 0.14, 0.33, 0.55, 0.72, 0.85, 0.95)

    def _hash(n):
        n = (n ^ fx * 374761393) & 0xFFFFFFFF
        n = (n * 1103515245 + 12345) & 0x7FFFFFFF
        return n

    if style == "1d":
        # Horizontal strip bursts (WLED Fireworks 1D mapped onto rows)
        for burst in range(10):
            seed = _hash(int(tt * 2.2) - burst * 17 + burst * 97)
            age = (tt * 2.2 - burst * 0.55 - (seed % 100) * 0.01) % 6.0
            if age > 2.8:
                continue
            cx = seed % WIDTH
            cy = 8 + (seed // WIDTH) % (HEIGHT - 16)
            h = hues[seed % len(hues)]
            n_sparks = 14 + seed % 10
            for s in range(n_sparks):
                ss = _hash(seed + s * 31)
                ang = (s / n_sparks) * 2 * math.pi + (ss % 100) * 0.01
                spd = 0.6 + (ss % 50) * 0.03
                dist = age * spd * 10
                fade = max(0.0, 1.0 - age / 2.6)
                x = int(cx + math.cos(ang) * dist)
                y = int(cy + math.sin(ang) * dist * 0.35 + age * age * 2.2)
                if fade > 0.05:
                    _put(buf, x, y, _dim(_hsv(h + s * 0.01, 0.85, 1), fade))
                    if fade > 0.4:
                        _put(buf, x, y - 1, _dim(_hsv(h, 0.5, 1), fade * 0.4))
        return bytes(buf)

    # classic / starburst / popcorn
    launch_rate = 1.35 if style != "popcorn" else 2.4
    n_shells = 7 if style != "popcorn" else 12
    for shell in range(n_shells):
        # Each shell's launch epoch
        epoch = int(tt * launch_rate) - shell
        seed = _hash(epoch * 131 + shell * 9176)
        t0 = epoch / launch_rate + (seed % 20) * 0.01
        age = tt - t0
        if age < 0 or age > 3.4:
            continue

        x0 = 8 + seed % (WIDTH - 16)
        # apex height
        apex = 12 + (seed // 7) % 28
        rise_t = 0.55 + (seed % 10) * 0.03
        h = hues[seed % len(hues)]

        if age < rise_t and style != "popcorn":
            # Rising shell (white-ish core)
            u = age / rise_t
            y = int(HEIGHT - 1 - u * (HEIGHT - 1 - apex))
            trail = 3 + seed % 3
            for k in range(trail):
                yy = y + k
                fade = max(0.0, 1.0 - k / trail) * (0.5 + 0.5 * u)
                _put(buf, x0, yy, _dim((255, 220, 160), fade * 0.85))
            continue

        # Burst
        bang_age = age if style == "popcorn" else (age - rise_t)
        if bang_age < 0:
            continue
        life = 1.6 if style == "starburst" else (1.1 if style == "popcorn" else 2.0)
        if bang_age > life:
            continue
        fade = max(0.0, 1.0 - bang_age / life)
        cy = apex if style != "popcorn" else (10 + seed % (HEIGHT - 14))
        cx = x0 if style != "popcorn" else (6 + seed % (WIDTH - 12))

        if style == "starburst":
            n_sparks = 28 + seed % 12
            gravity = 3.5
            speed_mul = 18.0
        elif style == "popcorn":
            n_sparks = 6 + seed % 5
            gravity = 6.0
            speed_mul = 8.0
        else:
            n_sparks = 18 + seed % 14
            gravity = 4.5
            speed_mul = 14.0

        for s in range(n_sparks):
            ss = _hash(seed + s * 57 + 3)
            ang = (s / max(1, n_sparks)) * 2 * math.pi + (ss % 64) * 0.02
            # slight speed variance
            spd = (0.55 + (ss % 40) * 0.012) * speed_mul
            # secondary rings for starburst
            ring = 1.0 + (0.55 if (style == "starburst" and s % 3 == 0) else 0.0)
            dist = bang_age * spd * ring
            x = cx + math.cos(ang) * dist
            y = cy + math.sin(ang) * dist * 0.92 + 0.5 * gravity * bang_age * bang_age
            xi, yi = int(x), int(y)
            bri = fade * (0.55 + 0.45 * ((ss % 10) / 10))
            if bri < 0.06:
                continue
            # colour shifts cooler as spark dies
            hh = (h + s * 0.01 + bang_age * 0.04) % 1.0
            col = _hsv(hh, 0.75 + 0.2 * fade, 1)
            _put(buf, xi, yi, _dim(col, bri))
            # short trail
            if bri > 0.35 and style != "popcorn":
                tx = int(cx + math.cos(ang) * max(0, dist - 1.6))
                ty = int(cy + math.sin(ang) * max(0, dist - 1.6) * 0.92
                         + 0.5 * gravity * bang_age * bang_age)
                _put(buf, tx, ty, _dim(col, bri * 0.35))
            # bright core flash at detonation
            if bang_age < 0.12 and s == 0:
                for dx in range(-1, 2):
                    for dy in range(-1, 2):
                        _put(buf, cx + dx, cy + dy, _dim((255, 255, 240), fade))
    return bytes(buf)


def _pat_chase(tt, fx):
    buf = bytearray(N)
    head = int(tt * (25 + fx % 20)) % (WIDTH + HEIGHT + 20)
    width = 4 + fx % 10
    h0 = (fx * 0.618) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = (x + y - head) % (20 + width)
            bri = max(0.0, 1.0 - d / width)
            if bri > 0:
                h = (h0 + x * 0.01 + tt * 0.04) % 1.0
                _put(buf, x, y, _dim(_hsv(h, 0.9, 1), bri * 0.55))
    return bytes(buf)


def _pat_spiral(tt, fx):
    buf = bytearray(N)
    arms = 2 + fx % 5
    h0 = (fx * 0.618) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx, dy = x - 32, y - 32
            ang = math.atan2(dy, dx)
            r = math.hypot(dx, dy)
            v = 0.5 + 0.5 * math.sin(ang * arms + r * 0.2 - tt * 2.2)
            h = (h0 + ang / (2 * math.pi) + tt * 0.04) % 1.0
            fade = max(0, 1 - r / 40)
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), v * 0.5 * fade))
    return bytes(buf)


def _pat_checker(tt, fx):
    buf = bytearray(N)
    cell = 2 + fx % 6
    h0 = (fx * 0.618) % 1.0
    h1 = (h0 + 0.35) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            bit = ((x // cell) + (y // cell) + int(tt * 2)) & 1
            h = h0 if bit else h1
            _put(buf, x, y, _dim(_hsv(h, 0.85, 1), 0.35 + 0.2 * bit))
    return bytes(buf)


def _pat_rain(tt, fx, snow=False):
    buf = bytearray(N)
    drops = 50 + fx % 40
    for i in range(drops):
        seed = (i * 7919 + fx * 104729) & 0x7FFFFFFF
        x = seed % WIDTH
        spd = 12 + (seed % 15)
        y = (seed // 64 + int(tt * spd)) % HEIGHT
        if snow:
            c = (200, 200, 220)
            _put(buf, x, y, _dim(c, 0.7))
        else:
            c = (80, 120, 255)
            _put(buf, x, y, _dim(c, 0.55))
            _put(buf, x, (y + 1) % HEIGHT, _dim(c, 0.25))
    return bytes(buf)


def _pat_life(tt, fx):
    buf = bytearray(N)
    rng = random.Random(fx * 99991 + int(tt * 2))
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if rng.random() < 0.28:
                _put(buf, x, y, _dim(_hsv(0.35, 0.85, 1), 0.55))
    return bytes(buf)


def _pat_radial(tt, fx):
    buf = bytearray(N)
    h0 = (fx * 0.618) % 1.0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d = math.hypot(x - 32, y - 32)
            v = 0.5 + 0.5 * math.sin(d * 0.3 - tt * 2)
            _put(buf, x, y, _dim(_hsv(h0 + d * 0.01, 0.85, 1), v * 0.5))
    return bytes(buf)


def _pat_party(tt, fx):
    buf = bytearray(N)
    for y in range(0, HEIGHT, 2):
        for x in range(0, WIDTH, 2):
            h = (x * 0.03 + y * 0.02 + tt * 0.2 + fx * 0.01) % 1.0
            c = _dim(_hsv(h, 0.95, 1), 0.55)
            for dy in range(2):
                for dx in range(2):
                    _put(buf, x + dx, y + dy, c)
    return bytes(buf)


def _pat_aurora(tt, fx):
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = 0.5 + 0.5 * math.sin(x * 0.08 + tt + math.sin(y * 0.12 + tt * 0.5))
            band = math.exp(-((y - 20 - 8 * math.sin(tt * 0.3 + x * 0.05)) ** 2) / 120)
            h = (0.35 + 0.15 * math.sin(x * 0.05 + tt * 0.2)) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.7, 1), v * band * 0.7))
    return bytes(buf)


def _pat_strobe(tt, fx):
    on = (int(tt * 8) % 2) == 0
    if not on:
        return bytes(N)
    c = _hsv((fx * 0.07) % 1, 0.2, 1)
    out = bytearray(N)
    r, g, b = _dim(c, 0.7)
    for i in range(0, N, 3):
        out[i], out[i + 1], out[i + 2] = r, g, b
    return bytes(out)


def _pat_rainbow(tt, fx):
    buf = bytearray(N)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            h = (x / WIDTH + tt * 0.1 + y * 0.002) % 1.0
            _put(buf, x, y, _dim(_hsv(h, 0.95, 1), 0.55))
    return bytes(buf)


def _pat_tv(tt, fx):
    buf = bytearray(N)
    rng = random.Random(int(tt * 20) + fx)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = rng.random()
            g = int(v * 180)
            _put(buf, x, y, (g, g, g))
    return bytes(buf)
