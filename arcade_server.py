"""
arcade_server.py — the local arcade brain for the Apollo M-1.

Runs on your Mac (same network as the panel). It:
  * runs the active game in a background thread,
  * streams frames to the panel over DDP (de-duplicated + rate capped),
  * accepts video frames CAST from a phone browser,
  * serves the web control page and its API.

Games are pure standard library. 'mirror' needs mss+Pillow from the local .venv,
so start it with the venv and every mode works:

    cd wled-m1-arcade
    .venv/bin/python arcade_server.py

Then open http://localhost:7333  (or http://<this-mac-LAN-ip>:7333 from a phone).
Phone controller (game picker + touch controls, designed for the panel): /remote

Modes: any engine name from engines.ENGINES, plus '<name>-demo' for auto-play,
  plus mirror | video | cast | off.
  'clock' is the resting/default state -- the panel shows time, date, temp
         and the next ISS pass rather than sitting dark.
  'off'  releases the panel; WLED/Home Assistant resumes its own lighting.
  'on'   is shorthand the UI sends to resume whatever was last selected.

DO NOT raise PANEL_FPS or remove the frame de-duplication: flooding WLED with
full frames locks the panel up hard enough to need a physical power cycle.
"""
import base64
import json
import threading
import time
import urllib.request
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import engines
import gameday
import mma
from engines import ENGINES, WIDTH, HEIGHT, TicCartEngine, CARTS_DIR
from display import get_renderer
import backgrounds
import events_log
import market
import satellite
import flights
import sports
import news
import weather
import blog
import brightness
import notify
import paneltext
import transitions

PORT = 7333
HERE = Path(__file__).parent
RENDER_FPS = 24                    # internal loop rate (preview + input responsiveness)
# Moving sources are SAMPLED faster than they are sent. Frames only go out when
# the image changed, so sampling at the send rate means every poll that lands
# between two source frames costs a whole send slot — that alone was halving the
# delivered rate on video. Oversampling fills each slot with a fresh frame.
# This costs nothing on the panel: PANEL_FPS still caps what is transmitted.
CAPTURE_FPS = 32
PANEL_FPS = 16                     # cap on packets actually SENT to the panel
# WLED drops out of realtime and falls back to its own effect after this long
# without a packet (its "realtime timeout", 2.5 s by default). Frames are only
# sent when the image CHANGES, so a static picture would silently hand the panel
# back mid-session. Re-send the last frame this often to hold the takeover.
KEEPALIVE_S = 1.0
# Mode-change transition length. ~0.4s at RENDER_FPS -- long enough to
# read as intentional motion, short enough never to feel like latency.
# Cost is ~0.001ms/frame (pure byte slicing), so this is nowhere near the
# frame-rate or DDP-flood limits documented above.
TRANSITION_FRAMES = 10
# The panel RESTS on the clock rather than sitting dark. A 64x64 display
# on a wall doing nothing is the one state that earns nothing, and clock
# needs no network so it works even with every feed down.
#
# "off" is deliberately KEPT as an explicit, user-chosen action, not
# replaced: it releases the panel so WLED/Home Assistant resumes its own
# lighting (see _release_panel). That handoff is a real capability -- the
# panel doubles as a house lamp -- and folding it into "resting state"
# would have removed it.
DEFAULT_MODE = "clock"             # resting state; "off" still releases to WLED
# GAME DAY hands the panel back here when its event ends; assigning it
# rather than duplicating the string means the two cannot drift.
engines.RESTING_MODE = DEFAULT_MODE
RESUME_GAME = "boot"                # "Turn On" always plays the boot curtain, then the menu

# HOME ASSISTANT NOTIFY, normal-priority banner display window (task #8).
# ~8-10s: long enough to actually read a short line on a wall panel you
# glance at rather than stare at, short enough that it never competes with
# whatever's actually running for more than a few seconds. Picked the
# middle of that range -- no measured data drove the exact number, same
# kind of reasoned default as brightness.py's own DEFAULTS.
NOTIFY_BANNER_SECONDS = 9.0
FRAME_BYTES = WIDTH * HEIGHT * 3
CAST_TIMEOUT = 5.0                 # s without a phone frame -> report cast idle
VIDEOS_DIR = HERE / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)
MAX_UPLOAD_BYTES = 800 * 1024 * 1024   # 800MB — plenty for a personal clip
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".gif"}


class Arcade:
    def __init__(self):
        self.lock = threading.Lock()
        self.panel = get_renderer()
        # Engine MUST match self.mode at startup. This was hardcoded to
        # snake while self.mode came from DEFAULT_MODE, which was harmless
        # only because DEFAULT_MODE used to be "off" (the loop skips
        # rendering entirely in that mode, so the mismatch never showed).
        # The moment the default became a real rendering mode, the panel
        # drew snake while the API reported "clock".
        self.engine = ENGINES.get(DEFAULT_MODE, ENGINES["snake"])()
        self.mode = DEFAULT_MODE
        self.last_game = RESUME_GAME
        # Pause freezes game logic only. The render loop keeps running and
        # keeps pushing the last frame, because the panel must never be left
        # without traffic -- see the PANEL_FPS/dedup notes at the top.
        self.paused = False
        self.pause_sel = 0          # index into engines.PAUSE_ITEMS
        self.pause_bg = None        # last live frame, shown under the overlay
        self.pause_pulse = 0
        self.latest = self.engine.frame()
        self._video_hold = None     # last raw decoded video frame (pre-dim)
        self.mirror = None
        self.mirror_error = None
        self.mirror_cfg = {"fit": "fill", "saturation": 1.35, "contrast": 1.15,
                           "autocrop": True, "monitor": 1, "region": None,
                           "cursor": True, "cursor_size": 0.055,
                           "cursor_style": "ring"}
        self.saved_state = None
        self.video = None
        self.video_error = None
        self.video_path = None
        self.video_cfg = {"fit": "fill", "saturation": 1.3,
                          "contrast": 1.1, "autocrop": False}
        self.cast_frame = None
        self.cast_at = 0.0
        # Mode-change transition state. _trans_from is the last frame of the
        # OUTGOING mode; the loop slides the incoming mode in over it.
        self._trans_from = None
        self._trans_i = 0
        # Per-swap style override -- every mode swap uses transitions'
        # DEFAULT_STYLE except this one narrow, explicit special case (set
        # right below in set_mode()). Not a general per-mode config table:
        # this is the ONLY swap in the whole project that needs to differ,
        # so a table would be an abstraction with a single real entry.
        self._trans_style = None
        # First-light: ramp the very first frames up out of black rather
        # than snapping to a fully-lit panel.
        self._wake_i = 0
        self.stats = {"sent": 0, "errors": 0, "loop_errors": 0}
        # Apollo M-1 WLED effect as background (id = effect index string, or "none")
        self.background = "none"   # "none" | "0".."254"
        self.bg_speed = 50         # 1–100 → WLED sx
        self._bg_applied = None    # last (fx, sx) pushed to native WLED
        self._bg_capture_needed = False  # request real-effect loop capture
        self._bg_capture_tried = {}      # fx -> last attempt time (cooldown)
        self._bg_capture_active = False  # capture thread owns the panel; hold DDP
        self._bg_last_good = None        # last real peeked ambient frame
        self._bg_fx_check_at = 0.0       # throttle panel fx drift checks
        # HOME ASSISTANT NOTIFY, normal-priority banner (task #8): mirrors
        # how severe-weather's own takeover state (_alert_ticks) is tracked
        # here rather than in a feed module -- a lightweight composite
        # overlay, not a mode, so it lives on Arcade next to the render
        # loop that applies it. None when no banner is showing; otherwise
        # {title, message, color, expires (wall-clock time.time())}.
        self._notify_banner = None
        self._notify_scroll = 0.0        # marquee offset for an overflowing banner message
        self.running = True
        # Warm the effects list cache
        try:
            backgrounds.fetch_effects(self.panel.ip, force=True)
        except Exception:
            pass
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- WLED handoff -----------------------------------------------------
    # Taking the panel and giving it back is explicit, so the arcade and the
    # house lighting never fight over it.
    def _wled(self, payload=None):
        url = f"http://{self.panel.ip}/json/state"
        try:
            if payload is None:
                with urllib.request.urlopen(url, timeout=2.5) as r:
                    return json.load(r)
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=2.5) as r:
                r.read()
            return True
        except Exception:
            return None                      # panel busy/offline: never fatal

    def _take_panel(self):
        """Remember the lighting we're interrupting so we can put it back."""
        st = self._wled()
        if not st:
            return
        seg = (st.get("seg") or [{}])[0]
        self.saved_state = {
            "on": st.get("on", True), "bri": st.get("bri", 128),
            "seg": [{k: seg[k] for k in ("fx", "pal", "col", "sx", "ix", "on", "bri")
                     if k in seg}],
        }
        if st.get("ps", -1) not in (-1, None):
            self.saved_state["ps"] = st["ps"]        # a preset was active

    def _release_panel(self):
        """Hand the panel back to exactly the lighting that was on before."""
        if not self.saved_state:
            return
        restore = ({"ps": self.saved_state["ps"]} if "ps" in self.saved_state
                   else self.saved_state)
        self._wled(restore)

    # ---- control ----------------------------------------------------------
    def set_mode(self, mode):
        with self.lock:
            if mode == "on":
                # Power-on always plays the boot curtain into the menu, like
                # a real console -- it never jumps straight back into
                # whatever was last running. last_game only feeds the menu's
                # cursor position (see MenuEngine._select below); it must
                # never drive what "on" itself resolves to, or a stray mode
                # (mirror/video/cast) picked up as last_game would silently
                # skip the boot sequence on the next power-on.
                mode = RESUME_GAME
            was_off = self.mode == "off"
            prev = self.mode
            prev_frame = self.latest if prev != "off" else None
            if mode != "off":
                # Don't treat pure ambient background, the boot splash, or
                # the system menu itself as a "game" to resume -- last_game
                # should always be the last thing actually played, so the
                # menu can highlight it.
                if mode not in ("background", "menu", "boot"):
                    self.last_game = mode
            self.mode = mode
            self.paused = False        # never land in a new mode already frozen
            base = mode.split("-")[0]
            if mode.startswith("tic:"):
                # Fantasy-console cart -- same launch pipeline as every
                # native game, just a different engine constructor shape
                # (it needs the cart's path, not zero args).
                cart_name = mode[len("tic:"):]
                self.engine = TicCartEngine(CARTS_DIR / cart_name)
            elif base in ENGINES:
                self.engine = ENGINES[base]()
                if base == "menu" and self.last_game and self.last_game != "menu":
                    # Land the cursor on whatever you actually played last,
                    # instead of always resetting to the first tile.
                    self.engine._select(self.last_game.split("-")[0])
            # Start a transition FROM whatever was on screen. Captured here,
            # after self.latest still holds the outgoing mode's last frame
            # for the paths above that don't rebuild it.
            if prev != mode and mode != "off":
                self._trans_from = prev_frame
                self._trans_i = 0
                # The ONE hand-off the owner wants to feel considered
                # rather than generic: PlaneWatchEngine's takeover handing
                # off into flights' ceremonial Hangar detail card (see
                # CLAUDE.md's PLANE-IN-WINDOW TAKEOVER section). Every
                # other swap in the project -- menu<->game, planewatch's
                # own entry, everything -- keeps the untouched default.
                self._trans_style = (
                    "radial_open" if prev == "planewatch" and mode == "flights"
                    else None
                )

        # Do panel HTTP outside the lock — it must never stall the render loop.
        if was_off and mode != "off":
            # Coming from released/dark: play the first-light ramp, and do
            # NOT also slide -- there is nothing meaningful to slide away
            # from, and doing both at once reads as a glitch.
            with self.lock:
                self._wake_i = 0
                self._trans_from = None
            self._take_panel()
        elif not was_off and mode == "off":
            threading.Timer(0.4, self._release_panel).start()   # after last packet

        # Entering ambient effect mode: stop DDP, run exact Apollo FX natively,
        # and stream liveview peeks into the UI preview (100% real pixels).
        if mode == "background":
            with self.lock:
                self._bg_applied = None
                self._bg_capture_needed = False  # never capture while ambient
            backgrounds.stop_live_peek()  # ambient peeks in the render loop (exclusive lv)
            self._apply_native_bg(force=True)
            # Re-assert after DDP timeout (~2.5s default) so the panel shows FX
            def _reassert():
                time.sleep(2.6)
                if self.mode == "background":
                    self._apply_native_bg(force=True)
            threading.Thread(target=_reassert, daemon=True).start()
        elif prev == "background" and mode not in ("off", "background"):
            with self.lock:
                self._bg_applied = None
            # Leaving ambient for a game: capture a real effect loop if needed
            self._request_bg_capture()
        elif mode != "background" and mode != "off":
            backgrounds.stop_live_peek()
            # Entering any DDP mode with a background selected → need capture
            self._request_bg_capture()
        elif mode == "off":
            backgrounds.stop_live_peek()
            with self.lock:
                self._bg_capture_needed = False

    def trigger_notify(self, priority, title, message):
        """Home Assistant notification pass-through (task #8). `title`/
        `message` must already be paneltext.panel_text()-folded by the
        caller (do_POST's /api/notify handler) -- this method just routes
        to whichever of the two existing takeover mechanisms the priority
        maps to, never draws or fabricates text itself.

        Guarded against waking `off`, same reasoning as the plane-in-
        window trigger in _loop(): the panel doubling as a house lamp,
        deliberately released to WLED/Home Assistant lighting, must not be
        woken by a notification any more than by a plane flying by --
        `off` is a deliberate hand-off, not a state any feature may
        override uninvited."""
        with self.lock:
            if self.mode == "off":
                return False
            color = engines.NOTIFY_URGENT_COLOR if priority == "urgent" \
                else engines.NOTIFY_NORMAL_COLOR
            if priority != "urgent":
                # Lightweight composite overlay -- see _notify_banner_frame().
                # No mode swap, no input capture; whatever's running keeps
                # running untouched underneath it.
                self._notify_banner = {
                    "title": title, "message": message, "color": color,
                    "expires": time.time() + NOTIFY_BANNER_SECONDS,
                }
                return True
            # Urgent: real mode swap. Capture "what was running right
            # before this interrupted it" -- the same `prev = self.mode`
            # idiom set_mode() itself already uses -- so NotifyEngine hands
            # back to the ACTUAL previous mode rather than a hardcoded
            # landing spot. If a notification arrives while one is already
            # showing, carry that one's own return_mode forward instead of
            # returning to "notify" itself.
            prev = self.mode
            if prev == "notify" and self.engine is not None:
                prev = getattr(self.engine, "return_mode", None) or engines.RESTING_MODE
        notify.push_pending({
            "title": title, "message": message, "color": color, "prev_mode": prev,
        })
        self.set_mode("notify")
        return True

    def _notify_banner_frame(self, frame):
        """Composite the normal-priority notify banner over `frame` if one
        is currently in its display window, else return `frame` unchanged
        and clear stale state. Mirrors _severe_alert_frame()'s own
        "no extra bookkeeping to clear it" shape: once `expires` passes,
        the very next call just falls through to clearing it."""
        banner = self._notify_banner
        if banner is None:
            return frame
        if time.time() >= banner["expires"]:
            self._notify_banner = None
            return frame
        if frame is None:
            return frame
        self._notify_scroll += 1.6
        return engines.draw_notify_banner(
            frame, banner["title"], banner["message"], banner["color"],
            scroll=self._notify_scroll)

    def _input_allowed(self):
        base = self.mode.split("-")[0]
        return self.engine and not self.mode.endswith("-demo") and (
            self.mode.startswith("tic:") or base in ENGINES
        )

    def set_paused(self, value):
        """value: True/False/None -- None toggles."""
        with self.lock:
            # Only a running game can be paused; pausing the menu or the boot
            # sequence would strand the system with no way back.
            base = self.mode.split("-")[0]
            if self.mode in ("off",) or base in ("menu", "boot"):
                self.paused = False
            else:
                was = self.paused
                self.paused = (not self.paused) if value is None else bool(value)
                if self.paused and not was:
                    # Freeze the board as it stood, so the overlay always has
                    # the exact moment you paused underneath it.
                    self.pause_bg = bytes(self.latest)
                    self.pause_sel = 0
            return self.paused

    def _pause_action(self):
        """Act on the highlighted pause item. Returns a mode to switch to
        after the lock is released, or None."""
        item = engines.PAUSE_ITEMS[self.pause_sel]
        if item == "RESUME":
            self.paused = False
        elif item == "RESTART":
            self.paused = False
            return self.mode                 # re-entering rebuilds the engine
        elif item == "MENU":
            self.paused = False
            return "menu"
        return None

    def _pause_input(self, cmd):
        """Drive the pause menu. Returns (handled, mode_to_switch_to)."""
        n = len(engines.PAUSE_ITEMS)
        if cmd in ("up", "left"):
            self.pause_sel = (self.pause_sel - 1) % n
        elif cmd in ("down", "right"):
            self.pause_sel = (self.pause_sel + 1) % n
        elif cmd == "rotate":
            return True, self._pause_action()
        elif cmd in ("drop", "hold"):
            self.paused = False              # secondary button = just resume
        return True, None

    def send_input(self, cmd):
        switch = None
        with self.lock:
            if self.paused:
                _, switch = self._pause_input(cmd)
            elif self._input_allowed():
                self.engine.input(cmd)
        # set_mode takes the same lock, so it has to happen outside it.
        if switch:
            self.set_mode(switch)

    def send_press(self, cmd):
        switch = None
        with self.lock:
            if self.paused:
                # While paused the controller drives the pause menu, not the
                # frozen game -- otherwise the phone's d-pad would appear dead.
                _, switch = self._pause_input(cmd)
            elif self._input_allowed() and hasattr(self.engine, "press"):
                self.engine.press(cmd)
            elif self._input_allowed():
                self.engine.input(cmd)
        if switch:
            self.set_mode(switch)

    def send_release(self, cmd):
        with self.lock:
            if self.paused:
                return                       # pause menu acts on press only
            if self._input_allowed() and hasattr(self.engine, "release"):
                self.engine.release(cmd)

    # ---- pointer/mouse (mouse-driven carts only) --------------------------
    def send_pointer(self, fx, fy, down):
        with self.lock:
            if self._input_allowed() and hasattr(self.engine, "pointer"):
                self.engine.pointer(fx, fy, down)

    def send_mouse_right(self, held):
        with self.lock:
            if self._input_allowed() and hasattr(self.engine, "mouse_right"):
                self.engine.mouse_right(held)

    def send_wheel(self, direction):
        with self.lock:
            if self._input_allowed() and hasattr(self.engine, "wheel"):
                self.engine.wheel(direction)

    def set_background(self, mode_id, speed=None):
        with self.lock:
            old_bg, old_spd = self.background, self.bg_speed
            self.background = str(mode_id if mode_id is not None else "none").strip() or "none"
            if speed is not None:
                try:
                    self.bg_speed = max(1, min(100, int(speed)))
                except (TypeError, ValueError):
                    pass
            bg, spd = self.background, self.bg_speed
            mode = self.mode
        fx = backgrounds.parse_fx(bg)
        name = backgrounds.effect_name(fx) if fx is not None else "none"
        # Only drop cached loops when fx changes or speed *bucket* changes
        # (avoids thrash-recapture on every slider tick within the same bucket).
        old_fx = backgrounds.parse_fx(old_bg)
        speed_bucket_changed = (
            backgrounds._cache_key_speed(old_spd) != backgrounds._cache_key_speed(spd)
        )
        # Only a speed-bucket change makes an existing recording *wrong*.
        # Selecting a different effect must NOT invalidate the effect being
        # selected: that discarded a perfectly good loop and forced a fresh
        # ~10s capture on every single background switch, which is most of what
        # made backgrounds feel slow.
        if speed_bucket_changed and fx is not None:
            backgrounds.invalidate_cache(fx)
        if fx is not None:
            with self.lock:
                self._bg_capture_tried.pop(fx, None)   # allow an immediate retry
                if bg != old_bg:
                    self._bg_last_good = None
        if old_fx is not None and old_fx != fx:
            # Merely switching away — the old recording is still valid, so evict
            # it from memory but keep the persisted copy. Switching back then
            # rehydrates from disk instantly instead of re-capturing.
            backgrounds.invalidate_cache(old_fx, disk=False)

        # Ambient-only: exact native FX on the panel (UI peeks in render loop)
        if mode in ("background", "off") and fx is not None:
            backgrounds.apply_wled(self.panel.ip, fx, spd)
            with self.lock:
                self._bg_applied = (fx, spd)
                self._bg_capture_needed = False
        elif mode not in ("off", "background") and fx is not None:
            # Game active: must capture real firmware frames (no fake stand-in)
            self._request_bg_capture(force=True)
        return {
            "background": bg, "bg_speed": spd, "fx": fx, "name": name,
            "capture": backgrounds.capture_status(),
            "pipeline": "native-wled + liveview-port (no software FX)",
        }

    def _request_bg_capture(self, force=False):
        """Flag the render loop to pause DDP and sample the real WLED effect."""
        with self.lock:
            bg = self.background
            spd = self.bg_speed
        fx = backgrounds.parse_fx(bg)
        if fx is None:
            return
        with self.lock:
            tried_at = self._bg_capture_tried.get(int(fx))
        if backgrounds.get_cached_loop(fx, spd) is not None:
            return
        # Retry cooldown (don't spin forever if panel offline) — 8s
        if not force and tried_at and (time.time() - tried_at) < 8.0:
            return
        with self.lock:
            self._bg_capture_needed = True

    def _start_bg_capture(self, fx, spd):
        """Kick the capture onto its own thread and hold DDP for its duration.

        capture_effect_loop needs the strip silent for several seconds while it
        peeks the liveview. Running it inline blocked the whole render loop, so
        the game stopped ticking and the browser preview froze with it for the
        entire capture. Off-thread, the loop keeps playing and refreshing the
        preview and merely withholds DDP.

        _bg_capture_active is set here, on the render thread, *before* the
        thread starts: if we waited for backgrounds.is_capturing() to flip we
        would send DDP for another iteration or two, and those packets keep
        WLED in realtime — the capture would then record our own game frames
        instead of the effect.
        """
        with self.lock:
            if self._bg_capture_active:
                return
            self._bg_capture_active = True

        def _job():
            try:
                self._run_bg_capture(fx, spd)
            except Exception:
                traceback.print_exc()
            finally:
                with self.lock:
                    self._bg_capture_active = False

        threading.Thread(target=_job, daemon=True,
                         name=f"bg-capture-{fx}").start()

    def _run_bg_capture(self, fx, spd):
        """Capture a real-effect loop. Runs off the render thread."""
        with self.lock:
            self._bg_capture_tried[int(fx)] = time.time()
        # LOOP_TARGET (48), not 72: capture_effect_loop already raises sparse
        # particle FX to 72 itself, so passing 72 here made *every* effect pay
        # the sparse-length capture — ~50% more time with DDP released, which
        # is time the running game is off the panel.
        backgrounds.capture_effect_loop(
            self.panel.ip, fx, spd,
            n_frames=backgrounds.LOOP_TARGET,
            settle_s=0.7,
            realtime_release_s=2.7,
            wait_for_ddp_release=True,
        )
        with self.lock:
            mode = self.mode
        if mode == "background":
            self._apply_native_bg(force=True)

    def _apply_native_bg(self, force=False):
        """Ensure Apollo is running the selected effect natively (no DDP)."""
        with self.lock:
            bg, spd = self.background, self.bg_speed
            applied = self._bg_applied
        fx = backgrounds.parse_fx(bg)
        if fx is None:
            return
        key = (fx, int(spd))
        if not force and applied == key:
            return
        if backgrounds.apply_wled(self.panel.ip, fx, spd):
            with self.lock:
                self._bg_applied = key

    def push_cast(self, frame):
        """A phone/browser sent us a rendered 64x64 frame."""
        with self.lock:
            self.cast_frame = frame
            self.cast_at = time.time()
            if self.mode != "cast":
                self.mode = "cast"
                self.last_game = "cast"

    def select_video(self, filename):
        with self.lock:
            self.video_path = str(VIDEOS_DIR / filename)
            self.video = None            # force reload with the new file
            self.video_error = None
            if self.mode != "video":
                self.mode = "video"
                self.last_game = "video"
        return self.mode == "video"

    def configure_video(self, cfg):
        with self.lock:
            self.video_cfg.update(cfg)
            v = self.video
        if v:
            try:
                v.configure(**cfg)
            except Exception as e:
                self.video_error = f"{type(e).__name__}: {e}"

    def configure_mirror(self, cfg):
        with self.lock:
            self.mirror_cfg.update(cfg)
            m = self.mirror
        if m:
            try:
                m.configure(**cfg)
            except Exception as e:
                self.mirror_error = f"{type(e).__name__}: {e}"

    def snapshot(self):
        with self.lock:
            live = self.mode != "off"
            cast_age = time.time() - self.cast_at if self.cast_at else None
            err = {"mirror": self.mirror_error, "video": self.video_error}.get(self.mode)
            return {
                "mode": self.mode,
                "on": live,
                "score": self.engine.score if self.engine else 0,
                "cart_input": getattr(self.engine, "control_scheme", None),
                # What the CURRENT engine can actually be driven with,
                # read off the live instance rather than a list the UI
                # keeps in parallel. A hardcoded list drifted the moment
                # sports gained a second browse axis: the control panel
                # kept advertising left/right only, so up/down (leagues)
                # was undiscoverable. Derived here, it cannot drift again.
                "browse": {
                    "horizontal": getattr(self.engine, "browse", None) is not None,
                    "vertical": getattr(self.engine, "browse_v", None) is not None,
                },
                "paused": self.paused,
                "err": err,
                "cast_active": bool(cast_age is not None and cast_age < CAST_TIMEOUT),
                "mirror_cfg": dict(self.mirror_cfg),
                "video_cfg": dict(self.video_cfg),
                "video_file": (Path(self.video_path).name if self.video_path else None),
                "background": self.background,
                "bg_speed": self.bg_speed,
                "stats": dict(self.stats),
                "frame": bytes(self.latest),
            }

    # ---- render/stream loop ----------------------------------------------
    def _game_frame(self, mode, eng, now, last_tick):
        launch_to = None
        with self.lock:
            if eng and not self.paused and now - last_tick >= eng.tick_rate:
                if mode.endswith("-demo"):
                    eng.auto()
                eng.tick()
                last_tick = now
                # System-chrome modes (BootEngine, MenuEngine) hand off to
                # whatever comes next by setting a plain `.launch` attribute
                # instead of calling the phone's API directly -- boot finishes
                # into the menu, the menu launches whatever SELECT/AUTO picked.
                # Real game engines never define this, so getattr is a no-op
                # for them. Completed after releasing the lock: set_mode takes
                # this same lock, so calling it while still holding it would
                # deadlock.
                launch_to = getattr(eng, "launch", None)
                if launch_to:
                    eng.launch = None
            frame = eng.frame() if eng else None
            if self.paused:
                # Composite the menu over the board as it stood when you
                # paused, not over a live frame -- some engines animate in
                # frame() and the background would keep twitching.
                self.pause_pulse += 1
                base = self.pause_bg or frame
                if base:
                    frame = engines.draw_pause_overlay(
                        base, self.pause_sel, self.pause_pulse)
            # Deliberately does NOT publish self.latest -- the render loop
            # is the single publisher, after dimming. See PUBLISH below.
        if launch_to:
            self.set_mode(launch_to)
        return frame, last_tick

    def _video_frame(self):
        if not self.video_path:
            return None
        if self.video is None:
            try:
                from video import VideoSource
                self.video = VideoSource(self.video_path, **self.video_cfg)
                self.video_error = None
            except Exception as e:
                self.video_error = f"{type(e).__name__}: {e}"
                time.sleep(0.4)
                return None
        try:
            frame = self.video.read()
            if frame is not None:
                self.video_error = None
            return frame
        except Exception as e:
            self.video_error = f"{type(e).__name__}: {e}"
            return None

    def _mirror_frame(self):
        if self.mirror is None:
            try:
                from mirror import Mirror
                cfg = dict(self.mirror_cfg)
                self.mirror = Mirror(monitor=cfg.pop("monitor", 1), **cfg)
                self.mirror_error = None
            except Exception as e:
                self.mirror_error = f"{type(e).__name__}: {e}"
                time.sleep(0.4)
                return None
        try:
            frame = self.mirror.capture()
            self.mirror_error = None
            return frame
        except Exception as e:
            self.mirror_error = f"{type(e).__name__}: {e}"
            time.sleep(0.2)
            return None

    def _severe_alert_frame(self):
        """Full-screen alert if NWS has an Extreme/Severe alert active for
        the configured location, else None.

        Reading weather.FEED here on every render tick is what keeps this
        working from any mode -- and it deliberately keeps weather's feed
        thread alive continuously (its IDLE_STOP never elapses), because a
        takeover that only knows about alerts while you're already looking
        at the weather mode would be pointless. That costs one observation
        call per 10 min and one alerts call per 2 min, always.

        Clears itself with no extra bookkeeping: when NWS stops listing the
        alert, the list empties and this returns None again on the very
        next tick.
        """
        try:
            data = weather.FEED.get()
        except Exception:                       # noqa: BLE001 - never kill the loop
            return None
        severe = [a for a in (data.get("alerts") or [])
                  if a.get("severity") in engines.GLOBAL_ALERT_SEVERITIES]
        if not severe:
            self._alert_ticks = 0
            return None
        self._alert_ticks = getattr(self, "_alert_ticks", 0) + 1
        # Rotate through multiple severe alerts rather than pinning the
        # first one, same dwell feel as the weather mode's own cycling.
        idx = (self._alert_ticks // 220) % len(severe)
        return engines.draw_alert_frame(
            severe[idx], self._alert_ticks, place=data.get("place", ""),
            n_alerts=len(severe), cur_alert=idx)

    def _loop(self):
        last_tick = time.time()
        min_send = 1.0 / PANEL_FPS
        last_sent = None
        last_send_t = 0.0
        next_t = time.time()
        while self.running:
            try:
                with self.lock:
                    mode, eng = self.mode, self.engine
                    bg_id, bg_spd = self.background, self.bg_speed

                if mode == "off":
                    last_sent = None       # force a resend when we take over again
                    time.sleep(0.1)
                    continue

                # PLANE-IN-WINDOW TAKEOVER. Cheap check every tick, same
                # cost class as flights.FEED.get()'s own cache read (this
                # is a plain list pop under a lock, not new I/O) --
                # matching the discipline _severe_alert_frame() already
                # uses for its own every-tick weather.FEED.get() read.
                # Guarded to skip "off" (already excluded above -- the
                # panel doubling as a house lamp must not be woken by a
                # plane flying by, per CLAUDE.md) and "planewatch" itself
                # (don't re-trigger a takeover already in progress).
                if mode != "planewatch":
                    batch = flights.FEED.pop_window_takeover_batch()
                    if batch:
                        self.set_mode("planewatch")
                        with self.lock:
                            mode, eng = self.mode, self.engine

                # Pause DDP and sample the real Apollo effect into a loop cache
                # so game underlays are exact (Polar_Waves ≠ Fire, Yves exact, …).
                # Never capture during pure ambient — native FX already is exact.
                with self.lock:
                    need_cap = self._bg_capture_needed and mode not in (
                        "background", "off")
                    if need_cap:
                        self._bg_capture_needed = False
                        cap_bg, cap_spd = self.background, self.bg_speed
                    elif mode in ("background", "off"):
                        self._bg_capture_needed = False
                if need_cap:
                    cap_fx = backgrounds.parse_fx(cap_bg)
                    if cap_fx is not None:
                        self._start_bg_capture(cap_fx, cap_spd)

                with self.lock:
                    holding = self._bg_capture_active
                if (holding or backgrounds.is_capturing()) and \
                        mode not in ("background", "off"):
                    # DDP must stay silent so the liveview peek sees the real
                    # effect — but there is no reason to stop *playing*. Keep
                    # the engine ticking and the web preview fresh; only the
                    # panel pauses, and only for as long as the capture runs.
                    last_sent = None
                    if mode not in ("mirror", "video", "cast"):
                        _, last_tick = self._game_frame(
                            mode, eng, time.time(), last_tick)
                    time.sleep(0.05)
                    next_t = time.time()
                    continue

                # Pure Apollo effect ambient: stop DDP so native WLED runs the FX
                if mode == "background":
                    last_sent = None
                    fx_i = backgrounds.parse_fx(bg_id)
                    now = time.time()
                    # Drift check throttled — WLED 503s if we hammer /json/state
                    cur = None  # only set on check ticks; used for verify_fx
                    if fx_i is not None and now - self._bg_fx_check_at >= 1.2:
                        self._bg_fx_check_at = now
                        cur = backgrounds.panel_fx(self.panel.ip)
                        if cur is not None and cur != fx_i:
                            backgrounds.apply_wled(self.panel.ip, fx_i, bg_spd)
                            with self.lock:
                                self._bg_applied = (fx_i, int(bg_spd))
                        else:
                            self._apply_native_bg()
                    # Exact UI = real LED buffer (firmware pixels via liveview)
                    frame = None
                    try:
                        got = backgrounds.peek_frames(
                            self.panel.ip, count=1, timeout_each=0.85)
                        if got:
                            frame = got[0]
                    except Exception:
                        frame = None
                    if frame is not None:
                        if fx_i is not None:
                            # Always verify panel fx before porting into game cache
                            # (throttled inside via panel_fx; drops Solid poison)
                            backgrounds.ingest_ambient_frame(
                                fx_i, bg_spd, frame, self.panel.ip,
                                verify_fx=True)
                        with self.lock:
                            self._bg_last_good = frame
                            self.latest = frame
                    else:
                        with self.lock:
                            hold = self._bg_last_good
                            self.latest = hold if hold is not None else bytes(FRAME_BYTES)
                    time.sleep(0.12)
                    continue

                if mode == "mirror":
                    frame = self._mirror_frame()
                elif mode == "video":
                    frame = self._video_frame()
                    if frame is None:            # not due yet -> hold last frame
                        # Hold the last RAW decoded frame, not self.latest:
                        # self.latest is dimmed now, and re-feeding it into
                        # the chain would dim it a second time on every
                        # held frame until the next decode.
                        frame = self._video_hold
                    else:
                        self._video_hold = frame
                elif mode == "cast":
                    with self.lock:
                        frame = self.cast_frame
                else:
                    frame, last_tick = self._game_frame(
                        mode, eng, time.time(), last_tick)

                # Game + Apollo: ONLY real captured firmware frames under sprites.
                # Black underlay until the port/capture finishes — never fake Fire.
                if frame is not None and mode not in ("mirror", "video", "cast"):
                    if bg_id and bg_id != "none":
                        gain = 0.48 if str(mode).endswith("-demo") else 0.36
                        fx_i = backgrounds.parse_fx(bg_id)
                        # Scale to the effect's own brightness. Headroom of
                        # 1.5x lets genuinely sparse effects (Lightning) stay
                        # punchy while full-field ones (Rainbow) get pulled
                        # right down so the sprites keep their contrast.
                        gain = backgrounds.underlay_gain(fx_i, bg_spd,
                                                         cap=gain * 1.5)
                        bg_frame = backgrounds.loop_frame(fx_i, time.time(), bg_spd)
                        if bg_frame is None:
                            bg_frame = bytes(FRAME_BYTES)
                            if fx_i is not None:
                                self._request_bg_capture()
                        frame = backgrounds.composite(
                            bg_frame, frame, threshold=14, bg_gain=gain)

                # HOME ASSISTANT NOTIFY, normal-priority banner (task #8).
                # Applied BEFORE the severe-weather takeover on purpose --
                # severe weather is life-safety and has to win outright if
                # both are somehow active at once; compositing the banner
                # after it would draw an FYI over a full-bleed severe
                # alert. Skipped for the "notify" mode itself (the urgent
                # tier already owns the whole frame; layering the ambient
                # banner under its own mode would be redundant) and for
                # None frames (background/off already `continue`d above,
                # but mirror/video error paths can still hand back None).
                if frame is not None and mode != "notify":
                    frame = self._notify_banner_frame(frame)

                # GLOBAL SEVERE-WEATHER TAKEOVER. Applied last, after every
                # other composite, so it covers whatever was about to be
                # shown -- a game, a video, a mirror, any mode. Only
                # Extreme/Severe qualify (see engines.GLOBAL_ALERT_SEVERITIES):
                # a routine advisory interrupting a game would train someone
                # to ignore the panel exactly when it finally matters.
                # Skipped in weather mode, which is already showing alerts
                # itself and cycles through ALL of them, not just severe.
                alert_frame = self._severe_alert_frame() if mode != "weather" else None
                if alert_frame is not None:
                    frame = alert_frame

                # MODE TRANSITION. Applied to the composed frame so it works
                # for every mode uniformly -- games included -- and needs no
                # cooperation from any engine.
                #
                # A severe-alert takeover is deliberately NOT transitioned:
                # it is an interrupt, and sliding it in gently would soften
                # exactly the moment that is supposed to feel abrupt.
                if frame is not None and alert_frame is None and self._trans_from:
                    if self._trans_i < TRANSITION_FRAMES:
                        p = (self._trans_i + 1) / float(TRANSITION_FRAMES)
                        style = self._trans_style or transitions.DEFAULT_STYLE
                        frame = transitions.blend(self._trans_from, frame, p, style=style)
                        self._trans_i += 1
                    else:
                        self._trans_from = None

                # FIRST LIGHT. The panel blooms up out of black on takeover
                # rather than snapping on fully lit.
                if frame is not None and self._wake_i < transitions.WAKE_FRAMES:
                    frame = transitions.wake(frame, self._wake_i)
                    self._wake_i += 1

                # TIME-OF-DAY DIMMING. Applied last, to whatever is about to
                # be sent, so it covers every mode uniformly -- a game at
                # 3am dims exactly like the clock does.
                #
                # SEVERE ALERTS ARE EXEMPT, deliberately: the entire point
                # of the global takeover is to grab attention, and a
                # tornado warning quietly dimmed to 28% at 3am would defeat
                # it at precisely the hour it matters most.
                #
                # self.latest is dimmed too, so the web preview and phone
                # remote show what the panel actually looks like rather
                # than a bright frame the panel never displayed.
                if frame is not None and alert_frame is None:
                    level = brightness.level_now()
                    if level < 0.999:
                        frame = brightness.apply(frame, level)

                # PUBLISH. Exactly one assignment to self.latest per pass,
                # after every composite AND after dimming, so /api/state can
                # never hand out a half-composited or undimmed frame. Each
                # stage above used to publish its own intermediate result,
                # which left a microsecond window where the web preview and
                # phone remote could sample a bright frame the panel never
                # actually displayed.
                if frame is not None:
                    with self.lock:
                        self.latest = frame

                # Push when the image CHANGED (never faster than PANEL_FPS), or
                # when the keepalive is due so WLED can't time out of realtime
                # and snap back to its own effect under a static picture.
                now = time.time()
                due = (frame != last_sent and now - last_send_t >= min_send) or \
                      (now - last_send_t >= KEEPALIVE_S)
                if frame is not None and due:
                    if self.panel.send(frame):
                        last_sent = frame
                        last_send_t = now
                    with self.lock:
                        self.stats["sent"] = self.panel.sent
                        self.stats["errors"] = self.panel.errors

                anim_bg = (bg_id and bg_id != "none"
                           and mode not in ("mirror", "video", "cast", "off"))
                loop_dt = 1.0 / (CAPTURE_FPS if mode in ("mirror", "video") or anim_bg
                                 else RENDER_FPS)
                next_t += loop_dt
                slack = next_t - time.time()
                if slack > 0:
                    time.sleep(slack)
                elif slack < -0.5:
                    next_t = time.time()      # fell far behind; resync, don't spin
            except Exception:
                # A bad frame must never kill the arcade. Log and keep going.
                self.stats["loop_errors"] += 1
                traceback.print_exc()
                time.sleep(0.25)


def _safe_filename(name):
    """Strip anything but a bare filename so an upload/select can't escape VIDEOS_DIR."""
    name = Path(name).name.strip()
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return None
    return name


ARCADE = Arcade()


def _page():
    """Read from disk each request so UI edits show on refresh (no restart)."""
    return (HERE / "arcade.html").read_bytes()


def _remote_page():
    return (HERE / "remote.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                      # phone navigated away mid-response

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def do_OPTIONS(self):
        self._send(204, "text/plain", b"")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/arcade"):
            self._send(200, "text/html; charset=utf-8", _page())
        elif path in ("/remote", "/controller", "/play"):
            self._send(200, "text/html; charset=utf-8", _remote_page())
        elif path == "/api/state":
            s = ARCADE.snapshot()
            s["rgb"] = base64.b64encode(s.pop("frame")).decode()
            s["w"], s["h"] = WIDTH, HEIGHT
            self._json(s)
        elif path == "/api/ticker/symbols":
            crypto, stocks = market.FEED.get_symbols()
            self._json({"crypto": crypto, "stocks": stocks,
                        "known_crypto": sorted(market.SYMBOL_TO_COINGECKO_ID)})
        elif path == "/api/satellite/location":
            lat, lon, label = satellite.FEED.get_location()
            self._json({"lat": lat, "lon": lon, "label": label,
                        "configured": satellite.FEED.configured})
        elif path == "/api/window":
            # Window bearing/FOV config -- confirmed genuinely missing
            # before adding (satellite.py already owns load_window()/
            # save_window() and location_config.json; there was simply no
            # HTTP surface for it yet). Same shape as /api/satellite/
            # location just above.
            w = satellite.load_window()
            self._json({"center_deg": w["center_deg"], "fov_deg": w["fov_deg"],
                        "max_nm": w["max_nm"]})
        elif path == "/api/sports/config":
            leagues, favorite = sports.FEED.get_config()
            u = sports.FEED.get_universal()
            pinned = u.get("golf_pinned")
            favorite_teams, favorite_teams_filter = sports.FEED.get_favorite_teams()
            self._json({"leagues": leagues, "favorite": favorite,
                        "known_leagues": sorted(sports.LEAGUE_PATHS),
                        "golf_player": sports.FEED.get_golf_player(),
                        # Enough to show whether the pinned name actually
                        # matched anyone on a live leaderboard, rather than
                        # silently saving a name that will never resolve.
                        "golf_status": ({"name": pinned["abbr"],
                                         "place": pinned["place"],
                                         "score": pinned["score"],
                                         "thru": pinned["thru"],
                                         "event": (u.get("golf_event") or {}).get("name")}
                                        if pinned else None),
                        "golf_live": sorted({e["league"] for e in u.get("events") or []
                                             if e.get("leaderboard")}),
                        "tennis_player": sports.FEED.get_tennis_player(),
                        # Same "did the pinned name actually resolve" shape
                        # golf_status already established above.
                        "tennis_status": ({"name": u["tennis_pinned"]["abbr"],
                                           "opponent": next(
                                               (c["abbr"] for c in
                                                (u.get("tennis_event") or {}).get("competitors", [])
                                                if c.get("id") != u["tennis_pinned"].get("id")), None),
                                           "state": (u.get("tennis_event") or {}).get("state"),
                                           "event": (u.get("tennis_event") or {}).get("name")}
                                          if u.get("tennis_pinned") else None),
                        "favorite_teams": favorite_teams,
                        "favorite_teams_filter": favorite_teams_filter})
        elif path == "/api/gameday/config":
            cfg = gameday.load_config()
            cfg["targets"] = list(gameday.TARGETS)
            # Enough to show what tonight actually is, without the caller
            # having to know which feed backs which target.
            if cfg["target"] == "ufc":
                d = mma.FEED.get()
                card = d.get("card") or {}
                cfg["event"] = {"name": card.get("name"), "total": card.get("total"),
                                "done": card.get("done"), "live": card.get("live"),
                                "completed": card.get("completed"),
                                "next": d.get("next_label"), "err": d.get("err")}
            else:
                g = (sports.FEED.get() or {}).get("favorite_game")
                cfg["event"] = {"game": g["short_name"] if g else None,
                                "state": g["state"] if g else None}
            self._json(cfg)
        elif path == "/api/weather/current":
            # Read-only: weather has no config of its own -- it reuses the
            # ISS/flights home location via /api/satellite/location.
            self._json(weather.FEED.get())
        elif path == "/api/brightness":
            cfg = brightness.load_config()
            cfg["current_level"] = round(brightness.level_now(cfg), 3)
            self._json(cfg)
        elif path == "/api/blog/config":
            self._json({"api_url": blog.FEED.get_config(),
                        "default_api_url": blog.DEFAULT_API_URL})
        elif path == "/api/news/config":
            feed_url, label = news.FEED.get_config()
            self._json({"feed_url": feed_url, "label": label,
                        "default_feed_url": news.DEFAULT_FEED_URL})
        elif path == "/api/frame":                    # legacy shape
            s = ARCADE.snapshot()
            self._json({"w": WIDTH, "h": HEIGHT, "mode": s["mode"],
                        "score": s["score"], "err": s["err"],
                        "rgb": base64.b64encode(s["frame"]).decode()})
        elif path == "/api/screenshot":
            # A still of a display so the UI can let you drag out a capture region.
            # Fresh mss instance per request: mss objects are not thread-safe and
            # ThreadingHTTPServer hands each request its own thread.
            try:
                import io
                import mss as _mss
                from PIL import Image
                mon = int(parse_qs(urlparse(self.path).query).get("monitor", ["1"])[0])
                with _mss.mss() as sct:
                    m = dict(sct.monitors[mon])
                    raw = sct.grab(m)
                shot = Image.frombytes("RGB", raw.size, raw.rgb)
                shot.thumbnail((720, 720))
                buf = io.BytesIO()
                shot.save(buf, "JPEG", quality=70)
                self._json({"left": m["left"], "top": m["top"],
                            "width": m["width"], "height": m["height"],
                            "thumb": "data:image/jpeg;base64," +
                                     base64.b64encode(buf.getvalue()).decode()})
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif path == "/api/videos":
            files = sorted(
                (p.name for p in VIDEOS_DIR.iterdir()
                 if p.is_file() and p.suffix.lower() in VIDEO_EXTS),
                key=lambda n: (VIDEOS_DIR / n).stat().st_mtime, reverse=True)
            self._json({"videos": files})
        elif path == "/api/displays":
            try:
                import mirror as _m
                self._json({"displays": _m.list_displays()})
            except Exception as e:
                self._json({"displays": [], "error": f"{type(e).__name__}: {e}"})
        elif path == "/api/panel-window":
            try:
                import panelwin
                self._json({"panel": panelwin.panel_display(),
                            "apps": panelwin.apps(),
                            "sizes": panelwin.PANEL_SIZES,
                            "size": panelwin.current_size()})
            except Exception as e:
                self._json({"panel": None, "apps": [],
                            "error": f"{type(e).__name__}: {e}"})
        elif path == "/api/backgrounds":
            modes = backgrounds.list_modes(ARCADE.panel.ip)
            fx = backgrounds.parse_fx(ARCADE.background)
            self._json({
                "modes": modes,
                "background": ARCADE.background,
                "bg_speed": ARCADE.bg_speed,
                "fx": fx,
                "name": backgrounds.effect_name(fx) if fx is not None else "none",
                "source": f"Apollo M-1 @ {ARCADE.panel.ip}",
                "count": max(0, len(modes) - 1),
                "capture": backgrounds.capture_status(),
                "pipeline": "native-wled + liveview-port only (no software FX)",
            })
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        n = int(self.headers.get("Content-Length") or 0)
        # Every non-upload endpoint here expects a small JSON body or none
        # at all. Reading `n` bytes unconditionally meant ANY POST -- not
        # just /api/upload-video -- could force an arbitrarily large read
        # into memory just by lying about Content-Length; only the upload
        # path itself was ever bounds-checked (and only AFTER the read).
        # Cap the generic path at 4MB (a raw uncompressed 64x64 frame via
        # /api/cast is 12288 bytes, so this is generous headroom for any
        # real client) and let only the upload endpoint go larger, still
        # bounded by MAX_UPLOAD_BYTES.
        if parsed.path == "/api/upload-video":
            if n > MAX_UPLOAD_BYTES:
                self._json({"ok": False, "error": "file too large (800MB max)"}, 413)
                return
            body = self.rfile.read(n) if n else b""
        else:
            cap = 4 * 1024 * 1024
            if n > cap:
                self._json({"ok": False, "error": "request body too large"}, 413)
                return
            body = self.rfile.read(n) if n else b""

        if len(parts) == 3 and parts[:2] == ["api", "mode"]:
            ARCADE.set_mode(unquote(parts[2]))
            self._json({"ok": True})
        elif len(parts) == 3 and parts[:2] == ["api", "input"]:
            ARCADE.send_input(parts[2])
            self._json({"ok": True})
        elif len(parts) == 3 and parts[:2] == ["api", "press"]:
            # Held-button state for fantasy-console carts (a platformer needs
            # to see a real hold, not one tap) -- native games only ever get
            # send_input taps, since they don't define press()/release().
            ARCADE.send_press(parts[2])
            self._json({"ok": True})
        elif len(parts) == 3 and parts[:2] == ["api", "release"]:
            ARCADE.send_release(parts[2])
            self._json({"ok": True})
        elif parsed.path == "/api/ticker/symbols":
            # Config-driven watchlist: the production device needs per-owner
            # symbols, and there is no reason a Mac install should need a
            # code edit either. Rejects are reported back rather than
            # silently dropped, so a typo doesn't just vanish.
            try:
                j = json.loads(body or b"{}")
                accepted, rejected = market.FEED.set_symbols(
                    j.get("crypto", []), j.get("stocks", []))
                resp = {"ok": True, "crypto": accepted, "stocks": j.get("stocks", [])}
                if rejected:
                    resp["rejected"] = rejected
                    resp["note"] = "unrecognized crypto symbol(s), not added: " + ", ".join(rejected)
                self._json(resp)
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/satellite/location":
            try:
                j = json.loads(body or b"{}")
                lat, lon, label = satellite.FEED.set_location(
                    j["lat"], j["lon"], j.get("label", "HOME"))
                self._json({"ok": True, "lat": lat, "lon": lon, "label": label})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/window":
            try:
                j = json.loads(body or b"{}")
                center = float(j["center_deg"])
                fov = float(j["fov_deg"])
                # max_nm is optional on write -- omitting it keeps whatever
                # was already configured (or the reasoned default on first
                # save) rather than forcing every caller to know/send it,
                # same "configurable but not mandatory" shape center_deg/
                # fov_deg already had before this key existed.
                max_nm = float(j["max_nm"]) if "max_nm" in j else None
                w = satellite.save_window(center, fov, max_nm)
                self._json({"ok": True, **w})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/notify":
            # HOME ASSISTANT NOTIFICATION PASS-THROUGH (task #8). Payload
            # mirrors HA's real `notify` service shape: {title, message,
            # data: {priority}}. The ONE endpoint in this project with a
            # real auth check -- see notify.py's module docstring for why
            # a spoofable notify endpoint is worth the exception to the
            # project's usual trusted-LAN no-auth posture.
            if not notify.check_token(self.headers.get("X-Arcade-Token")):
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                j = json.loads(body or b"{}")
                title = j["title"]
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("title required")
                message = j.get("message", "")
                # Same "malformed values fall back to defaults" convention
                # /api/brightness already uses: anything other than the
                # two recognized values -- missing, wrong type, a typo --
                # falls back to "normal" rather than rejecting the request
                # or guessing which tier was meant.
                data = j.get("data") or {}
                priority = data.get("priority") if isinstance(data, dict) else None
                if priority not in ("normal", "urgent"):
                    priority = "normal"
                # Fold at the boundary -- the one place external text
                # enters this endpoint. Never displays anything the real
                # POST body didn't contain; only ever fit/wrapped, never
                # fabricated.
                title_f = paneltext.panel_text(title)
                message_f = paneltext.panel_text(message)
                # RECENT EVENTS LOG (events_log.py) -- record right after
                # the fold, using the same real folded title/message the
                # banner/takeover itself will draw. Recorded regardless of
                # whether trigger_notify() actually displays it (e.g. the
                # panel is off) -- a real notification was genuinely
                # received, which is the event this log is about, not
                # whether the panel happened to be awake to show it.
                summary = f"{title_f}: {message_f}" if message_f else title_f
                events_log.LOG.record("notify", summary)
                ok = ARCADE.trigger_notify(priority, title_f, message_f)
                self._json({"ok": ok, "priority": priority,
                            "title": title_f, "message": message_f})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/test/planewatch":
            # MANUAL DEMO TRIGGER (2026-08-09), PERSONAL-RIG-ONLY -- same
            # scope precedent as atc.py's transcription feature. Exists
            # because the real plane-in-window takeover only ever fires
            # when a real aircraft crosses the configured window cone,
            # which the owner may not have caught happening organically
            # yet -- there was no way to actually SEE the feature on the
            # real panel without waiting for real traffic.
            #
            # Never fabricates an aircraft: pulls the closest REAL
            # currently-tracked aircraft from flights.FEED's own live
            # cache (the exact same data the radar scope is already
            # drawing right now) and seeds it into the SAME real one-shot
            # slot (flights.FEED._window_batch) the real window-entry
            # detector fills -- this is "manually satisfy the trigger
            # condition for a real aircraft", not a synthetic render. The
            # render loop's own existing planewatch check (_loop(), see
            # its "PLANE-IN-WINDOW TAKEOVER" comment) pops it and swaps
            # modes on its own very next tick, so this reuses that real
            # path end to end rather than duplicating any trigger logic.
            aircraft = flights.FEED.get().get("aircraft") or []
            with_dist = [a for a in aircraft if isinstance(a.get("dist_nm"), (int, float))]
            if not with_dist:
                self._json({"ok": False, "error": "no tracked aircraft right now"}, 404)
                return
            closest = min(with_dist, key=lambda a: a["dist_nm"])
            with flights.FEED._lock:
                flights.FEED._window_batch = [closest]
            self._json({"ok": True, "reg": closest.get("reg"),
                        "ident": closest.get("ident"), "dist_nm": closest.get("dist_nm")})
        elif parsed.path == "/api/sports/leagues":
            try:
                j = json.loads(body or b"{}")
                accepted, rejected = sports.FEED.set_leagues(j.get("leagues", []))
                resp = {"ok": True, "leagues": accepted}
                if rejected:
                    resp["rejected"] = rejected
                    resp["note"] = "unrecognized league(s), not added: " + ", ".join(rejected)
                self._json(resp)
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/sports/golf_player":
            try:
                j = json.loads(body or b"{}")
                name = sports.FEED.set_golf_player(j.get("name"))
                self._json({"ok": True, "golf_player": name})
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/sports/tennis_player":
            try:
                j = json.loads(body or b"{}")
                name = sports.FEED.set_tennis_player(j.get("name"))
                self._json({"ok": True, "tennis_player": name})
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/sports/favorite":
            try:
                j = json.loads(body or b"{}")
                if not j.get("league") or not j.get("team_abbr"):
                    sports.FEED.clear_favorite()
                    self._json({"ok": True, "favorite": None})
                else:
                    favorite = sports.FEED.set_favorite(j["league"], j["team_abbr"])
                    if favorite is None:
                        self._json({"ok": False, "error": "unknown league"}, 400)
                    else:
                        self._json({"ok": True, "favorite": favorite})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/sports/favorite_teams":
            # Multi-team ticker filter -- a SEPARATE mechanism from
            # /api/sports/favorite (ONE pinned team, its own full-screen
            # view). This is a cross-sport watchlist that filters the
            # ROTATING ticker; see event_matches_favorite_teams()'s own
            # docstring in sports.py for why the two don't share a field.
            try:
                j = json.loads(body or b"{}")
                teams = j.get("teams")
                if not isinstance(teams, list):
                    raise ValueError("teams must be a list of {league, team_abbr}")
                for t in teams:
                    if (not isinstance(t, dict) or t.get("league") not in sports.LEAGUE_PATHS
                            or not t.get("team_abbr")):
                        raise ValueError(f"invalid team entry: {t!r}")
                cleaned, enabled = sports.FEED.set_favorite_teams(
                    teams, bool(j.get("filter_enabled", True)))
                self._json({"ok": True, "favorite_teams": cleaned,
                            "favorite_teams_filter": enabled})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/gameday/config":
            try:
                j = json.loads(body or b"{}")
                if "target" in j and str(j["target"]).lower() not in gameday.TARGETS:
                    self._json({"ok": False,
                                "error": "target must be one of " + ", ".join(gameday.TARGETS)}, 400)
                else:
                    self._json({"ok": True, "config": gameday.save_config(j)})
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/brightness":
            try:
                j = json.loads(body or b"{}")
                cfg = brightness.save_config(j)
                cfg["current_level"] = round(brightness.level_now(cfg), 3)
                self._json({"ok": True, **cfg})
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/blog/source":
            try:
                j = json.loads(body or b"{}")
                if blog.FEED.set_api_url(j.get("api_url", "")):
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "api_url required"}, 400)
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/news/source":
            try:
                j = json.loads(body or b"{}")
                ok = news.FEED.set_source(j.get("feed_url", ""), j.get("label", ""))
                if ok:
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "feed_url required"}, 400)
            except (ValueError, AttributeError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif parsed.path == "/api/pause" or (
                len(parts) == 3 and parts[:2] == ["api", "pause"]):
            arg = parts[2] if len(parts) == 3 else ""
            want = {"on": True, "off": False}.get(arg)   # anything else toggles
            self._json({"ok": True, "paused": ARCADE.set_paused(want)})
        elif parsed.path == "/api/pointer":
            # Absolute touch position for mouse-driven carts (e.g. a
            # drawing/painting toy) -- fx, fy are fractions 0..1 of the
            # touchpad, mapped 1:1 onto the cart's drawable canvas.
            try:
                j = json.loads(body or b"{}")
                ARCADE.send_pointer(float(j["fx"]), float(j["fy"]), bool(j.get("down")))
                self._json({"ok": True})
            except (ValueError, KeyError, TypeError):
                self._json({"ok": False, "error": "expected {fx, fy, down}"}, 400)
        elif len(parts) == 3 and parts[:2] == ["api", "mouseright"]:
            ARCADE.send_mouse_right(parts[2] == "press")
            self._json({"ok": True})
        elif len(parts) == 3 and parts[:2] == ["api", "wheel"]:
            ARCADE.send_wheel(1 if parts[2] == "up" else -1)
            self._json({"ok": True})
        elif parsed.path == "/api/cast":
            if len(body) == FRAME_BYTES:
                ARCADE.push_cast(body)
                self._json({"ok": True})
            else:
                self._json({"ok": False,
                            "error": f"expected {FRAME_BYTES} bytes, got {len(body)}"}, 400)
        elif len(parts) == 3 and parts[:2] == ["api", "video-select"]:
            name = _safe_filename(parts[2])
            if name and (VIDEOS_DIR / name).is_file():
                ARCADE.select_video(name)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "file not found"}, 404)
        elif parsed.path == "/api/upload-video":
            name = _safe_filename(parse_qs(parsed.query).get("name", [""])[0])
            ext = Path(name).suffix.lower()
            if not name or ext not in VIDEO_EXTS:
                self._json({"ok": False, "error": "unsupported file type"}, 400)
            elif n > MAX_UPLOAD_BYTES:
                self._json({"ok": False, "error": "file too large (800MB max)"}, 413)
            else:
                (VIDEOS_DIR / name).write_bytes(body)
                self._json({"ok": True, "filename": name})
        elif parsed.path == "/api/video":
            try:
                cfg = json.loads(body or b"{}")
            except ValueError:
                cfg = {}
            for k in ("saturation", "contrast"):
                if k in cfg:
                    cfg[k] = float(cfg[k])
            if "autocrop" in cfg:
                cfg["autocrop"] = bool(cfg["autocrop"])
            ARCADE.configure_video(cfg)
            self._json({"ok": True, "cfg": ARCADE.video_cfg})
        elif parsed.path == "/api/mirror":
            try:
                cfg = json.loads(body or b"{}")
            except ValueError:
                cfg = {}
            for k in ("saturation", "contrast"):
                if k in cfg:
                    cfg[k] = float(cfg[k])
            if "monitor" in cfg:
                cfg["monitor"] = int(cfg["monitor"])
            if "cursor" in cfg:
                cfg["cursor"] = bool(cfg["cursor"])
            if "cursor_size" in cfg:
                cfg["cursor_size"] = float(cfg["cursor_size"])
            if "region" in cfg and cfg["region"]:
                cfg["region"] = {k: int(v) for k, v in cfg["region"].items()
                                 if k in ("left", "top", "width", "height")}
            ARCADE.configure_mirror(cfg)
            self._json({"ok": True, "cfg": ARCADE.mirror_cfg})
        elif parsed.path in ("/api/panel-window/send", "/api/panel-window/back",
                             "/api/panel-size"):
            try:
                cfg = json.loads(body or b"{}")
            except ValueError:
                cfg = {}
            try:
                import panelwin
                if parsed.path.endswith("/send"):
                    res = panelwin.send(cfg.get("app") or None)
                elif parsed.path.endswith("/back"):
                    res = panelwin.recall(cfg.get("app") or None)
                else:
                    res = panelwin.set_panel_size(cfg.get("size"))
                    # The virtual screen just resized, which also shifts the
                    # origin of every display left of it — force a re-read.
                    ARCADE.configure_mirror({"monitor": ARCADE.mirror_cfg["monitor"]})
                self._json({"ok": True, **res})
            except Exception as e:
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        elif parsed.path == "/api/background":
            try:
                cfg = json.loads(body or b"{}")
            except ValueError:
                cfg = {}
            mode_id = cfg.get("id") if "id" in cfg else cfg.get("background", "none")
            if mode_id is None:
                mode_id = "none"
            res = ARCADE.set_background(mode_id, cfg.get("speed"))
            # Ambient-only: real Apollo FX (no DDP). Games pause DDP briefly to
            # capture a real effect loop, then composite that under sprites.
            if ARCADE.mode in ("off", "background") and str(mode_id) not in ("", "none"):
                ARCADE.set_mode("background")
            elif ARCADE.mode == "background" and str(mode_id) in ("", "none"):
                ARCADE.set_mode("off")
            self._json({"ok": True, **res})
        else:
            self._send(404, "text/plain", b"not found")


def main():
    print("🎮  Arcade server running.")
    print(f"    Panel:        {ARCADE.panel.ip}")
    print(f"    Control page: http://localhost:{PORT}")
    print(f"    From phone:   http://<this-mac-LAN-ip>:{PORT}/remote")
    print(f"    Starts:       {DEFAULT_MODE} (resting state; POST /api/mode/off releases to WLED)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
