"""
display.py -- the one place that knows HOW pixels reach a panel.

Everything above this line (engines.py, every mode) is pure Python: a mode
computes a WIDTH*HEIGHT*3 RGB buffer and knows nothing about the hardware
underneath. Everything below it is transport. That split is the whole point:
the same mode code runs on the Mac setup (WLED-MM panel over DDP/UDP) and,
later, on the production device (Raspberry Pi driving a HUB75 panel through
an Adafruit Bonnet), with only the renderer swapped.

A renderer is anything with:
    send(rgb: bytes) -> bool     # one full frame; False on failure, never raises
    close() -> None

`send` returning False rather than raising is deliberate and load-bearing:
a dropped frame must never take down the render loop.

IMPORTANT -- the rate limiting and frame de-duplication live ABOVE this layer,
in arcade_server's render loop, not in here. Both panels need them for
different reasons (WLED locks up if flooded and needs a physical power cycle;
a Bonnet-driven panel wastes CPU it does not have), so they stay in the shared
loop rather than being reimplemented per renderer.
"""
import os

from wled_ddp import WledDDP

RENDERER = os.environ.get("ARCADE_RENDERER", "ddp").strip().lower()


class BonnetRenderer:
    """Raspberry Pi + Adafruit RGB Matrix Bonnet + HUB75 panel(s).

    Deliberately NOT implemented yet. This is a placeholder that fails loudly
    with instructions rather than a stub that silently pretends to work --
    there is no Pi hardware in the loop to test against, and a renderer that
    quietly no-ops would be worse than one that refuses to start.

    When the hardware arrives, this wraps hzeller's rpi-rgb-led-matrix Python
    bindings: build an RGBMatrix + FrameCanvas once, then in send() copy the
    RGB buffer into the canvas and SwapOnVSync it. The chain/parallel options
    are what map a flat 64x64 buffer onto tiled 32x32 panels, so panel-count
    differences get handled here and stay invisible to the modes.
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "The Bonnet renderer is not built yet -- no Pi hardware in the loop.\n"
            "On the Pi this needs hzeller/rpi-rgb-led-matrix installed, then this\n"
            "class implemented against RGBMatrix/FrameCanvas. Until then run with\n"
            "ARCADE_RENDERER=ddp (the default)."
        )


def get_renderer(name=None, **kwargs):
    """Return the renderer for this host. Modes never call this -- only the
    runner does, exactly once at startup."""
    name = (name or RENDERER).strip().lower()
    if name in ("ddp", "wled", "wled-mm"):
        return WledDDP(**kwargs)
    if name in ("bonnet", "hub75", "pi", "rgbmatrix"):
        return BonnetRenderer(**kwargs)
    raise ValueError(
        f"unknown ARCADE_RENDERER {name!r}; expected 'ddp' or 'bonnet'"
    )
