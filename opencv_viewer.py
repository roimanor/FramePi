"""
Native fullscreen slideshow (OpenCV + NumPy). Optional phone remote via Flask
SSE (/api/remote/stream) when started with app.py or FRAMEPI_REMOTE_URL set.

Letterbox sizing works on **Mac and Raspberry Pi**: optional ``FRAMEPI_VIEW_WIDTH`` /
``FRAMEPI_VIEW_HEIGHT``; macOS uses CoreGraphics; Linux uses ``xdpyinfo`` or
``fb0`` sysfs when available; otherwise OpenCV ``getWindowImageRect``.

Set ``FRAMEPI_OVERLAY_METADATA=1`` to show the filename / GPS strip on gallery photos and videos (off by default).

OLED burn-in: ``FRAMEPI_OLED_SHIFT=1`` (default) nudges the image 1–2 px on a slow orbit before each
frame is shown. Tune with ``FRAMEPI_OLED_SHIFT_PX`` and ``FRAMEPI_OLED_SHIFT_SEC``.

Video download size is set at sync via ``GOOGLE_PHOTOS_VIDEO_SUFFIX_ORDER`` in ``.env`` (``m18`` = smallest).
On Raspberry Pi, ``FRAMEPI_VIDEO_PROXY`` defaults on: small H.264 proxies are built at sync for smooth
playback. Videos play via **mpv** when installed (``FRAMEPI_VIDEO_PLAYER=auto``); set ``opencv`` to use the
legacy OpenCV frame loop. Install on Pi: ``sudo apt install mpv``.

On the **map** (↑), upcoming Google Calendar events appear in a bottom panel when ``token_calendar.json``
exists (run ``authorize_google_calendar.py``). Disable with ``FRAMEPI_MAP_CALENDAR=0``.

Keyboard: ← / [ previous (slide or pin on map) · → / ] next · ↑ map · ↓ open slideshow for selected pin’s photos · Enter same as ↓ on map · **t** same calendar date in past years (toggle) · q quit · r reload (clears pin filter, merges missing disk files into metadata, refreshes map).

Run:  python app.py          # sync + Flask (remote) + this viewer
      python opencv_viewer.py   # viewer + sync only (set FRAMEPI_REMOTE=0 or start app separately)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import app as fp
import google_calendar_service
import video_proxy
from metadata_store import media_preview_urls
from mpv_player import (
    RESULT_EOF,
    RESULT_FAILED,
    RESULT_GALLERY,
    RESULT_MAP,
    RESULT_NEXT,
    RESULT_ON_THIS_DAY,
    RESULT_PREV,
    RESULT_QUIT,
    RESULT_RELOAD,
    RESULT_TIMEOUT,
    play_video_mpv,
    preferred_video_player,
    shutdown_mpv,
    warm_mpv_async,
)
from video_proxy import is_pi_zero_class
from shared_album_sync import _append_disk_only_googleusercontent_items
from static_map import (
    build_map_layout,
    cluster_pin_pixels_for_map,
    map_image_cache_key,
    osm_cluster_pin_specs,
    osm_map_image_cache_key,
    osm_tile_composite_bytes,
    static_map_png_bytes,
)

WIN = "FramePi"
SLIDE_MS_DEFAULT = 5000
VIDEO_MAX_MS_DEFAULT = 120_000

# Letterbox target: cached for one viewer run (see window_view_size).
_LETTERBOX_TARGET_CACHE: tuple[int, int] | None = None

# Arrow keys: GTK/Linux (65361…), Windows-style (2424832…), macOS Cocoa (63232…), WASD-style (81…).
_NAV_PREV = frozenset({65361, 81, 2424832, 2, 63234})
_NAV_NEXT = frozenset({65363, 83, 2555904, 3, 63235})
_NAV_MAP = frozenset({65362, 82, 2490368, 0xFF52, 63232})
_NAV_GALLERY = frozenset({65364, 84, 2621440, 0xFF54, 63233})


def _parse_item_local_date(item: dict[str, Any]) -> date | None:
    raw = item.get("created_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.astimezone().date()
    except Exception:
        return None


def filter_on_this_day_past_years(library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items whose capture date matches today's month and day in an earlier calendar year (local)."""
    today = datetime.now().astimezone().date()
    dated: list[tuple[date, dict[str, Any]]] = []
    for it in library:
        d = _parse_item_local_date(it)
        if d is None:
            continue
        if d.month == today.month and d.day == today.day and d.year < today.year:
            dated.append((d, it))
    dated.sort(key=lambda x: (x[0].year, x[0]), reverse=True)
    return [pair[1] for pair in dated]


def _sort_key(item: dict[str, Any]):
    created = item.get("created_time")
    created_ts = None
    if created:
        try:
            created_ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        except Exception:
            created_ts = None
    if created_ts is not None:
        return (0, -created_ts)
    lp = str(item.get("local_path") or "")
    return (1, lp)


def _sorted_items(meta: dict[str, Any]) -> list[dict[str, Any]]:
    items = list(meta.get("items") or [])
    items.sort(key=_sort_key)
    return items


def is_video_item(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    if item.get("media_type") == "video":
        return True
    m = str(item.get("mime_type") or "").lower()
    if m.startswith("video/"):
        return True
    return bool(re.search(r"\.(mp4|webm|mov|m4v|mkv)(\?|$)", str(item.get("local_path") or ""), re.I))


def _clamp_video_fps(raw: float) -> float:
    if not (1.0 < raw < 240.0):
        raw = 30.0
    cap_env = os.getenv("FRAMEPI_VIDEO_MAX_FPS", "").strip()
    if cap_env:
        try:
            mx = float(cap_env)
            if mx > 0:
                raw = min(raw, mx)
        except ValueError:
            pass
    elif platform.machine().lower() in ("aarch64", "armv7l", "armv6l"):
        raw = min(raw, 24.0)
    return max(12.0, min(60.0, raw))


def _video_source_fps(cap: cv2.VideoCapture) -> float:
    return _clamp_video_fps(float(cap.get(cv2.CAP_PROP_FPS) or 0.0))


def _video_decode_max_edge(tw: int, th: int) -> int:
    """Long-edge pixel budget for decoded video frames (lower = faster on Pi)."""
    edge_env = os.getenv("FRAMEPI_VIDEO_DECODE_MAX_EDGE", "").strip()
    if edge_env:
        try:
            return max(160, min(1920, int(edge_env)))
        except ValueError:
            pass
    q = os.getenv("FRAMEPI_VIDEO_QUALITY", "").strip().lower()
    if is_pi_zero_class():
        presets = {"low": 320, "medium": 400, "high": 480}
    else:
        presets = {"low": 360, "medium": 540, "high": 960}
    if q in presets:
        return presets[q]
    if is_pi_zero_class():
        return 320
    if platform.machine().lower() in ("aarch64", "armv7l", "armv6l"):
        return max(320, min(480, max(tw, th)))
    return max(640, min(1280, int(max(tw, th) * 1.25)))


def _letterbox_fit_size(sw: int, sh: int, tw: int, th: int) -> tuple[int, int]:
    """Inner picture size when fitting ``sw x sh`` into ``tw x th`` (even dimensions for ffmpeg)."""
    if sw < 1 or sh < 1:
        return max(2, tw - tw % 2), max(2, th - th % 2)
    scale = min(tw / sw, th / sh)
    nw = max(2, int(round(sw * scale)))
    nh = max(2, int(round(sh * scale)))
    nw = nw - nw % 2
    nh = nh - nh % 2
    return nw, nh


def _ffmpeg_letterbox_filter(sw: int, sh: int, tw: int, th: int) -> str:
    nw, nh = _letterbox_fit_size(sw, sh, tw, th)
    return (
        f"scale={nw}:{nh}:flags=fast_bilinear,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,format=bgr24"
    )


def _scaled_frame_size(sw: int, sh: int, max_edge: int) -> tuple[int, int]:
    if sw < 1 or sh < 1:
        return max(2, max_edge), max(2, max_edge)
    if sw >= sh:
        dw = min(max_edge, sw)
        dh = max(2, int(round(sh * dw / sw)))
    else:
        dh = min(max_edge, sh)
        dw = max(2, int(round(sw * dh / sh)))
    dw = max(2, dw - (dw % 2))
    dh = max(2, dh - (dh % 2))
    return dw, dh


def _scale_frame_to_max_edge(bgr: np.ndarray, max_edge: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    if max(h, w) <= max_edge:
        return bgr
    dw, dh = _scaled_frame_size(w, h, max_edge)
    return cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_LINEAR)


def _ffprobe_video(path: Path) -> tuple[int, int, float] | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,r_frame_rate",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        parts = proc.stdout.strip().split(",")
        if len(parts) < 3:
            return None
        w, h = int(parts[0]), int(parts[1])
        fps_s = parts[2].strip()
        if "/" in fps_s:
            num, den = fps_s.split("/", 1)
            fps = float(num) / float(den) if float(den) else 30.0
        else:
            fps = float(fps_s)
        return w, h, fps
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _prefer_ffmpeg_video_decoder() -> bool:
    mode = os.getenv("FRAMEPI_VIDEO_DECODER", "auto").strip().lower()
    if mode == "ffmpeg":
        return shutil.which("ffmpeg") is not None
    if mode == "opencv":
        return False
    return shutil.which("ffmpeg") is not None


def _video_playback_fps(source_fps: float) -> float:
    """Wall-clock rate from the file we are playing (proxy fps must match for smooth motion)."""
    raw = os.getenv("FRAMEPI_VIDEO_PLAYBACK_FPS", "").strip()
    if raw:
        try:
            return max(8.0, min(30.0, float(raw)))
        except ValueError:
            pass
    return _clamp_video_fps(source_fps)


def _ffmpeg_video_input_args() -> list[str]:
    """Optional hardware decode on Pi 4+ (disabled on Pi Zero — no usable HW path)."""
    if is_pi_zero_class():
        return []
    mode = os.getenv("FRAMEPI_VIDEO_HWACCEL", "auto").strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return []
    if platform.machine().lower() not in ("aarch64", "armv7l", "armv6l"):
        return []
    if mode in {"drm", "auto"}:
        return ["-hwaccel", "drm", "-hwaccel_output_format", "drm_prime"]
    return []


def _ffmpeg_decode_threads() -> str:
    return "1" if is_pi_zero_class() else "2"


class _OpenCVVideoReader:
    def __init__(self, path: Path, tw: int, th: int, *, scale_max_edge: int | None = None) -> None:
        self.ok = False
        self.fps = 24.0
        self.full_canvas = False
        self._max_edge = (
            _video_decode_max_edge(tw, th) if scale_max_edge is None else max(0, scale_max_edge)
        )
        self._cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        if not self._cap.isOpened():
            return
        try:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        probed = _video_source_fps(self._cap)
        self.fps = _video_playback_fps(probed)
        ok, frame = self._cap.read()
        if not ok or frame is None:
            self.release()
            return
        try:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        except Exception:
            pass
        self.ok = True

    def read(self) -> tuple[bool, np.ndarray | None]:
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return False, None
        if self._max_edge > 0:
            frame = _scale_frame_to_max_edge(frame, self._max_edge)
        return True, frame

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self.ok = False


class _FfmpegVideoReader:
    """Decode via ffmpeg straight to display-sized BGR frames (no Python letterbox per frame)."""

    def __init__(self, path: Path, tw: int, th: int) -> None:
        self.ok = False
        self.fps = 24.0
        self.full_canvas = True
        self._proc: subprocess.Popen[bytes] | None = None
        self._tw = max(2, tw - tw % 2)
        self._th = max(2, th - th % 2)
        self._frame_bytes = self._tw * self._th * 3
        self._buf: np.ndarray | None = None
        if not shutil.which("ffmpeg"):
            return
        probe = _ffprobe_video(path)
        sw, sh = (probe[0], probe[1]) if probe else (1280, 720)
        source_fps = _clamp_video_fps(probe[2]) if probe else 24.0
        self.fps = _video_playback_fps(source_fps)
        self._buf = np.empty((self._th, self._tw, 3), dtype=np.uint8)
        vf = _ffmpeg_letterbox_filter(sw, sh, self._tw, self._th)
        for attempt in range(1):
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-threads",
                _ffmpeg_decode_threads(),
                "-i",
                str(path),
                "-an",
                "-vf",
                vf,
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "pipe:1",
            ]
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                return
            if self._proc.stdout is None:
                self.release()
                return
            break
        self.ok = True

    def read(self) -> tuple[bool, np.ndarray | None]:
        if (
            not self.ok
            or self._proc is None
            or self._proc.stdout is None
            or self._buf is None
        ):
            return False, None
        raw = self._proc.stdout.read(self._frame_bytes)
        if len(raw) != self._frame_bytes:
            self.ok = False
            return False, None
        np.copyto(self._buf, np.frombuffer(raw, dtype=np.uint8).reshape((self._th, self._tw, 3)))
        return True, self._buf

    def release(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None
        self.ok = False


def _open_video_reader(path: Path, tw: int, th: int) -> _OpenCVVideoReader | _FfmpegVideoReader | None:
    play_path = video_proxy.resolve_playback_path(path, fp.DATA_DIR, build_if_missing=True)
    is_proxy = video_proxy.is_proxy_file(play_path, fp.DATA_DIR)
    # Proxies are already small — decode natively and letterbox in Python (less pipe bandwidth than full canvas).
    if is_proxy:
        ocv = _OpenCVVideoReader(play_path, tw, th, scale_max_edge=0)
        if ocv.ok:
            return ocv
    if shutil.which("ffmpeg"):
        ff = _FfmpegVideoReader(play_path, tw, th)
        if ff.ok:
            return ff
    scale_edge = 0 if is_proxy else None
    ocv = _OpenCVVideoReader(play_path, tw, th, scale_max_edge=scale_edge)
    return ocv if ocv.ok else None


def _compose_video_frame(
    vf: np.ndarray,
    tw: int,
    th: int,
    item: dict[str, Any],
    *,
    same_date_past_years: bool,
    overlay_cache: dict[str, Any],
    full_canvas: bool = False,
) -> np.ndarray:
    if full_canvas and vf.shape[0] == th and vf.shape[1] == tw:
        frame = vf
    else:
        frame = letterbox(vf, tw, th, fast=True)
    if not _metadata_overlay_enabled():
        return frame
    key = (str(item.get("local_path") or ""), tw, th, same_date_past_years)
    if overlay_cache.get("key") != key:
        bar_h = draw_item_metadata_overlay(frame, item, same_date_past_years=same_date_past_years)
        overlay_cache["key"] = key
        overlay_cache["bar_h"] = bar_h
        overlay_cache["top"] = frame[0:bar_h].copy() if bar_h > 0 else None
    elif overlay_cache.get("top") is not None:
        bh = int(overlay_cache["bar_h"])
        frame[0:bh] = overlay_cache["top"]
    return frame


def _oled_shift_enabled() -> bool:
    return os.getenv("FRAMEPI_OLED_SHIFT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class _OledPixelShifter:
    """Slow 1–2 px canvas offset to reduce static OLED burn-in (applied in pump_frame)."""

    def __init__(self) -> None:
        self._max_px = max(0, min(4, int(os.getenv("FRAMEPI_OLED_SHIFT_PX", "2"))))
        shift_default = "90" if is_pi_zero_class() else "60"
        self._interval = max(10.0, float(os.getenv("FRAMEPI_OLED_SHIFT_SEC", shift_default)))
        patterns: list[tuple[int, int]] = [(0, 0)]
        for px in range(1, self._max_px + 1):
            patterns.extend(
                [
                    (px, 0),
                    (px, px),
                    (0, px),
                    (-px, px),
                    (-px, 0),
                    (-px, -px),
                    (0, -px),
                    (px, -px),
                ]
            )
        self._patterns = patterns
        self._index = 0
        self._last_step = time.monotonic()
        self._dx = 0
        self._dy = 0
        self._buf: np.ndarray | None = None

    def _step_if_due(self) -> tuple[int, int]:
        if self._max_px <= 0:
            return 0, 0
        now = time.monotonic()
        if now - self._last_step >= self._interval:
            self._last_step = now
            self._index = (self._index + 1) % len(self._patterns)
            self._dx, self._dy = self._patterns[self._index]
        return self._dx, self._dy

    def apply(self, bgr: np.ndarray) -> np.ndarray:
        dx, dy = self._step_if_due()
        if dx == 0 and dy == 0:
            return bgr
        h, w = bgr.shape[:2]
        if self._buf is None or self._buf.shape != bgr.shape:
            self._buf = np.zeros_like(bgr)
        else:
            self._buf.fill(0)
        out = self._buf
        y_src0 = max(0, -dy)
        y_src1 = h - max(0, dy)
        x_src0 = max(0, -dx)
        x_src1 = w - max(0, dx)
        y_dst0 = max(0, dy)
        x_dst0 = max(0, dx)
        y_dst1 = y_dst0 + (y_src1 - y_src0)
        x_dst1 = x_dst0 + (x_src1 - x_src0)
        if y_src1 > y_src0 and x_src1 > x_src0:
            out[y_dst0:y_dst1, x_dst0:x_dst1] = bgr[y_src0:y_src1, x_src0:x_src1]
        return out


_oled_shifter: _OledPixelShifter | None = None


def _oled_pixel_shift(bgr: np.ndarray, *, during_video: bool = False) -> np.ndarray:
    global _oled_shifter
    if during_video and os.getenv("FRAMEPI_OLED_SHIFT_VIDEO", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return bgr
    if not _oled_shift_enabled():
        return bgr
    if _oled_shifter is None:
        _oled_shifter = _OledPixelShifter()
    return _oled_shifter.apply(bgr)


def letterbox(bgr: np.ndarray, tw: int, th: int, *, fast: bool = False) -> np.ndarray:
    if bgr is None or tw < 2 or th < 2:
        return np.zeros((max(2, th), max(2, tw), 3), dtype=np.uint8)
    h, w = bgr.shape[:2]
    if h < 1 or w < 1:
        return np.zeros((th, tw, 3), dtype=np.uint8)
    scale = min(tw / w, th / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    interp = cv2.INTER_LINEAR if fast else cv2.INTER_AREA
    resized = cv2.resize(bgr, (nw, nh), interpolation=interp)
    canvas = np.zeros((th, tw, 3), dtype=np.uint8)
    x0 = (tw - nw) // 2
    y0 = (th - nh) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
    return canvas


def focus_viewer_window() -> None:
    """Raise the OpenCV window above a hidden/stopped mpv instance."""
    try:
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        if hasattr(cv2, "WND_PROP_TOPMOST"):
            cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 1)
            cv2.waitKeyEx(1)
            cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 0)
    except cv2.error:
        pass


def bootstrap_window_size() -> tuple[int, int]:
    """One-time: OpenCV needs an initial imshow before getWindowImageRect is reliable."""
    dummy = np.zeros((720, 1280, 3), np.uint8)
    cv2.imshow(WIN, dummy)
    _ = cv2.waitKeyEx(1)
    return window_view_size()


def _darwin_main_display_pixel_size() -> tuple[int, int] | None:
    """Physical pixel size of the main display (no Tk — avoids macOS 15+ Tk crashes)."""
    import ctypes

    try:
        lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        CGMainDisplayID = lib.CGMainDisplayID
        CGMainDisplayID.restype = ctypes.c_uint32
        CGMainDisplayID.argtypes = []
        CGDisplayPixelsWide = lib.CGDisplayPixelsWide
        CGDisplayPixelsWide.restype = ctypes.c_size_t
        CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        CGDisplayPixelsHigh = lib.CGDisplayPixelsHigh
        CGDisplayPixelsHigh.restype = ctypes.c_size_t
        CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        did = CGMainDisplayID()
        w = int(CGDisplayPixelsWide(did))
        h = int(CGDisplayPixelsHigh(did))
        if w >= 32 and h >= 32:
            return (w, h)
    except Exception:
        pass
    return None


def _linux_x11_screen_wh() -> tuple[int, int] | None:
    """Root window pixel size from xdpyinfo (Raspberry Pi OS desktop / typical X11)."""
    try:
        proc = subprocess.run(
            ["xdpyinfo"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        for line in proc.stdout.splitlines():
            if line.strip().startswith("dimensions:"):
                # dimensions:    1920x1080 pixels (508x285 millimeters)
                token = line.split("dimensions:", 1)[1].strip().split()[0]
                if "x" in token:
                    w_s, h_s = token.split("x", 1)
                    w, h = int(w_s), int(h_s)
                    if w >= 32 and h >= 32:
                        return (w, h)
                break
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def _linux_fb0_virtual_size() -> tuple[int, int] | None:
    """Framebuffer virtual size when present (some Pi / kiosk setups)."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size", encoding="utf-8") as f:
            raw = f.read().strip().replace(",", " ")
        parts = raw.split()
        if len(parts) >= 2:
            w, h = int(parts[0]), int(parts[1])
            if w >= 32 and h >= 32:
                return (w, h)
    except (OSError, ValueError):
        pass
    return None


def window_view_size() -> tuple[int, int]:
    """Size used to letterbox content before imshow.

    On macOS fullscreen, ``getWindowImageRect`` often reports only the drawable
    image area below the menu bar / notch, while the window is taller; matching
    letterboxing to that smaller height leaves a black band (commonly at the top).

    Resolution order (first hit wins, then cached for the run):

    1. ``FRAMEPI_VIEW_WIDTH`` and ``FRAMEPI_VIEW_HEIGHT`` — use on Pi or Mac if
       auto-detection does not match your display.
    2. **macOS:** CoreGraphics main display pixel size.
    3. **Linux (e.g. Raspberry Pi):** ``xdpyinfo`` root dimensions, else
       ``/sys/class/graphics/fb0/virtual_size`` if readable.
    4. ``getWindowImageRect`` (OpenCV).
    5. Default ``1280×720``.
    """
    global _LETTERBOX_TARGET_CACHE
    if _LETTERBOX_TARGET_CACHE is not None:
        return _LETTERBOX_TARGET_CACHE

    ew = os.getenv("FRAMEPI_VIEW_WIDTH", "").strip()
    eh = os.getenv("FRAMEPI_VIEW_HEIGHT", "").strip()
    if ew.isdigit() and eh.isdigit():
        w, h = int(ew), int(eh)
        if w >= 32 and h >= 32:
            _LETTERBOX_TARGET_CACHE = (w, h)
            return _LETTERBOX_TARGET_CACHE

    if sys.platform == "darwin":
        dwh = _darwin_main_display_pixel_size()
        if dwh is not None:
            _LETTERBOX_TARGET_CACHE = dwh
            return _LETTERBOX_TARGET_CACHE

    if sys.platform.startswith("linux"):
        xwh = _linux_x11_screen_wh()
        if xwh is not None:
            _LETTERBOX_TARGET_CACHE = xwh
            return _LETTERBOX_TARGET_CACHE
        fbwh = _linux_fb0_virtual_size()
        if fbwh is not None:
            _LETTERBOX_TARGET_CACHE = fbwh
            return _LETTERBOX_TARGET_CACHE

    r = cv2.getWindowImageRect(WIN)
    w, h = int(r[2]), int(r[3])
    if w >= 32 and h >= 32:
        _LETTERBOX_TARGET_CACHE = (w, h)
        return _LETTERBOX_TARGET_CACHE

    _LETTERBOX_TARGET_CACHE = (1280, 720)
    return _LETTERBOX_TARGET_CACHE


def _decode_png_jpeg_bytes(data: bytes) -> np.ndarray | None:
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def render_map_bgr(meta: dict[str, Any]) -> tuple[np.ndarray | None, dict[str, Any] | None, str]:
    """Return (image, layout, source) where source is \"google\" or \"osm\" for pin projection."""
    items = list(meta.get("items") or [])
    layout = build_map_layout(items)
    if not layout:
        return None, None, ""
    google_err: str | None = None
    if fp.GOOGLE_MAPS_API_KEY:
        try:
            ck = map_image_cache_key(meta, layout)
            body, _ctype = static_map_png_bytes(fp.GOOGLE_MAPS_API_KEY, layout, ck)
            im = _decode_png_jpeg_bytes(body)
            if im is not None:
                return im, layout, "google"
        except Exception as exc:
            google_err = str(exc)
            print(f"[opencv_viewer] Google static map failed: {exc}")
    try:
        ock = osm_map_image_cache_key(meta, layout)
        body, _ctype = osm_tile_composite_bytes(layout, ock)
        im = _decode_png_jpeg_bytes(body)
        return (im, layout, "osm") if im is not None else (None, layout, "osm")
    except Exception as exc:
        print(f"[opencv_viewer] OSM map failed: {exc}")
        if google_err:
            print(f"[opencv_viewer] (Google was: {google_err})")
        return None, None, ""


def _items_matching_cluster(library: list[dict[str, Any]], cluster: dict[str, Any]) -> list[dict[str, Any]]:
    paths = {
        str(x.get("local_path"))
        for x in cluster.get("items", [])
        if isinstance(x, dict) and x.get("local_path")
    }
    out = [i for i in library if str(i.get("local_path")) in paths]
    return out if out else list(library)


def show_message(canvas_w: int, canvas_h: int, lines: list[str]) -> np.ndarray:
    frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    y = 40
    for line in lines[:12]:
        cv2.putText(frame, line[:80], (24, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (220, 220, 220), 1, cv2.LINE_AA)
        y += 28
    return frame


def _metadata_overlay_enabled() -> bool:
    return os.getenv("FRAMEPI_OVERLAY_METADATA", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def item_display_title(item: dict[str, Any]) -> str:
    custom = str(item.get("name") or "").strip()
    if custom:
        return custom[:96]
    lp = str(item.get("local_path") or "")
    return str(item.get("filename") or "").strip() or Path(lp).name or "?"


def _metadata_overlay_lines(item: dict[str, Any]) -> list[str]:
    lines: list[str] = [item_display_title(item)]

    lat, lon = item.get("gps_latitude"), item.get("gps_longitude")
    la = lo = None
    if lat is not None and lon is not None:
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError):
            la = lo = None
    loc = str(item.get("location") or "").strip()
    city = str(item.get("city") or "").strip()
    country = str(item.get("country") or "").strip()

    if la is not None and lo is not None:
        lines.append(f"lat {la:.6f}  lon {lo:.6f}")
        loc_ns = loc.replace(" ", "") if loc else ""
        same_as_coords = loc_ns in (
            f"{la:.6f},{lo:.6f}",
            f"{la:.6f}, {lo:.6f}".replace(" ", ""),
        )
        if loc and not same_as_coords:
            lines.append(loc[:96])
    elif loc:
        lines.append(loc[:96])
    else:
        lines.append("No location in metadata")

    place = ", ".join(p for p in (city, country) if p)
    if place:
        lines.append(place[:96])

    created = item.get("created_time")
    if created:
        lines.append(str(created)[:72])
    return lines[:5]


def _fit_line_to_width(text: str, font: int, scale: float, thickness: int, max_w: int) -> str:
    t = text
    ell = "…"
    while t:
        w0, _ = cv2.getTextSize(t, font, scale, thickness)[0]
        if w0 <= max_w:
            return t
        if len(t) <= 5:
            return t[:1] + ell
        t = t[:-1]
    return ell


def draw_item_metadata_overlay(
    bgr: np.ndarray, item: dict[str, Any], *, same_date_past_years: bool = False
) -> int:
    """Darken top strip and draw metadata (mutates ``bgr`` in place). Returns strip height."""
    if not _metadata_overlay_enabled():
        return 0
    lines = _metadata_overlay_lines(item)
    if same_date_past_years:
        td = datetime.now().astimezone().date()
        tag = f"On this day · {td.strftime('%b')} {td.day} (past years)"
        if len(lines) >= 1:
            lines.insert(1, tag)
        lines = lines[:7]
    if not lines:
        return 0
    h, w = bgr.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    thickness = max(1, min(3, int(round(w / 700))))
    scale = max(0.48, min(0.95, w / 1400.0 * 0.72))
    margin_x = max(10, w // 120)
    max_text_w = w - 2 * margin_x
    fitted = [_fit_line_to_width(s, font, scale, thickness, max_text_w) for s in lines]
    line_h = 0
    for s in fitted:
        _tw, th0 = cv2.getTextSize(s, font, scale, thickness)[0]
        line_h = max(line_h, th0)
    line_step = int(line_h * 1.35) + 6
    pad_top = max(10, h // 80)
    bar_h = min(h // 3, pad_top + len(fitted) * line_step + 12)
    strip = bgr[0:bar_h, 0:w]
    strip[:] = (strip.astype(np.float32) * 0.42 + np.float32([28.0, 28.0, 32.0])).clip(0, 255).astype(np.uint8)

    y = pad_top + line_h
    shadow = (18, 18, 22)
    fg = (248, 248, 252)
    for s in fitted:
        ox, oy = margin_x + 1, y + 1
        cv2.putText(bgr, s, (ox, oy), font, scale, shadow, thickness + 1, cv2.LINE_AA)
        cv2.putText(bgr, s, (margin_x, y), font, scale, fg, thickness, cv2.LINE_AA)
        y += line_step
    return bar_h


def draw_map_calendar_overlay(bgr: np.ndarray, lines: list[str]) -> None:
    """Semi-opaque bottom strip + calendar lines (mutates ``bgr`` in place)."""
    if not lines:
        return
    h, w = bgr.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    thickness = max(1, min(2, w // 900))
    scale = max(0.4, min(0.68, w / 1500.0 * 0.62))
    margin_x = max(8, w // 100)
    max_text_w = w - 2 * margin_x
    fitted = [_fit_line_to_width(s, font, scale, thickness, max_text_w) for s in lines]
    line_h = 0
    for s in fitted:
        _tw, th0 = cv2.getTextSize(s, font, scale, thickness)[0]
        line_h = max(line_h, th0)
    line_step = int(line_h * 1.28) + 4
    pad = max(8, h // 90)
    bar_h = min(int(h * 0.34), pad * 2 + len(fitted) * line_step + 8)
    y0 = h - bar_h
    strip = bgr[y0:h, 0:w]
    strip[:] = (strip.astype(np.float32) * 0.38 + np.float32([22.0, 24.0, 32.0])).clip(0, 255).astype(np.uint8)

    shadow = (14, 14, 18)
    fg = (235, 240, 255)
    y = y0 + pad + line_h
    for s in fitted:
        ox, oy = margin_x + 1, y + 1
        cv2.putText(bgr, s, (ox, oy), font, scale, shadow, thickness + 1, cv2.LINE_AA)
        cv2.putText(bgr, s, (margin_x, y), font, scale, fg, thickness, cv2.LINE_AA)
        y += line_step


def _cluster_location_label(cluster: dict[str, Any]) -> str:
    items = [x for x in (cluster.get("items") or []) if isinstance(x, dict)]
    for it in items:
        city = str(it.get("city") or "").strip()
        country = str(it.get("country") or "").strip()
        if city or country:
            return ", ".join(p for p in (city, country) if p)
    for it in items:
        loc = str(it.get("location") or "").strip()
        if loc:
            loc_ns = loc.replace(" ", "")
            if not re.match(r"^-?\d+\.\d+,", loc_ns):
                return loc[:80]
    try:
        return f"{float(cluster['lat']):.4f}, {float(cluster['lon']):.4f}"
    except (KeyError, TypeError, ValueError):
        return "Unknown location"


def _cluster_media_summary(cluster: dict[str, Any]) -> str:
    items = [x for x in (cluster.get("items") or []) if isinstance(x, dict)]
    n = len(items)
    if n == 0:
        return "0 items"
    photos = sum(1 for x in items if not is_video_item(x))
    videos = n - photos
    parts: list[str] = []
    if photos:
        parts.append(f"{photos} photo{'s' if photos != 1 else ''}")
    if videos:
        parts.append(f"{videos} video{'s' if videos != 1 else ''}")
    return ", ".join(parts)


def _sort_map_pins_screen_order(
    specs: list[tuple[dict[str, Any], int, int]],
) -> list[tuple[dict[str, Any], int, int]]:
    """←/→ follow on-screen position: left to right, then top to bottom."""
    return sorted(specs, key=lambda t: (t[1], t[2]))


def _map_pin_index_for_cluster(
    specs: list[tuple[dict[str, Any], int, int]],
    cluster: dict[str, Any],
) -> int | None:
    """Index of ``cluster`` in ``specs`` (match lat/lon), or None."""
    try:
        lat = round(float(cluster["lat"]), 5)
        lon = round(float(cluster["lon"]), 5)
    except (KeyError, TypeError, ValueError):
        return None
    for i, (cl, _px, _py) in enumerate(specs):
        try:
            if round(float(cl["lat"]), 5) == lat and round(float(cl["lon"]), 5) == lon:
                return i
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _draw_map_pin_tooltip(
    bgr: np.ndarray,
    cx: int,
    cy: int,
    lines: list[str],
) -> None:
    """Label for the selected (hovered) pin: location name + media count."""
    if not lines:
        return
    h, w = bgr.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    thickness = max(1, min(2, w // 900))
    scale = max(0.42, min(0.62, w / 1600.0 * 0.65))
    margin_x = 10
    max_text_w = min(w - 24, int(w * 0.55))
    fitted = [_fit_line_to_width(s, font, scale, thickness, max_text_w) for s in lines[:3]]
    line_h = 0
    for s in fitted:
        _tw, th0 = cv2.getTextSize(s, font, scale, thickness)[0]
        line_h = max(line_h, th0)
    line_step = int(line_h * 1.28) + 4
    pad = 8
    box_w = min(w - 16, max((cv2.getTextSize(s, font, scale, thickness)[0][0] for s in fitted), default=0) + 2 * margin_x)
    box_h = pad * 2 + len(fitted) * line_step
    tx = int(np.clip(cx - box_w // 2, 8, w - box_w - 8))
    ty = cy - 14 - box_h
    if ty < 8:
        ty = cy + 14
    if ty + box_h > h - 8:
        ty = max(8, h - box_h - 8)
    strip = bgr[ty : ty + box_h, tx : tx + box_w]
    strip[:] = (strip.astype(np.float32) * 0.32 + np.float32([18.0, 48.0, 28.0])).clip(0, 255).astype(np.uint8)
    y = ty + pad + line_h
    shadow = (12, 28, 18)
    fg = (230, 248, 235)
    for s in fitted:
        cv2.putText(bgr, s, (tx + margin_x + 1, y + 1), font, scale, shadow, thickness + 1, cv2.LINE_AA)
        cv2.putText(bgr, s, (tx + margin_x, y), font, scale, fg, thickness, cv2.LINE_AA)
        y += line_step


def _blend_map_pin(
    bgr: np.ndarray,
    cx: int,
    cy: int,
    radius: int,
    fill_bgr: tuple[int, int, int],
    alpha: float,
    *,
    outline_bgr: tuple[int, int, int] | None = None,
    outline_w: int = 0,
) -> None:
    """Draw a semi-transparent filled pin (mutates ``bgr`` in place)."""
    if radius < 2 or alpha <= 0:
        return
    h, w = bgr.shape[:2]
    pad = outline_w + 2
    x0 = max(0, cx - radius - pad)
    y0 = max(0, cy - radius - pad)
    x1 = min(w, cx + radius + pad + 1)
    y1 = min(h, cy + radius + pad + 1)
    if x1 <= x0 or y1 <= y0:
        return
    roi = bgr[y0:y1, x0:x1]
    lx, ly = cx - x0, cy - y0
    mask = np.zeros(roi.shape[:2], dtype=np.uint8)
    cv2.circle(mask, (lx, ly), radius, 255, -1, lineType=cv2.LINE_AA)
    m = (mask.astype(np.float32) / 255.0 * alpha)[..., np.newaxis]
    tint = np.full_like(roi, fill_bgr, dtype=np.uint8)
    blended = (roi.astype(np.float32) * (1.0 - m) + tint.astype(np.float32) * m).astype(np.uint8)
    if outline_bgr is not None and outline_w > 0:
        cv2.circle(blended, (lx, ly), radius, outline_bgr, outline_w, lineType=cv2.LINE_AA)
    roi[:] = blended


def draw_map_location_pins(
    bgr: np.ndarray,
    pin_specs: list[tuple[dict[str, Any], int, int]],
    selected_index: int,
) -> None:
    """Location pins: light green; selected pin (hover) is dark green with a label."""
    if not pin_specs:
        return
    n = len(pin_specs)
    sel = selected_index % n
    base_r = max(4, min(9, bgr.shape[1] // 72))
    light_green = (140, 230, 140)
    light_outline = (90, 170, 90)
    dark_green = (50, 110, 50)
    dark_outline = (30, 70, 30)
    for i, (cluster, cx, cy) in enumerate(pin_specs):
        selected = i == sel
        if selected:
            _blend_map_pin(
                bgr,
                cx,
                cy,
                base_r + 1,
                dark_green,
                0.92,
                outline_bgr=dark_outline,
                outline_w=max(1, base_r // 4),
            )
        else:
            _blend_map_pin(
                bgr,
                cx,
                cy,
                base_r,
                light_green,
                0.62,
                outline_bgr=light_outline,
                outline_w=1,
            )
    _cl, cx, cy = pin_specs[sel]
    _draw_map_pin_tooltip(
        bgr,
        cx,
        cy,
        [_cluster_location_label(_cl), _cluster_media_summary(_cl)],
    )


def decode_nav(key_ex: int) -> str | None:
    if key_ex < 0:
        return None
    k = key_ex & 0xFFFFFFFF
    if k in _NAV_PREV or k == ord("["):
        return "prev"
    if k in _NAV_NEXT or k == ord("]"):
        return "next"
    if k in _NAV_MAP:
        return "map"
    if k in _NAV_GALLERY:
        return "gallery"
    if k == ord("t") or k == ord("T"):
        return "on_this_day"
    if k == ord(" "):
        return "pause_toggle"
    return None


def nav_from_remote_key(key: str, current_mode: str) -> str | None:
    if key == "ArrowLeft":
        return "prev"
    if key == "ArrowRight":
        return "next"
    if key == "ArrowUp":
        return "map"
    if key == "ArrowDown":
        return "gallery"
    if key == "Enter":
        return "gallery" if current_mode == "map" else "next"
    if key == "OnThisDay":
        return "on_this_day"
    if key == "ReloadMetadata":
        return "reload"
    if key == "TogglePause":
        return "pause_toggle"
    return None


def _item_for_remote(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "filename",
        "google_url",
        "local_path",
        "media_type",
        "mime_type",
        "description",
        "created_time",
        "location",
        "city",
        "country",
        "gps_latitude",
        "gps_longitude",
        "camera_make",
        "camera_model",
        "focal_length",
        "aperture_f_number",
        "iso_equivalent",
        "exposure_time",
        "width",
        "height",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k == "name":
            out[k] = str(item.get("name") or "")
        elif k in item and item[k] is not None:
            out[k] = item[k]
    return out


_remote_now_post_queue: queue.Queue[tuple[str, dict[str, Any]]] | None = None
_remote_now_post_lock = threading.Lock()


def _remote_post_now_sync(base: str, body: dict[str, Any]) -> None:
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{base.rstrip('/')}/api/remote/now",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3.0)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        pass


def _remote_post_now_async(base: str, body: dict[str, Any]) -> None:
    """Queue now updates so they reach Flask in send order (avoids stale empty/map states)."""
    global _remote_now_post_queue
    with _remote_now_post_lock:
        if _remote_now_post_queue is None:
            q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()

            def _worker() -> None:
                while True:
                    b, payload = q.get()
                    _remote_post_now_sync(b, payload)

            threading.Thread(target=_worker, daemon=True, name="framepi-remote-now").start()
            _remote_now_post_queue = q
        _remote_now_post_queue.put((base, body))


def _fetch_slide_ms(base: str, fallback: int) -> int:
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/api/remote/settings")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return max(500, min(3_600_000, int(data.get("slide_ms", fallback))))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return fallback


def _remote_sse_loop(
    base: str, key_out: queue.Queue[str], settings_out: queue.Queue[dict[str, Any]] | None
) -> None:
    url = f"{base.rstrip('/')}/api/remote/stream"
    while True:
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "text/event-stream", "Cache-Control": "no-store"},
            )
            with urllib.request.urlopen(req, timeout=None) as resp:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", errors="replace").strip()
                    if not s.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(s[6:])
                        if payload.get("type") == "settings" and settings_out is not None:
                            settings_out.put_nowait(payload)
                        elif payload.get("type") == "key":
                            k = payload.get("key")
                            if isinstance(k, str):
                                key_out.put_nowait(k)
                        elif "key" in payload and payload.get("type") is None:
                            k = payload.get("key")
                            if isinstance(k, str):
                                key_out.put_nowait(k)
                    except Exception:
                        pass
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            time.sleep(2.0)


def run_viewer(
    slide_ms: int,
    video_max_ms: int,
    focus: str | None,
    start_sync: bool,
    remote_stream_base: str | None,
) -> None:
    if start_sync:
        threading.Thread(target=fp._sync_loop, daemon=True).start()

    remote_q: queue.Queue[str] | None = queue.Queue() if remote_stream_base else None
    settings_q: queue.Queue[dict[str, Any]] | None = (
        queue.Queue() if remote_stream_base else None
    )
    if remote_stream_base and remote_q is not None:
        slide_ms = _fetch_slide_ms(remote_stream_base, slide_ms)
        threading.Thread(
            target=_remote_sse_loop,
            args=(remote_stream_base, remote_q, settings_q),
            daemon=True,
        ).start()

    global _LETTERBOX_TARGET_CACHE
    _LETTERBOX_TARGET_CACHE = None

    def apply_remote_settings() -> None:
        nonlocal slide_ms
        if settings_q is None:
            return
        try:
            while True:
                payload = settings_q.get_nowait()
                slide_ms = max(500, min(3_600_000, int(payload.get("slide_ms", slide_ms))))
        except queue.Empty:
            pass

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    index = 0
    library_items: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    map_cache: np.ndarray | None = None
    map_layout: dict[str, Any] | None = None
    map_pin_specs: list[tuple[dict[str, Any], int, int]] = []
    map_pin_index = 0
    map_raster_source = ""
    mode = "gallery"
    on_this_day_filter = False
    slideshow_paused = False
    last_empty_metadata_poll = 0.0
    need_map_now_sync = True

    def auto_advance_deadline_passed(deadline: float) -> bool:
        return not slideshow_paused and time.monotonic() >= deadline

    def recompute_visible_items() -> None:
        nonlocal items, index
        if on_this_day_filter:
            items = filter_on_this_day_past_years(library_items)
            if items:
                index = index % len(items)
            else:
                index = 0
        else:
            items = list(library_items)
            if library_items:
                index = index % len(library_items)
            else:
                index = 0

    def toggle_on_this_day() -> None:
        nonlocal on_this_day_filter
        on_this_day_filter = not on_this_day_filter
        recompute_visible_items()

    def try_on_this_day_toggle(nav: str | None, key_ex: int) -> bool:
        if nav != "on_this_day" and key_ex not in (ord("t"), ord("T")):
            return False
        toggle_on_this_day()
        return True

    def try_pause_toggle(nav: str | None, key_ex: int) -> bool:
        nonlocal slideshow_paused, last_remote_now_sig
        if nav != "pause_toggle" and key_ex != ord(" "):
            return False
        slideshow_paused = not slideshow_paused
        last_remote_now_sig = None
        publish_remote_now()
        return True

    _map_cal_state: dict[str, Any] = {"mono": 0.0, "lines": []}

    def map_calendar_overlay_lines() -> list[str]:
        if os.getenv("FRAMEPI_MAP_CALENDAR", "1").strip().lower() in {"0", "false", "no", "off"}:
            return []
        tok = fp.ROOT / "token_calendar.json"
        if not tok.is_file():
            return ["Calendar: run authorize_google_calendar.py"]
        refresh = max(30, int(os.getenv("FRAMEPI_CALENDAR_REFRESH_SEC", "300")))
        now_m = time.monotonic()
        if now_m - float(_map_cal_state["mono"]) < refresh and _map_cal_state.get("lines"):
            return list(_map_cal_state["lines"])
        try:
            evs = google_calendar_service.fetch_upcoming_events(tok)
            lines = google_calendar_service.format_events_for_map_overlay(evs)
            if not lines:
                lines = ["Upcoming", "(no events in this range)"]
        except (FileNotFoundError, RuntimeError) as exc:
            lines = ["Calendar", str(exc)[:96]]
        except Exception as exc:
            lines = ["Calendar unavailable", str(exc)[:96]]
        _map_cal_state["mono"] = now_m
        _map_cal_state["lines"] = lines
        return lines

    def reload_items() -> None:
        nonlocal items, index, map_cache, map_layout, map_pin_specs, map_pin_index, library_items, map_raster_source, last_remote_now_sig
        keep_path: str | None = None
        if mode == "gallery" and items:
            keep_path = str(items[index % len(items)].get("local_path") or "").strip() or None
        _map_cal_state["mono"] = 0.0
        _map_cal_state["lines"] = []
        store = fp.metadata_store()
        store.migrate_from_json(fp.METADATA_PATH)
        meta = fp._load_metadata()
        items_list = list(meta.get("items") or [])
        n_before = len(items_list)
        photos_dir = fp.DATA_DIR / "photos"
        managed_names = {
            Path(str(x.get("local_path") or "")).name for x in items_list if x.get("local_path")
        }
        _append_disk_only_googleusercontent_items(
            items_list, managed_names, photos_dir, {}, {}, {}, store=store
        )
        if len(items_list) != n_before:
            meta = fp._load_metadata()
        library_items = _sorted_items(meta)
        recompute_visible_items()
        if keep_path:
            found = False
            for i, it in enumerate(items):
                if it.get("local_path") == keep_path:
                    index = i
                    found = True
                    break
            if not found and not on_this_day_filter:
                for i, it in enumerate(library_items):
                    if it.get("local_path") == keep_path:
                        index = i
                        break
        elif focus and not on_this_day_filter:
            for i, it in enumerate(library_items):
                if it.get("local_path") == focus or it.get("filename") == focus:
                    index = i
                    break
        map_cache = None
        map_layout = None
        map_pin_specs = []
        map_pin_index = 0
        map_raster_source = ""
        last_remote_now_sig = None
        publish_remote_now(force=True)

    last_remote_now_sig: tuple | None = None
    _remote_now_publish_lock = threading.Lock()

    def publish_remote_now(*, empty_message: str | None = None, force: bool = False) -> None:
        nonlocal last_remote_now_sig
        if not remote_stream_base:
            return
        if mode == "map":
            label = summary = ""
            pin_index = pin_total = 0
            if map_pin_specs:
                _cl, _x, _y = map_pin_specs[map_pin_index % len(map_pin_specs)]
                label = _cluster_location_label(_cl)
                summary = _cluster_media_summary(_cl)
                pin_index = map_pin_index % len(map_pin_specs)
                pin_total = len(map_pin_specs)
            body: dict[str, Any] = {
                "mode": "map",
                "map_pin": {
                    "label": label,
                    "summary": summary,
                    "index": pin_index,
                    "total": pin_total,
                },
            }
            sig: tuple = ("map", pin_index, pin_total, label)
        elif not items:
            if empty_message:
                msg = empty_message
            elif on_this_day_filter and library_items:
                td = datetime.now().astimezone().date()
                msg = f"On this day ({td.strftime('%b')} {td.day}): no photos from past years."
            else:
                msg = "No photos yet."
            body = {"mode": "empty", "message": msg}
            sig = ("empty", msg, on_this_day_filter)
        else:
            it = items[index % len(items)]
            body = {
                "mode": "gallery",
                "index": index % len(items),
                "total": len(items),
                "on_this_day": on_this_day_filter,
                "paused": slideshow_paused,
                "item": _item_for_remote(it),
                "lines": _metadata_overlay_lines(it),
                "preview": media_preview_urls(it),
            }
            sig = (
                "gallery",
                body["index"],
                body["total"],
                str(it.get("local_path")),
                on_this_day_filter,
                slideshow_paused,
                str(it.get("name") or ""),
                str(it.get("created_time") or ""),
                str(it.get("location") or ""),
                str(it.get("city") or ""),
                str(it.get("country") or ""),
            )
        with _remote_now_publish_lock:
            if not force and sig == last_remote_now_sig:
                return
            last_remote_now_sig = sig
        _remote_post_now_async(remote_stream_base, body)

    video_reader: _OpenCVVideoReader | _FfmpegVideoReader | None = None

    def release_cap() -> None:
        nonlocal video_reader
        if video_reader is not None:
            video_reader.release()
            video_reader = None

    def exit_viewer() -> None:
        release_cap()
        shutdown_mpv()
        cv2.destroyAllWindows()

    def pump_frame(
        bgr: np.ndarray,
        current_mode: str,
        *,
        wait_ms: int | None = None,
        video_playback: bool = False,
    ) -> tuple[str | None, int]:
        cv2.imshow(WIN, _oled_pixel_shift(bgr, during_video=video_playback))
        nav: str | None = None
        if remote_q is not None:
            try:
                while True:
                    rk = remote_q.get_nowait()
                    n = nav_from_remote_key(rk, current_mode)
                    if n:
                        nav = n
            except queue.Empty:
                pass
        if wait_ms is not None:
            delay = max(1, wait_ms)
        elif current_mode == "map":
            delay = 30
        else:
            delay = 1
        k = int(cv2.waitKeyEx(delay))
        nk = decode_nav(k)
        if nk is not None:
            nav = nk
        if nav == "reload":
            reload_items()
            nav = None
        apply_remote_settings()
        return nav, k

    bootstrap_window_size()
    warm_mpv_async()
    reload_items()
    last_empty_metadata_poll = time.monotonic()

    while True:
        apply_remote_settings()
        tw, th = window_view_size()

        if mode == "map":
            if map_cache is None:
                map_cache, map_layout, map_raster_source = render_map_bgr(fp._load_metadata())
                if (
                    map_cache is not None
                    and map_layout is not None
                    and map_raster_source in ("google", "osm")
                ):
                    ih, iw = map_cache.shape[0], map_cache.shape[1]
                    prev_cluster: dict[str, Any] | None = None
                    if map_pin_specs:
                        prev_cluster = map_pin_specs[map_pin_index % len(map_pin_specs)][0]
                    if map_raster_source == "osm":
                        raw_specs, ptw, pth = osm_cluster_pin_specs(map_layout)
                        sx = iw / max(1, ptw)
                        sy = ih / max(1, pth)
                        map_pin_specs = [
                            (c, int(round(px * sx)), int(round(py * sy))) for c, px, py in raw_specs
                        ]
                    else:
                        clusters_s = sorted(
                            map_layout.get("clusters") or [],
                            key=lambda x: (float(x["lat"]), float(x["lon"])),
                        )
                        pts = cluster_pin_pixels_for_map((ih, iw), map_layout, "google")
                        map_pin_specs = []
                        for i in range(min(len(clusters_s), len(pts))):
                            map_pin_specs.append((clusters_s[i], pts[i][0], pts[i][1]))
                    map_pin_specs = _sort_map_pins_screen_order(map_pin_specs)
                    map_pin_index = 0
                    if prev_cluster is not None:
                        idx = _map_pin_index_for_cluster(map_pin_specs, prev_cluster)
                        if idx is not None:
                            map_pin_index = idx
                else:
                    map_pin_specs = []
            if map_cache is None:
                frame = show_message(
                    tw,
                    th,
                    [
                        "No map pins: no GPS or lat/lon in location text.",
                        "Run a full sync if photos are new.",
                        "",
                        "ArrowDown: gallery · q: quit",
                    ],
                )
            else:
                vis = map_cache.copy()
                if map_pin_specs:
                    draw_map_location_pins(vis, map_pin_specs, map_pin_index)
                frame = letterbox(vis, tw, th)
                cal_lines = map_calendar_overlay_lines()
                if cal_lines:
                    draw_map_calendar_overlay(frame, cal_lines)
            if need_map_now_sync:
                publish_remote_now()
                need_map_now_sync = False
            nav, key_ex = pump_frame(frame, mode)
            if key_ex in (27, ord("q")):
                break
            if try_on_this_day_toggle(nav, key_ex):
                mode = "gallery"
                need_map_now_sync = True
                continue
            if nav == "prev" and map_pin_specs:
                map_pin_index = (map_pin_index - 1) % len(map_pin_specs)
                publish_remote_now()
            elif nav == "next" and map_pin_specs:
                map_pin_index = (map_pin_index + 1) % len(map_pin_specs)
                publish_remote_now()
            elif nav == "gallery":
                need_map_now_sync = True
                if map_pin_specs:
                    on_this_day_filter = False
                    _cl, _x, _y = map_pin_specs[map_pin_index % len(map_pin_specs)]
                    items = _items_matching_cluster(library_items, _cl)
                    index = 0
                mode = "gallery"
            elif nav == "map":
                pass
            if key_ex == ord("r"):
                reload_items()
            continue

        if not items:
            if on_this_day_filter and library_items:
                td = datetime.now().astimezone().date()
                frame = show_message(
                    tw,
                    th,
                    [
                        f"On this day ({td.strftime('%b')} {td.day}): no photos from past years.",
                        "Press t for full gallery.",
                        "",
                        "ArrowUp: map · q: quit · r: reload",
                    ],
                )
            else:
                now_mt = time.monotonic()
                if now_mt - last_empty_metadata_poll >= 2.5:
                    last_empty_metadata_poll = now_mt
                    reload_items()
                if items:
                    continue
                frame = show_message(
                    tw,
                    th,
                    [
                        "No photos yet.",
                        "Sync runs in the background; the gallery refreshes every few seconds.",
                        "Configure GOOGLE_PHOTOS_* in .env if sync is not set up.",
                        "",
                        "q: quit · r: reload now",
                    ],
                )
            publish_remote_now()
            nav, key_ex = pump_frame(frame, mode)
            if key_ex in (27, ord("q")):
                break
            if try_on_this_day_toggle(nav, key_ex):
                pass
            if key_ex == ord("r"):
                reload_items()
            time.sleep(0.15)
            continue

        item = items[index % len(items)]
        publish_remote_now()
        path = fp.DATA_DIR / str(item.get("local_path") or "")

        if is_video_item(item):
            release_cap()
            if not path.is_file():
                frame = show_message(tw, th, [f"Missing file:", str(path)])
                t0 = time.monotonic()
                while time.monotonic() - t0 < slide_ms / 1000.0:
                    nav, key_ex = pump_frame(frame, mode)
                    if key_ex in (27, ord("q")):
                        exit_viewer()
                        return
                    if try_on_this_day_toggle(nav, key_ex):
                        release_cap()
                        break
                    if nav == "map":
                        mode = "map"
                        need_map_now_sync = True
                        break
                    if nav == "prev":
                        index = (index - 1 + len(items)) % len(items)
                        break
                    if nav == "next":
                        index = (index + 1) % len(items)
                        break
                else:
                    index = (index + 1) % len(items)
                continue

            play_path = video_proxy.resolve_playback_path(path, fp.DATA_DIR, build_if_missing=True)
            if preferred_video_player() == "mpv":

                def _mpv_pause_changed(p: bool) -> None:
                    nonlocal slideshow_paused, last_remote_now_sig
                    slideshow_paused = p
                    last_remote_now_sig = None
                    publish_remote_now()

                mpv_result = play_video_mpv(
                    play_path,
                    video_max_ms=video_max_ms,
                    mode=mode,
                    start_paused=slideshow_paused,
                    remote_q=remote_q,
                    settings_q=settings_q,
                    apply_remote_settings=apply_remote_settings,
                    on_pause_changed=_mpv_pause_changed,
                    nav_from_remote_key=nav_from_remote_key,
                    decode_nav=decode_nav,
                )
                release_cap()
                focus_viewer_window()
                if mpv_result == RESULT_QUIT:
                    exit_viewer()
                    return
                if mpv_result == RESULT_ON_THIS_DAY:
                    toggle_on_this_day()
                    continue
                if mpv_result == RESULT_PREV:
                    index = (index - 1 + len(items)) % len(items)
                    continue
                if mpv_result in (RESULT_NEXT, RESULT_GALLERY, RESULT_EOF, RESULT_TIMEOUT):
                    index = (index + 1) % len(items)
                    continue
                if mpv_result == RESULT_MAP:
                    mode = "map"
                    need_map_now_sync = True
                    continue
                if mpv_result == RESULT_RELOAD:
                    reload_items()
                    continue
                if mpv_result != RESULT_FAILED:
                    continue

            video_reader = _open_video_reader(path, tw, th)
            if video_reader is None:
                frame = show_message(tw, th, [f"Cannot open video:", str(path)])
                t0 = time.monotonic()
                while time.monotonic() - t0 < 2.0:
                    nav, key_ex = pump_frame(frame, mode)
                    if key_ex in (27, ord("q")):
                        exit_viewer()
                        return
                    if try_on_this_day_toggle(nav, key_ex):
                        release_cap()
                        break
                    if nav == "prev":
                        index = (index - 1 + len(items)) % len(items)
                        break
                    if nav == "next":
                        index = (index + 1) % len(items)
                        break
                    if nav == "map":
                        mode = "map"
                        need_map_now_sync = True
                        break
                else:
                    index = (index + 1) % len(items)
                continue

            fps = max(8.0, float(video_reader.fps))
            frame_period = 1.0 / fps
            t_video = time.monotonic()
            next_show = t_video
            frames_shown = 0
            video_overlay_cache: dict[str, Any] = {}
            eof = False
            hold_frame: np.ndarray | None = None
            last_frame: np.ndarray | None = None
            use_canvas = getattr(video_reader, "full_canvas", False)
            while True:
                now = time.monotonic()
                if slideshow_paused:
                    if hold_frame is None:
                        hold_frame = last_frame
                    if hold_frame is None:
                        hold_frame = show_message(tw, th, ["Paused"])
                    frame = hold_frame
                    wait_ms = 30
                elif now + 0.001 < next_show and last_frame is not None:
                    frame = last_frame
                    wait_ms = max(1, int((next_show - now) * 1000))
                else:
                    while now > next_show + frame_period:
                        ok_skip, _ = video_reader.read()
                        if not ok_skip:
                            eof = True
                            break
                        next_show += frame_period
                        now = time.monotonic()
                    if eof:
                        break
                    ok, vf = video_reader.read()
                    if not ok or vf is None:
                        eof = True
                        break

                    frame = _compose_video_frame(
                        vf,
                        tw,
                        th,
                        item,
                        same_date_past_years=on_this_day_filter,
                        overlay_cache=video_overlay_cache,
                        full_canvas=use_canvas,
                    )
                    hold_frame = frame
                    last_frame = frame
                    frames_shown += 1
                    next_show = time.monotonic() + frame_period
                    wait_ms = 1

                nav, key_ex = pump_frame(
                    frame, mode, wait_ms=wait_ms, video_playback=True
                )
                if key_ex in (27, ord("q")):
                    exit_viewer()
                    return
                if try_pause_toggle(nav, key_ex):
                    continue
                if try_on_this_day_toggle(nav, key_ex):
                    release_cap()
                    break
                if nav == "prev":
                    index = (index - 1 + len(items)) % len(items)
                    release_cap()
                    break
                if nav == "next":
                    index = (index + 1) % len(items)
                    release_cap()
                    break
                if nav == "map":
                    mode = "map"
                    need_map_now_sync = True
                    release_cap()
                    break
                if nav == "gallery":
                    index = (index + 1) % len(items)
                    release_cap()
                    break
                if not slideshow_paused and time.monotonic() - t_video > video_max_ms / 1000.0:
                    index = (index + 1) % len(items)
                    release_cap()
                    break
                if key_ex == ord("r"):
                    reload_items()
                    release_cap()
                    break
            if eof and frames_shown == 0:
                frame = show_message(
                    tw,
                    th,
                    [
                        "Video could not be decoded.",
                        Path(path).name[:64],
                        "",
                        "→ next · q: quit",
                    ],
                )
                t0 = time.monotonic()
                while time.monotonic() - t0 < 2.5:
                    nav, key_ex = pump_frame(frame, mode)
                    if key_ex in (27, ord("q")):
                        exit_viewer()
                        return
                    if nav in ("prev", "next", "gallery"):
                        break
                release_cap()
            if eof:
                index = (index + 1) % len(items)
                release_cap()
            continue

        release_cap()
        if not path.is_file():
            frame = show_message(tw, th, [f"Missing file:", str(path)])
            t0 = time.monotonic()
            while time.monotonic() - t0 < min(2.0, slide_ms / 1000.0):
                nav, key_ex = pump_frame(frame, mode)
                if key_ex in (27, ord("q")):
                    exit_viewer()
                    return
                if try_on_this_day_toggle(nav, key_ex):
                    break
                if nav == "map":
                    mode = "map"
                    need_map_now_sync = True
                    break
                if nav == "prev":
                    index = (index - 1 + len(items)) % len(items)
                    break
                if nav == "next":
                    index = (index + 1) % len(items)
                    break
            else:
                index = (index + 1) % len(items)
            continue

        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            frame = show_message(tw, th, [f"Cannot decode image:", str(path)])
            t_end = time.monotonic() + slide_ms / 1000.0
            while time.monotonic() < t_end:
                nav, key_ex = pump_frame(frame, mode)
                if key_ex in (27, ord("q")):
                    exit_viewer()
                    return
                if try_on_this_day_toggle(nav, key_ex):
                    break
                if nav == "prev":
                    index = (index - 1 + len(items)) % len(items)
                    break
                if nav == "next":
                    index = (index + 1) % len(items)
                    break
            else:
                index = (index + 1) % len(items)
            continue

        frame = letterbox(bgr, tw, th)
        draw_item_metadata_overlay(frame, item, same_date_past_years=on_this_day_filter)
        t_end = time.monotonic() + slide_ms / 1000.0
        advanced = False
        while not auto_advance_deadline_passed(t_end):
            nav, key_ex = pump_frame(frame, mode)
            if key_ex in (27, ord("q")):
                exit_viewer()
                return
            if try_pause_toggle(nav, key_ex):
                continue
            if try_on_this_day_toggle(nav, key_ex):
                advanced = True
                break
            if nav == "prev":
                index = (index - 1 + len(items)) % len(items)
                advanced = True
                break
            if nav == "next":
                index = (index + 1) % len(items)
                advanced = True
                break
            if nav == "map":
                mode = "map"
                need_map_now_sync = True
                advanced = True
                break
            if key_ex == ord("r"):
                reload_items()
                advanced = True
                break
            time.sleep(0.02)
        if not advanced:
            index = (index + 1) % len(items)

    exit_viewer()


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="FramePi OpenCV kiosk viewer")
    ap.add_argument("--slide-ms", type=int, default=int(os.getenv("FRAMEPI_SLIDE_MS", SLIDE_MS_DEFAULT)))
    ap.add_argument("--video-max-ms", type=int, default=int(os.getenv("FRAMEPI_VIDEO_MAX_MS", VIDEO_MAX_MS_DEFAULT)))
    ap.add_argument("--focus", default=os.getenv("FRAMEPI_FOCUS", "").strip() or None)
    ap.add_argument("--no-sync", action="store_true", help="Do not start background Google Photos sync thread")
    ap.add_argument(
        "--remote-url",
        default=os.getenv("FRAMEPI_REMOTE_URL", "").strip(),
        help="SSE base URL (default http://127.0.0.1:$PORT). Set FRAMEPI_REMOTE=0 to disable.",
    )
    ap.add_argument("--no-remote", action="store_true", help="Do not connect to /api/remote/stream")
    args = ap.parse_args()

    port = int(os.getenv("PORT", "8080"))
    remote_base: str | None = None
    if not args.no_remote and os.getenv("FRAMEPI_REMOTE", "1").strip() != "0":
        remote_base = args.remote_url.strip() if args.remote_url.strip() else f"http://127.0.0.1:{port}"

    fp.DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_viewer(
        slide_ms=max(500, args.slide_ms),
        video_max_ms=max(5000, args.video_max_ms),
        focus=args.focus,
        start_sync=not args.no_sync,
        remote_stream_base=remote_base,
    )


if __name__ == "__main__":
    main()
