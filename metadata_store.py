"""SQLite metadata store — source of truth for frame media (paths, Google URLs, EXIF)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared_album_sync import _is_default_framepi_placeholder_coords, googleusercontent_url_base

_ITEM_COLUMNS = (
    "id",
    "google_url",
    "google_stem",
    "local_path",
    "filename",
    "name",
    "mime_type",
    "media_type",
    "description",
    "created_time",
    "camera_make",
    "camera_model",
    "focal_length",
    "aperture_f_number",
    "iso_equivalent",
    "exposure_time",
    "location",
    "city",
    "country",
    "gps_latitude",
    "gps_longitude",
    "width",
    "height",
    "added_at",
    "updated_at",
)


def google_stem_from_url(url: str) -> str:
    if not url:
        return ""
    base = googleusercontent_url_base(url.strip())
    if not base:
        return ""
    token = base.rstrip("/").split("/")[-1]
    return token.split("=")[0]


def google_url_from_stem(stem: str) -> str:
    s = (stem or "").strip()
    if not s:
        return ""
    return f"https://lh3.googleusercontent.com/{s}"


def _item_is_video(item: dict[str, Any]) -> bool:
    if str(item.get("media_type") or "").lower() == "video":
        return True
    mime = str(item.get("mime_type") or "").lower()
    if mime.startswith("video/"):
        return True
    lp = str(item.get("local_path") or "").lower()
    return lp.endswith((".mp4", ".webm", ".mov", ".m4v", ".mkv"))


def media_preview_urls(item: dict[str, Any]) -> dict[str, str]:
    """Browser preview URLs: Google (sized) and local frame copy."""
    google = str(item.get("google_url") or "").strip()
    if not google:
        stem = str(item.get("google_stem") or "").strip()
        if stem:
            google = google_url_from_stem(stem)
    google_preview = ""
    if google:
        base = googleusercontent_url_base(google)
        google_preview = base + ("=m18" if _item_is_video(item) else "=w800-rw")
    lp = str(item.get("local_path") or "").strip().replace("\\", "/")
    local = f"/api/media/{lp}" if lp else ""
    return {"google": google_preview, "local": local}


_EXCLUDED_STEMS_KEY = "excluded_google_stems"
_SLIDE_MS_KEY = "slide_ms"
SLIDE_MS_MIN = 500
SLIDE_MS_MAX = 3_600_000


class MetadataStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS media (
                        id TEXT PRIMARY KEY,
                        google_url TEXT,
                        google_stem TEXT,
                        local_path TEXT NOT NULL UNIQUE,
                        filename TEXT NOT NULL,
                        name TEXT NOT NULL DEFAULT '',
                        mime_type TEXT,
                        media_type TEXT,
                        description TEXT DEFAULT '',
                        created_time TEXT,
                        camera_make TEXT,
                        camera_model TEXT,
                        focal_length TEXT,
                        aperture_f_number TEXT,
                        iso_equivalent INTEGER,
                        exposure_time TEXT,
                        location TEXT,
                        city TEXT,
                        country TEXT,
                        gps_latitude REAL,
                        gps_longitude REAL,
                        width INTEGER DEFAULT 0,
                        height INTEGER DEFAULT 0,
                        added_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_media_google_stem
                        ON media(google_stem) WHERE google_stem IS NOT NULL AND google_stem != '';
                    CREATE INDEX IF NOT EXISTS idx_media_google_url ON media(google_url);
                    CREATE TABLE IF NOT EXISTS sync_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(media)").fetchall()
                }
                if "name" not in cols:
                    conn.execute(
                        "ALTER TABLE media ADD COLUMN name TEXT NOT NULL DEFAULT ''"
                    )
                conn.commit()
            finally:
                conn.close()

    def migrate_from_json(self, json_path: Path) -> int:
        """Import legacy metadata.json rows not already in the database."""
        if not json_path.is_file():
            return 0
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        items = data.get("items")
        if not isinstance(items, list):
            return 0
        n = 0
        for raw in items:
            if isinstance(raw, dict):
                if self.insert_if_new(raw):
                    n += 1
        if n:
            self.set_sync_meta("source", str(data.get("source") or "imported-json"))
        return n

    def get_by_google_stem(self, stem: str) -> dict[str, Any] | None:
        s = (stem or "").strip()
        if not s:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM media WHERE google_stem = ? LIMIT 1", (s,)
                ).fetchone()
                return self._row_to_item(row) if row else None
            finally:
                conn.close()

    def get_by_local_path(self, local_path: str) -> dict[str, Any] | None:
        lp = self._norm_local_path(local_path)
        if not lp:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM media WHERE local_path = ? LIMIT 1", (lp,)
                ).fetchone()
                return self._row_to_item(row) if row else None
            finally:
                conn.close()

    def is_known_to_frame(self, *, google_stem: str = "", local_path: str = "") -> bool:
        if google_stem and self.get_by_google_stem(google_stem):
            return True
        if local_path and self.get_by_local_path(local_path):
            return True
        return False

    def insert_if_new(self, item: dict[str, Any]) -> bool:
        """Insert only when this Google item or local path is not already on the frame."""
        prepared = self._prepare_item(item)
        stem = prepared.get("google_stem") or ""
        lp = prepared.get("local_path") or ""
        if stem and self.is_excluded_stem(stem):
            return False
        if stem and self.get_by_google_stem(stem):
            return False
        if lp and self.get_by_local_path(lp):
            return False
        self._insert(prepared, or_replace=False)
        return True

    def is_excluded_stem(self, stem: str) -> bool:
        s = (stem or "").strip()
        if not s:
            return False
        return s in self._load_excluded_stems()

    def exclude_stem(self, stem: str) -> None:
        s = (stem or "").strip()
        if not s:
            return
        stems = self._load_excluded_stems()
        if s in stems:
            return
        stems.add(s)
        self._save_excluded_stems(stems)

    def _load_excluded_stems(self) -> set[str]:
        raw = self.get_sync_meta(_EXCLUDED_STEMS_KEY)
        if not raw:
            return set()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        if not isinstance(data, list):
            return set()
        return {str(x).strip() for x in data if str(x).strip()}

    def _save_excluded_stems(self, stems: set[str]) -> None:
        self.set_sync_meta(_EXCLUDED_STEMS_KEY, json.dumps(sorted(stems)))

    def delete_item(self, *, local_path: str = "", item_id: str = "") -> dict[str, Any] | None:
        """Remove one item from the frame DB; caller deletes the file on disk."""
        existing = None
        if item_id:
            row = self._fetch_row_by_id(item_id.strip())
            existing = self._row_to_item(row) if row else None
        if existing is None and local_path:
            existing = self.get_by_local_path(local_path)
        if existing is None:
            return None
        stem = str(existing.get("google_stem") or "").strip()
        if stem:
            self.exclude_stem(stem)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM media WHERE id = ?", (existing["id"],))
                conn.commit()
            finally:
                conn.close()
        return existing

    def save_item(self, item: dict[str, Any]) -> None:
        """Insert or replace a row (preserves ``added_at`` when the row already exists)."""
        prepared = self._prepare_item(item)
        existing = self.get_by_local_path(prepared["local_path"])
        if existing:
            row = self._fetch_row_by_id(existing["id"])
            if row:
                prepared["added_at"] = row["added_at"]
                prepared["id"] = row["id"]
            if "name" not in item:
                prepared["name"] = existing.get("name") or ""
            for field in ("created_time", "location", "city", "country"):
                if field not in item and existing.get(field):
                    prepared[field] = existing[field]
        self._insert(prepared, or_replace=True)

    def update_user_fields(
        self,
        *,
        local_path: str = "",
        item_id: str = "",
        name: str | None = None,
        created_time: str | None = None,
        location: str | None = None,
        city: str | None = None,
        country: str | None = None,
    ) -> dict[str, Any] | None:
        """Update user-editable fields for one item (from remote edits)."""
        existing = None
        if item_id:
            row = self._fetch_row_by_id(item_id.strip())
            existing = self._row_to_item(row) if row else None
        if existing is None and local_path:
            existing = self.get_by_local_path(local_path)
        if existing is None:
            return None

        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = str(name).strip()[:200]
        if created_time is not None:
            ct = str(created_time).strip()
            updates["created_time"] = ct[:72] if ct else None
        if location is not None:
            loc = str(location).strip()
            updates["location"] = loc[:240] if loc else None
            if loc:
                lat, lon = existing.get("gps_latitude"), existing.get("gps_longitude")
                if lat is not None and lon is not None and _is_default_framepi_placeholder_coords(
                    lat, lon
                ):
                    updates["gps_latitude"] = None
                    updates["gps_longitude"] = None
        if city is not None:
            c = str(city).strip()
            updates["city"] = c[:120] if c else None
        if country is not None:
            co = str(country).strip()
            updates["country"] = co[:120] if co else None

        if not updates:
            return existing

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [existing["id"]]
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE media SET {set_clause} WHERE id = ?", values)
                conn.commit()
            finally:
                conn.close()
        return self.get_by_local_path(existing["local_path"])

    def list_all_items(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM media ORDER BY COALESCE(created_time, added_at) DESC, local_path"
                ).fetchall()
                return [self._row_to_item(r) for r in rows]
            finally:
                conn.close()

    def load_metadata_dict(self) -> dict[str, Any]:
        items = self.list_all_items()
        return {
            "updated_at": self.get_sync_meta("updated_at"),
            "source": self.get_sync_meta("source"),
            "count": len(items),
            "items": items,
        }

    def set_sync_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO sync_meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
                conn.commit()
            finally:
                conn.close()

    def get_sync_meta(self, key: str) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM sync_meta WHERE key = ? LIMIT 1", (key,)
                ).fetchone()
                return str(row["value"]) if row else None
            finally:
                conn.close()

    def get_slide_ms(self, default: int) -> int:
        raw = self.get_sync_meta(_SLIDE_MS_KEY)
        if not raw:
            return self._clamp_slide_ms(default)
        try:
            return self._clamp_slide_ms(int(raw))
        except (TypeError, ValueError):
            return self._clamp_slide_ms(default)

    def set_slide_ms(self, ms: int) -> int:
        clamped = self._clamp_slide_ms(int(ms))
        self.set_sync_meta(_SLIDE_MS_KEY, str(clamped))
        return clamped

    @staticmethod
    def _clamp_slide_ms(ms: int) -> int:
        return max(SLIDE_MS_MIN, min(SLIDE_MS_MAX, int(ms)))

    def touch_sync(self, *, source: str, count: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.set_sync_meta("updated_at", now)
        self.set_sync_meta("source", source)
        if count is not None:
            self.set_sync_meta("count", str(count))

    def delete_by_filename(self, filename: str) -> None:
        name = Path(filename).name
        if not name:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM media WHERE filename = ?", (name,))
                conn.commit()
            finally:
                conn.close()

    def _norm_local_path(self, local_path: str) -> str:
        p = str(local_path or "").strip().replace("\\", "/")
        if not p:
            return ""
        if p.startswith("photos/"):
            return p
        return f"photos/{Path(p).name}"

    def _prepare_item(self, item: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        lp = self._norm_local_path(str(item.get("local_path") or ""))
        filename = str(item.get("filename") or "").strip() or Path(lp).name
        google_url = str(item.get("google_url") or "").strip()
        if not google_url:
            stem_guess = google_stem_from_url(str(item.get("google_stem") or ""))
            if stem_guess:
                google_url = google_url_from_stem(stem_guess)
            elif filename:
                google_url = google_url_from_stem(Path(filename).stem)
        stem = str(item.get("google_stem") or "").strip() or google_stem_from_url(google_url)
        if google_url and not stem:
            stem = google_stem_from_url(google_url)
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            item_id = f"media-{stem or Path(filename).stem}"
        return {
            "id": item_id,
            "google_url": google_url or None,
            "google_stem": stem or None,
            "local_path": lp,
            "filename": filename,
            "name": str(item.get("name") or "").strip()[:200],
            "mime_type": item.get("mime_type"),
            "media_type": item.get("media_type"),
            "description": str(item.get("description") or ""),
            "created_time": item.get("created_time"),
            "camera_make": item.get("camera_make"),
            "camera_model": item.get("camera_model"),
            "focal_length": item.get("focal_length"),
            "aperture_f_number": item.get("aperture_f_number"),
            "iso_equivalent": item.get("iso_equivalent"),
            "exposure_time": item.get("exposure_time"),
            "location": item.get("location"),
            "city": item.get("city"),
            "country": item.get("country"),
            "gps_latitude": item.get("gps_latitude"),
            "gps_longitude": item.get("gps_longitude"),
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
            "added_at": now,
            "updated_at": now,
        }

    def _fetch_row_by_id(self, item_id: str) -> sqlite3.Row | None:
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute("SELECT * FROM media WHERE id = ? LIMIT 1", (item_id,)).fetchone()
            finally:
                conn.close()

    def _insert(self, prepared: dict[str, Any], *, or_replace: bool) -> None:
        keys = list(_ITEM_COLUMNS)
        values = [prepared.get(c) for c in keys]
        placeholders = ", ".join("?" for _ in keys)
        col_names = ", ".join(keys)
        verb = "INSERT OR REPLACE" if or_replace else "INSERT"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"{verb} INTO media ({col_names}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    def _row_to_item(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        return {
            "id": d["id"],
            "google_url": d.get("google_url"),
            "google_stem": d.get("google_stem"),
            "local_path": d["local_path"],
            "filename": d["filename"],
            "name": d.get("name") or "",
            "mime_type": d["mime_type"],
            "media_type": d["media_type"],
            "description": d.get("description") or "",
            "created_time": d.get("created_time"),
            "camera_make": d.get("camera_make"),
            "camera_model": d.get("camera_model"),
            "focal_length": d.get("focal_length"),
            "aperture_f_number": d.get("aperture_f_number"),
            "iso_equivalent": d.get("iso_equivalent"),
            "exposure_time": d.get("exposure_time"),
            "location": d.get("location"),
            "city": d.get("city"),
            "country": d.get("country"),
            "gps_latitude": d.get("gps_latitude"),
            "gps_longitude": d.get("gps_longitude"),
            "width": d.get("width") or 0,
            "height": d.get("height") or 0,
        }
