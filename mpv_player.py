"""Hardware-friendly video playback via mpv (Pi slideshow)."""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from video_proxy import is_pi_zero_class

# Outcomes for the gallery loop
RESULT_EOF = "eof"
RESULT_TIMEOUT = "timeout"
RESULT_FAILED = "failed"
RESULT_QUIT = "quit"
RESULT_PREV = "prev"
RESULT_NEXT = "next"
RESULT_MAP = "map"
RESULT_GALLERY = "gallery"
RESULT_RELOAD = "reload"
RESULT_ON_THIS_DAY = "on_this_day"

_daemon: MpvDaemon | None = None
_daemon_lock = threading.Lock()


def mpv_available() -> bool:
    return shutil.which("mpv") is not None


def _use_persistent_mpv() -> bool:
    return os.getenv("FRAMEPI_MPV_PERSIST", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def preferred_video_player() -> str:
    """``auto`` (default): mpv when installed, else OpenCV."""
    mode = os.getenv("FRAMEPI_VIDEO_PLAYER", "auto").strip().lower()
    if mode in {"opencv", "cv", "opencv2"}:
        return "opencv"
    if mode in {"mpv", "1", "true", "yes", "on"}:
        return "mpv"
    return "mpv" if mpv_available() else "opencv"


def warm_mpv_async() -> None:
    """Start idle mpv in the background so the first video slide opens faster."""
    if preferred_video_player() != "mpv" or not _use_persistent_mpv():
        return
    threading.Thread(target=_warm_mpv, daemon=True, name="framepi-mpv-warm").start()


def _warm_mpv() -> None:
    try:
        MpvDaemon.get().ensure_started()
    except Exception:
        pass


def shutdown_mpv() -> None:
    global _daemon
    with _daemon_lock:
        if _daemon is not None:
            _daemon.shutdown()
            _daemon = None


def _ipc_socket_path() -> Path:
    raw = os.getenv("FRAMEPI_MPV_IPC", "").strip()
    if raw:
        return Path(raw)
    import tempfile

    return Path(tempfile.gettempdir()) / "framepi-mpv.sock"


def _mpv_hwdec() -> str:
    return os.getenv("FRAMEPI_MPV_HWDEC", "auto").strip() or "auto"


def _mpv_vo() -> str:
    """Video output driver while playing (gpu, xv, etc.). Hidden idle daemon uses vo=null."""
    return os.getenv("FRAMEPI_MPV_VO", "gpu").strip() or "gpu"


def _mpv_cmd(mpv: str, sock_path: Path, *, idle: bool, path: Path | None = None) -> list[str]:
    cmd: list[str] = [
        mpv,
        "--no-terminal",
        "--no-osc",
        "--no-osd-bar",
        "--no-input-default-bindings",
        f"--input-ipc-server={sock_path}",
        f"--hwdec={_mpv_hwdec()}",
        "--no-border",
        "--cache=no",
    ]
    if idle:
        # No window while waiting — avoids a black fullscreen layer over OpenCV photos.
        cmd.extend(["--idle=yes", "--keep-open=always", "--vo=null"])
    else:
        cmd.extend(["--fs", "--ontop", "--keep-open=no"])
    if is_pi_zero_class():
        cmd.append("--profile=fast")
    if path is not None:
        cmd.append(str(path))
    return cmd


class MpvSession:
    """One mpv process per video (simple, reliable)."""

    def __init__(self, path: Path, *, start_paused: bool = False) -> None:
        self.path = path
        self.start_paused = start_paused
        self.sock_path = _ipc_socket_path()
        self._proc: subprocess.Popen[Any] | None = None

    def start(self) -> bool:
        mpv = shutil.which("mpv")
        if not mpv or not self.path.is_file():
            return False
        self.stop()
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass
        cmd = _mpv_cmd(mpv, self.sock_path, idle=False, path=self.path)
        if self.start_paused:
            cmd.insert(-1, "--pause")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        if not self._wait_ipc_ready(timeout=8.0):
            self.stop()
            return False
        return True

    def _wait_ipc_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            if self.sock_path.exists():
                try:
                    self._command(["get_property", "filename"], timeout=0.3)
                    return True
                except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                    pass
            time.sleep(0.03)
        return False

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self.running():
            try:
                self._command(["quit"], timeout=0.5)
            except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass

    def eof_reached(self) -> bool:
        if not self.running():
            return True
        try:
            res = self._command(["get_property", "eof-reached"], timeout=0.3)
            if isinstance(res, dict) and res.get("error") == "success":
                return bool(res.get("data"))
        except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
            pass
        return False

    def toggle_pause(self) -> bool | None:
        if not self.running():
            return None
        try:
            res = self._command(["cycle", "pause"], timeout=0.5)
            if isinstance(res, dict) and res.get("error") == "success":
                data = res.get("data")
                if isinstance(data, bool):
                    return data
        except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
            pass
        try:
            res = self._command(["get_property", "pause"], timeout=0.5)
            if isinstance(res, dict) and res.get("error") == "success":
                return bool(res.get("data"))
        except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
            pass
        return None

    def _command(self, command: list[Any], *, timeout: float = 2.0) -> dict[str, Any]:
        if not self.sock_path.exists():
            raise OSError("mpv IPC socket missing")
        payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.sock_path))
            sock.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            raise ValueError("empty mpv IPC response")
        data = json.loads(line.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ValueError("invalid mpv IPC response")
        return data


class MpvDaemon:
    """Long-lived mpv; each slide uses ``loadfile`` (faster repeat playback)."""

    def __init__(self) -> None:
        self.sock_path = _ipc_socket_path()
        self._proc: subprocess.Popen[Any] | None = None
        self._lock = threading.Lock()

    @classmethod
    def get(cls) -> MpvDaemon:
        global _daemon
        with _daemon_lock:
            if _daemon is None:
                _daemon = cls()
            return _daemon

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self) -> bool:
        with self._lock:
            return self._ensure_started_unlocked()

    def _ensure_started_unlocked(self) -> bool:
        if self.running():
            return True
        return self._spawn_idle()

    def _spawn_idle(self) -> bool:
        mpv = shutil.which("mpv")
        if not mpv:
            return False
        self._terminate_process()
        try:
            self.sock_path.parent.mkdir(parents=True, exist_ok=True)
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass
        try:
            self._proc = subprocess.Popen(
                _mpv_cmd(mpv, self.sock_path, idle=True),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._proc = None
            return False
        if not self._wait_ipc_ready(timeout=8.0):
            self._terminate_process()
            return False
        self._set_visible(False)
        return True

    def _wait_ipc_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            if self.sock_path.exists():
                try:
                    self._command(["get_property", "idle-active"], timeout=0.3)
                    return True
                except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                    pass
            time.sleep(0.03)
        return False

    def load(self, path: Path, *, start_paused: bool = False) -> bool:
        if not path.is_file():
            return False
        with self._lock:
            if not self._ensure_started_unlocked():
                return False
            try:
                self._set_visible(True)
                self._command(
                    ["loadfile", str(path.resolve()), "replace"],
                    timeout=5.0,
                )
                if not self._wait_until_playing(timeout=6.0):
                    return False
                self._command(["set_property", "pause", start_paused], timeout=0.5)
                if not start_paused:
                    self._command(["set_property", "time-pos", 0], timeout=0.5)
                return True
            except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                return False

    def _wait_until_playing(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return False
            try:
                idle = self._command(["get_property", "idle-active"], timeout=0.3)
                if isinstance(idle, dict) and idle.get("error") == "success":
                    if not bool(idle.get("data")):
                        return True
            except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                pass
            time.sleep(0.05)
        return False

    def eof_reached(self) -> bool:
        if not self.running():
            return True
        try:
            idle = self._command(["get_property", "idle-active"], timeout=0.3)
            if isinstance(idle, dict) and idle.get("error") == "success" and bool(idle.get("data")):
                return False
            res = self._command(["get_property", "eof-reached"], timeout=0.3)
            if isinstance(res, dict) and res.get("error") == "success":
                return bool(res.get("data"))
        except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
            pass
        return False

    def toggle_pause(self) -> bool | None:
        if not self.running():
            return None
        try:
            res = self._command(["cycle", "pause"], timeout=0.5)
            if isinstance(res, dict) and res.get("error") == "success":
                data = res.get("data")
                if isinstance(data, bool):
                    return data
        except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
            pass
        try:
            res = self._command(["get_property", "pause"], timeout=0.5)
            if isinstance(res, dict) and res.get("error") == "success":
                return bool(res.get("data"))
        except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
            pass
        return None

    def stop_playback(self) -> None:
        if not self.running():
            return
        with self._lock:
            try:
                self._command(["stop"], timeout=0.5)
            except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                pass
            self._set_visible(False)

    def shutdown(self) -> None:
        with self._lock:
            self._terminate_process()

    def _terminate_process(self) -> None:
        if self.running():
            try:
                self._command(["quit"], timeout=0.5)
            except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass

    def _set_visible(self, visible: bool) -> None:
        """Show video on screen, or vo=null so no black window covers OpenCV."""
        if visible:
            steps: list[tuple[str, Any]] = [
                ("vo", _mpv_vo()),
                ("fullscreen", True),
                ("ontop", True),
            ]
        else:
            steps = [
                ("ontop", False),
                ("fullscreen", False),
                ("vo", "null"),
            ]
        for prop, val in steps:
            try:
                self._command(["set_property", prop, val], timeout=0.5)
            except (OSError, json.JSONDecodeError, TimeoutError, ValueError):
                pass

    def _command(self, command: list[Any], *, timeout: float = 2.0) -> dict[str, Any]:
        if not self.sock_path.exists():
            raise OSError("mpv IPC socket missing")
        payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.sock_path))
            sock.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            raise ValueError("empty mpv IPC response")
        data = json.loads(line.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ValueError("invalid mpv IPC response")
        return data


def _play_with_session(
    path: Path,
    *,
    video_max_ms: int,
    mode: str,
    start_paused: bool,
    remote_q: queue.Queue[str] | None,
    settings_q: queue.Queue[dict[str, Any]] | None,
    apply_remote_settings: Callable[[], None],
    on_pause_changed: Callable[[bool], None],
    nav_from_remote_key: Callable[[str, str], str | None],
    decode_nav: Callable[[int], str | None],
    poll_wait_ms: int,
    session: MpvSession | MpvDaemon,
) -> str:
    t0 = time.monotonic()
    max_sec = max(1.0, video_max_ms / 1000.0)
    paused = start_paused

    def _pause_changed(p: bool) -> None:
        nonlocal paused
        paused = p
        on_pause_changed(p)

    try:
        while session.running():
            apply_remote_settings()
            nav: str | None = None
            key_ex = 0

            if remote_q is not None:
                try:
                    while True:
                        rk = remote_q.get_nowait()
                        n = nav_from_remote_key(rk, mode)
                        if n:
                            nav = n
                except queue.Empty:
                    pass

            if settings_q is not None:
                try:
                    while True:
                        settings_q.get_nowait()
                except queue.Empty:
                    pass

            import cv2

            key_ex = int(cv2.waitKeyEx(max(1, poll_wait_ms)))
            nk = decode_nav(key_ex)
            if nk is not None:
                nav = nk

            if key_ex in (27, ord("q")):
                return RESULT_QUIT
            if nav == "pause_toggle" or key_ex == ord(" "):
                p = session.toggle_pause()
                if p is not None:
                    _pause_changed(p)
                continue
            if nav == "prev":
                return RESULT_PREV
            if nav in ("next", "gallery"):
                return RESULT_NEXT if nav == "next" else RESULT_GALLERY
            if nav == "map":
                return RESULT_MAP
            if nav == "reload":
                return RESULT_RELOAD
            if nav == "on_this_day":
                return RESULT_ON_THIS_DAY
            if not paused and session.eof_reached():
                return RESULT_EOF
            if not paused and time.monotonic() - t0 > max_sec:
                return RESULT_TIMEOUT
        return RESULT_EOF
    finally:
        if isinstance(session, MpvDaemon):
            session.stop_playback()
        else:
            session.stop()


def play_video_mpv(
    path: Path,
    *,
    video_max_ms: int,
    mode: str,
    start_paused: bool,
    remote_q: queue.Queue[str] | None,
    settings_q: queue.Queue[dict[str, Any]] | None,
    apply_remote_settings: Callable[[], None],
    on_pause_changed: Callable[[bool], None],
    nav_from_remote_key: Callable[[str, str], str | None],
    decode_nav: Callable[[int], str | None],
    poll_wait_ms: int = 40,
) -> str:
    if _use_persistent_mpv():
        daemon = MpvDaemon.get()
        if not daemon.load(path, start_paused=start_paused):
            return RESULT_FAILED
        return _play_with_session(
            path,
            video_max_ms=video_max_ms,
            mode=mode,
            start_paused=start_paused,
            remote_q=remote_q,
            settings_q=settings_q,
            apply_remote_settings=apply_remote_settings,
            on_pause_changed=on_pause_changed,
            nav_from_remote_key=nav_from_remote_key,
            decode_nav=decode_nav,
            poll_wait_ms=poll_wait_ms,
            session=daemon,
        )

    session = MpvSession(path, start_paused=start_paused)
    if not session.start():
        return RESULT_FAILED
    return _play_with_session(
        path,
        video_max_ms=video_max_ms,
        mode=mode,
        start_paused=start_paused,
        remote_q=remote_q,
        settings_q=settings_q,
        apply_remote_settings=apply_remote_settings,
        on_pause_changed=on_pause_changed,
        nav_from_remote_key=nav_from_remote_key,
        decode_nav=decode_nav,
        poll_wait_ms=poll_wait_ms,
        session=session,
    )
