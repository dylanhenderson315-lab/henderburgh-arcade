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
from engines import ENGINES, WIDTH, HEIGHT, TicCartEngine, CARTS_DIR
from display import get_renderer
import backgrounds

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
DEFAULT_MODE = "off"               # start released; user Turns On to take over
RESUME_GAME = "boot"                # "Turn On" always plays the boot curtain, then the menu
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
        self.engine = ENGINES["snake"]()
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
                self.latest = self.engine.frame()
            elif base in ENGINES:
                self.engine = ENGINES[base]()
                if base == "menu" and self.last_game and self.last_game != "menu":
                    # Land the cursor on whatever you actually played last,
                    # instead of always resetting to the first tile.
                    self.engine._select(self.last_game.split("-")[0])
                self.latest = self.engine.frame()

        # Do panel HTTP outside the lock — it must never stall the render loop.
        if was_off and mode != "off":
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
            # Assign AFTER compositing: self.latest is what /api/state serves
            # and what both pages preview, so setting it to the raw frame
            # first would show the panel and the UI two different things.
            if frame:
                self.latest = frame
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
                with self.lock:
                    self.latest = frame
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
            with self.lock:
                self.latest = frame
            return frame
        except Exception as e:
            self.mirror_error = f"{type(e).__name__}: {e}"
            time.sleep(0.2)
            return None

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
                        with self.lock:
                            frame = self.latest
                elif mode == "cast":
                    with self.lock:
                        frame = self.cast_frame
                        if frame:
                            self.latest = frame
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
    print(f"    Starts:       {DEFAULT_MODE} (panel released; Turn On to take over)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
