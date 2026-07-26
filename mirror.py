"""
mirror.py — screen -> 64x64 panel mirror (live window / emulator source).

Image processing (downscale, crop, colour, gamma) lives in imgproc.py, shared
with video.py so a live window and a video file look the same on the panel.

Requires mss + Pillow (installed in the arcade venv).

macOS: capturing needs Screen Recording permission for whatever app runs
Python. This is granted per LAUNCHING CONTEXT, not just the binary — a process
started by launchd is a different grant than one started from a Terminal, even
though it is the exact same interpreter. Without the grant, mss does NOT error;
it silently returns the desktop wallpaper with every window stripped out, which
looks like a working mirror pointed at the wrong thing. screen_recording_ok()
below catches this up front instead of letting it fail silently.

Create and use the Mirror in ONE thread (mss objects are not thread-safe).
"""
import mss
from PIL import Image

import cursor
import imgproc

WIDTH, HEIGHT = imgproc.WIDTH, imgproc.HEIGHT


def screen_recording_ok():
    try:
        import ctypes
        import ctypes.util
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        return True                      # can't tell -> don't block the user


def list_displays():
    """Current display geometry, read fresh.

    mss caches its monitor list at construction, so a long-lived Mirror keeps
    reporting the layout from whenever the server started. Resizing the virtual
    screen changes both its size AND the origin of every display left of it, so
    a stale list silently captures the wrong rectangle. This makes a throwaway
    mss instead of reusing the render thread's (which is not thread-safe).
    """
    with mss.mss() as sct:
        out = []
        for i, m in enumerate(sct.monitors):
            if i == 0:
                label = "All screens"
            else:
                label = f"Display {i} · {m['width']}×{m['height']} @ x={m['left']}"
                if m["width"] == m["height"]:
                    label += " · SQUARE"
            out.append({"index": i, "width": m["width"], "height": m["height"],
                        "left": m["left"], "top": m["top"], "label": label,
                        "square": m["width"] == m["height"]})
        return out


PERMISSION_HELP = (
    "Screen Recording permission missing — macOS is returning the wallpaper with "
    "all windows removed. Grant it to the Python that runs this server (System "
    "Settings -> Privacy & Security -> Screen Recording), then restart it. A "
    "launchd-run server needs the grant on Python.app itself, separately from "
    "a Terminal-run one."
)


class Mirror:
    def __init__(self, monitor=1, region=None, fit="fill",
                 saturation=1.35, contrast=1.15, gamma=True, autocrop=True,
                 cursor=True, cursor_size=0.055, cursor_style="ring"):
        if not screen_recording_ok():
            raise PermissionError(PERMISSION_HELP)
        self.sct = mss.mss()
        self.monitor = monitor
        self.region = region
        self.fit = fit
        self.saturation = saturation
        self.contrast = contrast
        self.gamma = gamma
        self.autocrop = autocrop
        self.cursor = cursor
        self.cursor_size = cursor_size
        self.cursor_style = cursor_style
        self._stale = False
        self._area = region if region else self.sct.monitors[monitor]

    def configure(self, **kw):
        for k, v in kw.items():
            if not hasattr(self, k) or k.startswith("_"):
                continue
            if k == "region":
                self.region = v or None          # explicit null clears it
            elif v is not None:
                setattr(self, k, v)
        if "region" in kw or "monitor" in kw:
            # Don't touch self.sct here — configure() runs on the HTTP thread and
            # mss objects belong to the render thread. Flag it and let capture()
            # do the rebuild on the right thread.
            self._stale = True

    def _resync(self):
        """Rebuild the capture handle so display-geometry changes are picked up."""
        try:
            self.sct.close()
        except Exception:
            pass
        self.sct = mss.mss()
        self._area = self.region if self.region else self.sct.monitors[self.monitor]
        self._stale = False

    def capture(self):
        if self._stale:
            self._resync()
        raw = self.sct.grab(self._area)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        if self.cursor:
            # Before process(): the marker then gets cropped and scaled with the
            # rest of the frame instead of needing its own coordinate mapping.
            img = cursor.draw(img, self._area, scale=self.cursor_size,
                              style=self.cursor_style)
        return imgproc.process(img, fit=self.fit, saturation=self.saturation,
                                contrast=self.contrast, gamma=self.gamma,
                                autocrop=self.autocrop)

    def displays(self):
        return list_displays()


if __name__ == "__main__":
    import time
    from wled_ddp import WledDDP
    m = Mirror()
    panel = WledDDP()
    print("Mirroring primary display to the panel for 15s (Ctrl-C to stop)...")
    t0 = time.time()
    while time.time() - t0 < 15:
        panel.send(m.capture())
        time.sleep(1 / 15)
    print("done.")
