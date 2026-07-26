"""
test_stream.py — proves the Mac -> panel live pipe works, and confirms mapping.

Streams for a few seconds:
  - a solid RED top row (row 0)
  - a WHITE 4x4 marker in the TOP-LEFT corner
  - a moving cyan vertical bar sweeping left->right
So you can confirm the stream is live AND that top-left / row order is correct.
"""
import sys
import time
import math
from wled_ddp import WledDDP, WIDTH, HEIGHT

DURATION = 8.0   # seconds
FPS = 30

def build_frame(t):
    buf = bytearray(WIDTH * HEIGHT * 3)
    bar_x = int((t * 24) % WIDTH)  # cyan bar sweeps across
    for y in range(HEIGHT):
        for x in range(WIDTH):
            i = (y * WIDTH + x) * 3
            if y == 0:                              # top row -> red
                r, g, b = 60, 0, 0
            elif y < 4 and x < 4:                   # top-left marker -> white
                r, g, b = 120, 120, 120
            else:                                   # dim blue background
                r, g, b = 0, 0, 12
            if x == bar_x:                          # moving cyan bar
                r, g, b = 0, 120, 120
            buf[i] = r; buf[i+1] = g; buf[i+2] = b
    return bytes(buf)

def main():
    # optional args:  test_stream.py [duration_seconds] [ip]
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else DURATION
    ip = sys.argv[2] if len(sys.argv) > 2 else None
    globals()['DURATION'] = dur
    panel = WledDDP(ip) if ip else WledDDP()
    print(f"Streaming test pattern to {panel.ip} for {DURATION:.0f}s "
          f"({FPS} fps)...  Ctrl-C to stop.")
    frame_dt = 1.0 / FPS
    t0 = time.time()
    try:
        while (t := time.time() - t0) < DURATION:
            panel.send(build_frame(t))
            time.sleep(frame_dt)
    except KeyboardInterrupt:
        pass
    finally:
        panel.close()
    print("Done. Panel will return to its WLED effect shortly.")

if __name__ == "__main__":
    main()
