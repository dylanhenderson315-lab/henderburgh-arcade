"""
imgproc.py — shared 64x64 image pipeline: crop, downscale, colour, gamma.

Used by mirror.py (screen capture) and video.py (file playback) so a live
window and a video file get identical, tuned treatment on the panel. Squeezing
anything into 64x64 loses almost all detail, so colour and contrast have to
carry the image — see process() below.
"""
from PIL import Image, ImageEnhance

from wled_ddp import GAMMA_LUT

try:
    BOX = Image.Resampling.BOX
except AttributeError:                                   # Pillow < 9.1
    BOX = Image.BOX

WIDTH = HEIGHT = 64


def crop_letterbox(img, thresh=18):
    """Drop near-black borders (video letterboxing / window chrome)."""
    bbox = img.convert("L").point(lambda p: 255 if p > thresh else 0).getbbox()
    if not bbox:
        return img
    w, h = img.size
    cw, ch = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if cw < w * 0.35 or ch < h * 0.35 or (cw > w * 0.97 and ch > h * 0.97):
        return img                          # mostly-black frame, or nothing to gain
    return img.crop(bbox)


def to_square(img, fit="fill"):
    w, h = img.size
    if fit == "fit":                        # letterbox: whole image, black bars
        img = img.copy()
        img.thumbnail((WIDTH, HEIGHT), Image.LANCZOS)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        canvas.paste(img, ((WIDTH - img.width) // 2, (HEIGHT - img.height) // 2))
        return canvas
    s = min(w, h)                           # fill: centre-crop to square
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    return img.resize((WIDTH, HEIGHT), BOX)


def process(img, fit="fill", saturation=1.35, contrast=1.15,
            gamma=True, autocrop=True):
    """Full-size PIL image -> 64x64 RGB bytes ready for the panel."""
    if autocrop:
        img = crop_letterbox(img)
    img = to_square(img, fit)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    data = img.tobytes()
    if gamma:
        # bytes.translate does this whole 12288-byte LUT pass in C. The
        # generator form it replaces cost 0.287 ms/frame against 0.003 ms —
        # 90x — for byte-identical output, and it ran on every mirror and
        # video frame at CAPTURE_FPS.
        data = data.translate(GAMMA_LUT)
    return data
