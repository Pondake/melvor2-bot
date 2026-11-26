"""Text recognition with the OCR engine built into Windows (no external install)."""
import asyncio
from dataclasses import dataclass

import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter


@dataclass
class Line:
    text: str
    x: int  # bounding box in source-image coordinates
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def __repr__(self) -> str:
        return f"Line({self.text!r} @ {self.x},{self.y} {self.w}x{self.h})"


def _make_engine() -> OcrEngine:
    for tag in ("en-US", "en-GB"):
        lang = Language(tag)
        if OcrEngine.is_language_supported(lang):
            return OcrEngine.try_create_from_language(lang)
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("No Windows OCR language installed")
    return engine


_engine = _make_engine()
_loop = asyncio.new_event_loop()


def read(img: np.ndarray, region: tuple[int, int, int, int] | None = None, scale: float = 1.0) -> list[Line]:
    """OCR an RGB image (optionally a region x, y, w, h) and return text lines with boxes."""
    ox, oy = 0, 0
    if region:
        ox, oy, rw, rh = region
        img = img[oy:oy + rh, ox:ox + rw]
    if scale != 1.0:
        import cv2
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    h, w = img.shape[:2]
    rgba = np.dstack([img, np.full((h, w), 255, np.uint8)])
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.RGBA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    result = _loop.run_until_complete(_engine.recognize_async(bmp))
    lines = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        x0 = min(wd.bounding_rect.x for wd in words)
        y0 = min(wd.bounding_rect.y for wd in words)
        x1 = max(wd.bounding_rect.x + wd.bounding_rect.width for wd in words)
        y1 = max(wd.bounding_rect.y + wd.bounding_rect.height for wd in words)
        lines.append(Line(
            text=line.text,
            x=int(x0 / scale) + ox, y=int(y0 / scale) + oy,
            w=int((x1 - x0) / scale), h=int((y1 - y0) / scale),
        ))
    return lines
