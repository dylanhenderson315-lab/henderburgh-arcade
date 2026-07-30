"""
audio_sync.py -- real WLED Audio Sync UDP receiver for the audio-reactive
visualizer. Same shape as market.py/satellite.py/flights.py: all I/O lives
here so the mode that draws it stays pure.

The M-1 panel has a real digital mic (Rev 6 add-on) and WLED-MM's built-in
AudioReactive usermod analyzes it on-device (volume + 16-band FFT). With
sync mode set to "Send" on the panel, it broadcasts that analyzed data as
a UDP multicast "V2" packet to 239.0.0.1:11988 ten times a second. This
module listens for those packets and hands back the real numbers -- it
never captures or fabricates audio itself. If no packets are arriving
(mic off, sync mode wrong, panel offline), that's reported honestly as
staleness, not papered over with a fake waveform.

Packet layout confirmed against the real WLED-MM firmware source
(usermods/audioreactive/audio_reactive.cpp, struct audioSyncPacket,
"new V2 audiosync struct - 44 Bytes", little-endian/packed):

    offset  size  field
    0       6     header, ASCII, must be "00002" + NUL
    6       2     reserved1 (compiler alignment gap)
    8       4     sampleRaw    float
    12      4     sampleSmth   float
    16      1     samplePeak   uint8 (0 = no peak, >=1 = peak detected)
    17      1     reserved2
    18      16    fftResult[16] uint8  -- the 16 GEQ frequency-band levels
    34      2     reserved3 (compiler alignment gap)
    36      4     FFT_Magnitude float
    40      4     FFT_MajorPeak float

struct format: "<6s2x2f2B16s2x2f" -- 44 bytes, matches sizeof(audioSyncPacket).
"""
import socket
import struct
import threading
import time

MCAST_GROUP = "239.0.0.1"
MCAST_PORT = 11988
HEADER = b"00002"

_PACKET_FMT = "<6s2x2f2B16s2x2f"
_PACKET_SIZE = struct.calcsize(_PACKET_FMT)   # 44

STALE_AFTER = 1.0       # no packet in this long -> report as stale (WLED sends ~10/sec)
IDLE_STOP = 120.0       # stop listening if nobody has read for this long


class AudioSyncFeed:
    """Background UDP multicast listener with a last-good cache -- same
    contract as MarketFeed/SatelliteFeed/FlightFeed, but no polling: packets
    arrive on their own schedule from the panel, so this just blocks on
    recvfrom() between reads instead of sleeping/refetching."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sample_raw = 0.0
        self._sample_smth = 0.0
        self._peak = False
        self._fft = [0] * 16
        self._fft_magnitude = 0.0
        self._fft_major_peak = 0.0
        self._updated = 0.0
        self._last_read = 0.0
        self._thread = None
        self._sock = None
        self._err = None

    def get(self):
        """Returns {sample_raw, sample_smth, peak, fft (16 ints 0-255),
        fft_magnitude, fft_major_peak, age, stale, err}. Never blocks."""
        now = time.time()
        with self._lock:
            self._last_read = now
            sample_raw, sample_smth = self._sample_raw, self._sample_smth
            peak = self._peak
            fft = list(self._fft)
            fft_magnitude, fft_major_peak = self._fft_magnitude, self._fft_major_peak
            updated, err = self._updated, self._err
        self._ensure_thread()
        age = (now - updated) if updated else None
        return {
            "sample_raw": sample_raw, "sample_smth": sample_smth,
            "peak": peak, "fft": fft,
            "fft_magnitude": fft_magnitude, "fft_major_peak": fft_major_peak,
            "age": age, "stale": age is None or age > STALE_AFTER,
            "err": err,
        }

    # ---- listening ----------------------------------------------------
    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _open_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", MCAST_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(MCAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(1.0)   # so the idle-stop check can run even with no traffic
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
            return          # not a V2 packet (e.g. V1/legacy sender) -- ignore, don't guess
        try:
            header, sample_raw, sample_smth, peak, _reserved2, fft, fft_mag, fft_peak = \
                struct.unpack(_PACKET_FMT, data)
        except struct.error:
            return
        if not header.startswith(HEADER):
            return           # wrong protocol version -- ignore rather than misparse
        with self._lock:
            self._sample_raw = sample_raw
            self._sample_smth = sample_smth
            self._peak = peak >= 1
            self._fft = list(fft)
            self._fft_magnitude = fft_mag
            self._fft_major_peak = fft_peak
            self._updated = time.time()
            self._err = None


FEED = AudioSyncFeed()
