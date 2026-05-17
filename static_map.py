"""Google Static Maps: one lightweight PNG (no Leaflet / Maps JavaScript API)."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
from PIL import Image, ImageDraw

CLUSTER_RADIUS_KM = 300.0
CLUSTER_GRID_SIZE = CLUSTER_RADIUS_KM / 111.0

# ``location`` in metadata is often ``"lat, lon"`` even when numeric GPS fields were omitted.
_LOCATION_LAT_LON = re.compile(r"^\s*(-?\d+(?:\.\d*)?)\s*,\s*(-?\d+(?:\.\d*)?)")


def item_coordinates_for_map(item: dict[str, Any]) -> tuple[float, float] | None:
    """Return (lat, lon) for map clustering if the item is plottable, else None."""
    lat, lon = item.get("gps_latitude"), item.get("gps_longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        try:
            a, b = float(lat), float(lon)
            if math.isfinite(a) and math.isfinite(b) and -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0:
                return a, b
        except (TypeError, ValueError):
            pass
    loc = str(item.get("location") or "").strip()
    if not loc:
        return None
    m = _LOCATION_LAT_LON.match(loc)
    if not m:
        return None
    try:
        a, b = float(m.group(1)), float(m.group(2))
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(a) and math.isfinite(b) and -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0):
        return None
    return a, b


# Google allows max 640 per dimension at scale=1; keep 2:1 to match map layout.
DEFAULT_MAP_W = 640
DEFAULT_MAP_H = 320

_STATIC_MAP_CACHE: dict[str, tuple[bytes, str]] = {}
_MAX_CACHE = 6

_OSM_TILE_CACHE: dict[str, tuple[bytes, str]] = {}
_OSM_TILE_MAX_CACHE = 4

# Google Static Map style rules (each becomes a separate `style=` query param).
# Preset "frame": softer colors, no POI clutter — reads well on a wall display.
_STYLE_PRESETS: dict[str, list[str]] = {
    "none": [],
    "frame": [
        "feature:poi|visibility:off",
        "feature:poi.business|visibility:off",
        "feature:transit|visibility:off",
        "feature:transit.station|visibility:off",
        "feature:road|element:labels.icon|visibility:off",
        "feature:road|element:labels.text|visibility:simplified",
        "feature:administrative|element:labels|visibility:simplified",
        "feature:administrative.land_parcel|visibility:off",
        "feature:water|element:geometry|color:0xc5ddf5",
        "feature:landscape|element:geometry|color:0xe8ede6",
    ],
    "dark": [
        "feature:all|element:geometry|color:0x242f3e",
        "feature:all|element:labels.text.fill|color:0xaeb6bf",
        "feature:water|element:geometry|color:0x17263c",
        "feature:road|element:geometry|color:0x2b3544",
        "feature:road|element:geometry.stroke|color:0x1f2836",
        "feature:poi|visibility:off",
        "feature:transit|visibility:off",
    ],
}


def _style_rules_for_request() -> list[str]:
    name = os.getenv("STATIC_MAP_STYLE", "frame").strip().lower()
    return list(_STYLE_PRESETS.get(name, _STYLE_PRESETS["frame"]))


def _static_map_visual_fingerprint() -> str:
    """Bust image cache when visual-related env vars change."""
    return "|".join(
        [
            os.getenv("STATIC_MAP_STYLE", "frame"),
            os.getenv("STATIC_MAP_SCALE", "2"),
            os.getenv("STATIC_MAP_FORMAT", "jpg"),
            os.getenv("STATIC_MAP_TYPE", "roadmap"),
            os.getenv("STATIC_MAP_ZOOM_OUT", "1"),
        ]
    )


def _cluster_geotagged(geotagged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grid: dict[str, list[dict[str, Any]]] = {}
    for item in geotagged:
        ll = item_coordinates_for_map(item)
        if ll is None:
            continue
        lat, lon = ll
        lat_key = round(lat / CLUSTER_GRID_SIZE)
        lon_key = round(lon / CLUSTER_GRID_SIZE)
        key = f"{lat_key}:{lon_key}"
        grid.setdefault(key, []).append(item)

    clusters: list[dict[str, Any]] = []
    for cell in grid.values():
        lats: list[float] = []
        lons: list[float] = []
        for i in cell:
            ll = item_coordinates_for_map(i)
            if ll is not None:
                lats.append(ll[0])
                lons.append(ll[1])
        if not lats:
            continue
        lat = sum(lats) / len(lats)
        lon = sum(lons) / len(lons)
        clusters.append({"lat": lat, "lon": lon, "items": cell})
    return clusters


def _world_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    scale = 256.0 * (2.0**zoom)
    x = (float(lon) + 180.0) / 360.0 * scale
    y = (0.5 - math.log((1.0 + siny) / (1.0 - siny)) / (4.0 * math.pi)) * scale
    return x, y


def _pixel_for_lat_lon(
    lat: float,
    lon: float,
    zoom: int,
    center_lat: float,
    center_lon: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    wx, wy = _world_pixel(lat, lon, zoom)
    cx, cy = _world_pixel(center_lat, center_lon, zoom)
    return width / 2.0 + (wx - cx), height / 2.0 + (wy - cy)


def _google_cluster_pixels_mercator(shape_hw: tuple[int, int], layout: dict[str, Any]) -> list[tuple[int, int]]:
    """Google Static Map: Web Mercator vs logical width/height, scaled to decoded bitmap size."""
    ih, iw = int(shape_hw[0]), int(shape_hw[1])
    lw = max(1, int(layout["width"]))
    lh = max(1, int(layout["height"]))
    z = int(layout["zoom"])
    clat = float(layout["center_lat"])
    clon = float(layout["center_lon"])
    out: list[tuple[int, int]] = []
    for c in sorted(layout.get("clusters") or [], key=lambda x: (float(x["lat"]), float(x["lon"]))):
        px, py = _pixel_for_lat_lon(float(c["lat"]), float(c["lon"]), z, clat, clon, lw, lh)
        xi = int(round(px * (iw / lw)))
        yi = int(round(py * (ih / lh)))
        xi = max(0, min(iw - 1, xi))
        yi = max(0, min(ih - 1, yi))
        out.append((xi, yi))
    return out


def _osm_viewport(layout: dict[str, Any]) -> dict[str, Any]:
    """Shared OSM tile viewport (must match osm_tile_composite_bytes)."""
    z = int(layout["zoom"])
    z = max(0, min(z, 19))
    w = max(64, min(int(layout["width"]), 1280))
    h = max(64, min(int(layout["height"]), 1280))
    r = int(os.getenv("STATIC_MAP_OSM_RENDER_SCALE", "2").strip() or "2")
    r = max(1, min(r, 4))
    tw = int(round(w * r))
    th = int(round(h * r))
    tile_side = int(round(256 * r))
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.LANCZOS  # type: ignore[attr-defined]
    cx, cy = _world_pixel(layout["center_lat"], layout["center_lon"], z)
    world_px = 256 * (1 << z)
    tlx = float(cx) - w / 2.0
    tly = float(cy) - h / 2.0
    tlx = max(0.0, min(tlx, float(world_px - w)))
    tly = max(0.0, min(tly, float(world_px - h)))
    n_tiles = 1 << z
    first_tx = int(math.floor(tlx / 256.0))
    last_tx = int(math.ceil((tlx + w) / 256.0)) - 1
    first_ty = int(math.floor(tly / 256.0))
    last_ty = int(math.ceil((tly + h) / 256.0)) - 1
    first_tx = max(0, min(first_tx, n_tiles - 1))
    last_tx = max(0, min(last_tx, n_tiles - 1))
    first_ty = max(0, min(first_ty, n_tiles - 1))
    last_ty = max(0, min(last_ty, n_tiles - 1))
    return {
        "z": z,
        "w": w,
        "h": h,
        "r": r,
        "tw": tw,
        "th": th,
        "tlx": tlx,
        "tly": tly,
        "tile_side": tile_side,
        "resample": resample,
        "first_tx": first_tx,
        "last_tx": last_tx,
        "first_ty": first_ty,
        "last_ty": last_ty,
    }


def osm_cluster_pin_specs(layout: dict[str, Any]) -> tuple[list[tuple[dict[str, Any], int, int]], int, int]:
    """Clusters that get red pins on OSM raster: (cluster, x, y), canvas tw, th."""
    vp = _osm_viewport(layout)
    z = int(vp["z"])
    tlx = float(vp["tlx"])
    tly = float(vp["tly"])
    r = float(vp["r"])
    tw = int(vp["tw"])
    th = int(vp["th"])
    pin_r = max(4, int(round(5 * r)))
    specs: list[tuple[dict[str, Any], int, int]] = []
    clusters = sorted(layout.get("clusters") or [], key=lambda x: (float(x["lat"]), float(x["lon"])))[:80]
    for c in clusters:
        try:
            wx, wy = _world_pixel(float(c["lat"]), float(c["lon"]), z)
        except (KeyError, TypeError, ValueError):
            continue
        px = int((wx - tlx) * r)
        py = int((wy - tly) * r)
        if not (-pin_r * 2 <= px <= tw + pin_r * 2 and -pin_r * 2 <= py <= th + pin_r * 2):
            continue
        specs.append((c, px, py))
    return specs, tw, th


def osm_cluster_pixel_centers(layout: dict[str, Any]) -> tuple[list[tuple[int, int]], int, int]:
    """(x, y) on OSM JPEG canvas — same points as red pins drawn in osm_tile_composite_bytes."""
    specs, tw, th = osm_cluster_pin_specs(layout)
    return [(px, py) for _, px, py in specs], tw, th


def cluster_pin_pixels_for_map(shape_hw: tuple[int, int], layout: dict[str, Any], map_source: str) -> list[tuple[int, int]]:
    """Pin highlight positions on decoded map image (source: \"google\" | \"osm\")."""
    ih, iw = int(shape_hw[0]), int(shape_hw[1])
    if map_source == "osm":
        pts, ptw, pth = osm_cluster_pixel_centers(layout)
        if not pts:
            return []
        if iw == ptw and ih == pth:
            return pts
        sx = iw / max(1, ptw)
        sy = ih / max(1, pth)
        return [(int(round(x * sx)), int(round(y * sy))) for x, y in pts]
    return _google_cluster_pixels_mercator(shape_hw, layout)


def _bounds_fit_zoom(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    center_lat: float,
    center_lon: float,
    width: int,
    height: int,
    margin: float = 24.0,
) -> int:
    corners = [
        (lat_min, lon_min),
        (lat_min, lon_max),
        (lat_max, lon_min),
        (lat_max, lon_max),
    ]
    for z in range(20, 0, -1):
        ok = True
        for la, lo in corners:
            px, py = _pixel_for_lat_lon(la, lo, z, center_lat, center_lon, width, height)
            if not (margin <= px <= width - margin and margin <= py <= height - margin):
                ok = False
                break
        if ok:
            return z
    return 1


def _padded_bounds(clusters: list[dict[str, Any]], pad: float = 0.16) -> tuple[float, float, float, float]:
    lats = [c["lat"] for c in clusters]
    lons = [c["lon"] for c in clusters]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    lat_pad = max((lat_max - lat_min) * pad, 0.5)
    lon_pad = max((lon_max - lon_min) * pad, 0.5)
    return lat_min - lat_pad, lat_max + lat_pad, lon_min - lon_pad, lon_max + lon_pad


def build_map_layout(
    items: list[dict[str, Any]],
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any] | None:
    """Return center, zoom, dimensions, and clusters for the frame map (no image bytes)."""
    w = width if width is not None else int(os.getenv("STATIC_MAP_WIDTH", str(DEFAULT_MAP_W)))
    h = height if height is not None else int(os.getenv("STATIC_MAP_HEIGHT", str(DEFAULT_MAP_H)))
    w = max(100, min(w, 640))
    h = max(100, min(h, 640))

    geotagged = [i for i in items if item_coordinates_for_map(i) is not None]
    if not geotagged:
        return None

    clusters = _cluster_geotagged(geotagged)
    lat_min, lat_max, lon_min, lon_max = _padded_bounds(clusters)
    center_lat = (lat_min + lat_max) / 2.0
    center_lon = (lon_min + lon_max) / 2.0
    zoom = _bounds_fit_zoom(lat_min, lat_max, lon_min, lon_max, center_lat, center_lon, w, h)
    zoom_out = int(os.getenv("STATIC_MAP_ZOOM_OUT", "1"))
    zoom_out = max(0, min(zoom_out, 3))
    zoom = max(1, zoom - zoom_out)

    # OSM/Google tiles need enough world pixels to cover the requested frame.
    world_px = 256 * (1 << zoom)
    while world_px < max(w, h) and zoom < 20:
        zoom += 1
        world_px = 256 * (1 << zoom)

    z_cap = int(os.getenv("STATIC_MAP_MAX_ZOOM", "18"))
    zoom = min(zoom, max(1, min(z_cap, 21)))

    return {
        "center_lat": center_lat,
        "center_lon": center_lon,
        "zoom": zoom,
        "width": w,
        "height": h,
        "clusters": clusters,
    }


def map_image_cache_key(metadata: dict[str, Any], layout: dict[str, Any]) -> str:
    parts: list[str] = [
        "staticmap_nopins_v1",
        _static_map_visual_fingerprint(),
        str(metadata.get("updated_at") or ""),
        str(layout["zoom"]),
        f"{layout['center_lat']:.6f}",
        f"{layout['center_lon']:.6f}",
        f"{layout['width']}x{layout['height']}",
    ]
    for c in sorted(layout["clusters"], key=lambda x: (x["lat"], x["lon"])):
        parts.append(f"{c['lat']:.6f},{c['lon']:.6f},{len(c['items'])}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def osm_map_image_cache_key(metadata: dict[str, Any], layout: dict[str, Any]) -> str:
    """Cache key for OSM tile composite (independent of Google style env)."""
    parts: list[str] = [
        "osm_tiles_v4_nopins",
        os.getenv("OSM_TILE_URL_TEMPLATE", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
        os.getenv("STATIC_MAP_OSM_RENDER_SCALE", "2"),
        str(metadata.get("updated_at") or ""),
        str(layout["zoom"]),
        f"{layout['center_lat']:.6f}",
        f"{layout['center_lon']:.6f}",
        f"{layout['width']}x{layout['height']}",
    ]
    for c in sorted(layout["clusters"], key=lambda x: (x["lat"], x["lon"])):
        parts.append(f"{c['lat']:.6f},{c['lon']:.6f},{len(c['items'])}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _paste_tile_fragment(
    canvas: Image.Image, tile_img: Image.Image, dest_x: int, dest_y: int
) -> None:
    tw, th = tile_img.size
    cw, ch = canvas.size
    left = max(0, dest_x)
    top = max(0, dest_y)
    right = min(cw, dest_x + tw)
    bottom = min(ch, dest_y + th)
    if left >= right or top >= bottom:
        return
    src_l = left - dest_x
    src_t = top - dest_y
    cropped = tile_img.crop((src_l, src_t, src_l + (right - left), src_t + (bottom - top)))
    canvas.paste(cropped, (left, top))


def osm_tile_composite_bytes(layout: dict[str, Any], cache_key: str) -> tuple[bytes, str]:
    """Build one map image from OSM raster tiles (no API key; real geography).

    Viewport top-left uses float world-pixel coords (same as pin math). Output size is
    ``(width * r, height * r)`` so the image is sharp while pins still use logical width/height.
    """
    cached = _OSM_TILE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    vp = _osm_viewport(layout)
    z = int(vp["z"])
    w = int(vp["w"])
    h = int(vp["h"])
    r = int(vp["r"])
    tw = int(vp["tw"])
    th = int(vp["th"])
    tlx = float(vp["tlx"])
    tly = float(vp["tly"])
    tile_side = int(vp["tile_side"])
    resample = vp["resample"]
    first_tx = int(vp["first_tx"])
    last_tx = int(vp["last_tx"])
    first_ty = int(vp["first_ty"])
    last_ty = int(vp["last_ty"])

    template = os.getenv("OSM_TILE_URL_TEMPLATE", "https://tile.openstreetmap.org/{z}/{x}/{y}.png").strip()
    ua = os.getenv(
        "OSM_STATIC_USER_AGENT",
        "FramePi/1.0 (photo frame; https://operations.osmfoundation.org/policies/tiles/)",
    )
    pairs = [(tx, ty) for tx in range(first_tx, last_tx + 1) for ty in range(first_ty, last_ty + 1)]

    def fetch_one(pair: tuple[int, int]) -> tuple[int, int, Image.Image]:
        tx, ty = pair
        url = template.format(z=z, x=tx, y=ty)
        rq = requests.get(url, headers={"User-Agent": ua}, timeout=25)
        rq.raise_for_status()
        im = Image.open(io.BytesIO(rq.content)).convert("RGB")
        return tx, ty, im

    tiles: dict[tuple[int, int], Image.Image] = {}
    workers = int(os.getenv("OSM_TILE_FETCH_WORKERS", "4"))
    workers = min(max(workers, 1), 8, len(pairs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, p) for p in pairs]
        for fut in as_completed(futures):
            tx, ty, im = fut.result()
            tiles[(tx, ty)] = im

    canvas = Image.new("RGB", (tw, th), (184, 212, 232))
    for ty in range(first_ty, last_ty + 1):
        for tx in range(first_tx, last_tx + 1):
            tile_img = tiles.get((tx, ty))
            if tile_img is None:
                continue
            tile_big = tile_img.resize((tile_side, tile_side), resample)
            dest_x = int(math.floor((tx * 256.0 - tlx) * r + 1e-6))
            dest_y = int(math.floor((ty * 256.0 - tly) * r + 1e-6))
            _paste_tile_fragment(canvas, tile_big, dest_x, dest_y)

    # Pins are drawn in opencv_viewer (translucent, color highlight on selection).

    q = int(os.getenv("OSM_TILE_JPEG_QUALITY", "90"))
    q = max(60, min(q, 95))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=q, optimize=True)
    data = buf.getvalue()
    ctype = "image/jpeg"
    _OSM_TILE_CACHE[cache_key] = (data, ctype)
    while len(_OSM_TILE_CACHE) > _OSM_TILE_MAX_CACHE:
        _OSM_TILE_CACHE.pop(next(iter(_OSM_TILE_CACHE)))

    return data, ctype


def static_map_png_bytes(api_key: str, layout: dict[str, Any], cache_key: str) -> tuple[bytes, str]:
    """Fetch PNG from Google Static Maps. Returns (png_bytes, content_type)."""
    cached = _STATIC_MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    w, h = layout["width"], layout["height"]
    img_format = os.getenv("STATIC_MAP_FORMAT", "jpg").lower()
    if img_format not in ("png", "jpg", "jpeg", "gif"):
        img_format = "jpg"
    if img_format == "jpeg":
        img_format = "jpg"

    scale = os.getenv("STATIC_MAP_SCALE", "2").strip() or "2"
    if scale not in ("1", "2"):
        scale = "2"
    maptype = os.getenv("STATIC_MAP_TYPE", "roadmap").strip() or "roadmap"

    # Duplicate `style` keys require tuple params, not a dict.
    query: list[tuple[str, str]] = [
        ("center", f"{layout['center_lat']:.6f},{layout['center_lon']:.6f}"),
        ("zoom", str(layout["zoom"])),
        ("size", f"{w}x{h}"),
        ("scale", scale),
        ("maptype", maptype),
        ("format", img_format),
        ("key", api_key),
    ]
    for rule in _style_rules_for_request():
        query.append(("style", rule))

    # Location pins are drawn in opencv_viewer (not Static Maps markers).

    url = "https://maps.googleapis.com/maps/api/staticmap"
    r = requests.get(url, params=query, timeout=30)
    ctype = r.headers.get("Content-Type", "image/png")
    if r.status_code != 200:
        raise RuntimeError(f"Google Static Maps HTTP {r.status_code}: {r.text[:500]}")
    if "image" not in ctype:
        raise RuntimeError(f"Google Static Maps returned non-image: {ctype} {r.text[:500]}")

    data = r.content
    _STATIC_MAP_CACHE[cache_key] = (data, ctype)
    while len(_STATIC_MAP_CACHE) > _MAX_CACHE:
        _STATIC_MAP_CACHE.pop(next(iter(_STATIC_MAP_CACHE)))

    return data, ctype
