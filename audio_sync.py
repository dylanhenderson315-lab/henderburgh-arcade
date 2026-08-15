"""
audio_sync.py -- panel mic, two real paths, never a fake waveform.

The M-1 Rev 6 I2S mic is analyzed on-device by WLED-MM AudioReactive
(volume + 16-band FFT). Two ways that analysis can reach this Mac:

  1. UDP Sound Sync V2 multicast to 239.0.0.1:11988 (16-band FFT).
     This is what the firmware already has set to "send mode v2+".
  2. HTTP /json/info -- the same numbers the WLED Info page shows:
     Audio Source (I2S + peak %), Sound Processing, UDP Sound Sync.
     No FFT on this path. Peak percent is a real on-device reading.

Confirmed live on this house: the mic is working (peak moves 30-50%+
with room sound) and sync is send/v2+, but multicast packets never
arrive on the Mac (panel is Wi-Fi, Mac is Ethernet on the same subnet).
That is a real network/AP multicast hole, not a dead mic. Path 2 is
the honest replica so the glass is not stuck on NO AUDIO SIGNAL while
the panel itself can hear.

Never invent a 16-band FFT from a single peak. Missing FFT stays
missing. Stale only when BOTH paths are dead.
"""
import json
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

MCAST_GROUP = "239.0.0.1"
MCAST_PORT = 11988
HEADER = b"00002"

_PACKET_FMT = "<6s2x2f2B16s2x2f"
_PACKET_SIZE = struct.calcsize(_PACKET_FMT)   # 44

STALE_AFTER = 1.0
HTTP_STALE_AFTER = 3.0
HTTP_REFRESH = 0.20
IDLE_STOP = 120.0

_PEAK_RE = re.compile(r"peak\s+(\d+)\s*%", re.I)


def _panel_ip():
    try:
        from wled_ddp import DEFAULT_IP
        return DEFAULT_IP
    except Exception:
        return "192.168.40.24"


def _local_ipv4s():
    """IPv4s we can join multicast on -- include the iface that
    actually routes to the panel, not just INADDR_ANY (macOS often
    joins the VPN/utun face first and never hears LAN multicast)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((_panel_ip(), 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(None, None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def _parse_info_audio(info):
    """Real fields from WLED /json/info `u`, or Nones. Zero invention."""
    u = (info or {}).get("u") or {}
    if not isinstance(u, dict):
        return None

    def first_list(key):
        v = u.get(key)
        if isinstance(v, list):
            return [str(x) for x in v if x is not None]
        return []

    src_parts = first_list("Audio Source")
    peak_pct = None
    for part in src_parts:
        m = _PEAK_RE.search(part)
        if m:
            peak_pct = int(m.group(1))
            break
    source = None
    for part in src_parts:
        t = part.strip(" -")
        if t and "peak" not in t.lower():
            source = t
            break
    proc = (first_list("Sound Processing") or [""])[0].strip().lower()
    sync = " ".join(first_list("UDP Sound Sync")).strip()
    return {
        "peak_pct": peak_pct,
        "source": source,
        "processing": proc,
        "sync": sync,
    }


class AudioSyncFeed:
    """UDP listener + HTTP replica. get() never blocks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sample_raw = 0.0
        self._sample_smth = 0.0
        self._peak = False
        self._fft = None          # list of 16 ints, or None (HTTP path)
        self._fft_magnitude = 0.0
        self._fft_major_peak = 0.0
        self._updated = 0.0       # last UDP packet
        self._http_updated = 0.0
        self._peak_pct = None
        self._source_name = None
        self._processing = None
        self._sync = None
        self._path = None         # "udp" | "http" | None
        self._last_read = 0.0
        self._thread = None
        self._http_thread = None
        self._sock = None
        self._err = None

    def get(self):
        now = time.time()
        with self._lock:
            self._last_read = now
            sample_raw, sample_smth = self._sample_raw, self._sample_smth
            peak = self._peak
            fft = list(self._fft) if self._fft is not None else None
            fft_magnitude, fft_major_peak = self._fft_magnitude, self._fft_major_peak
            updated, http_updated = self._updated, self._http_updated
            peak_pct = self._peak_pct
            source_name, processing, sync = self._source_name, self._processing, self._sync
            err = self._err
        self._ensure_thread()
        udp_age = (now - updated) if updated else None
        http_age = (now - http_updated) if http_updated else None
        udp_live = udp_age is not None and udp_age <= STALE_AFTER
        http_live = (http_age is not None and http_age <= HTTP_STALE_AFTER
                     and peak_pct is not None and processing == "running")
        if udp_live:
            path = "udp"
            stale = False
        elif http_live:
            path = "http"
            stale = False
            sample_smth = peak_pct / 100.0
            sample_raw = sample_smth
            peak = peak_pct >= 80
        else:
            path = None
            stale = True
        return {
            "sample_raw": sample_raw, "sample_smth": sample_smth,
            "peak": peak, "fft": fft,
            "fft_magnitude": fft_magnitude, "fft_major_peak": fft_major_peak,
            "peak_pct": peak_pct, "source_name": source_name,
            "processing": processing, "sync": sync, "path": path,
            "age": udp_age if udp_live else http_age,
            "stale": stale, "err": err,
        }

    def _ensure_thread(self):
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
            if not (self._http_thread and self._http_thread.is_alive()):
                self._http_thread = threading.Thread(target=self._http_loop, daemon=True)
                self._http_thread.start()

    def _open_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", MCAST_PORT))
        # Join on every real LAN face plus INADDR_ANY. One join on ANY
        # is not enough on macOS when a utun/VPN is up.
        faces = _local_ipv4s() + ["0.0.0.0"]
        last_err = None
        joined = 0
        for ip in faces:
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GROUP),
                                   socket.inet_aton(ip))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError as e:
                last_err = e
        if joined == 0 and last_err is not None:
            raise last_err
        sock.settimeout(1.0)
        return sock

    def _loop(self):
        try:
            self._sock = self._open_socket()
        except OSError as e:
            with self._lock:
                self._err = f"{type(e).__name__}: {e}"
            return
        with self._lock:
            self._err = None
        try:
            while True:
                with self._lock:
                    idle = time.time() - self._last_read
                if idle > IDLE_STOP:
                    return
                try:
                    data, _addr = self._sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError as e:
                    with self._lock:
                        self._err = f"{type(e).__name__}: {e}"
                    return
                self._handle_packet(data)
        finally:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _handle_packet(self, data):
        if len(data) != _PACKET_SIZE:
            return
        try:
            header, sample_raw, sample_smth, peak, _reserved2, fft, fft_mag, fft_peak = \
                struct.unpack(_PACKET_FMT, data)
        except struct.error:
            return
        if not header.startswith(HEADER):
            return
        with self._lock:
            self._sample_raw = sample_raw
            self._sample_smth = sample_smth
            self._peak = peak >= 1
            self._fft = list(fft)
            self._fft_magnitude = fft_mag
            self._fft_major_peak = fft_peak
            self._updated = time.time()
            self._path = "udp"
            self._err = None

    def _http_loop(self):
        url = "http://%s/json/info" % _panel_ip()
        while True:
            with self._lock:
                idle = time.time() - self._last_read
            if idle > IDLE_STOP:
                return
            try:
                with urllib.request.urlopen(url, timeout=1.5) as r:
                    info = json.loads(r.read().decode("utf-8", "replace"))
                parsed = _parse_info_audio(info)
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError, UnicodeError, ValueError, TypeError) as e:
                with self._lock:
                    if self._err is None:
                        self._err = f"{type(e).__name__}: {e}"
                time.sleep(HTTP_REFRESH)
                continue
            if parsed:
                with self._lock:
                    self._peak_pct = parsed["peak_pct"]
                    self._source_name = parsed["source"]
                    self._processing = parsed["processing"]
                    self._sync = parsed["sync"]
                    self._http_updated = time.time()
                    if parsed["processing"] == "running":
                        self._err = None
            time.sleep(HTTP_REFRESH)


FEED = AudioSyncFeed()
