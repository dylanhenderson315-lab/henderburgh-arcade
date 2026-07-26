"""
video.py — loop a local video file straight to the panel.

Deliberately separate from mirror.py: this decodes the file directly with
OpenCV, so there is no screen capture, no window-hunting, no Screen Recording
permission, and no risk of catching the wrong window. This is the clean way
to "play a cartoon on the panel" — mirror.py is for live windows/emulators,
this is for a file you already have.

Requires opencv-python-headless (installed in the arcade venv).
"""
import time

import cv2
from PIL import Image

import imgproc

WIDTH, HEIGHT = imgproc.WIDTH, imgproc.HEIGHT


class VideoSource:
    def __init__(self, path, fit="fill", saturation=1.3, contrast=1.1,
                 gamma=True, autocrop=False):
        self.path = str(path)
        self.fit = fit
        self.saturation = saturation
        self.contrast = contrast
        self.gamma = gamma
        self.autocrop = autocrop
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open video: {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 24
        self.frame_dt = 1.0 / max(min(fps, 60), 1)
        self._next_due = 0.0

    def configure(self, **kw):
        for k, v in kw.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)

    def read(self):
        """Return a processed 64x64 RGB frame paced to the file's own fps, or
        None if it isn't time for the next frame yet (caller keeps the last one)."""
        now = time.time()
        if now < self._next_due:
            return None
        ok, frame = self.cap.read()
        if not ok:                                          # end of file -> loop
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
            if not ok:
                return None                                  # unreadable/empty file
        self._next_due = now + self.frame_dt
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        return imgproc.process(img, fit=self.fit, saturation=self.saturation,
                                contrast=self.contrast, gamma=self.gamma,
                                autocrop=self.autocrop)

    def close(self):
        self.cap.release()
