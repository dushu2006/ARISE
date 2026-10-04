"""Screenshot capture, crop/downscale, deduplication, OCR, and multimodal vision grounding.

All visual and OCR outputs are untrusted perception candidates that feed into
``TargetResolver``. Coordinate fallback fails closed whenever DPI, focus,
observation freshness, or explicit coordinate opt-in is missing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import sys
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from arise.core.computer import (
    CapturedImage,
    ComputerFailureCode,
    CoordinateMapper,
    CoordinateSpace,
    DisplayGeometry,
    GroundingProposal,
    OCRText,
    PerceptionSource,
    Point,
    Rect,
    ResolutionStatus,
    SelectorQuality,
    TargetCandidate,
    TargetDescriptor,
    TargetQuery,
    TargetResolution,
    WindowRecord,
)
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import TargetIdentity, utc_now, validate_safe_token
from arise.core.grounding import TargetResolver
from arise.core.model_gateway import ModelRouter
from arise.core.models import ModelMessage, ModelRequest, ModelRole, ModelSelectionRequest
from arise.core.redaction import DEFAULT_REDACTOR

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_DIMENSION = 4096
_MAX_DEDUP_ENTRIES = 64


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def encode_rgba_png(width: int, height: int, rgba_bytes: bytes) -> bytes:
    """Encode raw RGBA8888 pixels into a valid lossless PNG byte stream."""

    if not 1 <= width <= _MAX_IMAGE_DIMENSION or not 1 <= height <= _MAX_IMAGE_DIMENSION:
        raise ValueError("PNG dimensions must be between 1 and 4096")
    expected_len = width * height * 4
    if len(rgba_bytes) != expected_len:
        raise ValueError(f"RGBA buffer length {len(rgba_bytes)} does not match {expected_len}")
    stride = width * 4
    raw_scanlines = bytearray((stride + 1) * height)
    for y in range(height):
        row_start = y * (stride + 1)
        raw_scanlines[row_start] = 0  # Filter type 0 (None)
        src_start = y * stride
        raw_scanlines[row_start + 1 : row_start + 1 + stride] = rgba_bytes[
            src_start : src_start + stride
        ]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_scanlines), level=6)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def decode_rgba_png(png_bytes: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGBA or RGB PNG (filter 0) into (width, height, rgba_bytes)."""

    if not png_bytes.startswith(_PNG_SIGNATURE):
        raise ValueError("invalid PNG signature")
    offset = len(_PNG_SIGNATURE)
    width = 0
    height = 0
    color_type = 6
    idat_parts: list[bytes] = []
    while offset + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        chunk_data = png_bytes[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data[:13]
            )
            if bit_depth != 8 or comp != 0 or filt != 0 or interlace != 0:
                raise ValueError("unsupported PNG bit depth or interlace mode")
            if color_type not in {2, 6}:
                raise ValueError("only RGB and RGBA PNGs are supported")
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break
    if width <= 0 or height <= 0 or not idat_parts:
        raise ValueError("incomplete PNG stream")
    decompressed = zlib.decompress(b"".join(idat_parts))
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    if len(decompressed) < (stride + 1) * height:
        raise ValueError("truncated PNG scanline data")
    out = bytearray(width * height * 4)
    prev_row = bytearray(stride)
    for y in range(height):
        row_offset = y * (stride + 1)
        filter_type = decompressed[row_offset]
        scanline = bytearray(decompressed[row_offset + 1 : row_offset + 1 + stride])
        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                scanline[i] = (scanline[i] + scanline[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                scanline[i] = (scanline[i] + prev_row[i]) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported PNG scanline filter {filter_type}")
        prev_row = scanline
        dst_row = y * width * 4
        if channels == 4:
            out[dst_row : dst_row + stride] = scanline
        else:
            for x in range(width):
                src_px = x * 3
                dst_px = dst_row + x * 4
                out[dst_px] = scanline[src_px]
                out[dst_px + 1] = scanline[src_px + 1]
                out[dst_px + 2] = scanline[src_px + 2]
                out[dst_px + 3] = 255
    return width, height, bytes(out)


def make_captured_image_from_rgba(
    width: int,
    height: int,
    rgba_bytes: bytes,
    *,
    bounds: Rect,
    display_id: str | None = None,
) -> CapturedImage:
    """Create a validated ``CapturedImage`` from raw RGBA pixels."""

    png_bytes = encode_rgba_png(width, height, rgba_bytes)
    digest = hashlib.sha256(png_bytes).hexdigest()
    return CapturedImage(
        content=png_bytes,
        content_type="image/png",
        width=width,
        height=height,
        bounds=bounds,
        captured_at=utc_now(),
        sha256=digest,
        display_id=display_id,
    )


def crop_captured_image(image: CapturedImage, crop_rect: Rect) -> CapturedImage:
    """Crop a ``CapturedImage`` using either image-pixel or physical-desktop bounds."""

    width, height, rgba = decode_rgba_png(image.content)
    # Determine whether crop_rect is in physical desktop space or local image pixel space
    if (
        image.bounds.x <= crop_rect.x
        and image.bounds.y <= crop_rect.y
        and crop_rect.right <= image.bounds.right
        and crop_rect.bottom <= image.bounds.bottom
        and (image.bounds.x != 0 or image.bounds.y != 0 or image.bounds.width != width)
    ):
        top_left = CoordinateMapper.physical_to_image(
            Point(crop_rect.x, crop_rect.y),
            image_width=width,
            image_height=height,
            capture_bounds=image.bounds,
        )
        bottom_right = CoordinateMapper.physical_to_image(
            Point(crop_rect.right, crop_rect.bottom),
            image_width=width,
            image_height=height,
            capture_bounds=image.bounds,
        )
        x0 = max(0, min(width - 1, int(round(top_left.x))))
        y0 = max(0, min(height - 1, int(round(top_left.y))))
        x1 = max(x0 + 1, min(width, int(round(bottom_right.x))))
        y1 = max(y0 + 1, min(height, int(round(bottom_right.y))))
        physical_bounds = crop_rect
    else:
        if (
            crop_rect.x < 0
            or crop_rect.y < 0
            or crop_rect.right > width
            or crop_rect.bottom > height
        ):
            raise ValueError("crop_rect is outside the captured image dimensions")
        x0 = int(round(crop_rect.x))
        y0 = int(round(crop_rect.y))
        x1 = max(x0 + 1, int(round(crop_rect.right)))
        y1 = max(y0 + 1, int(round(crop_rect.bottom)))
        tl_phys = CoordinateMapper.image_to_physical(
            Point(float(x0), float(y0)),
            image_width=width,
            image_height=height,
            capture_bounds=image.bounds,
        )
        br_phys = CoordinateMapper.image_to_physical(
            Point(float(x1), float(y1)),
            image_width=width,
            image_height=height,
            capture_bounds=image.bounds,
        )
        physical_bounds = Rect(
            tl_phys.x,
            tl_phys.y,
            max(1.0, br_phys.x - tl_phys.x),
            max(1.0, br_phys.y - tl_phys.y),
        )

    crop_w = x1 - x0
    crop_h = y1 - y0
    cropped = bytearray(crop_w * crop_h * 4)
    for row in range(crop_h):
        src_start = ((y0 + row) * width + x0) * 4
        dst_start = row * crop_w * 4
        cropped[dst_start : dst_start + crop_w * 4] = rgba[src_start : src_start + crop_w * 4]
    return make_captured_image_from_rgba(
        crop_w,
        crop_h,
        bytes(cropped),
        bounds=physical_bounds,
        display_id=image.display_id,
    )


def downscale_captured_image(image: CapturedImage, *, max_dimension: int) -> CapturedImage:
    """Downscale a ``CapturedImage`` preserving aspect ratio and physical bounds mapping."""

    if not 1 <= max_dimension <= _MAX_IMAGE_DIMENSION:
        raise ValueError(f"max_dimension must be between 1 and {_MAX_IMAGE_DIMENSION}")
    if image.width <= max_dimension and image.height <= max_dimension:
        return image
    width, height, rgba = decode_rgba_png(image.content)
    scale = min(max_dimension / width, max_dimension / height)
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    out = bytearray(new_w * new_h * 4)
    for ny in range(new_h):
        sy = min(height - 1, int(ny / scale))
        for nx in range(new_w):
            sx = min(width - 1, int(nx / scale))
            src_idx = (sy * width + sx) * 4
            dst_idx = (ny * new_w + nx) * 4
            out[dst_idx : dst_idx + 4] = rgba[src_idx : src_idx + 4]
    return make_captured_image_from_rgba(
        new_w,
        new_h,
        bytes(out),
        bounds=image.bounds,
        display_id=image.display_id,
    )


class ScreenshotDeduplicator:
    """Bounded deduplicator avoiding redundant OCR/vision calls on unchanged frames."""

    def __init__(self, *, max_entries: int = _MAX_DEDUP_ENTRIES) -> None:
        if not 1 <= max_entries <= 512:
            raise ValueError("max_entries must be between 1 and 512")
        self.max_entries = max_entries
        self._by_scope: OrderedDict[str, CapturedImage] = OrderedDict()
        self._dedup_hits = 0

    @property
    def dedup_hits(self) -> int:
        return self._dedup_hits

    def record_or_reuse(self, scope: str, image: CapturedImage) -> tuple[CapturedImage, bool]:
        """Return ``(canonical_image, was_duplicate)`` for the given capture scope."""

        previous = self._by_scope.get(scope)
        if (
            previous is not None
            and previous.sha256 == image.sha256
            and previous.bounds == image.bounds
        ):
            self._by_scope.move_to_end(scope)
            self._dedup_hits += 1
            return previous, True
        self._by_scope[scope] = image
        self._by_scope.move_to_end(scope)
        while len(self._by_scope) > self.max_entries:
            self._by_scope.popitem(last=False)
        return image, False


class RawCaptureBackend(Protocol):
    """Platform screenshot capture backend."""

    async def capture_rect(
        self, bounds: Rect, *, display_id: str | None = None
    ) -> tuple[int, int, bytes]: ...


class Win32ScreenCaptureBackend:
    """Win32 GDI BitBlt screen capture backend on Windows hosts."""

    def __init__(self, *, user32: Any | None = None, gdi32: Any | None = None) -> None:
        self._user32 = user32
        self._gdi32 = gdi32

    async def capture_rect(
        self, bounds: Rect, *, display_id: str | None = None
    ) -> tuple[int, int, bytes]:
        del display_id
        if sys.platform != "win32" and (self._user32 is None or self._gdi32 is None):
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "Desktop screen capture requires a supported Windows desktop session.",
                source=PerceptionSource.VISION,
            )
        return await asyncio.to_thread(self._sync_capture_rect, bounds)

    def _sync_capture_rect(self, bounds: Rect) -> tuple[int, int, bytes]:
        import ctypes
        from ctypes import wintypes

        width = max(1, min(_MAX_IMAGE_DIMENSION, int(round(bounds.width))))
        height = max(1, min(_MAX_IMAGE_DIMENSION, int(round(bounds.height))))
        left = int(round(bounds.x))
        top = int(round(bounds.y))

        user32 = self._user32 or ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = self._gdi32 or ctypes.WinDLL("gdi32", use_last_error=True)

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        srccopy = 0x00CC0020
        dib_rgb_colors = 0
        screen_dc = user32.GetDC(0)
        if not screen_dc:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "Interactive desktop session required for GDI screen capture.",
                source=PerceptionSource.VISION,
            )
        mem_dc = 0
        bmp = 0
        old_obj = 0
        try:
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            bmp = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not mem_dc or not bmp:
                raise ComputerAdapterError(
                    ComputerFailureCode.CAPTURE_FAILED,
                    "Failed to allocate Win32 GDI compatible bitmap.",
                    source=PerceptionSource.VISION,
                )
            old_obj = gdi32.SelectObject(mem_dc, bmp)
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, srccopy):
                raise ComputerAdapterError(
                    ComputerFailureCode.CAPTURE_FAILED,
                    "Win32 GDI BitBlt failed during desktop capture.",
                    source=PerceptionSource.VISION,
                )
            header = BitmapInfoHeader()
            header.biSize = ctypes.sizeof(BitmapInfoHeader)
            header.biWidth = width
            header.biHeight = -height  # top-down DIB
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            buf_len = width * height * 4
            bgra_buf = ctypes.create_string_buffer(buf_len)
            scanlines = gdi32.GetDIBits(
                mem_dc,
                bmp,
                0,
                height,
                bgra_buf,
                ctypes.byref(header),
                dib_rgb_colors,
            )
            if not scanlines:
                raise ComputerAdapterError(
                    ComputerFailureCode.CAPTURE_FAILED,
                    "Win32 GDI GetDIBits failed to read pixel buffer.",
                    source=PerceptionSource.VISION,
                )
            raw = bytearray(bgra_buf.raw)
            # Convert BGRA -> RGBA in place
            raw[0::4], raw[2::4] = raw[2::4], raw[0::4]
            return width, height, bytes(raw)
        finally:
            if mem_dc and old_obj:
                gdi32.SelectObject(mem_dc, old_obj)
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)


class ScreenCaptureAdapter:
    """Bounded screenshot provider with active-window/region capture, downscale, and dedup."""

    def __init__(
        self,
        *,
        backend: RawCaptureBackend | None = None,
        displays_fn: Callable[[], Any] | None = None,
        windows_fn: Callable[[], Any] | None = None,
        foreground_window_fn: Callable[[], Any] | None = None,
        deduplicator: ScreenshotDeduplicator | None = None,
    ) -> None:
        self._backend: RawCaptureBackend = backend or Win32ScreenCaptureBackend()
        self._displays_fn = displays_fn
        self._windows_fn = windows_fn
        self._foreground_window_fn = foreground_window_fn
        self.deduplicator = deduplicator or ScreenshotDeduplicator()

    async def capture_desktop(self, *, max_dimension: int = _MAX_IMAGE_DIMENSION) -> CapturedImage:
        displays = await self._resolve_displays()
        min_x = min(d.physical_bounds.x for d in displays)
        min_y = min(d.physical_bounds.y for d in displays)
        max_r = max(d.physical_bounds.right for d in displays)
        max_b = max(d.physical_bounds.bottom for d in displays)
        bounds = Rect(min_x, min_y, max_r - min_x, max_b - min_y)
        return await self._capture_and_process(
            scope="desktop",
            bounds=bounds,
            display_id=None,
            max_dimension=max_dimension,
        )

    async def capture_screen(self, *, max_dimension: int = _MAX_IMAGE_DIMENSION) -> CapturedImage:
        return await self.capture_desktop(max_dimension=max_dimension)

    async def capture_display(
        self, display_id: str, *, max_dimension: int = _MAX_IMAGE_DIMENSION
    ) -> CapturedImage:
        validate_safe_token(display_id, "display_id")
        displays = await self._resolve_displays()
        target = next((d for d in displays if d.display_id == display_id), None)
        if target is None:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Requested display_id was not found.",
                source=PerceptionSource.VISION,
            )
        return await self._capture_and_process(
            scope=f"display:{display_id}",
            bounds=target.physical_bounds,
            display_id=display_id,
            max_dimension=max_dimension,
        )

    async def capture_window(
        self, window_id: str, *, max_dimension: int = _MAX_IMAGE_DIMENSION
    ) -> CapturedImage:
        validate_safe_token(window_id, "window_id")
        windows = await self._resolve_windows()
        target = next((w for w in windows if w.window_id == window_id), None)
        if target is None or target.bounds is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Requested window_id was not found or has no measurable bounds.",
                source=PerceptionSource.VISION,
            )
        return await self._capture_and_process(
            scope=f"window:{window_id}",
            bounds=target.bounds,
            display_id=None,
            max_dimension=max_dimension,
        )

    async def capture_active_window(
        self, *, max_dimension: int = _MAX_IMAGE_DIMENSION
    ) -> CapturedImage:
        if self._foreground_window_fn is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Foreground window provider is not configured.",
                source=PerceptionSource.VISION,
            )
        fg: WindowRecord | None = await self._foreground_window_fn()
        if fg is None or fg.bounds is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "No foreground window with valid bounds is active.",
                source=PerceptionSource.VISION,
            )
        return await self._capture_and_process(
            scope=f"window:{fg.window_id}",
            bounds=fg.bounds,
            display_id=None,
            max_dimension=max_dimension,
        )

    async def capture_region(
        self,
        bounds: Rect,
        *,
        display_id: str | None = None,
        max_dimension: int = _MAX_IMAGE_DIMENSION,
    ) -> CapturedImage:
        if display_id is not None:
            validate_safe_token(display_id, "display_id")
        scope = (
            f"region:{display_id or 'desktop'}:{bounds.x},{bounds.y},{bounds.width},{bounds.height}"
        )
        return await self._capture_and_process(
            scope=scope,
            bounds=bounds,
            display_id=display_id,
            max_dimension=max_dimension,
        )

    async def _capture_and_process(
        self,
        *,
        scope: str,
        bounds: Rect,
        display_id: str | None,
        max_dimension: int,
    ) -> CapturedImage:
        width, height, rgba = await self._backend.capture_rect(bounds, display_id=display_id)
        raw_image = make_captured_image_from_rgba(
            width,
            height,
            rgba,
            bounds=bounds,
            display_id=display_id,
        )
        scaled = downscale_captured_image(raw_image, max_dimension=max_dimension)
        canonical, _was_dup = self.deduplicator.record_or_reuse(scope, scaled)
        return canonical

    async def _resolve_displays(self) -> Sequence[DisplayGeometry]:
        if self._displays_fn is None:
            return (
                DisplayGeometry(
                    display_id="display-primary",
                    physical_bounds=Rect(0, 0, 1920, 1080),
                    dpi_x=96.0,
                    dpi_y=96.0,
                    primary=True,
                ),
            )
        return tuple(await self._displays_fn())

    async def _resolve_windows(self) -> Sequence[WindowRecord]:
        if self._windows_fn is None:
            return ()
        return tuple(await self._windows_fn())


class OcrBackend(Protocol):
    """Low-level OCR engine protocol."""

    async def extract_text(
        self, image: CapturedImage, *, language: str | None = None
    ) -> Sequence[OCRText]: ...


class OcrPerceptionAdapter:
    """Bounded OCR perception provider with deduplicated caching and coordinate mapping."""

    def __init__(
        self,
        *,
        backend: OcrBackend | None = None,
        model_router: ModelRouter | None = None,
        cache_size: int = 32,
    ) -> None:
        self._backend = backend
        self._router = model_router
        self._cache: OrderedDict[tuple[str, str | None], tuple[OCRText, ...]] = OrderedDict()
        self._cache_size = max(1, min(cache_size, 256))
        self._cache_hits = 0

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    async def recognize(
        self,
        image: CapturedImage,
        *,
        language: str | None = None,
        region: Rect | None = None,
    ) -> Sequence[OCRText]:
        target_image = crop_captured_image(image, region) if region is not None else image
        cache_key = (target_image.sha256, language)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            self._cache_hits += 1
            return cached

        if self._backend is not None:
            raw_items = await self._backend.extract_text(target_image, language=language)
        elif self._router is not None:
            raw_items = await self._recognize_via_router(target_image, language=language)
        else:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "No OCR backend or OCR model route is configured.",
                source=PerceptionSource.OCR_LAYOUT,
            )

        sanitized: list[OCRText] = []
        for item in list(raw_items)[:512]:
            clean_text = DEFAULT_REDACTOR.redact(item.text).strip()[:512]
            if not clean_text:
                continue
            physical_bounds = self._map_box_to_physical(item.bounds, target_image)
            sanitized.append(
                OCRText(
                    text=clean_text,
                    bounds=physical_bounds,
                    confidence=item.confidence,
                    language=item.language or language,
                    source=PerceptionSource.OCR_LAYOUT,
                )
            )
        result = tuple(sanitized)
        self._cache[cache_key] = result
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    def candidates_from_ocr(
        self,
        items: Sequence[OCRText],
        *,
        observation_id: str,
        platform: str = "windows",
        application: str | None = None,
        window_id: str | None = None,
        page_id: str | None = None,
    ) -> tuple[TargetCandidate, ...]:
        validate_safe_token(observation_id, "observation_id")
        observed_at = utc_now()
        candidates: list[TargetCandidate] = []
        for idx, item in enumerate(items):
            stable_id = f"ocr-{idx}-{hashlib.sha256(item.text.encode('utf-8')).hexdigest()[:10]}"
            identity = TargetIdentity(
                platform=platform,
                application=application,
                window_id=window_id,
                page_id=page_id,
                object_id=stable_id,
                role="text",
                semantic_name=item.text,
                stable_id=stable_id,
                locator={"text": item.text, "source": "ocr_layout"},
                bounds=(item.bounds.x, item.bounds.y, item.bounds.width, item.bounds.height),
                confidence=item.confidence,
            )
            descriptor = TargetDescriptor(
                identity=identity,
                source=PerceptionSource.OCR_LAYOUT,
                observed_at=observed_at,
                observation_id=observation_id,
                bounds=item.bounds,
                coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP,
                selector_quality=SelectorQuality.EXACT_TEXT,
                visible=True,
                enabled=True,
                automation_id=None,
            )
            candidates.append(
                TargetCandidate(
                    descriptor=descriptor,
                    confidence=item.confidence,
                    evidence=("OCR text layout extraction",),
                )
            )
        return tuple(candidates)

    async def _recognize_via_router(
        self, image: CapturedImage, *, language: str | None
    ) -> Sequence[OCRText]:
        assert self._router is not None
        encoded = base64.b64encode(image.content).decode("ascii")
        request = ModelRequest(
            role=ModelRole.OCR,
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "Extract visible UI text labels and bounding boxes in image pixel "
                        "coordinates as JSON: "
                        '{"items":[{"text":"...","bounds":[x,y,w,h],"confidence":0.95}]}.'
                    ),
                ),
                ModelMessage(
                    role="user",
                    content=(
                        {
                            "type": "text",
                            "text": f"Extract OCR text (language={language or 'en'}).",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image.content_type};base64,{encoded}"},
                        },
                    ),
                ),
            ),
            stream=False,
            timeout_seconds=20.0,
            required_modalities=frozenset({"text", "image"}),
        )
        selection = ModelSelectionRequest(
            role=ModelRole.OCR,
            task_type="ocr_extraction",
            required_modalities=frozenset({"text", "image"}),
        )
        response = await self._router.complete(request, selection=selection)
        payload = json.loads(response.content)
        raw_items = payload.get("items", ()) if isinstance(payload, Mapping) else ()
        parsed: list[OCRText] = []
        for entry in raw_items:
            if not isinstance(entry, Mapping):
                continue
            text = str(entry.get("text", "")).strip()
            bounds_raw = entry.get("bounds")
            conf = float(entry.get("confidence", 0.8))
            if text and isinstance(bounds_raw, Sequence) and len(bounds_raw) == 4:
                rect = Rect(*(float(v) for v in bounds_raw))
                parsed.append(OCRText(text=text, bounds=rect, confidence=conf, language=language))
        return parsed

    @staticmethod
    def _map_box_to_physical(box: Rect, image: CapturedImage) -> Rect:
        if (
            0 <= box.x <= image.width
            and 0 <= box.y <= image.height
            and box.right <= image.width
            and box.bottom <= image.height
            and (image.bounds.x != 0 or image.bounds.y != 0 or image.bounds.width != image.width)
        ):
            tl = CoordinateMapper.image_to_physical(
                Point(box.x, box.y),
                image_width=image.width,
                image_height=image.height,
                capture_bounds=image.bounds,
            )
            br = CoordinateMapper.image_to_physical(
                Point(box.right, box.bottom),
                image_width=image.width,
                image_height=image.height,
                capture_bounds=image.bounds,
            )
            return Rect(
                tl.x,
                tl.y,
                max(1.0, br.x - tl.x),
                max(1.0, br.y - tl.y),
            )
        return box


class VisionBackend(Protocol):
    """Low-level vision grounding backend protocol."""

    async def propose_grounding(
        self,
        image: CapturedImage,
        *,
        question: str,
        candidates: Sequence[TargetCandidate] = (),
        timeout_seconds: float = 30.0,
    ) -> GroundingProposal: ...


class VisionGroundingAdapter:
    """Multimodal vision grounding adapter with confidence gating and target verification."""

    def __init__(
        self,
        *,
        backend: VisionBackend | None = None,
        model_router: ModelRouter | None = None,
        minimum_confidence: float = 0.75,
        cache_size: int = 32,
    ) -> None:
        if not 0.5 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence for vision grounding must be between 0.5 and 1.0")
        self._backend = backend
        self._router = model_router
        self.minimum_confidence = minimum_confidence
        self._cache: OrderedDict[tuple[str, str], GroundingProposal] = OrderedDict()
        self._cache_size = max(1, min(cache_size, 256))

    async def ground(
        self,
        image_or_query: CapturedImage | TargetQuery,
        *,
        question: str | None = None,
        image: CapturedImage | None = None,
        candidates: Sequence[TargetCandidate] = (),
        timeout_seconds: float = 30.0,
    ) -> GroundingProposal:
        if isinstance(image_or_query, TargetQuery):
            if image is None:
                raise ValueError("image is required when grounding a TargetQuery")
            target_image = image
            prompt_question = question or image_or_query.semantic_name
        else:
            target_image = image_or_query
            prompt_question = question or ""
        if not prompt_question.strip():
            raise ValueError("vision grounding question must be non-empty")

        cache_key = (target_image.sha256, prompt_question.strip().casefold())
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        if self._backend is not None:
            proposal = await self._backend.propose_grounding(
                target_image,
                question=prompt_question,
                candidates=candidates,
                timeout_seconds=timeout_seconds,
            )
        elif self._router is not None:
            proposal = await self._ground_via_router(
                target_image,
                question=prompt_question,
                candidates=candidates,
                timeout_seconds=timeout_seconds,
            )
        else:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "No multimodal vision provider or vision route is configured.",
                source=PerceptionSource.VISION,
            )

        self._cache[cache_key] = proposal
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return proposal

    def verify_and_build_candidate(
        self,
        proposal: GroundingProposal,
        *,
        image: CapturedImage,
        query: TargetQuery,
        corroborating_candidates: Sequence[TargetCandidate] = (),
        corroborating_ocr: Sequence[OCRText] = (),
        unsafe_regions: tuple[Rect, ...] = (),
        require_corroboration: bool = False,
    ) -> TargetCandidate:
        """Validate vision confidence, geometry, and optional corroboration before creation."""

        required_conf = max(self.minimum_confidence, query.minimum_confidence)
        if proposal.confidence < required_conf:
            raise ComputerAdapterError(
                ComputerFailureCode.VERIFICATION_FAILED,
                f"Vision grounding confidence {proposal.confidence:.2f} is below "
                f"required {required_conf:.2f}.",
                source=PerceptionSource.VISION,
            )
        physical_bounds = OcrPerceptionAdapter._map_box_to_physical(proposal.bounds, image)
        if not image.bounds.intersects(physical_bounds):
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                "Vision grounding bounds lie outside the captured window/display region.",
                source=PerceptionSource.VISION,
            )
        try:
            _safe_point = CoordinateMapper.safe_click_point(
                physical_bounds, unsafe_regions=unsafe_regions
            )
        except ValueError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                f"Vision grounding bounds intersect an unsafe region ({exc}).",
                source=PerceptionSource.VISION,
            ) from exc

        corroborated = any(
            c.descriptor.bounds is not None and c.descriptor.bounds.intersects(physical_bounds)
            for c in corroborating_candidates
        ) or any(o.bounds.intersects(physical_bounds) for o in corroborating_ocr)
        if require_corroboration and not corroborated:
            raise ComputerAdapterError(
                ComputerFailureCode.VERIFICATION_FAILED,
                "Vision grounding proposal lacks corroborating OCR or structural layout evidence.",
                source=PerceptionSource.VISION,
            )

        evidence = list(proposal.evidence[:14])
        evidence.append("vision target bounds verified")
        if corroborated:
            evidence.append("corroborated by layout/OCR overlap")

        stable_id = (
            f"vision-{hashlib.sha256(proposal.target_description.encode('utf-8')).hexdigest()[:10]}"
        )
        identity = TargetIdentity(
            platform="browser" if query.page_id is not None else "windows",
            application=query.application,
            process_id=query.process_id,
            window_id=query.window_id,
            page_id=query.page_id,
            object_id=stable_id,
            role=query.role or "button",
            semantic_name=DEFAULT_REDACTOR.redact(proposal.target_description)[:256],
            stable_id=stable_id,
            locator={
                "visual_description": DEFAULT_REDACTOR.redact(proposal.target_description)[:256],
                "verified": True,
                "corroborated": corroborated,
            },
            display_id=image.display_id,
            bounds=(
                physical_bounds.x,
                physical_bounds.y,
                physical_bounds.width,
                physical_bounds.height,
            ),
            confidence=proposal.confidence,
        )
        descriptor = TargetDescriptor(
            identity=identity,
            source=PerceptionSource.VISION,
            observed_at=utc_now(),
            observation_id=proposal.observation_id,
            bounds=physical_bounds,
            coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP,
            selector_quality=SelectorQuality.VISUAL,
            visible=True,
            enabled=True,
        )
        return TargetCandidate(
            descriptor=descriptor,
            confidence=proposal.confidence,
            evidence=tuple(evidence[:16]),
        )

    async def _ground_via_router(
        self,
        image: CapturedImage,
        *,
        question: str,
        candidates: Sequence[TargetCandidate],
        timeout_seconds: float,
    ) -> GroundingProposal:
        assert self._router is not None
        encoded = base64.b64encode(image.content).decode("ascii")
        candidate_hints = [
            {
                "name": c.descriptor.identity.semantic_name,
                "role": c.descriptor.identity.role,
                "bounds": (
                    [
                        c.descriptor.bounds.x,
                        c.descriptor.bounds.y,
                        c.descriptor.bounds.width,
                        c.descriptor.bounds.height,
                    ]
                    if c.descriptor.bounds is not None
                    else None
                ),
            }
            for c in candidates[:16]
        ]
        request = ModelRequest(
            role=ModelRole.VISION,
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "You are an untrusted visual grounding assistant. Locate the "
                        "requested UI element and return JSON only: "
                        '{"target_description":"...","bounds":[x,y,w,h],'
                        '"confidence":0.9,"evidence":["..."]}. '
                        "Never output action commands."
                    ),
                ),
                ModelMessage(
                    role="user",
                    content=(
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"target": question, "candidate_hints": candidate_hints}
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image.content_type};base64,{encoded}"},
                        },
                    ),
                ),
            ),
            stream=False,
            timeout_seconds=timeout_seconds,
            required_modalities=frozenset({"text", "image"}),
        )
        selection = ModelSelectionRequest(
            role=ModelRole.VISION,
            task_type="vision_grounding",
            required_modalities=frozenset({"text", "image"}),
        )
        response = await self._router.complete(request, selection=selection)
        payload = json.loads(response.content)
        bounds_raw = payload.get("bounds", (0, 0, 10, 10))
        rect = Rect(*(float(v) for v in bounds_raw))
        desc = DEFAULT_REDACTOR.redact(str(payload.get("target_description") or question))[:512]
        conf = float(payload.get("confidence", 0.0))
        raw_ev = payload.get("evidence", ("vision model proposal",))
        ev = tuple(DEFAULT_REDACTOR.redact(str(item))[:256] for item in raw_ev[:8] if item)
        return GroundingProposal(
            target_description=desc,
            bounds=rect,
            confidence=conf,
            evidence=ev or ("vision model proposal",),
            observation_id=f"vis-obs-{uuid.uuid4().hex[:16]}",
            source=PerceptionSource.VISION,
        )


@dataclass(frozen=True, slots=True)
class CoordinateFallbackSafetyGate:
    """Explicit safety gate for last-resort coordinate fallback; fails closed when unsafe."""

    allow_coordinate_fallback: bool = False
    observation_current: bool = False
    dpi_verified: bool = False
    focus_verified: bool = False
    no_human_interference: bool = False
    unsafe_regions: tuple[Rect, ...] = ()

    def validate_or_raise(self, candidate: TargetCandidate) -> Point:
        if not self.allow_coordinate_fallback:
            raise ComputerAdapterError(
                ComputerFailureCode.POLICY_DENIED,
                "Coordinate fallback is disabled by policy; semantic grounding is required.",
                source=PerceptionSource.COORDINATE,
            )
        if not self.observation_current:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "Coordinate fallback rejected because the source observation is stale.",
                source=PerceptionSource.COORDINATE,
            )
        if not self.dpi_verified:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                "Coordinate fallback rejected because monitor DPI scaling is unverified.",
                source=PerceptionSource.COORDINATE,
            )
        if not self.focus_verified:
            raise ComputerAdapterError(
                ComputerFailureCode.ENVIRONMENT_CHANGED,
                "Coordinate fallback rejected because foreground window focus is unverified.",
                source=PerceptionSource.COORDINATE,
            )
        if not self.no_human_interference:
            raise ComputerAdapterError(
                ComputerFailureCode.USER_INTERFERENCE,
                "Coordinate fallback rejected due to detected human input or cursor drift.",
                source=PerceptionSource.COORDINATE,
            )
        if candidate.descriptor.bounds is None:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                "Coordinate fallback target has no bounding rectangle.",
                source=PerceptionSource.COORDINATE,
            )
        try:
            return CoordinateMapper.safe_click_point(
                candidate.descriptor.bounds, unsafe_regions=self.unsafe_regions
            )
        except ValueError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                f"Coordinate fallback point is unsafe ({exc}).",
                source=PerceptionSource.COORDINATE,
            ) from exc


class PerceptionHierarchyPipeline:
    """Resolve targets across API -> Browser DOM -> Windows UIA -> OCR -> Vision -> Coordinate."""

    def __init__(
        self,
        *,
        resolver: TargetResolver | None = None,
        ocr: OcrPerceptionAdapter | None = None,
        vision: VisionGroundingAdapter | None = None,
        unsafe_regions: tuple[Rect, ...] = (),
    ) -> None:
        if any(not isinstance(region, Rect) for region in unsafe_regions):
            raise ValueError("unsafe_regions must contain only Rect values")
        self.resolver = resolver or TargetResolver()
        self.ocr = ocr
        self.vision = vision
        self.unsafe_regions = tuple(unsafe_regions)

    async def resolve_hierarchical(
        self,
        query: TargetQuery,
        *,
        structural_candidates: Sequence[TargetCandidate] = (),
        screenshot: CapturedImage | None = None,
        observation_id: str | None = None,
        coordinate_safety: CoordinateFallbackSafetyGate | None = None,
    ) -> TargetResolution:
        obs_id = observation_id or f"hier-obs-{uuid.uuid4().hex[:16]}"
        pool: list[TargetCandidate] = list(structural_candidates)

        # Tier 1-3: Official API, Browser DOM, Windows UIA / Accessibility
        initial = self.resolver.resolve(query, pool)
        if initial.status in {ResolutionStatus.RESOLVED, ResolutionStatus.AMBIGUOUS}:
            if (
                initial.selected is not None
                and initial.selected.descriptor.source is PerceptionSource.COORDINATE
            ):
                gate = coordinate_safety or CoordinateFallbackSafetyGate()
                gate.validate_or_raise(initial.selected)
            return initial

        # Tier 4: OCR Layout fallback when screenshot is available
        ocr_items: Sequence[OCRText] = ()
        if (
            screenshot is not None
            and self.ocr is not None
            and PerceptionSource.OCR_LAYOUT in query.allowed_sources
        ):
            try:
                ocr_items = await self.ocr.recognize(screenshot)
                ocr_candidates = self.ocr.candidates_from_ocr(
                    ocr_items,
                    observation_id=obs_id,
                    platform="browser" if query.page_id is not None else "windows",
                    application=query.application,
                    window_id=query.window_id,
                    page_id=query.page_id,
                )
                pool.extend(ocr_candidates)
                ocr_resolution = self.resolver.resolve(query, pool)
                if ocr_resolution.status in {
                    ResolutionStatus.RESOLVED,
                    ResolutionStatus.AMBIGUOUS,
                }:
                    return ocr_resolution
            except ComputerAdapterError:
                pass

        # Tier 5: Multimodal Vision grounding fallback when screenshot is available
        if (
            screenshot is not None
            and self.vision is not None
            and PerceptionSource.VISION in query.allowed_sources
        ):
            try:
                proposal = await self.vision.ground(
                    query,
                    image=screenshot,
                    candidates=pool,
                )
                vision_candidate = self.vision.verify_and_build_candidate(
                    proposal,
                    image=screenshot,
                    query=query,
                    corroborating_candidates=pool,
                    corroborating_ocr=ocr_items,
                    unsafe_regions=self.unsafe_regions,
                )
                pool.append(vision_candidate)
                vision_resolution = self.resolver.resolve(query, pool)
                if vision_resolution.status in {
                    ResolutionStatus.RESOLVED,
                    ResolutionStatus.AMBIGUOUS,
                }:
                    return vision_resolution
            except ComputerAdapterError:
                pass

        return TargetResolution(
            ResolutionStatus.NOT_FOUND,
            (),
            reason="Target could not be grounded across API, DOM, UIA, OCR, or Vision layers.",
        )


__all__ = [
    "CoordinateFallbackSafetyGate",
    "OcrBackend",
    "OcrPerceptionAdapter",
    "PerceptionHierarchyPipeline",
    "RawCaptureBackend",
    "ScreenCaptureAdapter",
    "ScreenshotDeduplicator",
    "VisionBackend",
    "VisionGroundingAdapter",
    "Win32ScreenCaptureBackend",
    "crop_captured_image",
    "decode_rgba_png",
    "downscale_captured_image",
    "encode_rgba_png",
    "make_captured_image_from_rgba",
]
