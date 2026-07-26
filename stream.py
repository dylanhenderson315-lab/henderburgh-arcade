"""
stream.py — play a video FILE or a URL straight to the panel, via ffmpeg.

This replaces screen-mirroring a video player, which was the wrong shape for the
job: mirroring meant BetterDisplay composited a window at 60 Hz, we poll-
screenshotted it at ~32 Hz, downscaled on the CPU, then dropped every poll that
landed between two source frames. Measured end result was 8 fps of a 24 fps
video. Nothing in that chain was doing useful work.

Here ffmpeg decodes, downscales to 64x64 and converts the frame rate in one
pass, writing raw rgb24 straight to a pipe. Every frame that arrives is real,
distinct and already panel-sized, so the de-duplication in the render loop stops
costing anything and the panel gets a steady stream on the video's own clock.

URLs go through yt-dlp first (YouTube and ~1800 other sites), piped into the
same ffmpeg. Nothing is shown on screen, nothing needs a permission grant, and
no window has to be positioned anywhere — which also means it works when the
request comes from a phone.

Requires: ffmpeg, yt-dlp (Homebrew).
"""
import os
import shutil
import subprocess
import threading

import imgproc
import wled_ddp

WIDTH, HEIGHT = imgproc.WIDTH, imgproc.HEIGHT
FRAME_BYTES = WIDTH * HEIGHT * 3
GAMMA_LUT = wled_ddp.GAMMA_LUT

# launchd gives the server a bare PATH with no Homebrew in it, so bare tool
# names resolve when run by hand and fail as the running service.
_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]


def _tool(name):
    for d in _BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"{name} not found — install it with `brew install ffmpeg yt-dlp`.")


def have_tools():
    try:
        _tool("ffmpeg"), _tool("yt-dlp")
        return True
    except RuntimeError:
        return False


def is_url(src):
    return str(src).startswith(("http://", "https://"))


def _vf(fit, saturation, contrast):
    """ffmpeg filter chain: downscale to the panel, then colour.

    flags=area is box/area averaging — the same choice imgproc makes, and the
    only sane one at this ratio; bilinear aliases badly when shrinking 12:1.
    """
    if fit == "fit":
        geom = ("scale=64:64:force_original_aspect_ratio=decrease:flags=area,"
                "pad=64:64:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        geom = ("scale=64:64:force_original_aspect_ratio=increase:flags=area,"
                "crop=64:64")
    return f"{geom},eq=saturation={saturation}:contrast={contrast},format=rgb24"


class StreamSource:
    """Drop-in replacement for VideoSource: .read() -> 64x64 RGB bytes or None."""

    def __init__(self, src, fit="fill", saturation=1.3, contrast=1.1,
                 gamma=True, autocrop=False, fps=16):
        self.src = str(src)
        self.fit = fit
        self.saturation = float(saturation)
        self.contrast = float(contrast)
        self.gamma = gamma
        self.autocrop = autocrop            # accepted for interface parity
        self.fps = int(fps)
        self.title = None
        self.error = None
        self._latest = None
        self._new = False
        self._lock = threading.Lock()
        self._procs = []
        self._stop = threading.Event()
        self._thread = None
        self._start()

    # ---------- process plumbing ----------

    def _spawn(self):
        vf = _vf(self.fit, self.saturation, self.contrast)
        ff = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin"]
        procs = []
        if is_url(self.src):
            # yt-dlp resolves and downloads; ffmpeg only ever sees a byte stream,
            # so expiring signed URLs and per-site headers stay yt-dlp's problem.
            dl = subprocess.Popen(
                [_tool("yt-dlp"), "-q", "--no-warnings", "-o", "-",
                 "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
                 self.src],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            procs.append(dl)
            # -re paces a real-time read so ffmpeg can't race ahead of playback.
            ff += ["-re", "-i", "pipe:0"]
            stdin = dl.stdout
        else:
            ff += ["-stream_loop", "-1", "-re", "-i", self.src]
            stdin = None
        ff += ["-an", "-vf", vf, "-r", str(self.fps), "-f", "rawvideo", "pipe:1"]
        enc = subprocess.Popen(ff, stdin=stdin, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        if stdin is not None:
            stdin.close()          # ffmpeg owns the read end now
        procs.append(enc)
        self._procs = procs
        return enc

    def _kill(self):
        for p in self._procs:
            try:
                p.kill()
            except Exception:
                pass
        self._procs = []

    def _reader(self):
        """Read whole frames; keep only the newest. Restart a URL on EOF to loop."""
        while not self._stop.is_set():
            try:
                enc = self._spawn()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                return
            buf = b""
            while not self._stop.is_set():
                chunk = enc.stdout.read(FRAME_BYTES - len(buf))
                if not chunk:
                    break                                  # EOF or died
                buf += chunk
                if len(buf) < FRAME_BYTES:
                    continue                               # partial frame
                frame = buf.translate(GAMMA_LUT) if self.gamma else buf
                with self._lock:
                    self._latest = frame
                    self._new = True
                buf = b""
            err = b""
            try:
                err = enc.stderr.read() or b""
            except Exception:
                pass
            self._kill()
            if self._stop.is_set():
                return
            if err.strip():
                self.error = err.decode("utf-8", "replace").strip()[-300:]
                return                # a real failure would just respawn-loop
            # Clean EOF: a URL played to the end, so start it over.

    def _start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    # ---------- public interface ----------

    def read(self):
        """Newest unseen frame, or None when nothing new has arrived yet."""
        with self._lock:
            if not self._new:
                return None
            self._new = False
            return self._latest

    def configure(self, **kw):
        """Apply settings, restarting ffmpeg only when the filter chain changed."""
        restart = False
        for k, v in kw.items():
            if v is None or not hasattr(self, k) or k.startswith("_"):
                continue
            if k in ("fit", "saturation", "contrast", "fps"):
                old = getattr(self, k)
                new = float(v) if k in ("saturation", "contrast") else (
                    int(v) if k == "fps" else v)
                if old != new:
                    setattr(self, k, new)
                    restart = True
            else:
                setattr(self, k, v)          # gamma is applied per-frame here
        if restart:
            self.stop()
            self.error = None
            self._start()

    def stop(self):
        self._stop.set()
        self._kill()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)


def probe_title(url, timeout=25):
    """Human-readable name for a URL, for the UI. Best effort."""
    try:
        r = subprocess.run([_tool("yt-dlp"), "-q", "--no-warnings",
                            "--print", "%(title)s", "--playlist-items", "1", url],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout.strip().splitlines() or [None])[0]
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    import time
    from wled_ddp import WledDDP
    src = sys.argv[1]
    s = StreamSource(src, fps=16)
    panel = WledDDP()
    print(f"streaming {src} for 20s…")
    t0 = time.time()
    n = 0
    while time.time() - t0 < 20:
        f = s.read()
        if f:
            panel.send(f)
            n += 1
        time.sleep(1 / 60)
    s.stop()
    print(f"{n} frames in 20s -> {n/20:.1f} fps   error={s.error}")
