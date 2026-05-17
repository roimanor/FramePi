from __future__ import annotations

import json
import re
import os
import io
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import requests
from PIL import ExifTags, Image, ImageOps

if TYPE_CHECKING:
    from metadata_store import MetadataStore

IMAGE_URL_PATTERN = re.compile(
    r"https://(?:lh3|lh4|lh5|lh6)\.googleusercontent\.com/[A-Za-z0-9_\-=/]+|"
    r"https://photos\.fife\.usercontent\.google\.com/[A-Za-z0-9_\-=/]+"
)
# Embedded in photos.app.goo.gl shell HTML (Python GET often does not HTTP-redirect to share page).
_SHARE_PAGE_URL_IN_HTML = re.compile(r"https://photos\.google\.com/share/[A-Za-z0-9_\-\?=&%.]+")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv"})

# Stored files: max HD box (Google resizes to fit inside; preserves aspect ratio).
GOOGLE_PHOTOS_HD_MAX_W = 1920
GOOGLE_PHOTOS_HD_MAX_H = 1080
GOOGLE_PHOTOS_HD_IMAGE_SUFFIX = f"=w{GOOGLE_PHOTOS_HD_MAX_W}-h{GOOGLE_PHOTOS_HD_MAX_H}"
# googleusercontent video tiers: =m37 ~1080p, =m22 ~720p, =m18 ~360–480p (lightest), =dv original.
# Default tries the smallest transcode first (best for Pi Zero playback).
GOOGLE_PHOTOS_HD_VIDEO_SUFFIXES: Tuple[str, ...] = ("=m18", "=m22", "=m37", "=dv")


def video_download_suffixes() -> Tuple[str, ...]:
    """
    Directive order for shared-album / Library video downloads.

    Override with ``GOOGLE_PHOTOS_VIDEO_SUFFIX_ORDER`` (comma/space; leading ``=`` optional).
    Examples: ``m18`` (smallest, Pi-friendly), ``m18,m22``, ``m22,m18,m37,dv``.
    """
    raw = os.getenv("GOOGLE_PHOTOS_VIDEO_SUFFIX_ORDER", "").strip()
    if not raw:
        return GOOGLE_PHOTOS_HD_VIDEO_SUFFIXES
    parts: List[str] = []
    for token in re.split(r"[\s,]+", raw):
        t = token.strip()
        if not t:
            continue
        if not t.startswith("="):
            t = "=" + t
        parts.append(t)
    return tuple(parts) if parts else GOOGLE_PHOTOS_HD_VIDEO_SUFFIXES


def googleusercontent_url_base(url: str) -> str:
    """Strip trailing size/format directives (first '=' onward)."""
    return url.split("=")[0]


def _sniff_media(content: bytes) -> Tuple[str, str]:
    """Return (file_extension_with_dot, media_type) from leading bytes."""
    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return ".jpg", "image"
    if len(content) >= 8 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12]
        if brand == b"qt  ":
            return ".mov", "video"
        return ".mp4", "video"
    if len(content) >= 4 and content[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm", "video"
    return ".jpg", "image"


def _classify_media(content: bytes, content_type: str) -> Tuple[str, str, str]:
    """
    Return (extension_with_dot, media_type, mime_type).
    Prefer magic-byte sniff over Content-Type (shared album responses are often generic).
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    ext, kind = _sniff_media(content)
    if kind == "video":
        if ext == ".webm":
            mime = "video/webm"
        elif ext == ".mov":
            mime = "video/quicktime"
        else:
            mime = "video/mp4"
        return ext, kind, mime
    if ct.startswith("video/"):
        if "webm" in ct:
            return ".webm", "video", "video/webm"
        return ".mp4", "video", ct or "video/mp4"
    if ct == "image/png" or ext == ".png":
        return ".png", "image", "image/png"
    if ct == "image/jpeg" or ext == ".jpg":
        return ".jpg", "image", "image/jpeg"
    return ext, kind, "image/jpeg" if ext == ".jpg" else "image/png" if ext == ".png" else "application/octet-stream"


def _normalize_exif_datetime(value: object) -> Optional[str]:
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    # Common EXIF format: "YYYY:MM:DD HH:MM:SS"
    try:
        parsed = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
        return parsed.isoformat()
    except ValueError:
        pass

    # Already ISO-like; keep normalized if parseable.
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.isoformat()
    except ValueError:
        return None


def _normalize_download_url(raw_url: str) -> str | None:
    if "googleusercontent.com/a/" in raw_url:
        # Profile/avatar assets appear in album HTML and are not downloadable photos.
        return None
    if "googleusercontent.com/p/" not in raw_url and "googleusercontent.com/pw/" not in raw_url:
        return None

    # Resize-to-fit inside HD box (not full "=d" originals — saves space on device).
    base = raw_url.split("=")[0]
    return f"{base}{GOOGLE_PHOTOS_HD_IMAGE_SUFFIX}"


def _download_with_sniff(url: str, timeout: int = 30) -> tuple[bytes, str, str, str]:
    """
    Download URL and classify from the leading bytes.

    Returns: (body_bytes, extension_with_dot, media_kind, mime_type)
    """
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"download failed {r.status_code}")
    body = r.content
    header_ct = r.headers.get("Content-Type", "")
    ext, media_kind, mime_type = _classify_media(body[:65536], header_ct)
    return body, ext, media_kind, mime_type


def _download_googleusercontent_preferred_size(url_with_any_suffix: str, stem_hint: str = "") -> tuple[bytes, str, str, str]:
    """
    Prefer HD (=w*h); fall back to full (=d) when Google rejects resize or returns errors.
    """
    base = url_with_any_suffix.split("=")[0]
    errs: List[str] = []
    for suffix in (GOOGLE_PHOTOS_HD_IMAGE_SUFFIX, "=s1920", "=w1920", "=d"):
        attempt = f"{base}{suffix}"
        try:
            return _download_with_sniff(attempt)
        except Exception as exc:
            errs.append(f"{suffix}:{exc}")
            continue
    hint = f" stem={stem_hint}" if stem_hint else ""
    print(f"[sync] download failed{hint}: {' | '.join(errs)}")
    raise RuntimeError("; ".join(errs))


def _extract_exif_from_bytes(content: bytes) -> Dict[str, object]:
    """Like _extract_exif, but reads from bytes so we can try alternate download variants."""
    metadata = {
        "created_time": None,
        "camera_make": None,
        "camera_model": None,
        "focal_length": None,
        "aperture_f_number": None,
        "iso_equivalent": None,
        "exposure_time": None,
        "location": None,
        "gps_latitude": None,
        "gps_longitude": None,
        "width": 0,
        "height": 0,
    }
    try:
        with Image.open(io.BytesIO(content)) as image:
            metadata["width"] = image.width
            metadata["height"] = image.height
            exif = image.getexif()
            if not exif:
                return metadata
            tag_map = {ExifTags.TAGS.get(tag, str(tag)): value for tag, value in exif.items()}
            metadata["camera_make"] = str(tag_map.get("Make")) if tag_map.get("Make") else None
            metadata["camera_model"] = str(tag_map.get("Model")) if tag_map.get("Model") else None
            metadata["iso_equivalent"] = tag_map.get("ISOSpeedRatings")
            metadata["created_time"] = (
                _normalize_exif_datetime(tag_map.get("DateTimeOriginal"))
                or _normalize_exif_datetime(tag_map.get("DateTimeDigitized"))
                or _normalize_exif_datetime(tag_map.get("DateTime"))
            )
            metadata["exposure_time"] = str(tag_map.get("ExposureTime")) if tag_map.get("ExposureTime") else None
            metadata["aperture_f_number"] = str(tag_map.get("FNumber")) if tag_map.get("FNumber") else None
            metadata["focal_length"] = str(tag_map.get("FocalLength")) if tag_map.get("FocalLength") else None

            lat_lon = _extract_gps(exif)
            if lat_lon:
                lat, lon = lat_lon
                metadata["gps_latitude"] = round(lat, 6)
                metadata["gps_longitude"] = round(lon, 6)
                metadata["location"] = f"{lat:.6f}, {lon:.6f}"
    except Exception:
        return metadata
    return metadata


def _gps_coordinates_from_heavy_variants(
    normalized_url: str,
    *,
    variant_suffixes: Tuple[str, ...] = ("=s0", "=w0-h0", "=d"),
    timeout: int = 30,
) -> Optional[Tuple[float, float]]:
    """
    Full-size googleusercontent variants sometimes carry EXIF GPS that stripped HD JPEGs omit.

    Used for metadata only — never persist these downloads as the on-disk image (that would undo HD limits).
    """
    base = normalized_url.split("=")[0]
    for suf in variant_suffixes:
        u = base + suf
        try:
            body, _, kind, _ = _download_with_sniff(u, timeout=timeout)
        except Exception:
            continue
        if kind != "image":
            del body
            continue
        exif = _extract_exif_from_bytes(body)
        del body
        lat = exif.get("gps_latitude")
        lon = exif.get("gps_longitude")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    return None


def _gps_from_google_photo_media_stem(stem: str) -> Optional[Tuple[float, float]]:
    """
    Recover EXIF GPS from googleusercontent full-size variants (HD files often strip GPS).

    Fast path: ``=s0`` on lh3 /pw, /p, and fife /pw. If still missing, one full variant pass on lh3 /pw.
    """
    quick_bases = (
        f"https://lh3.googleusercontent.com/pw/{stem}",
        f"https://lh3.googleusercontent.com/p/{stem}",
        f"https://photos.fife.usercontent.google.com/pw/{stem}",
    )
    for raw in quick_bases:
        normalized = _normalize_download_url(raw)
        if not normalized:
            continue
        p = _gps_coordinates_from_heavy_variants(
            normalized, variant_suffixes=("=s0",), timeout=12
        )
        if p:
            return p
    raw = f"https://lh3.googleusercontent.com/pw/{stem}"
    normalized = _normalize_download_url(raw)
    if normalized:
        p = _gps_coordinates_from_heavy_variants(
            normalized, variant_suffixes=("=s0", "=w0-h0", "=d"), timeout=22
        )
        if p:
            return p
    return None


_TEL_AVIV_PLACEHOLDER_LAT = 32.0853
_TEL_AVIV_PLACEHOLDER_LON = 34.7818


def _is_default_framepi_placeholder_coords(lat: object, lon: object) -> bool:
    """True only for coords from ``tel_aviv_default_placemark`` (not real photos near Tel Aviv)."""
    try:
        a, b = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return (
        abs(a - _TEL_AVIV_PLACEHOLDER_LAT) < 5e-5
        and abs(b - _TEL_AVIV_PLACEHOLDER_LON) < 5e-5
    )


def _backfill_gps_from_google_heavy_for_images(
    items: List[Dict[str, Any]],
    geocode_cache: Dict[Tuple[float, float], Tuple[Optional[str], Optional[str]]],
    max_calls: int = 12,
) -> int:
    """Fill missing GPS or replace default Tel Aviv placeholder using Google full-size EXIF (sync only)."""
    calls = 0
    for it in items:
        if calls >= max_calls:
            break
        if it.get("media_type") != "image":
            continue
        lp = str(it.get("local_path") or "")
        if not lp.startswith("photos/"):
            continue
        name = Path(lp).name
        if not _DISK_ONLY_GOOGLE_FILENAME.match(name):
            continue
        stem = Path(lp).stem
        lat, lon = it.get("gps_latitude"), it.get("gps_longitude")
        need = lat is None or lon is None or _is_default_framepi_placeholder_coords(lat, lon)
        if not need:
            continue
        gps = _gps_from_google_photo_media_stem(stem)
        if not gps:
            continue
        gla, glo = round(gps[0], 6), round(gps[1], 6)
        it["gps_latitude"] = gla
        it["gps_longitude"] = glo
        it["location"] = f"{gla:.6f}, {glo:.6f}"
        city, country = _reverse_geocode_city_country(gla, glo, geocode_cache)
        it["city"] = city
        it["country"] = country
        calls += 1
        print(
            f"[sync][location] backfilled from Google full-size EXIF: {name} "
            f"lat={gla} lon={glo} city={city or '-'} country={country or '-'}"
        )
    return calls


def _finalize_hd_photo_bytes(content: bytes, ext: str) -> Tuple[bytes, str]:
    """
    Normalize stored photos so the Pi/browser never decodes more pixels than the HD box.

    - Apply EXIF orientation before measuring (avoids skipping resize on rotated files).
    - Downscale when either side exceeds the HD box.
    - Re-encode as progressive JPEG when resizing or when source is PNG/WebP (stable decode cost).

    Full-resolution downloads are fine upstream; only these bytes are persisted.
    Returns (bytes, suffix) — suffix may change (e.g. ``.png`` → ``.jpg``).
    """
    ext_lower = (ext or "").lower()
    if ext_lower not in (".jpg", ".jpeg", ".png", ".webp"):
        return content, ext
    try:
        with Image.open(io.BytesIO(content)) as raw:
            im = ImageOps.exif_transpose(raw)
        mw, mh = GOOGLE_PHOTOS_HD_MAX_W, GOOGLE_PHOTOS_HD_MAX_H
        fits = im.width <= mw and im.height <= mh
        if fits and ext_lower in (".jpg", ".jpeg"):
            return content, ext
        work = im.copy()
        if not fits:
            work.thumbnail((mw, mh), Image.Resampling.LANCZOS)
        rgb = work.convert("RGB") if work.mode in ("RGBA", "P", "LA") else work
        if rgb.mode != "RGB":
            rgb = rgb.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=87, optimize=True, progressive=True)
        return buf.getvalue(), ".jpg"
    except Exception as exc:
        print(f"[sync] HD finalize failed ({ext}): {exc}")
        return content, ext


def _try_video_variants(normalized_url: str) -> tuple[bytes, str, str, str] | None:
    """
    Shared album HTML often links to poster JPGs for videos.

    Try a small set of known googleusercontent video directives. If any returns
    MP4/WebM bytes (sniffed), return the downloaded payload tuple.
    """
    base = normalized_url.split("=")[0]
    candidates = [f"{base}{s}" for s in video_download_suffixes()]
    for u in candidates:
        try:
            body, ext, kind, mime = _download_with_sniff(u)
        except Exception:
            continue
        if kind == "video":
            return body, ext, kind, mime
    return None


def _extract_exif(image_path: Path) -> Dict[str, object]:
    metadata = {
        "created_time": None,
        "camera_make": None,
        "camera_model": None,
        "focal_length": None,
        "aperture_f_number": None,
        "iso_equivalent": None,
        "exposure_time": None,
        "location": None,
        "gps_latitude": None,
        "gps_longitude": None,
        "width": 0,
        "height": 0,
    }
    try:
        with Image.open(image_path) as image:
            metadata["width"] = image.width
            metadata["height"] = image.height
            exif = image.getexif()
            if not exif:
                return metadata

            tag_map = {ExifTags.TAGS.get(tag, str(tag)): value for tag, value in exif.items()}
            metadata["camera_make"] = str(tag_map.get("Make")) if tag_map.get("Make") else None
            metadata["camera_model"] = str(tag_map.get("Model")) if tag_map.get("Model") else None
            metadata["iso_equivalent"] = tag_map.get("ISOSpeedRatings")
            metadata["created_time"] = (
                _normalize_exif_datetime(tag_map.get("DateTimeOriginal"))
                or _normalize_exif_datetime(tag_map.get("DateTimeDigitized"))
                or _normalize_exif_datetime(tag_map.get("DateTime"))
            )
            metadata["exposure_time"] = str(tag_map.get("ExposureTime")) if tag_map.get("ExposureTime") else None
            metadata["aperture_f_number"] = str(tag_map.get("FNumber")) if tag_map.get("FNumber") else None
            metadata["focal_length"] = str(tag_map.get("FocalLength")) if tag_map.get("FocalLength") else None

            lat_lon = _extract_gps(exif)
            if lat_lon:
                lat, lon = lat_lon
                metadata["gps_latitude"] = round(lat, 6)
                metadata["gps_longitude"] = round(lon, 6)
                metadata["location"] = f"{lat:.6f}, {lon:.6f}"
    except Exception:
        return metadata

    return metadata


def _dms_to_decimal(values, ref: str) -> Optional[float]:
    if not values or len(values) < 3:
        return None
    d = _rational_to_float(values[0])
    m = _rational_to_float(values[1])
    s = _rational_to_float(values[2])
    if d is None or m is None or s is None:
        return None
    decimal = d + (m / 60.0) + (s / 3600.0)
    if ref in ("S", "W"):
        decimal *= -1
    return decimal


def _rational_to_float(value) -> Optional[float]:
    try:
        numerator = getattr(value, "numerator", None)
        denominator = getattr(value, "denominator", None)
        if numerator is not None and denominator not in (None, 0):
            return float(numerator) / float(denominator)

        if isinstance(value, tuple) and len(value) == 2:
            n, d = value
            return float(n) / float(d) if d else None

        return float(value)
    except Exception:
        return None


def _extract_gps(exif) -> Optional[Tuple[float, float]]:
    gps_info = None
    try:
        gps_info = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        pass
    if not gps_info:
        return None

    lat_values = gps_info.get(2)
    lat_ref = gps_info.get(1)
    lon_values = gps_info.get(4)
    lon_ref = gps_info.get(3)
    if not lat_values or not lon_values or not lat_ref or not lon_ref:
        return None

    lat = _dms_to_decimal(lat_values, str(lat_ref))
    lon = _dms_to_decimal(lon_values, str(lon_ref))
    if lat is None or lon is None:
        return None
    return lat, lon


def _reverse_geocode_city_country(lat: float, lon: float, cache: Dict[Tuple[float, float], Tuple[Optional[str], Optional[str]]]) -> Tuple[Optional[str], Optional[str]]:
    key = (round(lat, 3), round(lon, 3))
    if key in cache:
        return cache[key]

    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "zoom": 10,
        "addressdetails": 1,
        "accept-language": "en",
    }
    headers = {"User-Agent": "FramePi/1.0 (local photo frame)"}
    try:
        response = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        address = payload.get("address", {})
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
        country = address.get("country")
        cache[key] = (city, country)
        return city, country
    except Exception:
        cache[key] = (None, None)
        return None, None


def tel_aviv_default_placemark(
    geocode_cache: Dict[Tuple[float, float], Tuple[Optional[str], Optional[str]]],
) -> Tuple[float, float, str, Optional[str], Optional[str]]:
    """Central Tel Aviv when an item has no coordinates (common for shared videos)."""
    lat = round(32.0853, 6)
    lon = round(34.7818, 6)
    city, country = _reverse_geocode_city_country(lat, lon, geocode_cache)
    if not city and not country:
        city, country = "Tel Aviv", "Israel"
    return lat, lon, f"{lat:.6f}, {lon:.6f}", city, country


_TEL_AVIV_SERVE_CACHE: Optional[Tuple[float, float, str, str, str]] = None


def enrich_item_placemark_tel_aviv_if_missing(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill Tel Aviv when an item has no GPS and no place text (used when serving metadata so the
    gallery/map stay correct even if metadata.json on disk predates a sync run).

    Coordinates that match the default Tel-Aviv placeholder (used when GPS was unknown) are
    stripped so they are not mistaken for a real location. ``shared-extra-*`` items stay without
    a synthetic placemark until a sync recovers real GPS from Google.
    """
    global _TEL_AVIV_SERVE_CACHE
    work = dict(item)
    lat, lon = work.get("gps_latitude"), work.get("gps_longitude")
    if lat is not None and lon is not None and _is_default_framepi_placeholder_coords(lat, lon):
        has_place_text = bool(
            str(work.get("location") or "").strip() or work.get("city") or work.get("country")
        )
        work["gps_latitude"] = None
        work["gps_longitude"] = None
        if not has_place_text:
            work["location"] = None
            work["city"] = None
            work["country"] = None

    lat, lon = work.get("gps_latitude"), work.get("gps_longitude")
    if lat is not None and lon is not None:
        return work
    if work.get("city") or work.get("country") or str(work.get("location") or "").strip():
        return work
    if str(work.get("id") or "").startswith("shared-extra-"):
        return work
    if _TEL_AVIV_SERVE_CACHE is None:
        m: Dict[Tuple[float, float], Tuple[Optional[str], Optional[str]]] = {}
        la, lo, loc, c, co = tel_aviv_default_placemark(m)
        _TEL_AVIV_SERVE_CACHE = (float(la), float(lo), loc, c or "Tel Aviv", co or "Israel")
    la, lo, loc, c, co = _TEL_AVIV_SERVE_CACHE
    out = dict(work)
    out["gps_latitude"] = la
    out["gps_longitude"] = lo
    out["location"] = loc
    out["city"] = c
    out["country"] = co
    return out


_ALBUM_PAGE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 FramePi/1.0"
)
_ALBUM_PAGE_HEADERS = {
    "User-Agent": _ALBUM_PAGE_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def _fetch_shared_album_html(shared_album_url: str) -> str:
    """
    Load photos.google.com/share/… HTML (lh3 URLs + AF_initData).

    photos.app.goo.gl / goo.gl: Python ``requests`` usually stays on a ~35KB SPA shell with no
    media URLs; ``curl -sL`` follows to the real share page (~1MB) where lh3 links exist.
    Fallback: regex-resolve ``photos.google.com/share/…`` from the shell and GET it.
    """
    target = (shared_album_url or "").strip()
    if not target:
        raise ValueError("empty album URL")
    low = target.lower()
    short_link = ("photos.app.goo.gl" in low or "goo.gl/" in low) and "photos.google.com/share/" not in low

    # Use curl's default User-Agent here: with a browser UA, Google often returns a ~35KB SPA on
    # photos.app.goo.gl without HTTP redirects; plain curl gets the full photos.google.com/share HTML.
    if short_link and shutil.which("curl"):
        try:
            proc = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-L",
                    "--max-redirs",
                    "15",
                    "-H",
                    "Cache-Control: no-cache",
                    "--connect-timeout",
                    "25",
                    "-m",
                    "180",
                    target,
                ],
                capture_output=True,
                text=True,
                timeout=185,
                check=False,
            )
            html = proc.stdout or ""
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                print(f"[sync] curl album fetch exit {proc.returncode}: {err[:400]}")
            if len(html) > 50000 or "googleusercontent.com" in html:
                return html
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            print(f"[sync] curl album fetch failed: {exc}")

    r = requests.get(target, timeout=45, headers=dict(_ALBUM_PAGE_HEADERS))
    r.raise_for_status()
    text = r.text

    if short_link and len(text) < 80000:
        m = _SHARE_PAGE_URL_IN_HTML.search(text)
        if m:
            resolved = m.group(0)
            while resolved and resolved[-1] in ")\"'\\.":
                resolved = resolved[:-1]
            r2 = requests.get(resolved, timeout=45, headers=dict(_ALBUM_PAGE_HEADERS))
            r2.raise_for_status()
            return r2.text

    return text


def _discover_image_urls_from_html(html: str) -> List[str]:
    matches = IMAGE_URL_PATTERN.findall(html)
    seen = set()
    normalized_urls = []
    for url in matches:
        normalized = _normalize_download_url(url)
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            normalized_urls.append(normalized)
    return normalized_urls


def _extract_taken_times_from_album_html(html: str) -> Dict[str, str]:
    """
    Best-effort: extract capture timestamps from the shared album HTML.

    Some shared items have no EXIF DateTimeOriginal in the downloadable bytes, but the
    album page still includes a millisecond timestamp for ordering.

    Returns mapping: content_token -> ISO string (UTC).
    """
    if not html:
        return {}

    # Example structure (truncated):
    # ["AF1Qip...",["https://lh3.googleusercontent.com/pw/AP1Gcz... ",3000,4000,...],1715420871000,"..."]
    # We key by the pw token (AP1Gcz...) since that matches our filenames.
    token_to_iso: Dict[str, str] = {}
    pattern = re.compile(
        r"https://(?:lh3\.googleusercontent\.com|photos\.fife\.usercontent\.google\.com)/pw/"
        r'([A-Za-z0-9_\-]+)".*?\],(\d{13}),',
        re.DOTALL,
    )
    for token, ms in pattern.findall(html):
        try:
            dt = datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
            token_to_iso[token] = dt.isoformat()
        except Exception:
            continue
    return token_to_iso


_PAIR_LATLNG_E7 = re.compile(
    r'"latE7"\s*:\s*(-?\d+)\s*,\s*"lngE7"\s*:\s*(-?\d+)|"lngE7"\s*:\s*(-?\d+)\s*,\s*"latE7"\s*:\s*(-?\d+)'
)
_PAIR_LATLNG_DEC = re.compile(
    r'"latitude"\s*:\s*(-?\d+\.?\d*)\s*,\s*"longitude"\s*:\s*(-?\d+\.?\d*)|'
    r'"longitude"\s*:\s*(-?\d+\.?\d*)\s*,\s*"latitude"\s*:\s*(-?\d+\.?\d*)'
)


def _parse_lat_lng_from_e7_match(m: re.Match) -> Optional[Tuple[float, float]]:
    if m.group(1) is not None and m.group(2) is not None:
        lat_e7, lng_e7 = int(m.group(1)), int(m.group(2))
    elif m.group(3) is not None and m.group(4) is not None:
        lng_e7, lat_e7 = int(m.group(3)), int(m.group(4))
    else:
        return None
    lat, lon = lat_e7 / 1e7, lng_e7 / 1e7
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None
    return lat, lon


def _parse_lat_lng_from_dec_match(m: re.Match) -> Optional[Tuple[float, float]]:
    if m.group(1) is not None and m.group(2) is not None:
        lat_s, lon_s = m.group(1), m.group(2)
    elif m.group(3) is not None and m.group(4) is not None:
        lon_s, lat_s = m.group(3), m.group(4)
    else:
        return None
    try:
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None
    return lat, lon


def _nearest_geo_in_chunk(chunk: str, anchor: int) -> Optional[Tuple[float, float]]:
    """Pick the lat/lng pair in chunk whose match position is closest to anchor (often the media URL)."""
    best: Optional[Tuple[float, float]] = None
    best_dist = 10**18
    for rx, parser in ((_PAIR_LATLNG_E7, _parse_lat_lng_from_e7_match), (_PAIR_LATLNG_DEC, _parse_lat_lng_from_dec_match)):
        for m in rx.finditer(chunk):
            parsed = parser(m)
            if parsed is None:
                continue
            center = (m.start() + m.end()) // 2
            dist = abs(center - anchor)
            if dist < best_dist:
                best_dist = dist
                best = parsed
    return best


def googleusercontent_media_stem(url: str) -> Optional[str]:
    """Path token after /p/ or /pw/ — matches local filename stems and HTML geo keys."""
    m = re.search(
        r"(?:lh3|lh4|lh5|lh6)\.googleusercontent\.com/(?:pw|p)/([A-Za-z0-9_\-]+)|"
        r"photos\.fife\.usercontent\.google\.com/(?:pw|p)/([A-Za-z0-9_\-]+)",
        url,
    )
    if not m:
        return None
    return m.group(1) or m.group(2)


def _extract_balanced_json_fragment(text: str, start: int) -> Optional[str]:
    """Return a balanced [...] or {...} substring starting at start (JSON string rules for quotes)."""
    if start >= len(text) or text[start] not in "[{":
        return None
    open_ch, close_ch = text[start], "]" if text[start] == "[" else "}"
    depth = 0
    i = start
    in_str = False
    esc = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _try_adjacent_ints_as_latlng_e7(a: object, b: object) -> Optional[Tuple[float, float]]:
    if not isinstance(a, int) or not isinstance(b, int):
        return None
    if abs(a) >= 10**11 or abs(b) >= 10**11:
        return None
    if max(abs(a), abs(b)) < 500_000:
        return None
    la, lo = a / 1e7, b / 1e7
    if -90 <= la <= 90 and -180 <= lo <= 180 and (abs(la) > 1e-5 or abs(lo) > 1e-5):
        return (round(la, 6), round(lo, 6))
    la, lo = b / 1e7, a / 1e7
    if -90 <= la <= 90 and -180 <= lo <= 180 and (abs(la) > 1e-5 or abs(lo) > 1e-5):
        return (round(la, 6), round(lo, 6))
    return None


def _try_adjacent_floats_as_latlng_deg(a: object, b: object) -> Optional[Tuple[float, float]]:
    if not isinstance(a, float) or not isinstance(b, float):
        return None
    if not (-90 <= a <= 90 and -180 <= b <= 180):
        return None
    if abs(a) < 1e-5 and abs(b) < 1e-5:
        return None
    return (round(a, 6), round(b, 6))


def _collect_e7_pairs_in_json(obj: object) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []

    def walk(x: object) -> None:
        if isinstance(x, list):
            for i in range(len(x) - 1):
                p = _try_adjacent_ints_as_latlng_e7(x[i], x[i + 1])
                if p is not None:
                    out.append(p)
                p2 = _try_adjacent_floats_as_latlng_deg(x[i], x[i + 1])
                if p2 is not None:
                    out.append(p2)
            for el in x:
                walk(el)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(obj)
    return out


def _looks_like_af_media_row(row: list) -> bool:
    if len(row) < 2:
        return False
    z = row[0] if isinstance(row[0], str) else ""
    # Logged-in pages use AF1… ids; guest/share embeds often use AP1…
    if len(z) < 12 or not (z.startswith("AF1") or z.startswith("AP1")):
        return False
    second = row[1]
    if not isinstance(second, list) or not second or not isinstance(second[0], str):
        return False
    u = second[0]
    return "googleusercontent.com" in u and ("/pw/" in u or "/p/" in u)


def _iter_af_media_rows(obj: object) -> List[list]:
    rows: List[list] = []

    def walk(x: object) -> None:
        if isinstance(x, list):
            if _looks_like_af_media_row(x):
                rows.append(x)
            for el in x:
                walk(el)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(obj)
    return rows


_AF_INIT_BLOCK_START = re.compile(r"AF_initDataCallback\s*\(\s*\{")


def _extract_geo_from_af_init_data_callbacks(html: str) -> Dict[str, Tuple[float, float]]:
    """
    Google Photos embeds album/media payloads in AF_initDataCallback({..., data: [...]});
    Match only real callback openings — a bare 'AF_initDataCallback' substring appears inside huge unrelated inline blobs.
    """
    token_to_geo: Dict[str, Tuple[float, float]] = {}
    if not html:
        return token_to_geo
    for m in _AF_INIT_BLOCK_START.finditer(html):
        block = m.start()
        data_idx = html.find("data:", block, block + 32768)
        if data_idx == -1:
            continue
        j = data_idx + len("data:")
        while j < len(html) and html[j] in " \n\r\t":
            j += 1
        frag = _extract_balanced_json_fragment(html, j)
        if not frag:
            continue
        try:
            payload = json.loads(frag)
        except json.JSONDecodeError:
            continue
        for row in _iter_af_media_rows(payload):
            stem = googleusercontent_media_stem(row[1][0])
            if not stem or stem in token_to_geo:
                continue
            pairs = _collect_e7_pairs_in_json(row)
            if pairs:
                token_to_geo[stem] = pairs[0]
    return token_to_geo


def _extract_geo_from_url_proximity(html: str) -> Dict[str, Tuple[float, float]]:
    """Legacy: latE7 / latitude keys in HTML near lh3 /pw/ URLs (older or lighter pages)."""
    token_to_geo: Dict[str, Tuple[float, float]] = {}
    url_token = re.compile(
        r"https://(?:lh3|lh4|lh5|lh6)\.googleusercontent\.com/(?:pw|p)/([A-Za-z0-9_\-]+)|"
        r"https://photos\.fife\.usercontent\.google\.com/(?:pw|p)/([A-Za-z0-9_\-]+)"
    )
    before, after = 4000, 10000
    for m in url_token.finditer(html):
        token = m.group(1) or m.group(2)
        if token in token_to_geo:
            continue
        start = m.start()
        lo = max(0, start - before)
        hi = min(len(html), start + after)
        chunk = html[lo:hi]
        anchor = start - lo
        pair = _nearest_geo_in_chunk(chunk, anchor)
        if pair is not None:
            token_to_geo[token] = pair
    return token_to_geo


def _extract_geo_from_album_html(html: str) -> Dict[str, Tuple[float, float]]:
    """
    GPS hints from the album HTML: (1) AF_initDataCallback embedded JSON (photos.google.com),
    (2) latE7 strings near media URLs (older shells).
    """
    if not html:
        return {}
    merged: Dict[str, Tuple[float, float]] = {}
    merged.update(_extract_geo_from_url_proximity(html))
    for stem, pair in _extract_geo_from_af_init_data_callbacks(html).items():
        merged[stem] = pair
    return merged


_DISK_ONLY_GOOGLE_FILENAME = re.compile(
    r"^(AP1|AF1)[A-Za-z0-9_-]{16,}\.(?:jpg|jpeg|png|webp|mp4|webm|mov|m4v|mkv)$",
    re.I,
)


def _append_disk_only_googleusercontent_items(
    items: List[Dict[str, Any]],
    current_managed: set[str],
    photos_dir: Path,
    geocode_cache: Dict[Tuple[float, float], Tuple[Optional[str], Optional[str]]],
    taken_time_fallback: Dict[str, str],
    html_geo_by_stem: Dict[str, Tuple[float, float]],
    store: Optional["MetadataStore"] = None,
) -> None:
    """
    Shared-album HTML often omits some media (lazy loading). A download can succeed while a later
    run rebuilds metadata from a shorter URL list, orphaning files under ``photos/``. Re-include
    those files so the gallery matches disk.
    """
    present = {Path(str(it.get("local_path") or "")).name for it in items if it.get("local_path")}
    extra_i = 0
    for path in sorted(photos_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name in present:
            continue
        if not _DISK_ONLY_GOOGLE_FILENAME.match(path.name):
            continue
        stem = path.stem
        local_path = path
        local_name = path.name
        sample = local_path.read_bytes()[:65536]
        _ext, media_kind, mime_type = _classify_media(sample, "")
        if media_kind == "image":
            exif_data = dict(_extract_exif(local_path))
            if exif_data.get("gps_latitude") is None or exif_data.get("gps_longitude") is None:
                gps_heavy = _gps_from_google_photo_media_stem(stem)
                if gps_heavy:
                    gla, glo = round(gps_heavy[0], 6), round(gps_heavy[1], 6)
                    exif_data["gps_latitude"] = gla
                    exif_data["gps_longitude"] = glo
                    exif_data["location"] = f"{gla:.6f}, {glo:.6f}"
        else:
            exif_data = {
                "created_time": None,
                "camera_make": None,
                "camera_model": None,
                "focal_length": None,
                "aperture_f_number": None,
                "iso_equivalent": None,
                "exposure_time": None,
                "location": None,
                "gps_latitude": None,
                "gps_longitude": None,
                "width": 0,
                "height": 0,
            }
        if not exif_data.get("created_time"):
            exif_data["created_time"] = taken_time_fallback.get(stem)
        if (
            exif_data.get("gps_latitude") is None or exif_data.get("gps_longitude") is None
        ) and stem in html_geo_by_stem:
            glat, glon = html_geo_by_stem[stem]
            exif_data["gps_latitude"] = round(glat, 6)
            exif_data["gps_longitude"] = round(glon, 6)
            exif_data["location"] = f"{glat:.6f}, {glon:.6f}"

        city: Optional[str] = None
        country: Optional[str] = None
        if exif_data.get("gps_latitude") is not None and exif_data.get("gps_longitude") is not None:
            city, country = _reverse_geocode_city_country(
                float(exif_data["gps_latitude"]), float(exif_data["gps_longitude"]), geocode_cache
            )
        elif media_kind == "image":
            city, country = None, None
        else:
            dlat, dlon, dloc, city, country = tel_aviv_default_placemark(geocode_cache)
            exif_data["gps_latitude"] = dlat
            exif_data["gps_longitude"] = dlon
            exif_data["location"] = dloc

        kind_label = "video" if media_kind == "video" else "photo"
        glat, glon = exif_data.get("gps_latitude"), exif_data.get("gps_longitude")
        loc = exif_data.get("location")
        print(
            f"[sync][location] disk_only=1 {kind_label}={local_name} lat={glat} lon={glon} "
            f"location={loc!s} city={city or '-'} country={country or '-'}"
        )
        extra_i += 1
        from metadata_store import google_url_from_stem

        row = {
            "id": f"shared-extra-{extra_i}",
            "google_url": google_url_from_stem(stem),
            "google_stem": stem,
            "filename": local_name,
            "local_path": f"photos/{local_name}",
            "mime_type": mime_type,
            "media_type": media_kind,
            "description": "",
            "created_time": exif_data["created_time"],
            "camera_make": exif_data["camera_make"],
            "camera_model": exif_data["camera_model"],
            "focal_length": exif_data["focal_length"],
            "aperture_f_number": exif_data["aperture_f_number"],
            "iso_equivalent": exif_data["iso_equivalent"],
            "exposure_time": exif_data["exposure_time"],
            "location": exif_data["location"],
            "city": city,
            "country": country,
            "gps_latitude": exif_data["gps_latitude"],
            "gps_longitude": exif_data["gps_longitude"],
            "width": exif_data["width"],
            "height": exif_data["height"],
        }
        if store is not None:
            if store.insert_if_new(row):
                items.append(row)
        else:
            items.append(row)
        current_managed.add(local_name)
        present.add(local_name)


def sync_shared_album(shared_album_url: str, output_dir: Path) -> List[Dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    photos_dir = output_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    managed_manifest_path = output_dir / "managed_files.json"

    delete_removed = os.getenv("DELETE_REMOVED_FROM_FRAME", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    previous_managed: set[str] = set()
    if managed_manifest_path.exists():
        try:
            prev = json.loads(managed_manifest_path.read_text(encoding="utf-8"))
            if isinstance(prev, list):
                previous_managed = {str(x) for x in prev}
        except Exception:
            previous_managed = set()

    album_html = ""
    try:
        album_html = _fetch_shared_album_html(shared_album_url)
    except Exception as exc:
        print(f"[sync] failed to load album page: {exc}")
    if not album_html:
        try:
            fallback = requests.get(
                shared_album_url,
                timeout=30,
                headers=dict(_ALBUM_PAGE_HEADERS),
            )
            fallback.raise_for_status()
            album_html = fallback.text
        except Exception:
            pass

    image_urls = _discover_image_urls_from_html(album_html) if album_html else []
    taken_time_fallback = _extract_taken_times_from_album_html(album_html)
    html_geo_by_stem = _extract_geo_from_album_html(album_html)
    from metadata_store import MetadataStore

    store = MetadataStore(output_dir / "framepi.db")
    migrated = store.migrate_from_json(output_dir / "metadata.json")
    if migrated:
        print(f"[sync] imported {migrated} item(s) from metadata.json into SQLite")

    geocode_cache: Dict[Tuple[float, float], Tuple[Optional[str], Optional[str]]] = {}
    current_managed: set[str] = set()

    _known_suffixes = (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mov", ".m4v", ".mkv")

    for index, image_url in enumerate(image_urls, start=1):
        gps_from_variant: Optional[Tuple[float, float]] = None
        # Use the stable token (strip "=d" / size params).
        # Also keep legacy stem for already-downloaded files that used to include "=d".
        raw_token = image_url.split("/")[-1]
        stem = raw_token.split("=")[0]
        google_url = googleusercontent_url_base(image_url)
        existing_row = store.get_by_google_stem(stem)
        if existing_row is not None:
            existing_name = Path(str(existing_row.get("local_path") or "")).name
            if existing_name and (photos_dir / existing_name).is_file():
                current_managed.add(existing_name)
                continue
        legacy_stem = raw_token
        local_path: Optional[Path] = None
        for suf in _known_suffixes:
            candidate = photos_dir / (stem + suf)
            if candidate.exists():
                local_path = candidate
                break
        if local_path is None:
            for suf in _known_suffixes:
                candidate = photos_dir / (legacy_stem + suf)
                if candidate.exists():
                    local_path = candidate
                    break

        if local_path is None:
            try:
                body, ext, media_kind, mime_type = _download_googleusercontent_preferred_size(image_url, stem_hint=stem)
            except Exception:
                continue

            # Heuristic: if it's an image, it might be a video poster. Try video variants.
            if media_kind == "image":
                maybe_video = _try_video_variants(image_url)
                if maybe_video is not None:
                    body, ext, media_kind, mime_type = maybe_video

            # Heavy variants (=d / =s0) may carry EXIF GPS — use coords only; never store those bytes as the frame file.
            if media_kind == "image":
                try_gps = os.getenv("TRY_ORIGINAL_FOR_GPS", "").strip().lower() not in {"0", "false", "no", "off"}
                if try_gps:
                    exif_preview = _extract_exif_from_bytes(body)
                    if exif_preview.get("gps_latitude") is None or exif_preview.get("gps_longitude") is None:
                        gps_from_variant = _gps_coordinates_from_heavy_variants(image_url)
                body, ext = _finalize_hd_photo_bytes(body, ext)

            local_name = stem + ext
            local_path = photos_dir / local_name
            local_path.write_bytes(body)
        else:
            sample = local_path.read_bytes()[:65536]
            ext, media_kind, mime_type = _classify_media(sample, "")
            # Upgrade path: if an existing file is an image, it may be a video poster from a previous run.
            if media_kind == "image":
                maybe_video = _try_video_variants(image_url)
                if maybe_video is not None:
                    body, vext, media_kind, mime_type = maybe_video
                    new_name = stem + vext
                    new_path = photos_dir / new_name
                    if not new_path.exists():
                        new_path.write_bytes(body)
                    try:
                        if local_path.exists() and local_path.is_file() and local_path.name != new_name:
                            local_path.unlink()
                    except Exception:
                        pass
                    local_path = new_path
                    local_name = new_path.name
                else:
                    try_gps = os.getenv("TRY_ORIGINAL_FOR_GPS", "").strip().lower() not in {"0", "false", "no", "off"}
                    if try_gps:
                        snap = _extract_exif(local_path)
                        if snap.get("gps_latitude") is None or snap.get("gps_longitude") is None:
                            gps_from_variant = _gps_coordinates_from_heavy_variants(image_url)

                    # Also normalize legacy filenames like "...=d.jpg" -> "<token>.jpg" when safe.
                    desired_name = stem + ext
                    desired_path = photos_dir / desired_name
                    if local_path.name != desired_name and not desired_path.exists():
                        try:
                            local_path.rename(desired_path)
                            local_path = desired_path
                        except Exception:
                            pass

            if media_kind == "video" and local_path.suffix.lower() not in _VIDEO_SUFFIXES:
                new_path = local_path.with_suffix(ext)
                if new_path != local_path and not new_path.exists():
                    local_path.rename(new_path)
                    local_path = new_path
            elif media_kind == "image" and local_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                new_path = local_path.with_suffix(ext)
                if new_path != local_path and not new_path.exists():
                    local_path.rename(new_path)
                    local_path = new_path
            local_name = local_path.name

        if media_kind == "image" and local_path.exists():
            raw_img = local_path.read_bytes()
            scaled, sfx = _finalize_hd_photo_bytes(raw_img, local_path.suffix)
            if sfx != local_path.suffix:
                dest = local_path.with_suffix(sfx)
                dest.write_bytes(scaled)
                if dest != local_path:
                    try:
                        local_path.unlink()
                    except Exception:
                        pass
                local_path = dest
                local_name = local_path.name
            elif scaled != raw_img:
                local_path.write_bytes(scaled)

        if media_kind == "video" and local_path.is_file():
            try:
                from video_proxy import warm_video_proxy

                warm_video_proxy(local_path, output_dir)
            except Exception as exc:
                print(f"[sync] video proxy skipped for {local_name}: {exc}")

        if media_kind == "image":
            exif_data = _extract_exif(local_path)
            if gps_from_variant is not None and (
                exif_data.get("gps_latitude") is None or exif_data.get("gps_longitude") is None
            ):
                glat, glon = gps_from_variant
                exif_data["gps_latitude"] = round(glat, 6)
                exif_data["gps_longitude"] = round(glon, 6)
                exif_data["location"] = f"{glat:.6f}, {glon:.6f}"
        else:
            exif_data = {
                "created_time": None,
                "camera_make": None,
                "camera_model": None,
                "focal_length": None,
                "aperture_f_number": None,
                "iso_equivalent": None,
                "exposure_time": None,
                "location": None,
                "gps_latitude": None,
                "gps_longitude": None,
                "width": 0,
                "height": 0,
            }
        if not exif_data.get("created_time"):
            # If EXIF is missing, try the shared-album HTML timestamp for this item.
            # This improves ordering even when location/GPS is unavailable.
            exif_data["created_time"] = taken_time_fallback.get(stem)

        if (
            exif_data.get("gps_latitude") is None or exif_data.get("gps_longitude") is None
        ) and stem in html_geo_by_stem:
            glat, glon = html_geo_by_stem[stem]
            exif_data["gps_latitude"] = round(glat, 6)
            exif_data["gps_longitude"] = round(glon, 6)
            exif_data["location"] = f"{glat:.6f}, {glon:.6f}"

        city = None
        country = None
        if exif_data["gps_latitude"] is not None and exif_data["gps_longitude"] is not None:
            city, country = _reverse_geocode_city_country(
                exif_data["gps_latitude"], exif_data["gps_longitude"], geocode_cache
            )
        else:
            dlat, dlon, dloc, city, country = tel_aviv_default_placemark(geocode_cache)
            exif_data["gps_latitude"] = dlat
            exif_data["gps_longitude"] = dlon
            exif_data["location"] = dloc

        kind_label = "video" if media_kind == "video" else "photo"
        glat, glon = exif_data.get("gps_latitude"), exif_data.get("gps_longitude")
        loc = exif_data.get("location")
        print(
            f"[sync][location] {kind_label}={local_name} lat={glat} lon={glon} "
            f"location={loc!s} city={city or '-'} country={country or '-'}"
        )

        item_row = {
            "id": f"shared-{index}",
            "google_url": google_url,
            "google_stem": stem,
            "filename": local_name,
            "local_path": f"photos/{local_name}",
            "mime_type": mime_type,
            "media_type": media_kind,
            "description": "",
            "created_time": exif_data["created_time"],
            "camera_make": exif_data["camera_make"],
            "camera_model": exif_data["camera_model"],
            "focal_length": exif_data["focal_length"],
            "aperture_f_number": exif_data["aperture_f_number"],
            "iso_equivalent": exif_data["iso_equivalent"],
            "exposure_time": exif_data["exposure_time"],
            "location": exif_data["location"],
            "city": city,
            "country": country,
            "gps_latitude": exif_data["gps_latitude"],
            "gps_longitude": exif_data["gps_longitude"],
            "width": exif_data["width"],
            "height": exif_data["height"],
        }
        if store.insert_if_new(item_row):
            print(
                f"[sync] new to frame: {local_name} "
                f"(google_url={google_url[:72]}{'…' if len(google_url) > 72 else ''})"
            )
        current_managed.add(local_name)

    scratch_items: List[Dict[str, Any]] = []
    _append_disk_only_googleusercontent_items(
        scratch_items,
        current_managed,
        photos_dir,
        geocode_cache,
        taken_time_fallback,
        html_geo_by_stem,
        store=store,
    )
    items = [
        it
        for it in store.list_all_items()
        if Path(str(it.get("local_path") or "")).name in current_managed
    ]
    if _backfill_gps_from_google_heavy_for_images(items, geocode_cache) > 0:
        for it in items:
            store.save_item(it)

    # Optional cleanup: delete files that were previously managed but no longer present in the album.
    # This avoids touching unrelated files in data/photos/.
    if delete_removed and previous_managed:
        removed = sorted(previous_managed - current_managed)
        for name in removed:
            try:
                target = photos_dir / name
                if target.exists() and target.is_file():
                    target.unlink()
            except Exception as exc:
                print(f"[sync] failed to delete {name}: {exc}")
            store.delete_by_filename(name)

    store.touch_sync(source="google-photos-shared-link", count=len(items))
    print(f"[sync] SQLite metadata: {len(items)} items in {store.db_path}")
    managed_manifest_path.write_text(json.dumps(sorted(current_managed), indent=2), encoding="utf-8")
    return items
