"""Lightweight MP4 proxies for smooth playback on Raspberry Pi-class hardware."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

_pi_zero_class: bool | None = None


def is_pi_zero_class() -> bool:
    """Raspberry Pi Zero / Zero 2 W (512MB RAM, no reliable HW decode)."""
    global _pi_zero_class
    if _pi_zero_class is not None:
        return _pi_zero_class
    model = ""
    try:
        model = Path("/proc/device-tree/model").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    _pi_zero_class = "zero" in model.lower()
    return _pi_zero_class


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def proxy_dir_for(data_dir: Path) -> Path:
    return data_dir / "video_proxy"


def proxy_path_for(source: Path, data_dir: Path) -> Path:
    return proxy_dir_for(data_dir) / f"{source.stem}.proxy.mp4"


def use_proxy_for_playback() -> bool:
    """
    Lightweight H.264 proxies for smooth Pi playback.
    Default: on for ARM (Pi), off elsewhere. Set ``FRAMEPI_VIDEO_PROXY=0`` to disable.
    """
    mode = os.getenv("FRAMEPI_VIDEO_PROXY", "").strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return False
    if mode in {"1", "true", "yes", "on"}:
        return True
    return platform.machine().lower() in ("aarch64", "armv7l", "armv6l")


def proxy_max_edge() -> int:
    q = os.getenv("FRAMEPI_VIDEO_QUALITY", "").strip().lower()
    if is_pi_zero_class():
        presets = {"low": 320, "medium": 400, "high": 480}
        default = 320
    else:
        presets = {"low": 426, "medium": 560, "high": 720}
        default = 560
    if q in presets:
        return presets[q]
    return _env_int("FRAMEPI_VIDEO_PROXY_MAX_EDGE", default)


def proxy_fps() -> int:
    # Pi Zero 2 W: 15 fps proxies play smoothly at correct speed; 24 fps decode cannot keep up.
    default = 15 if is_pi_zero_class() else 24
    return max(12, min(30, _env_int("FRAMEPI_VIDEO_PROXY_FPS", default)))


def proxy_crf() -> int:
    default = 28 if is_pi_zero_class() else 24
    return max(18, min(35, _env_int("FRAMEPI_VIDEO_PROXY_CRF", default)))


def proxy_x264_preset() -> str:
    if is_pi_zero_class():
        return "ultrafast"
    return "veryfast"


def _ffprobe_fps(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
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
                "stream=r_frame_rate",
                "-of",
                "csv=p=0",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        fps_s = proc.stdout.strip()
        if "/" in fps_s:
            num, den = fps_s.split("/", 1)
            return float(num) / float(den) if float(den) else None
        return float(fps_s)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _proxy_playback_too_slow(proxy: Path) -> bool:
    """Rebuild proxies whose frame rate does not match the current Pi target."""
    target = proxy_fps()
    fps = _ffprobe_fps(proxy)
    if fps is None:
        return False
    if fps < max(12.0, target - 2.0):
        return True
    # e.g. 24 fps proxies on Pi Zero 2 W — re-encode to 15 fps for sustainable playback
    return is_pi_zero_class() and fps > target + 2.0


def ensure_video_proxy(source: Path, data_dir: Path, *, force: bool = False) -> Path | None:
    """
    Build ``data/video_proxy/<stem>.proxy.mp4`` — small H.264, fixed FPS, tuned for Pi decode.
    Returns proxy path on success, else None.
    """
    if not source.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    out_dir = proxy_dir_for(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = proxy_path_for(source, data_dir)
    if (
        not force
        and dest.is_file()
        and dest.stat().st_mtime >= source.stat().st_mtime
        and dest.stat().st_size > 1024
    ):
        return dest

    edge = proxy_max_edge()
    fps = proxy_fps()
    crf = proxy_crf()
    tmp = dest.with_suffix(".tmp.mp4")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    vf = f"scale='min({edge},iw)':-2:flags=fast_bilinear,fps={fps}"
    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        proxy_x264_preset(),
        "-tune",
        "fastdecode",
        "-crf",
        str(crf),
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[video_proxy] ffmpeg failed for {source.name}: {exc}")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:400]
        print(f"[video_proxy] ffmpeg exit {proc.returncode} for {source.name}: {err}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        tmp.replace(dest)
    except OSError as exc:
        print(f"[video_proxy] could not move {tmp.name}: {exc}")
        return None
    print(f"[video_proxy] wrote {dest.name} ({dest.stat().st_size // 1024} KiB, {edge}px @ {fps}fps)")
    return dest


def resolve_playback_path(source: Path, data_dir: Path, *, build_if_missing: bool = True) -> Path:
    """Path to play: proxy on Pi when available (optionally build on demand)."""
    if not use_proxy_for_playback() or is_proxy_file(source, data_dir):
        return source
    proxy = proxy_path_for(source, data_dir)
    if proxy.is_file() and proxy.stat().st_mtime >= source.stat().st_mtime:
        if _proxy_playback_too_slow(proxy):
            rebuilt = ensure_video_proxy(source, data_dir, force=True)
            return rebuilt if rebuilt is not None else proxy
        return proxy
    if not build_if_missing:
        return source
    built = ensure_video_proxy(source, data_dir)
    return built if built is not None else source


def warm_video_proxy(source: Path, data_dir: Path) -> None:
    """Build proxy after sync or on a background thread (no-op when disabled)."""
    if not use_proxy_for_playback() or not source.is_file():
        return
    ensure_video_proxy(source, data_dir)


def is_proxy_file(path: Path, data_dir: Path) -> bool:
    try:
        return path.resolve().parent == proxy_dir_for(data_dir).resolve()
    except OSError:
        return "video_proxy" in path.parts and path.name.endswith(".proxy.mp4")
