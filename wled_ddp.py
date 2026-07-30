"""
wled_ddp.py — the streaming core for the Apollo M-1 (64x64 WLED matrix).

Everything (games, emulator capture, video, phone casting) feeds frames through
this. A frame is width*height*3 bytes of RGB, row-major, starting top-left.

WLED listens for DDP on UDP port 4048 and displays whatever it receives in
realtime, reverting to its normal effects a few seconds after the stream stops.
No third-party libraries required.

Two hard-won rules live in here:
  * Never flood the panel. WLED will lock up (needing a physical power cycle)
    if you fire full frames faster than it can drain them. The arcade server
    de-duplicates frames and caps the send rate; keep it that way.
  * Photographic content needs GAMMA. LEDs are linear, sRGB images are not, so
    without it "black" video looks like grey fog on the panel.
"""
import socket
import struct

DEFAULT_IP = "192.168.40.24"   # Apollo M-1 (or use hostname "wled-5018e8.local")
DDP_PORT = 4048
WIDTH = 64
HEIGHT = 64

# DDP header (10 bytes):  flags, sequence, type, dest-id, offset(u32), length(u16)
_HDR = ">BBBBIH"
_FLAG_VER1 = 0x40
_FLAG_PUSH = 0x01
_MAX_DATA = 1440  # bytes per UDP packet (must be a multiple of 3 -> 480 pixels)


def make_gamma_lut(gamma=2.2, ceiling=255):
    """sRGB -> linear LED response. Makes blacks actually black and colours pop."""
    return bytes(min(ceiling, int(((i / 255.0) ** gamma) * ceiling + 0.5))
                 for i in range(256))


GAMMA_LUT = make_gamma_lut(2.2)


def apply_gamma(rgb, lut=GAMMA_LUT):
    return bytes(lut[b] for b in rgb)


class WledDDP:
    def __init__(self, ip=DEFAULT_IP, width=WIDTH, height=HEIGHT, port=DDP_PORT):
        self.ip = ip
        self.port = port
        self.width = width
        self.height = height
        self.n_bytes = width * height * 3
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 18)
        # A send must never be able to stall the render loop. "UDP doesn't
        # block" is only true when the destination is resolvable: for an
        # address on the local subnet that has gone offline, sendto() waits
        # on ARP resolution and can park the calling thread for SECONDS.
        # That is exactly what happened when the panel dropped off the
        # network -- the whole arcade loop froze, self.latest stopped
        # updating, and the web preview and mode switching looked dead even
        # though nothing had crashed (no exception, loop_errors stayed 0).
        # A timeout raises socket.timeout, which IS an OSError subclass, so
        # send()'s existing `except OSError` already turns it into the
        # intended "return False, drop the frame, keep running".
        self.sock.settimeout(0.25)
        self._seq = 0
        self.errors = 0
        self.sent = 0

    def send(self, rgb: bytes) -> bool:
        """Push one full frame. Returns False on a network error instead of raising —
        a dropped frame must never take down the arcade loop."""
        if len(rgb) != self.n_bytes:
            return False
        self._seq = (self._seq % 15) + 1
        offset = 0
        total = self.n_bytes
        try:
            while offset < total:
                chunk = rgb[offset:offset + _MAX_DATA]
                last = offset + len(chunk) >= total
                flags = _FLAG_VER1 | (_FLAG_PUSH if last else 0)
                header = struct.pack(_HDR, flags, self._seq, 0, 1, offset, len(chunk))
                self.sock.sendto(header + chunk, (self.ip, self.port))
                offset += len(chunk)
            self.sent += 1
            return True
        except OSError:
            self.errors += 1
            return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
