"""
Thin ctypes wrapper around the headless tic80_libretro.dylib build
(vendor/tic80_libretro.dylib -- built with BUILD_SDL=OFF, BUILD_STATIC=ON,
BUILD_LIBRETRO=ON; see the Fantasy Console integration plan for build steps).

No window, no SDL, no audio device -- this talks to the TIC-80 core purely
through the standard libretro callback API and hands back raw framebuffer
bytes, exactly the shape every native engine's frame() already returns.

Two ctypes gotchas fixed here, worth knowing if this file is ever touched:
  - BUILD_STATIC must be ON, or scripting languages (Lua etc) never get
    linked in and the core silently falls back to a null-callback stub
    that segfaults on the first tick.
  - Callback return types must never be c_bool -- ctypes CFUNCTYPE callbacks
    returning c_bool corrupt the stack on macOS/arm64. Use c_ubyte and
    plain 0/1 instead.
"""
import ctypes as C
import pathlib

HERE = pathlib.Path(__file__).parent
DYLIB_PATH = HERE / "vendor" / "tic80_libretro.dylib"

RETRO_ENVIRONMENT_SET_PIXEL_FORMAT = 10
RETRO_ENVIRONMENT_GET_VARIABLE = 15
RETRO_ENVIRONMENT_SET_FRAME_TIME_CALLBACK = 21

RETRO_DEVICE_JOYPAD = 1
RETRO_DEVICE_MOUSE = 2
RETRO_DEVICE_POINTER = 6
RETRO_DEVICE_ID_JOYPAD_B = 0
RETRO_DEVICE_ID_JOYPAD_Y = 1
RETRO_DEVICE_ID_JOYPAD_SELECT = 2
RETRO_DEVICE_ID_JOYPAD_START = 3
RETRO_DEVICE_ID_JOYPAD_UP = 4
RETRO_DEVICE_ID_JOYPAD_DOWN = 5
RETRO_DEVICE_ID_JOYPAD_LEFT = 6
RETRO_DEVICE_ID_JOYPAD_RIGHT = 7
RETRO_DEVICE_ID_JOYPAD_A = 8
RETRO_DEVICE_ID_JOYPAD_X = 9
RETRO_DEVICE_ID_MOUSE_LEFT = 2
RETRO_DEVICE_ID_MOUSE_RIGHT = 3
RETRO_DEVICE_ID_MOUSE_WHEELUP = 4
RETRO_DEVICE_ID_MOUSE_WHEELDOWN = 5
RETRO_DEVICE_ID_POINTER_X = 0
RETRO_DEVICE_ID_POINTER_Y = 1
RETRO_DEVICE_ID_POINTER_PRESSED = 2

TIC80_WIDTH = 240
TIC80_HEIGHT = 136
TIC80_FULLWIDTH = 256
TIC80_FULLHEIGHT = 144
TIC80_MARGIN_LEFT = (TIC80_FULLWIDTH - TIC80_WIDTH) // 2    # 8
TIC80_MARGIN_TOP = (TIC80_FULLHEIGHT - TIC80_HEIGHT) // 2   # 4

retro_environment_t = C.CFUNCTYPE(C.c_ubyte, C.c_uint, C.c_void_p)
retro_video_refresh_t = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
retro_audio_sample_t = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
retro_audio_sample_batch_t = C.CFUNCTYPE(C.c_size_t, C.POINTER(C.c_int16), C.c_size_t)
retro_input_poll_t = C.CFUNCTYPE(None)
retro_input_state_t = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


class retro_game_info(C.Structure):
    _fields_ = [
        ("path", C.c_char_p), ("data", C.c_void_p),
        ("size", C.c_size_t), ("meta", C.c_char_p),
    ]


class retro_game_geometry(C.Structure):
    _fields_ = [
        ("base_width", C.c_uint), ("base_height", C.c_uint),
        ("max_width", C.c_uint), ("max_height", C.c_uint),
        ("aspect_ratio", C.c_float),
    ]


class retro_system_timing(C.Structure):
    _fields_ = [("fps", C.c_double), ("sample_rate", C.c_double)]


class retro_system_av_info(C.Structure):
    _fields_ = [("geometry", retro_game_geometry), ("timing", retro_system_timing)]


class retro_variable(C.Structure):
    _fields_ = [("key", C.c_char_p), ("value", C.c_char_p)]


# Two core variables we care about: crop TIC-80's bezel-padded 256x144
# framebuffer down to its real 240x136 drawable area (so we downscale
# exactly the pixels a cart actually draws), and treat the pointer channel
# as an absolute touchscreen rather than a relative mouse. The latter costs
# nothing for gamepad-only carts (they never call mouse()) and is what
# makes mouse-driven carts (drawing/painting toys, point-and-click) usable
# at all from a phone -- see TicCore.set_pointer below.
_CORE_VARIABLES = {
    b"tic80_crop_border": b"enabled",
    b"tic80_pointer_device": b"touchscreen",
}


class TicCore:
    """
    A libretro-hosted TIC-80 core. There is exactly one of these alive at
    a time in the arcade process -- like every other engine, only one game
    runs at once, which happens to match libretro's own single-instance
    model perfectly.
    """

    def __init__(self):
        self.lib = C.CDLL(str(DYLIB_PATH))
        self._frame = {}
        self._buttons = {}
        self._loaded = False
        # Pointer/mouse state for mouse-driven carts. (fx, fy) are fractions
        # 0..1 of the drawable 240x136 canvas -- absolute position, not a
        # relative delta, because the player is looking at the panel while
        # dragging on the phone and expects the cursor to land exactly
        # where their finger is proportionally, like any external pointing
        # device used while looking at a separate display.
        self._pointer_fx = 0.5
        self._pointer_fy = 0.5
        self._pointer_pressed = False
        self._mouse_right = False
        self._wheel = 0   # +1/-1 one-shot tick, consumed at end of tick()

        self._c_environ = retro_environment_t(self._environ_cb)
        self._c_video = retro_video_refresh_t(self._video_cb)
        self._c_audio = retro_audio_sample_t(lambda l, r: None)
        self._c_audio_batch = retro_audio_sample_batch_t(lambda data, frames: frames)
        self._c_input_poll = retro_input_poll_t(lambda: None)
        self._c_input_state = retro_input_state_t(self._input_state_cb)

        self.lib.retro_set_environment(self._c_environ)
        self.lib.retro_set_video_refresh(self._c_video)
        self.lib.retro_set_audio_sample(self._c_audio)
        self.lib.retro_set_audio_sample_batch(self._c_audio_batch)
        self.lib.retro_set_input_poll(self._c_input_poll)
        self.lib.retro_set_input_state(self._c_input_state)
        self.lib.retro_load_game.restype = C.c_bool

        self.lib.retro_init()

    def _environ_cb(self, cmd, data):
        if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
            return 1  # XRGB8888, exactly what the core requests
        if cmd == RETRO_ENVIRONMENT_SET_FRAME_TIME_CALLBACK:
            return 1  # accepted; we don't need to drive it ourselves
        if cmd == RETRO_ENVIRONMENT_GET_VARIABLE:
            var = C.cast(data, C.POINTER(retro_variable)).contents
            val = _CORE_VARIABLES.get(var.key)
            if val is not None:
                var.value = val
                return 1
            return 0
        return 0

    def _video_cb(self, data, width, height, pitch):
        if not data:
            return
        self._frame = {
            "w": width, "h": height, "pitch": pitch,
            "bytes": C.string_at(data, pitch * height),
        }

    def _input_state_cb(self, port, device, index, id_):
        if port == 0 and device == RETRO_DEVICE_JOYPAD:
            return 1 if self._buttons.get(id_) else 0
        if port == 0 and device == RETRO_DEVICE_POINTER:
            if id_ == RETRO_DEVICE_ID_POINTER_X:
                return self._pointer_axis(self._pointer_fx, TIC80_WIDTH, TIC80_FULLWIDTH, TIC80_MARGIN_LEFT)
            if id_ == RETRO_DEVICE_ID_POINTER_Y:
                return self._pointer_axis(self._pointer_fy, TIC80_HEIGHT, TIC80_FULLHEIGHT, TIC80_MARGIN_TOP)
            if id_ == RETRO_DEVICE_ID_POINTER_PRESSED:
                return 1 if self._pointer_pressed else 0
        if port == 0 and device == RETRO_DEVICE_MOUSE:
            if id_ == RETRO_DEVICE_ID_MOUSE_RIGHT:
                return 1 if self._mouse_right else 0
            if id_ == RETRO_DEVICE_ID_MOUSE_WHEELUP:
                return 1 if self._wheel > 0 else 0
            if id_ == RETRO_DEVICE_ID_MOUSE_WHEELDOWN:
                return 1 if self._wheel < 0 else 0
        return 0

    @staticmethod
    def _pointer_axis(frac, drawable, full, margin):
        # Exact inverse of the core's own pointer->screen conversion
        # (tic80_libretro_mouse_pointer_convert in tic80_libretro.c), so a
        # touch at fraction `frac` of the drawable canvas lands on that
        # same pixel once the core converts it back.
        frac = max(0.0, min(1.0, frac))
        screen_coord = frac * (drawable - 1)
        coord = ((screen_coord + margin) / full) * 65534 - 32767
        return int(max(-32767, min(32767, round(coord))))

    def load(self, cart_bytes: bytes, path: str = "cart.tic"):
        if self._loaded:
            self.lib.retro_unload_game()
        buf = C.create_string_buffer(cart_bytes, len(cart_bytes))
        info = retro_game_info(
            path=path.encode(), data=C.cast(buf, C.c_void_p),
            size=len(cart_bytes), meta=None,
        )
        self._keepalive_buf = buf  # must outlive the retro_load_game call
        ok = self.lib.retro_load_game(C.byref(info))
        self._loaded = bool(ok)
        return self._loaded

    def unload(self):
        if self._loaded:
            self.lib.retro_unload_game()
            self._loaded = False

    def set_buttons(self, held: dict):
        """held: {RETRO_DEVICE_ID_JOYPAD_*: bool}"""
        self._buttons = held

    def set_pointer(self, fx, fy, pressed):
        """fx, fy: fraction (0..1) of the drawable canvas; pressed: bool."""
        self._pointer_fx, self._pointer_fy, self._pointer_pressed = fx, fy, pressed

    def set_mouse_right(self, held: bool):
        self._mouse_right = held

    def pulse_wheel(self, direction: int):
        """direction: +1 or -1, consumed by the next tick() only."""
        self._wheel = direction

    def tick(self):
        self.lib.retro_run()
        self._wheel = 0  # one-shot: the core already saw it this frame

    def raw_frame(self):
        """Returns (width, height, pitch, XRGB8888 bytes) straight from the
        core -- callers sample directly out of this rather than converting
        every source pixel, since a downscale only ever needs a handful of
        them."""
        f = self._frame
        if not f:
            return TIC80_WIDTH, TIC80_HEIGHT, TIC80_WIDTH * 4, None
        return f["w"], f["h"], f["pitch"], f["bytes"]
