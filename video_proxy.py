"""Lightweight MP4 proxies for smooth playback on Raspberry Pi-class hardware."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


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
    """Off by default; use ``FRAMEPI_VIDEO_PROXY=1`` if you want ffmpeg re-encode at playback time."""
    mode = os.getenv("FRAMEPI_VIDEO_PROXY", "0").strip().lower()
    return mode in {"1", "true", "yes", "on"}


def proxy_max_edge() -> int:
    q = os.getenv("FRAMEPI_VIDEO_QUALITY", "").strip().lower()
    presets = {"low": 426, "medium": 560, "high": 720}
    if q in presets:
        return presets[q]
    return _env_int("FRAMEPI_VIDEO_PROXY_MAX_EDGE", 560)


def proxy_fps() -> int:
    return max(8, min(24, _env_int("FRAMEPI_VIDEO_PROXY_FPS", 12)))


def proxy_crf() -> int:
    return max(18, min(35, _env_int("FRAMEPI_VIDEO_PROXY_CRF", 24)))


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
        "veryfast",
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


def resolve_playback_path(source: Path, data_dir: Path) -> Path:
    """Path to play: proxy on Pi when available (build on demand if needed)."""
    if not use_proxy_for_playback() or is_proxy_file(source, data_dir):
        return source
    proxy = proxy_path_for(source, data_dir)
    if proxy.is_file() and proxy.stat().st_mtime >= source.stat().st_mtime:
        return proxy
    built = ensure_video_proxy(source, data_dir)
    return built if built is not None else source


def is_proxy_file(path: Path, data_dir: Path) -> bool:
    try:
        return path.resolve().parent == proxy_dir_for(data_dir).resolve()
    except OSError:
        return "video_proxy" in path.parts and path.name.endswith(".proxy.mp4")
