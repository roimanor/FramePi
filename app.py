from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
)

from google_photos_album_sync import sync_google_photos_album
from metadata_store import MetadataStore, media_preview_urls
from shared_album_sync import enrich_item_placemark_tel_aviv_if_missing, sync_shared_album

load_dotenv()

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
METADATA_DB_PATH = DATA_DIR / "framepi.db"
METADATA_PATH = DATA_DIR / "metadata.json"  # legacy import only
PHOTOS_TOKEN_PATH = ROOT / "token_photos.json"
SHARED_ALBUM_URL = os.getenv("GOOGLE_PHOTOS_SHARED_ALBUM_URL", "").strip()
# Still read for opencv_viewer (static map) via `import app as fp`.
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()

# Default 15 min so new uploads appear without waiting a day; raise if you want less traffic.
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)

_sync_lock = threading.Lock()
_remote_sub_lock = threading.Lock()
_remote_now_lock = threading.Lock()
_remote_subscribers: list[queue.Queue] = []
_remote_now_state: dict | None = None
_remote_now_seq = 0
_remote_settings_state: dict | None = None

_REMOTE_NOW_ITEM_KEYS = frozenset(
    {
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
    }
)


def _library_album_id_from_env() -> str:
    raw = os.getenv("GOOGLE_PHOTOS_ALBUM_ID", "").strip()
    if not raw:
        return ""
    if raw.upper() in {"YOUR_ALBUM_ID", "REPLACE_ME", "CHANGEME", "YOUR-ID", "XXX"}:
        return ""
    if raw.startswith("<") and raw.endswith(">"):
        return ""
    return raw


def _remote_register() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=64)
    with _remote_sub_lock:
        _remote_subscribers.append(q)
    return q


def _remote_unregister(q: queue.Queue) -> None:
    with _remote_sub_lock:
        try:
            _remote_subscribers.remove(q)
        except ValueError:
            pass


def _remote_publish(event: dict) -> int:
    with _remote_sub_lock:
        subs = list(_remote_subscribers)
    n = 0
    for q in subs:
        try:
            q.put_nowait(event)
            n += 1
        except queue.Full:
            pass
    return n


def _remote_publish_key(key: str) -> int:
    return _remote_publish({"type": "key", "key": key})


def _sanitize_now_item(item: object) -> dict:
    if not isinstance(item, dict):
        return {}
    out: dict = {}
    for k in _REMOTE_NOW_ITEM_KEYS:
        if k not in item:
            continue
        v = item[k]
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _build_now_event(body: dict) -> dict:
    mode = body.get("mode")
    if mode not in ("gallery", "map", "empty"):
        raise ValueError("mode must be gallery, map, or empty")
    event: dict = {"type": "now", "mode": mode}
    if mode == "gallery":
        try:
            index = int(body.get("index", 0))
            total = int(body.get("total", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("index and total must be integers") from exc
        event["index"] = index
        event["total"] = total
        event["on_this_day"] = bool(body.get("on_this_day"))
        item = _sanitize_now_item(body.get("item"))
        event["item"] = item
        lines = body.get("lines")
        if isinstance(lines, list):
            event["lines"] = [str(x) for x in lines[:8]]
        preview = body.get("preview")
        if isinstance(preview, dict):
            event["preview"] = {
                k: str(preview[k])[:500]
                for k in ("google", "local")
                if k in preview and preview[k]
            }
        elif item:
            event["preview"] = media_preview_urls(item)
        if "paused" in body:
            event["paused"] = bool(body.get("paused"))
    elif mode == "map":
        pin = body.get("map_pin")
        if isinstance(pin, dict):
            try:
                pin_index = int(pin.get("index", 0))
                pin_total = int(pin.get("total", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("map_pin index and total must be integers") from exc
            event["map_pin"] = {
                "label": str(pin.get("label") or "").strip()[:120],
                "summary": str(pin.get("summary") or "").strip()[:120],
                "index": pin_index,
                "total": pin_total,
            }
    else:
        msg = str(body.get("message") or "").strip()
        if msg:
            event["message"] = msg[:240]
    seq = body.get("now_seq")
    if seq is not None:
        try:
            event["now_seq"] = int(seq)
        except (TypeError, ValueError):
            pass
    return event


def _safe_sync_once() -> None:
    with _sync_lock:
        try:
            lib_album = _library_album_id_from_env()
            if lib_album:
                sync_google_photos_album(lib_album, token_path=PHOTOS_TOKEN_PATH, output_dir=DATA_DIR)
            elif SHARED_ALBUM_URL:
                sync_shared_album(shared_album_url=SHARED_ALBUM_URL, output_dir=DATA_DIR)
            else:
                print("[sync] skipped: set GOOGLE_PHOTOS_ALBUM_ID or GOOGLE_PHOTOS_SHARED_ALBUM_URL in .env")
        except Exception as exc:
            print(f"[sync] failed: {exc}")


def _sync_loop() -> None:
    """Background album rescans (shared link or Library API)."""
    while True:
        _safe_sync_once()
        time.sleep(SYNC_INTERVAL_SECONDS)


def metadata_store() -> MetadataStore:
    return MetadataStore(METADATA_DB_PATH)


def _default_slide_ms() -> int:
    from opencv_viewer import SLIDE_MS_DEFAULT

    raw = os.getenv("FRAMEPI_SLIDE_MS", "").strip()
    if raw:
        try:
            return MetadataStore._clamp_slide_ms(int(raw))
        except ValueError:
            pass
    return SLIDE_MS_DEFAULT


def _effective_slide_ms() -> int:
    return metadata_store().get_slide_ms(_default_slide_ms())


def _settings_event(slide_ms: int) -> dict:
    return {"type": "settings", "slide_ms": slide_ms, "slide_seconds": slide_ms / 1000.0}


def _load_metadata():
    store = metadata_store()
    store.migrate_from_json(METADATA_PATH)
    data = store.load_metadata_dict()
    items = data.get("items")
    if isinstance(items, list):
        data = dict(data)
        data["items"] = [
            enrich_item_placemark_tel_aviv_if_missing(dict(x)) if isinstance(x, dict) else x
            for x in items
        ]
    return data


@app.route("/")
def root_redirect():
    return redirect("/remote", code=302)


@app.route("/remote")
def remote_control():
    return render_template("remote.html")


@app.route("/api/remote/stream")
def remote_stream():
    def generate():
        q = _remote_register()
        try:
            yield ": stream open\n\n"
            with _remote_now_lock:
                cached = _remote_now_state
            if cached:
                yield f"data: {json.dumps(cached)}\n\n"
            if _remote_settings_state:
                yield f"data: {json.dumps(_remote_settings_state)}\n\n"
            while True:
                try:
                    event = q.get(timeout=20)
                    if isinstance(event, dict):
                        yield f"data: {json.dumps(event)}\n\n"
                    elif isinstance(event, str):
                        yield f"data: {json.dumps({'type': 'key', 'key': event})}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            _remote_unregister(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/remote/command", methods=["POST"])
def remote_command():
    allowed = frozenset(
        {
            "ArrowLeft",
            "ArrowRight",
            "ArrowUp",
            "ArrowDown",
            "Enter",
            "OnThisDay",
            "ReloadMetadata",
            "TogglePause",
        }
    )
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    if key not in allowed:
        return jsonify(
            {
                "error": "key must be one of ArrowLeft, ArrowRight, ArrowUp, ArrowDown, Enter, OnThisDay, ReloadMetadata"
            }
        ), 400
    n = _remote_publish_key(key)
    return jsonify({"ok": True, "delivered_to_streams": n})


@app.route("/api/remote/settings", methods=["GET", "PATCH", "POST"])
def remote_settings():
    global _remote_settings_state
    if request.method == "GET":
        ms = _effective_slide_ms()
        return jsonify({"slide_ms": ms, "slide_seconds": ms / 1000.0})

    body = request.get_json(silent=True) or {}
    store = metadata_store()
    if "slide_seconds" in body:
        try:
            sec = float(body["slide_seconds"])
        except (TypeError, ValueError) as exc:
            return jsonify({"error": "slide_seconds must be a number"}), 400
        ms = store.set_slide_ms(int(sec * 1000))
    elif "slide_ms" in body:
        try:
            ms = store.set_slide_ms(int(body["slide_ms"]))
        except (TypeError, ValueError) as exc:
            return jsonify({"error": "slide_ms must be an integer"}), 400
    else:
        return jsonify({"error": "provide slide_seconds or slide_ms"}), 400

    event = _settings_event(ms)
    _remote_settings_state = event
    n = _remote_publish(event)
    return jsonify({"ok": True, "slide_ms": ms, "slide_seconds": ms / 1000.0, "delivered_to_streams": n})


@app.route("/api/remote/now", methods=["GET"])
def remote_now_get():
    with _remote_now_lock:
        state = _remote_now_state
    return jsonify(state or {})


@app.route("/api/media/<path:subpath>")
def serve_media(subpath: str):
    """Serve a local gallery file to the phone remote (fallback when Google URL fails)."""
    rel = Path(subpath.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        abort(404)
    path = (DATA_DIR / rel).resolve()
    data_root = DATA_DIR.resolve()
    if not str(path).startswith(str(data_root)) or not path.is_file():
        abort(404)
    return send_from_directory(path.parent, path.name)


@app.route("/api/remote/item", methods=["PATCH", "POST", "DELETE"])
def remote_item():
    if request.method == "DELETE":
        body = request.get_json(silent=True) or {}
        local_path = str(body.get("local_path") or request.args.get("local_path") or "").strip()
        item_id = str(body.get("id") or request.args.get("id") or "").strip()
        if not local_path and not item_id:
            return jsonify({"error": "local_path or id required"}), 400
        store = metadata_store()
        removed = store.delete_item(local_path=local_path, item_id=item_id)
        if removed is None:
            return jsonify({"error": "item not found"}), 404
        file_path = DATA_DIR / str(removed.get("local_path") or "").replace("\\", "/")
        if file_path.is_file():
            try:
                file_path.unlink()
            except OSError as exc:
                return jsonify({"error": f"failed to delete file: {exc}"}), 500
        _remote_publish_key("ReloadMetadata")
        return jsonify({"ok": True, "deleted": removed})

    # PATCH / POST — update metadata fields
    body = request.get_json(silent=True) or {}
    local_path = str(body.get("local_path") or "").strip()
    item_id = str(body.get("id") or "").strip()
    if not local_path and not item_id:
        return jsonify({"error": "local_path or id required"}), 400

    kwargs: dict = {"local_path": local_path, "item_id": item_id}
    if "name" in body:
        kwargs["name"] = body.get("name")
    if "created_time" in body:
        kwargs["created_time"] = body.get("created_time")
    if "location" in body:
        kwargs["location"] = body.get("location")
    if "city" in body:
        kwargs["city"] = body.get("city")
    if "country" in body:
        kwargs["country"] = body.get("country")
    if len(kwargs) <= 2:
        return jsonify(
            {"error": "provide at least one of name, created_time, location, city, country"}
        ), 400

    store = metadata_store()
    updated = store.update_user_fields(**kwargs)
    if updated is None:
        return jsonify({"error": "item not found"}), 404

    _remote_publish_key("ReloadMetadata")
    # Return DB row as-is (do not run Tel Aviv enrich — it can mask user-edited location).
    return jsonify({"ok": True, "item": updated})


@app.route("/api/remote/now", methods=["POST"])
def remote_now_post():
    global _remote_now_state, _remote_now_seq
    body = request.get_json(silent=True) or {}
    try:
        event = _build_now_event(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with _remote_now_lock:
        _remote_now_seq += 1
        event["now_seq"] = _remote_now_seq
        _remote_now_state = event
    n = _remote_publish(event)
    return jsonify({"ok": True, "delivered_to_streams": n, "now_seq": event["now_seq"]})


@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    """Run one album sync in the background (same work as the periodic sync loop)."""

    def run() -> None:
        _safe_sync_once()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started; watch logs for [sync][location] lines."})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _remote_settings_state = _settings_event(_effective_slide_ms())

    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    sync_thread = threading.Thread(target=_sync_loop, daemon=True)
    sync_thread.start()

    time.sleep(0.35)

    import opencv_viewer

    remote_base = os.getenv("FRAMEPI_REMOTE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
    opencv_viewer.run_viewer(
        slide_ms=_effective_slide_ms(),
        video_max_ms=max(5000, int(os.getenv("FRAMEPI_VIDEO_MAX_MS", str(opencv_viewer.VIDEO_MAX_MS_DEFAULT)))),
        focus=os.getenv("FRAMEPI_FOCUS", "").strip() or None,
        start_sync=False,
        remote_stream_base=remote_base if os.getenv("FRAMEPI_REMOTE", "1").strip() != "0" else None,
    )
