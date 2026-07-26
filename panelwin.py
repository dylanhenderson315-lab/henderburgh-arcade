"""
panelwin.py — put a window ON the panel display, and take it back off.

Dragging a window off the edge of the screen to reach the virtual display is
awkward and easy to get wrong (it is a 320x320 target). This does it for you:
pick an app, it gets moved and sized to exactly fill the panel display, and one
call puts it back on the main screen.

Displays are located by GEOMETRY read live from mss, never by a remembered
index. Display indices and AppleScript "desktop N" numbers drift when screens
are added, removed or resized — trusting them once repositioned the user's real
monitors, so nothing here addresses a screen by number.
"""
import ctypes
import ctypes.util
import os
import subprocess
import time

import mirror

ACCESSIBILITY_HELP = (
    "Accessibility permission missing — macOS blocks moving other apps' windows. "
    "Grant it to the Python that runs this server (System Settings → Privacy & "
    "Security → Accessibility), then restart it. Like Screen Recording, this is "
    "granted per launching context, so a launchd-run server needs its own grant."
)


def accessibility_ok(prompt=False):
    """True if this process may drive other apps' windows.

    Without it AppleScript fails with -25211 ('not allowed assistive access')
    rather than anything that names the real problem, so check up front.
    """
    try:
        ax = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        if prompt and not ax.AXIsProcessTrusted():
            # Ask macOS to show the "grant access" dialog for this exact binary.
            subprocess.run(["osascript", "-e",
                            'tell application "System Events" to get name of '
                            'first process whose frontmost is true'],
                           capture_output=True, timeout=10)
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return True                      # can't tell -> don't block the user


VIRTUAL_SCREEN = "Apollo-M1"

# Square sizes for the virtual screen, all exact power-of-two multiples of the
# panel's 64px so every LED is a clean box-average with no resampling.
#
# Smaller is sharper for anything the Mac RENDERS (text and UI are drawn at a
# fixed point size, so a smaller screen makes them bigger relative to the frame).
# It makes no difference to a fullscreen video, which is the same image either
# way. The floor is set by app minimum window sizes, not by macOS: Chrome will
# not go below ~500x420, so 320 fits only fullscreen content.
PANEL_SIZES = [
    {"size": "1024x1024", "label": "1024 · roomy", "note": "16:1 — most room for windows"},
    {"size": "512x512", "label": "512 · balanced", "note": "8:1 — windows still fit. Default"},
    {"size": "320x320", "label": "320 · max zoom", "note": "5:1 — fullscreen only, windows won't fit"},
]


# launchd hands a process a bare PATH with no Homebrew in it, so a plain
# "betterdisplaycli" resolves interactively and fails as the running server.
BD_CANDIDATES = [
    "/opt/homebrew/bin/betterdisplaycli",
    "/usr/local/bin/betterdisplaycli",
    "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay",
]


def _bd_bin():
    from shutil import which
    for p in BD_CANDIDATES:
        if os.path.exists(p):
            return p
    found = which("betterdisplaycli")
    if found:
        return found
    raise RuntimeError(
        "BetterDisplay CLI not found — install it with "
        "`brew install waydabber/betterdisplay/betterdisplaycli`.")


def _bd(*args):
    r = subprocess.run([_bd_bin(), *args], capture_output=True,
                       text=True, timeout=20)
    return r.stdout.strip()


def current_size():
    d = panel_display()
    return f"{d['width']}x{d['height']}" if d else None


def set_panel_size(size):
    """Resize the virtual screen.

    Setting `resolution` alone does nothing — macOS keeps picking the largest
    entry in the screen's resolution list — so the list itself is narrowed to
    the single wanted mode. That is the only thing that actually takes effect.
    """
    if size not in {s["size"] for s in PANEL_SIZES}:
        raise ValueError(f"unsupported size {size!r}")
    _bd("set", f"-namelike={VIRTUAL_SCREEN}", f"-resolutionList={size}")
    for _ in range(12):                       # the resize is not instantaneous
        time.sleep(0.4)
        if current_size() == size:
            break
    return {"size": current_size(), "requested": size, "panel": panel_display()}


def panel_display():
    """The square display we stream to, or None.

    Identified by being square — that is what the virtual screen was configured
    to be and no real monitor here is. Falls back to the leftmost (most negative
    x) display, which is where the virtual screen sits.
    """
    ds = [d for d in mirror.list_displays() if d["index"] != 0]
    square = [d for d in ds if d["square"]]
    if square:
        return min(square, key=lambda d: d["width"] * d["height"])
    off = [d for d in ds if d["left"] < 0]
    return min(off, key=lambda d: d["left"]) if off else None


def main_display():
    ds = [d for d in mirror.list_displays() if d["index"] != 0]
    for d in ds:
        if d["left"] == 0 and d["top"] == 0:
            return d
    return ds[0] if ds else None


def _osa(script):
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        err = r.stderr.strip()
        if "-25211" in err or "assistive access" in err:
            raise PermissionError(ACCESSIBILITY_HELP)
        raise RuntimeError(err or "AppleScript failed")
    return r.stdout.strip()


# Apps that expose `bounds of window` in their own AppleScript dictionary. Going
# through the app itself only needs Automation permission (which macOS prompts
# for automatically, per app) rather than Accessibility (which has to be granted
# by hand in System Settings and is invisible until something fails with -25211).
NATIVE_BOUNDS = {
    "Google Chrome", "Safari", "Brave Browser", "Microsoft Edge", "Arc",
    "VLC", "QuickTime Player", "Preview", "Finder", "Music", "TV", "Terminal",
}


def apps():
    """Apps that currently have at least one moveable window."""
    # System Events cannot filter on `count of windows` inside a `whose` clause
    # ("Can't make 0 into type specifier"), so collect names and counts as two
    # parallel lists and pair them up here.
    names = _osa('tell application "System Events" to get name of every process '
                 'whose background only is false')
    names = [n.strip() for n in names.split(",") if n.strip()]
    try:
        counts = _osa('tell application "System Events" to get (count of windows) '
                      'of every process whose background only is false')
        counts = [c.strip() for c in counts.split(",") if c.strip()]
        running = [n for n, c in zip(names, counts) if c.isdigit() and int(c) > 0]
    except PermissionError:
        # No Accessibility: we can still name the running apps, and the ones we
        # can actually drive natively are the only ones that matter anyway.
        running = names
    return [{"name": n, "native": n in NATIVE_BOUNDS} for n in running]


def _set_bounds_native(app, l, t, w, h):
    """Move/resize via the app's own dictionary. Raises if the app can't."""
    _osa(f'tell application "{app}" to set bounds of window 1 '
         f'to {{{l}, {t}, {l + w}, {t + h}}}')
    got = _osa(f'tell application "{app}" to get bounds of window 1')
    b = [int(x) for x in got.split(",")]
    return {"left": b[0], "top": b[1], "width": b[2] - b[0], "height": b[3] - b[1]}


def _set_bounds_ax(app, l, t, w, h):
    """Fallback via System Events — needs Accessibility permission."""
    _osa(f'''
        tell application "System Events" to tell process "{app}"
            set frontmost to true
            set position of window 1 to {{{l}, {t}}}
            set size of window 1 to {{{w}, {h}}}
            set position of window 1 to {{{l}, {t}}}
        end tell
    ''')
    return {"left": l, "top": t, "width": w, "height": h}


def _move(app, l, t, w, h):
    if app in NATIVE_BOUNDS:
        try:
            return _set_bounds_native(app, l, t, w, h)
        except Exception:
            pass                                  # fall through to the AX path
    return _set_bounds_ax(app, l, t, w, h)


def _front_app():
    return _osa('tell application "System Events" to get name of first process '
                'whose frontmost is true')


def send(app=None):
    """Move an app's front window onto the panel display, filling it exactly."""
    d = panel_display()
    if not d:
        raise RuntimeError("No panel display found — is the virtual screen on?")
    app = app or _front_app()
    got = _move(app, d["left"], d["top"], d["width"], d["height"])
    # Anything wider than the display got clamped by the app's own minimum size,
    # so say so rather than leaving a half-off-screen window looking like a bug.
    clamped = got["width"] > d["width"] + 2 or got["height"] > d["height"] + 2
    return {"app": app, "display": d, "window": got, "clamped": clamped}


def recall(app=None):
    """Bring a window back from the panel display to the main screen."""
    m = main_display()
    if not m:
        raise RuntimeError("No main display found")
    p = panel_display()
    if app is None and p:
        # Whatever is sitting on the panel display right now. Only apps we can
        # query without Accessibility are checked, which is the same set we can
        # move back anyway.
        for a in apps():
            if not a["native"]:
                continue
            try:
                b = _osa(f'tell application "{a["name"]}" to get bounds of window 1')
                x = int(b.split(",")[0])
            except Exception:
                continue
            if p["left"] <= x < p["left"] + p["width"]:
                app = a["name"]
                break
    if not app:
        raise RuntimeError("No window is on the panel display")
    w, h = min(1280, m["width"] - 200), min(800, m["height"] - 200)
    got = _move(app, m["left"] + 100, m["top"] + 60, w, h)
    return {"app": app, "display": m, "window": got}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "info":
        print("panel :", panel_display())
        print("main  :", main_display())
        print("apps  :", apps())
    elif cmd == "send":
        print(send(arg))
    elif cmd == "back":
        print(recall(arg))
