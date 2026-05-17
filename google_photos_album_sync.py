from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from google_photos_service import get_media_item, iter_album_media_items
from shared_album_sync import (
    GOOGLE_PHOTOS_HD_IMAGE_SUFFIX,
    _finalize_hd_photo_bytes,
    _extract_geo_from_album_html,
    _fetch_shared_album_html,
    _reverse_geocode_city_country,
    googleusercontent_media_stem,
    googleusercontent_url_base,
    tel_aviv_default_placemark,
    video_download_suffixes,
)


def _iso_or_none(value: object) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


def _parse_coord(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _media_location(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    loc = ((item.get("mediaMetadata") or {}).get("location") or {})
    lat = _parse_coord(loc.get("latitude"))
    lon = _parse_coord(loc.get("longitude"))
    if lat is not None and lon is not None:
        return lat, lon
    return None, None


def _item_with_full_metadata(token_path: Path, mi: Dict[str, Any], mid: str) -> Dict[str, Any]:
    """
    `mediaItems.search` often omits `mediaMetadata.location` (common for video).
    Merge in `mediaItems.get` when coordinates are missing from the list payload.
    """
    lat, lon = _media_location(mi)
    if lat is not None and lon is not None:
        return mi
    try:
        full = get_media_item(token_path, mid)
    except Exception:
        return mi
    merged = dict(mi)
    merged_meta = {**(mi.get("mediaMetadata") or {}), **(full.get("mediaMetadata") or {})}
    merged["mediaMetadata"] = merged_meta
    if full.get("mimeType"):
        merged["mimeType"] = full["mimeType"]
    if full.get("baseUrl"):
        merged["baseUrl"] = full["baseUrl"]
    return merged


def sync_google_photos_album(album_id: str, token_path: Path, output_dir: Path) -> List[Dict]:
    """
    Sync from Google Photos Library API.

    Location comes only from API coordinates (including after mediaItems.get) and optional
    numeric GPS hints from a shared album URL in GOOGLE_PHOTOS_SHARED_ALBUM_URL — not from UI strings.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    photos_dir = output_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)

    html_geo_by_stem: Dict[str, Tuple[float, float]] = {}
    shared_album_for_geo = os.getenv("GOOGLE_PHOTOS_SHARED_ALBUM_URL", "").strip()
    if shared_album_for_geo:
        try:
            html = _fetch_shared_album_html(shared_album_for_geo)
            html_geo_by_stem = _extract_geo_from_album_html(html)
        except Exception as exc:
            print(f"[gphotos sync] optional shared album HTML for geo: {exc}")

    from metadata_store import MetadataStore

    store = MetadataStore(output_dir / "framepi.db")
    migrated = store.migrate_from_json(output_dir / "metadata.json")
    if migrated:
        print(f"[gphotos sync] imported {migrated} item(s) from metadata.json into SQLite")

    session = requests.Session()
    geocode_cache: Dict[tuple[float, float], tuple[Optional[str], Optional[str]]] = {}
    current_managed: set[str] = set()

    for index, mi in enumerate(iter_album_media_items(token_path, album_id), start=1):
        mid = str(mi.get("id") or f"item-{index}")
        filename = str(mi.get("filename") or mid)
        mime = str(mi.get("mimeType") or "")
        if not str(mi.get("baseUrl") or ""):
            continue

        is_video = mime.lower().startswith("video/")
        mi = _item_with_full_metadata(token_path, mi, mid)
        base_url = str(mi.get("baseUrl") or "")
        if not base_url:
            continue

        ext = Path(filename).suffix.lower()
        if not ext:
            ext = ".mp4" if is_video else ".jpg"

        local_name = f"{mid}{ext}"
        local_path = photos_dir / local_name
        stem = googleusercontent_media_stem(base_url)
        google_url = googleusercontent_url_base(base_url)

        existing_row = store.get_by_google_stem(stem) if stem else store.get_by_local_path(
            f"photos/{local_name}"
        )
        if existing_row is not None and local_path.is_file():
            current_managed.add(local_name)
            continue

        if not local_path.exists():
            base = googleusercontent_url_base(base_url)
            if is_video:
                downloaded = False
                for suf in video_download_suffixes():
                    try:
                        r = session.get(base + suf, timeout=180)
                        if r.status_code != 200 or len(r.content) < 256:
                            continue
                        local_path.write_bytes(r.content)
                        downloaded = True
                        break
                    except Exception as exc:
                        print(f"[gphotos sync] video id={mid} suffix {suf}: {exc}")
                        continue
                if not downloaded:
                    print(f"[gphotos sync] skip video id={mid}: all URL variants failed")
                    continue
            else:
                r = session.get(base + GOOGLE_PHOTOS_HD_IMAGE_SUFFIX, timeout=90)
                if r.status_code != 200:
                    r = session.get(base + "=s1920", timeout=90)
                if r.status_code != 200:
                    r = session.get(base + "=w1920", timeout=90)
                if r.status_code != 200:
                    r = session.get(base + "=d", timeout=120)
                if r.status_code != 200:
                    print(f"[gphotos sync] skip photo id={mid}: HTTP {r.status_code} (HD and =d failed)")
                    continue
                payload, out_ext = _finalize_hd_photo_bytes(r.content, ext)
                local_name = f"{mid}{out_ext}"
                local_path = photos_dir / local_name
                local_path.write_bytes(payload)

        created = _iso_or_none(((mi.get("mediaMetadata") or {}).get("creationTime")))
        lat, lon = _media_location(mi)
        if (lat is None or lon is None) and html_geo_by_stem:
            if stem and stem in html_geo_by_stem:
                lat, lon = html_geo_by_stem[stem]

        city: Optional[str] = None
        country: Optional[str] = None
        if lat is not None and lon is not None:
            lat, lon = round(float(lat), 6), round(float(lon), 6)
            location_str = f"{lat:.6f}, {lon:.6f}"
            city, country = _reverse_geocode_city_country(lat, lon, geocode_cache)
        else:
            lat, lon, location_str, city, country = tel_aviv_default_placemark(geocode_cache)

        item_row = {
            "id": f"gphotos-{mid}",
            "google_url": google_url,
            "google_stem": stem,
            "filename": local_name,
            "local_path": f"photos/{local_name}",
            "mime_type": mime or ("video/mp4" if is_video else "image/jpeg"),
            "media_type": "video" if is_video else "image",
            "description": "",
            "created_time": created,
            "camera_make": None,
            "camera_model": None,
            "focal_length": None,
            "aperture_f_number": None,
            "iso_equivalent": None,
            "exposure_time": None,
            "location": location_str,
            "city": city,
            "country": country,
            "gps_latitude": lat,
            "gps_longitude": lon,
            "width": int(((mi.get("mediaMetadata") or {}).get("width") or 0) or 0),
            "height": int(((mi.get("mediaMetadata") or {}).get("height") or 0) or 0),
        }
        if store.insert_if_new(item_row):
            print(f"[gphotos sync] new to frame: {local_name}")
        current_managed.add(local_name)

    items = [
        it
        for it in store.list_all_items()
        if Path(str(it.get("local_path") or "")).name in current_managed
    ]
    store.touch_sync(source="google-photos-library-api", count=len(items))
    print(f"[gphotos sync] SQLite metadata: {len(items)} items in {store.db_path}")
    return items
