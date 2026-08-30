from __future__ import annotations

import concurrent.futures
from collections import deque
from functools import lru_cache
import io
import hashlib
import json
import math
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageEnhance
from overturemaps.core import record_batch_reader
from shapely import wkb
from shapely.geometry import MultiPolygon, Polygon


EARTH_RADIUS_M = 6_378_137.0
WEB_MERCATOR_WORLD_M = 2.0 * math.pi * EARTH_RADIUS_M
MAX_LATITUDE = 85.05112878
USER_AGENT = "MeshLabRF (native Windows RF planning application)"
OVERTURE_BUILDING_CACHE_SECONDS = 7 * 24 * 60 * 60
OVERTURE_BUILDING_LIMIT = 1000
OVERTURE_VIEWPORT_BUILDING_LIMIT = 2400
OVERTURE_MAX_VIEWPORT_BUILDING_LIMIT = 20_000
OVERTURE_CELL_QUERY_LIMIT = 750
OVERTURE_ADAPTIVE_MAX_DEPTH = 2
OBSTACLE_DETAIL_CELL_AREA_M2 = 3_000_000.0
OBSTACLE_IMPORT_MAX_CELLS = 16
OBSTACLE_IMPORT_MAX_AREA_M2 = 12_000_000.0
MAP_TILE_WORKERS = 10
_GEOCODE_LOCK = threading.Lock()
_LAST_GEOCODE_REQUEST = 0.0

TILE_LAYERS = {
    "Street": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
    },
    "Topographic": {
        "url": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "max_zoom": 17,
    },
    "TerrainDEM": {
        "url": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
        "max_zoom": 15,
    },
    "Generated": {
        "url": "",
        "max_zoom": 19,
    },
}


def latlon_to_mercator(latitude: float, longitude: float) -> tuple[float, float]:
    latitude = max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude))
    x = EARTH_RADIUS_M * math.radians(longitude)
    y = EARTH_RADIUS_M * math.log(math.tan(math.pi / 4.0 + math.radians(latitude) / 2.0))
    return x, y


def mercator_to_latlon(x: float, y: float) -> tuple[float, float]:
    longitude = math.degrees(x / EARTH_RADIUS_M)
    latitude = math.degrees(2.0 * math.atan(math.exp(y / EARTH_RADIUS_M)) - math.pi / 2.0)
    return latitude, longitude


@lru_cache(maxsize=64)
def _map_center_mercator(latitude: float, longitude: float) -> tuple[float, float]:
    """Cache the stable map center used by every imported geometry point."""
    return latlon_to_mercator(latitude, longitude)


def world_scale_factor(center_latitude: float) -> float:
    """Ratio of true ground meters to raw Web Mercator meters at a latitude.

    Web Mercator inflates ground distance by 1/cos(latitude) away from the
    equator. Every "world" x/y used for placing nodes and obstacles is scaled
    by this factor so that one world unit equals one true ground meter near
    the map center, instead of a latitude-dependent, inflated Mercator meter.
    """
    clamped = max(-MAX_LATITUDE, min(MAX_LATITUDE, center_latitude))
    return math.cos(math.radians(clamped))


def latlon_to_world(
    latitude: float,
    longitude: float,
    center_latitude: float,
    center_longitude: float,
) -> tuple[float, float]:
    """Return unrestricted, center-relative coordinates in true ground meters."""
    x, y = latlon_to_mercator(latitude, longitude)
    center_x, center_y = _map_center_mercator(center_latitude, center_longitude)
    scale = world_scale_factor(center_latitude)
    return (x - center_x) * scale, (center_y - y) * scale


def world_to_latlon(
    x: float,
    y: float,
    center_latitude: float,
    center_longitude: float,
) -> tuple[float, float]:
    """Convert unrestricted center-relative true-meter coordinates to latitude/longitude."""
    center_x, center_y = _map_center_mercator(center_latitude, center_longitude)
    scale = world_scale_factor(center_latitude)
    return mercator_to_latlon(center_x + x / scale, center_y - y / scale)


def world_viewport_to_mercator_bounds(
    world_left: float,
    world_top: float,
    world_right: float,
    world_bottom: float,
    center_latitude: float,
    center_longitude: float,
) -> tuple[float, float, float, float]:
    """Convert an unrestricted true-meter canvas viewport into ordered Web Mercator bounds."""
    left, right = min(world_left, world_right), max(world_left, world_right)
    top, bottom = min(world_top, world_bottom), max(world_top, world_bottom)
    center_x, center_y = _map_center_mercator(center_latitude, center_longitude)
    scale = world_scale_factor(center_latitude)
    return (
        center_x + left / scale,
        center_y - top / scale,
        center_x + right / scale,
        center_y - bottom / scale,
    )


@lru_cache(maxsize=32)
def tile_size_m(zoom: int) -> float:
    return WEB_MERCATOR_WORLD_M / (2**zoom)


def mercator_to_tile(x: float, y: float, zoom: int) -> tuple[float, float]:
    size = tile_size_m(zoom)
    return (x + WEB_MERCATOR_WORLD_M / 2.0) / size, (WEB_MERCATOR_WORLD_M / 2.0 - y) / size


def tile_bounds_mercator(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = tile_size_m(zoom)
    left = x * size - WEB_MERCATOR_WORLD_M / 2.0
    top = WEB_MERCATOR_WORLD_M / 2.0 - y * size
    return left, top - size, left + size, top


GENERATED_TILE_SIZE = 256

# --- Locally-rendered base map from OpenFreeMap vector tiles -------------
#
# Raster Street/Topographic tiles bake roads, land, water, and every label
# into one flat image with no separation, so there is no way to hide street
# names (Incognito mode) without degrading everything else. OpenFreeMap
# publishes the same OSM-derived cartographic data (land use, parks, water,
# roads, place/road names) as small per-tile vector (.pbf/MVT) files, so a
# real-looking basemap can be rendered locally, tile by tile, with the name
# labels kept out of the baked image entirely (drawn as a separate live
# overlay by the UI instead, so Incognito can hide them with zero effect on
# the roads/land/water pixels). Buildings are deliberately never drawn here
# -- they stay on the existing separate Overture/OSM obstacle-import path.
OPENFREEMAP_VECTOR_URL = "https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf"
OPENFREEMAP_MAX_DETAIL_ZOOM = 14
VECTOR_EXTENT = 4096
GENERATED_BACKGROUND_COLOR = "#f7f8f4"

# Internal supersampling factor: vector shapes are drawn at
# GENERATED_TILE_SIZE * SUPERSAMPLE and downsampled with LANCZOS at the end.
# PIL's ImageDraw has no anti-aliasing, so a line/polygon edge drawn straight
# at 256x256 looks visibly jagged/pixelated, especially once the tile is
# upscaled on screen for a continuous zoom between discrete tile levels --
# drawing bigger and shrinking down is the standard cheap way to soften that.
SUPERSAMPLE = 3

# (fill, outline) per land polygon layer, drawn in this order (later layers
# on top of earlier ones) -- soft pastel tones now that "Generated" tiles
# keep their color instead of being flattened to grayscale like the old
# raster layers.
VECTOR_LAND_LAYERS: list[tuple[str, str, str | None]] = [
    ("landcover", "#dce6d8", "#c3d2bd"),
    ("landuse", "#f1ece0", "#ddd4bf"),
    ("park", "#c9e4bd", "#aad198"),
]
VECTOR_WATER_FILL = "#bfe0f2"
VECTOR_WATER_OUTLINE = "#9bc9e3"

VECTOR_PATH_CLASSES = {"track", "path", "footway", "cycleway", "bridleway", "steps"}
VECTOR_ROAD_DEFAULT_STYLE: tuple[str, str | None, int, int] = ("#aeb6b0", None, 1, 0)
VECTOR_WATERWAY_WIDTH_M = {"river": 10, "canal": 8, "stream": 5, "drain": 2, "ditch": 1}


def _vector_road_style(road_class: str, zoom: int) -> tuple[str, str | None, int, int]:
    """(casing color, fill color or None, casing width px, fill width px).

    Below each class's own detail threshold, even a motorway draws as a
    single thin line with no casing/fill split -- a wide dual-tone road at a
    regional/state zoom reads as a fat blob rather than a road, which is
    exactly what a real basemap avoids by only widening roads once there's
    enough screen space per pixel for the detail to read as a road.
    """
    if road_class == "motorway":
        return ("#9ea296", None, 1, 0) if zoom < 12 else ("#8c8570", "#f6cf87", 9, 6)
    if road_class == "trunk":
        return ("#a4a89c", None, 1, 0) if zoom < 12 else ("#8c8570", "#f8da9c", 8, 5)
    if road_class == "primary":
        return ("#a4a89c", None, 1, 0) if zoom < 12 else ("#8a8a80", "#fbe6b0", 7, 4)
    if road_class == "secondary":
        return ("#aeb2a4", None, 1, 0) if zoom < 13 else ("#95968c", "#ffffff", 6, 3)
    if road_class == "tertiary":
        return ("#b6b9ae", None, 1, 0) if zoom < 13 else ("#a3a49a", "#ffffff", 5, 2)
    if road_class == "minor":
        return ("#b7bab0", None, 1, 0) if zoom < 14 else ("#aeb1a7", "#ffffff", 4, 2)
    if road_class == "service":
        return ("#c1c4ba", None, 1, 0) if zoom < 15 else ("#b7bab0", "#ffffff", 3, 1)
    if road_class in VECTOR_PATH_CLASSES:
        return ("#a9ac9f", None, 1, 0)
    if road_class == "rail":
        return ("#7c8078", "#f0f1ec", 3, 1) if zoom >= 10 else ("#9a9c96", None, 1, 0)
    return VECTOR_ROAD_DEFAULT_STYLE

# A named road only gets a live-overlay label once the view is zoomed to at
# least this level, mirroring typical basemap label density.
MIN_LABEL_ZOOM_BY_CLASS: dict[str, int] = {
    "motorway": 9, "trunk": 10, "primary": 11, "secondary": 12,
    "tertiary": 13, "minor": 14, "service": 16,
}


def scale_point(
    point: list[float] | tuple[float, float], render_scale: float = 1.0
) -> tuple[float, float]:
    """Vector-tile local coordinates (0..VECTOR_EXTENT) to tile pixel
    coordinates. `render_scale` (default 1x, the logical 256px tile space
    label extraction relies on) lets the image-drawing helpers below target
    a larger supersampled canvas instead -- see SUPERSAMPLE."""
    return (
        float(point[0]) / VECTOR_EXTENT * GENERATED_TILE_SIZE * render_scale,
        float(point[1]) / VECTOR_EXTENT * GENERATED_TILE_SIZE * render_scale,
    )


def _overzoom_coordinates(value: Any, offset_x: float, offset_y: float, scale: int) -> Any:
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        return [(value[0] - offset_x) * scale, (value[1] - offset_y) * scale]
    if isinstance(value, list):
        return [_overzoom_coordinates(item, offset_x, offset_y, scale) for item in value]
    return value


def _overzoom_vector_data(data: dict, x: int, y: int, scale: int) -> dict:
    """Re-express a coarser parent tile's features in a finer child tile's
    local coordinate space, so a zoom level beyond OpenFreeMap's own detail
    level still shows (enlarged, blockier) geometry instead of nothing."""
    offset_x = (x % scale) * VECTOR_EXTENT / scale
    offset_y = (y % scale) * VECTOR_EXTENT / scale
    transformed: dict[str, dict] = {}
    for layer_name, layer in data.items():
        features = []
        for feature in layer.get("features", []):
            geometry = feature.get("geometry", {})
            features.append(
                {
                    **feature,
                    "geometry": {
                        **geometry,
                        "coordinates": _overzoom_coordinates(
                            geometry.get("coordinates", []), offset_x, offset_y, scale
                        ),
                    },
                }
            )
        transformed[layer_name] = {**layer, "features": features}
    return transformed


_VECTOR_TILE_CACHE: dict[tuple[int, int, int], dict] = {}
_VECTOR_TILE_CACHE_ORDER: list[tuple[int, int, int]] = []
_VECTOR_TILE_CACHE_MAX = 300
_VECTOR_TILE_CACHE_LOCK = threading.Lock()


def get_vector_tile_data(map_service: "MapDataService", zoom: int, x: int, y: int) -> dict:
    """Decoded OpenFreeMap vector-tile data for (zoom, x, y), with a small
    in-memory LRU cache -- the same tile is read once by the tile-image
    generator and again by the label overlay, and re-decoding the same .pbf
    on every render frame would be far too slow."""
    key = (zoom, x, y)
    with _VECTOR_TILE_CACHE_LOCK:
        cached = _VECTOR_TILE_CACHE.get(key)
        if cached is not None:
            return cached
    from mapbox_vector_tile import decode as decode_mvt

    if zoom <= OPENFREEMAP_MAX_DETAIL_ZOOM:
        raw = map_service.fetch_vector_tile_bytes(zoom, x, y)
        data = decode_mvt(raw, default_options={"y_coord_down": True}) if raw else {}
    else:
        scale = 2 ** (zoom - OPENFREEMAP_MAX_DETAIL_ZOOM)
        parent_x, parent_y = x // scale, y // scale
        raw = map_service.fetch_vector_tile_bytes(OPENFREEMAP_MAX_DETAIL_ZOOM, parent_x, parent_y)
        parent_data = decode_mvt(raw, default_options={"y_coord_down": True}) if raw else {}
        data = _overzoom_vector_data(parent_data, x, y, scale)
    with _VECTOR_TILE_CACHE_LOCK:
        _VECTOR_TILE_CACHE[key] = data
        _VECTOR_TILE_CACHE_ORDER.append(key)
        while len(_VECTOR_TILE_CACHE_ORDER) > _VECTOR_TILE_CACHE_MAX:
            oldest = _VECTOR_TILE_CACHE_ORDER.pop(0)
            _VECTOR_TILE_CACHE.pop(oldest, None)
    return data


def _draw_vector_polygon(
    drawing: ImageDraw.ImageDraw, rings: list, fill: str, outline: str | None, render_scale: float
) -> None:
    if not rings:
        return
    outer = [scale_point(point, render_scale) for point in rings[0]]
    if len(outer) >= 3:
        drawing.polygon(outer, fill=fill, outline=outline)


def _draw_vector_polygon_layer(
    drawing: ImageDraw.ImageDraw,
    data: dict,
    layer_name: str,
    fill: str,
    outline: str | None,
    render_scale: float,
) -> None:
    for feature in data.get(layer_name, {}).get("features", []):
        geometry = feature.get("geometry", {})
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates", [])
        if geometry_type == "Polygon":
            _draw_vector_polygon(drawing, coordinates, fill, outline, render_scale)
        elif geometry_type == "MultiPolygon":
            for polygon in coordinates:
                _draw_vector_polygon(drawing, polygon, fill, outline, render_scale)


def _draw_vector_line(
    drawing: ImageDraw.ImageDraw, points: list, color: str, width: int, render_scale: float
) -> None:
    scaled = [scale_point(point, render_scale) for point in points]
    if len(scaled) >= 2:
        drawing.line(scaled, fill=color, width=max(1, round(width * render_scale)), joint="curve")


def _draw_vector_geometry_lines(
    drawing: ImageDraw.ImageDraw, geometry: dict, color: str, width: int, render_scale: float
) -> None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if geometry_type == "LineString":
        _draw_vector_line(drawing, coordinates, color, width, render_scale)
    elif geometry_type == "MultiLineString":
        for line in coordinates:
            _draw_vector_line(drawing, line, color, width, render_scale)


def _draw_vector_waterways(drawing: ImageDraw.ImageDraw, data: dict, zoom: int, render_scale: float) -> None:
    meters_per_pixel = tile_size_m(zoom) / GENERATED_TILE_SIZE
    for feature in data.get("waterway", {}).get("features", []):
        properties = feature.get("properties", {})
        water_class = properties.get("class", properties.get("brunnel", "stream"))
        real_m = VECTOR_WATERWAY_WIDTH_M.get(water_class)
        if real_m is None:
            continue
        width = max(1, round(real_m / meters_per_pixel))
        _draw_vector_geometry_lines(
            drawing, feature.get("geometry", {}), VECTOR_WATER_OUTLINE, width, render_scale
        )


def _draw_vector_transportation(drawing: ImageDraw.ImageDraw, data: dict, zoom: int, render_scale: float) -> None:
    if zoom <= 5:
        return
    features = data.get("transportation", {}).get("features", [])
    jobs = []
    for feature in features:
        road_class = feature.get("properties", {}).get("class", "")
        if zoom < 12 and road_class not in {"motorway", "trunk", "primary"}:
            continue
        casing, fill, casing_width, fill_width = _vector_road_style(road_class, zoom)
        jobs.append((feature.get("geometry", {}), casing, fill, casing_width, fill_width))
    # Casing pass first, then fill on top -- gives through roads a bordered
    # look instead of a flat single-color line.
    for geometry, casing, _fill, casing_width, _fill_width in jobs:
        _draw_vector_geometry_lines(drawing, geometry, casing, casing_width, render_scale)
    for geometry, _casing, fill, _casing_width, fill_width in jobs:
        if fill and fill_width > 0:
            _draw_vector_geometry_lines(drawing, geometry, fill, fill_width, render_scale)


def _draw_vector_boundaries(drawing: ImageDraw.ImageDraw, data: dict, zoom: int, render_scale: float) -> None:
    """Country/state/county administrative boundary lines -- the only
    features shown at world/regional zoom besides place labels, so a
    zoomed-out view isn't just bare roads with nothing else."""
    for feature in data.get("boundary", {}).get("features", []):
        properties = feature.get("properties", {})
        try:
            admin_level = int(properties.get("admin_level", 8))
        except (TypeError, ValueError):
            admin_level = 8
        if admin_level <= 2:
            color, width = "#8f7f8f", 2
        elif admin_level <= 4:
            color, width = "#9f8f9f", 1
        elif admin_level <= 6 and zoom >= 8:
            color, width = "#aea0ae", 1
        else:
            continue
        _draw_vector_geometry_lines(drawing, feature.get("geometry", {}), color, width, render_scale)


# Below this zoom, landcover/landuse/park shading is skipped entirely --
# at a wide/regional view those polygons fragment into visual noise, and
# roads/water/boundaries/labels are the detail that actually matters there.
LAND_SHADING_MIN_ZOOM = 9


def render_vector_tile_image(data: dict, zoom: int) -> Image.Image:
    """Render one 256x256 basemap tile (land, water, boundaries, roads --
    no buildings, no text) from decoded OpenFreeMap vector data. Drawn at
    SUPERSAMPLE x the final size and downsampled with LANCZOS at the end,
    since PIL's ImageDraw has no anti-aliasing of its own and a straight
    line/polygon edge drawn at 1x looks visibly jagged, especially once the
    tile is upscaled on screen for a continuous zoom."""
    render_size = GENERATED_TILE_SIZE * SUPERSAMPLE
    image = Image.new("RGB", (render_size, render_size), GENERATED_BACKGROUND_COLOR)
    drawing = ImageDraw.Draw(image)
    if zoom >= LAND_SHADING_MIN_ZOOM:
        for layer_name, fill, outline in VECTOR_LAND_LAYERS:
            _draw_vector_polygon_layer(drawing, data, layer_name, fill, outline, SUPERSAMPLE)
    _draw_vector_polygon_layer(drawing, data, "water", VECTOR_WATER_FILL, VECTOR_WATER_OUTLINE, SUPERSAMPLE)
    _draw_vector_waterways(drawing, data, zoom, SUPERSAMPLE)
    _draw_vector_boundaries(drawing, data, zoom, SUPERSAMPLE)
    _draw_vector_transportation(drawing, data, zoom, SUPERSAMPLE)
    return image.resize((GENERATED_TILE_SIZE, GENERATED_TILE_SIZE), Image.Resampling.LANCZOS)


def _tile_pixel_to_world(
    pixel_x: float, pixel_y: float, zoom: int, x: int, y: int, center_lat: float, center_lon: float
) -> tuple[float, float]:
    left, bottom, right, top = tile_bounds_mercator(zoom, x, y)
    mercator_x = left + pixel_x / GENERATED_TILE_SIZE * (right - left)
    mercator_y = top - pixel_y / GENERATED_TILE_SIZE * (top - bottom)
    latitude, longitude = mercator_to_latlon(mercator_x, mercator_y)
    return latlon_to_world(latitude, longitude, center_lat, center_lon)


def _vector_label_text(properties: dict) -> str:
    text = properties.get("name:en") or properties.get("name")
    return str(text).strip() if text else ""


_PLACE_RANK_BY_CLASS = {"city": 4, "town": 3, "borough": 3, "suburb": 2, "village": 2, "hamlet": 1, "neighbourhood": 1}


def extract_vector_labels(
    data: dict, zoom: int, x: int, y: int, center_lat: float, center_lon: float
) -> list[dict[str, Any]]:
    """Place names + named-road labels from one decoded vector tile, as plain
    dicts (kind/text/x/y/rank/bearing_deg) in world coordinates -- kept
    entirely separate from the baked tile image so the UI can draw (or, under
    Incognito, not draw) them as a live overlay."""
    labels: list[dict[str, Any]] = []
    for feature in data.get("place", {}).get("features", []):
        properties = feature.get("properties", {})
        name = _vector_label_text(properties)
        geometry = feature.get("geometry", {})
        if not name or geometry.get("type") != "Point":
            continue
        point = geometry.get("coordinates")
        if not point:
            continue
        pixel_x, pixel_y = scale_point(point)
        if not (0.0 <= pixel_x <= GENERATED_TILE_SIZE and 0.0 <= pixel_y <= GENERATED_TILE_SIZE):
            continue
        world_x, world_y = _tile_pixel_to_world(pixel_x, pixel_y, zoom, x, y, center_lat, center_lon)
        labels.append(
            {
                "kind": "place",
                "text": name,
                "x": world_x,
                "y": world_y,
                "rank": _PLACE_RANK_BY_CLASS.get(properties.get("class", ""), 0),
                "bearing_deg": 0.0,
            }
        )

    if zoom >= 13:
        seen_names: set[str] = set()
        for feature in data.get("transportation_name", {}).get("features", []):
            properties = feature.get("properties", {})
            name = _vector_label_text(properties)
            if not name or name in seen_names:
                continue
            geometry = feature.get("geometry", {})
            geometry_type = geometry.get("type")
            coordinates = geometry.get("coordinates", [])
            if geometry_type == "MultiLineString" and coordinates:
                coordinates = coordinates[0]
            elif geometry_type != "LineString":
                continue
            if len(coordinates) < 2:
                continue
            mid = len(coordinates) // 2
            point_before = coordinates[max(0, mid - 1)]
            point_after = coordinates[min(len(coordinates) - 1, mid + 1)]
            px1, py1 = scale_point(point_before)
            px2, py2 = scale_point(point_after)
            bearing = math.degrees(math.atan2(py2 - py1, px2 - px1))
            if bearing > 90.0:
                bearing -= 180.0
            elif bearing < -90.0:
                bearing += 180.0
            pixel_x, pixel_y = scale_point(coordinates[mid])
            if not (0.0 <= pixel_x <= GENERATED_TILE_SIZE and 0.0 <= pixel_y <= GENERATED_TILE_SIZE):
                continue
            world_x, world_y = _tile_pixel_to_world(pixel_x, pixel_y, zoom, x, y, center_lat, center_lon)
            road_class = properties.get("class", "")
            if MIN_LABEL_ZOOM_BY_CLASS.get(road_class, 15) > zoom:
                continue
            labels.append(
                {
                    "kind": "road",
                    "text": name,
                    "x": world_x,
                    "y": world_y,
                    "rank": 0,
                    "bearing_deg": bearing,
                    "highway_class": road_class,
                }
            )
            seen_names.add(name)
    return labels


def choose_tile_zoom(screen_pixels_per_meter: float, max_zoom: int) -> int:
    ideal = math.log2(max(1e-9, screen_pixels_per_meter) * WEB_MERCATOR_WORLD_M / 256.0)
    # Prefer the next coarser level so an interactive viewport needs fewer community-hosted tiles.
    return max(1, min(max_zoom, int(math.floor(ideal))))


def decode_grayscale_tile(data: bytes) -> Image.Image:
    """Decode + contrast-enhance a raw tile once, at its natural resolution.

    This is the expensive step (JPEG/PNG decode, alpha compositing, contrast
    enhancement). Callers that need many different output sizes from the same
    source bytes -- e.g. re-resizing on every tick of a continuous zoom --
    should cache this result and resize it themselves rather than repeating
    the decode.
    """
    source_rgba = Image.open(io.BytesIO(data)).convert("RGBA")
    white = Image.new("RGBA", source_rgba.size, (255, 255, 255, 255))
    source = Image.alpha_composite(white, source_rgba).convert("RGB")
    return ImageEnhance.Contrast(source).enhance(1.15).convert("L")


def decode_color_tile(data: bytes) -> Image.Image:
    """Decode a tile keeping its full color, for the locally-rendered
    "Generated" layer -- its land/water colors should actually show instead
    of being flattened to grayscale the way the (now-unused) raster
    Street/Topographic layers were."""
    source_rgba = Image.open(io.BytesIO(data)).convert("RGBA")
    white = Image.new("RGBA", source_rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, source_rgba).convert("RGB")


def grayscale_map_tile(data: bytes, pixel_size: int) -> Image.Image:
    return decode_grayscale_tile(data).resize((pixel_size, pixel_size), Image.Resampling.BILINEAR)


def overture_rows_to_elements(
    rows: list[dict[str, Any]],
    *,
    limit: int = OVERTURE_BUILDING_LIMIT,
) -> list[dict[str, Any]]:
    """Convert Overture WKB building rows to the obstacle import's element shape."""
    elements: list[dict[str, Any]] = []
    for row in rows:
        if len(elements) >= limit:
            break
        geometry_bytes = row.get("geometry")
        if not geometry_bytes:
            continue
        try:
            feature_geometry = wkb.loads(bytes(geometry_bytes))
        except Exception:
            continue
        if isinstance(feature_geometry, Polygon):
            polygons = [feature_geometry]
        elif isinstance(feature_geometry, MultiPolygon):
            polygons = list(feature_geometry.geoms)
        else:
            continue
        names = row.get("names") or {}
        name = names.get("primary", "") if isinstance(names, dict) else ""
        sources = row.get("sources") or []
        source_datasets = list(
            dict.fromkeys(
                str(source.get("dataset", "")).strip()
                for source in sources
                if isinstance(source, dict) and source.get("dataset")
            )
        )
        tags = {
            "building": "yes",
            "name": str(name or ""),
            "source": "Overture Maps",
            "source:datasets": ", ".join(source_datasets),
        }
        height = row.get("height")
        floors = row.get("num_floors")
        if isinstance(height, (int, float)) and math.isfinite(float(height)) and float(height) > 0:
            tags["height"] = f"{float(height):.3f}"
        if isinstance(floors, (int, float)) and float(floors) > 0:
            tags["building:levels"] = str(int(floors))
        feature_id = str(row.get("id", "")).strip()
        for polygon_index, polygon in enumerate(polygons):
            if len(elements) >= limit:
                break
            coordinates = list(polygon.exterior.coords)
            if len(coordinates) < 4:
                continue
            elements.append(
                {
                    "type": "overture",
                    "id": f"{feature_id}:{polygon_index}" if polygon_index else feature_id,
                    "tags": dict(tags),
                    "geometry": [
                        {"lat": float(latitude), "lon": float(longitude)}
                        for longitude, latitude in coordinates
                    ],
                }
            )
    return elements


def split_geographic_bounds(
    south: float,
    west: float,
    north: float,
    east: float,
    *,
    columns: int,
    rows: int,
) -> list[tuple[float, float, float, float]]:
    """Split bounds into cells covering the complete visible geographic area."""
    columns = max(1, columns)
    rows = max(1, rows)
    latitude_step = (north - south) / rows
    longitude_step = (east - west) / columns
    return [
        (
            south + row * latitude_step,
            west + column * longitude_step,
            north if row == rows - 1 else south + (row + 1) * latitude_step,
            east if column == columns - 1 else west + (column + 1) * longitude_step,
        )
        for row in range(rows)
        for column in range(columns)
    ]


def obstacle_import_plan(width_m: float, height_m: float) -> tuple[int, int, int, bool]:
    """Choose a bounded geographic grid and spatially distributed building capacity."""
    width_m = max(1.0, width_m)
    height_m = max(1.0, height_m)
    area_m2 = width_m * height_m
    desired_cells = max(4, math.ceil(area_m2 / OBSTACLE_DETAIL_CELL_AREA_M2))
    target_cells = min(OBSTACLE_IMPORT_MAX_CELLS, desired_cells)
    aspect = width_m / height_m
    candidates = [
        (columns, rows)
        for columns in range(1, OBSTACLE_IMPORT_MAX_CELLS + 1)
        for rows in range(1, OBSTACLE_IMPORT_MAX_CELLS + 1)
        if target_cells <= columns * rows <= OBSTACLE_IMPORT_MAX_CELLS
    ]
    columns, rows = min(
        candidates,
        key=lambda pair: (
            pair[0] * pair[1] - target_cells,
            abs(math.log(max(1e-9, (pair[0] / pair[1]) / aspect))),
        ),
    )
    cell_count = columns * rows
    building_limit = min(
        OVERTURE_MAX_VIEWPORT_BUILDING_LIMIT,
        max(6000, cell_count * 1500),
    )
    spatially_sampled = desired_cells > OBSTACLE_IMPORT_MAX_CELLS
    return columns, rows, building_limit, spatially_sampled


class MapDataService:
    def __init__(self) -> None:
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home())
        self.cache_root = Path(local_app_data) / "MeshLabRF" / "map-cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.tile_requests: queue.Queue[tuple[str, int, int, int]] = queue.Queue()
        self.tile_results: queue.Queue[tuple[tuple[str, int, int, int], bytes | Exception]] = queue.Queue()
        self.pending: set[tuple[str, int, int, int]] = set()
        self.pending_lock = threading.Lock()
        self._tile_generators: dict[str, Callable[[int, int, int], bytes]] = {}
        self._tile_generator_versions: dict[str, int] = {}
        for index in range(MAP_TILE_WORKERS):
            threading.Thread(target=self._tile_worker, name=f"MapTileWorker{index + 1}", daemon=True).start()

    def set_tile_generator(
        self, layer: str, generator: Callable[[int, int, int], bytes], *, version: int = 1
    ) -> None:
        """Register a purely-local (layer, zoom, x, y) -> PNG bytes generator
        as a drop-in data source for get_tile_bytes, so the existing
        fetch/cache/stitch pipeline works unchanged for locally-rendered tiles.

        `version` is baked into the on-disk cache path (see _cache_path) so a
        future change to the rendering logic -- a new style, a fixed bug --
        can bump it to invalidate every previously-cached tile automatically,
        rather than silently keeping serving stale renders from disk forever
        the way a raster tile's cache correctly can (its source URL's content
        doesn't change out from under it)."""
        self._tile_generators[layer] = generator
        self._tile_generator_versions[layer] = version

    def request_tile(self, layer: str, zoom: int, x: int, y: int) -> None:
        maximum = 2**zoom
        if y < 0 or y >= maximum:
            return
        key = (layer, zoom, x % maximum, y)
        with self.pending_lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.tile_requests.put(key)

    def _tile_worker(self) -> None:
        while True:
            key = self.tile_requests.get()
            try:
                result: bytes | Exception = self.get_tile_bytes(*key)
            except Exception as error:
                result = error
            with self.pending_lock:
                self.pending.discard(key)
            self.tile_results.put((key, result))

    def _cache_path(self, layer: str, zoom: int, x: int, y: int) -> Path:
        version = self._tile_generator_versions.get(layer)
        cache_layer = f"{layer}-v{version}" if version is not None else layer
        return self.cache_root / cache_layer / str(zoom) / str(x) / f"{y}.png"

    def cache_path_for(self, layer: str, zoom: int, x: int, y: int) -> Path:
        """Public accessor so a registered tile generator can check/write its
        own on-disk cache -- see get_tile_bytes for why a generated layer
        needs this instead of the generic cache-then-generate flow below."""
        return self._cache_path(layer, zoom, x, y)

    def get_tile_bytes(self, layer: str, zoom: int, x: int, y: int) -> bytes:
        definition = TILE_LAYERS[layer]
        maximum = 2**zoom
        x %= maximum
        if y < 0 or y >= maximum:
            raise ValueError("Tile is outside the Web Mercator world")
        generator = self._tile_generators.get(layer)
        if generator is not None:
            # Always invoke the generator, even when its rendered image is
            # already disk-cached -- it may have per-tile side effects (e.g.
            # extracting text labels for a live overlay cache) that need to
            # run regardless, so a fresh app launch reusing a previously
            # disk-cached tile doesn't silently end up with no labels for
            # it. The generator owns its own cache-path read/write.
            return generator(zoom, x, y)
        cache_path = self._cache_path(layer, zoom, x, y)
        if cache_path.exists():
            return cache_path.read_bytes()
        url = str(definition["url"]).format(z=zoom, x=x, y=y)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix(".tmp")
        temporary_path.write_bytes(data)
        temporary_path.replace(cache_path)
        return data

    @staticmethod
    def geocode(query: str) -> dict[str, Any]:
        global _LAST_GEOCODE_REQUEST
        with _GEOCODE_LOCK:
            wait_seconds = 1.0 - (time.monotonic() - _LAST_GEOCODE_REQUEST)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _LAST_GEOCODE_REQUEST = time.monotonic()
        parameters = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1})
        request = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{parameters}",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            results = json.loads(response.read().decode("utf-8"))
        if not results:
            raise ValueError("No OpenStreetMap location matched that search.")
        return results[0]

    @staticmethod
    def _fetch_overpass_elements(
        south: float,
        west: float,
        north: float,
        east: float,
        element_type: str,
        selectors: list[str],
        out_clause: str,
    ) -> list[dict[str, Any]]:
        bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
        query = (
            "[out:json][timeout:30][maxsize:134217728];"
            "("
            + "".join(f"{element_type}[{selector}]({bbox});" for selector in selectors)
            +
            ");"
            f"{out_clause}"
        )
        body = urllib.parse.urlencode({"data": query}).encode("utf-8")
        request = urllib.request.Request(
            "https://overpass-api.de/api/interpreter",
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return list(payload.get("elements", []))

    @staticmethod
    def _fetch_overpass(
        south: float,
        west: float,
        north: float,
        east: float,
        selectors: list[str],
    ) -> list[dict[str, Any]]:
        return MapDataService._fetch_overpass_elements(
            south, west, north, east, "way", selectors, "out tags geom 1000;"
        )

    @staticmethod
    def fetch_osm_obstacles(south: float, west: float, north: float, east: float) -> list[dict[str, Any]]:
        return MapDataService._fetch_overpass(
            south,
            west,
            north,
            east,
            ['"building"', '"landuse"="forest"', '"natural"="wood"'],
        )

    @staticmethod
    def fetch_osm_forests(south: float, west: float, north: float, east: float) -> list[dict[str, Any]]:
        return MapDataService._fetch_overpass(
            south,
            west,
            north,
            east,
            ['"landuse"="forest"', '"natural"="wood"'],
        )

    def fetch_vector_tile_bytes(self, zoom: int, x: int, y: int) -> bytes:
        """Raw OpenFreeMap vector tile (.pbf) bytes for the generated base
        map, disk-cached exactly like a fetched raster tile."""
        cache_path = self.cache_root / "vector" / str(zoom) / str(x) / f"{y}.pbf"
        if cache_path.exists():
            return cache_path.read_bytes()
        url = OPENFREEMAP_VECTOR_URL.format(z=zoom, x=x, y=y)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix(".tmp")
        temporary_path.write_bytes(data)
        temporary_path.replace(cache_path)
        return data

    def _overture_cache_path(
        self, south: float, west: float, north: float, east: float, limit: int
    ) -> Path:
        bounds = ",".join(f"{value:.5f}" for value in (south, west, north, east, limit))
        digest = hashlib.sha256(bounds.encode("ascii")).hexdigest()[:24]
        return self.cache_root / "obstacles" / "overture" / f"{digest}.json"

    def fetch_overture_buildings(
        self,
        south: float,
        west: float,
        north: float,
        east: float,
        *,
        limit: int = OVERTURE_BUILDING_LIMIT,
    ) -> list[dict[str, Any]]:
        cache_path = self._overture_cache_path(south, west, north, east, limit)
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= OVERTURE_BUILDING_CACHE_SECONDS:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    return cached[:limit]
            except (OSError, ValueError, TypeError):
                pass

        reader = record_batch_reader(
            "building",
            bbox=(west, south, east, north),
            connect_timeout=10,
            request_timeout=60,
            stac=True,
        )
        if reader is None:
            return []
        rows: list[dict[str, Any]] = []
        for batch in reader:
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            rows.extend(batch.slice(0, remaining).to_pylist())
        elements = overture_rows_to_elements(rows, limit=limit)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_path.with_suffix(".tmp")
            temporary_path.write_text(json.dumps(elements, separators=(",", ":")), encoding="utf-8")
            temporary_path.replace(cache_path)
        except OSError:
            pass
        return elements

    def fetch_overture_buildings_for_viewport(
        self,
        south: float,
        west: float,
        north: float,
        east: float,
        *,
        limit: int = OVERTURE_VIEWPORT_BUILDING_LIMIT,
        columns: int = 2,
        rows: int = 2,
        query_workers: int = 4,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Recursively split saturated cells so capped source batches do not leave internal holes."""
        initial_cells = split_geographic_bounds(south, west, north, east, columns=columns, rows=rows)
        frontier = [(cell, 0) for cell in initial_cells]
        leaf_results: list[list[dict[str, Any]]] = []
        # A 750-feature cap forced dense city cells through as many as 84 remote
        # queries per viewport tile.  Give each initial cell enough headroom to
        # supply its proportional share of the requested result (plus 50%).  A
        # genuinely saturated cell is still subdivided, preserving spatial
        # coverage, but ordinary urban imports normally finish in one wave.
        proportional_limit = math.ceil(limit / max(1, len(initial_cells)) * 1.5)
        cell_query_limit = max(
            1,
            min(limit, max(OVERTURE_CELL_QUERY_LIMIT, proportional_limit)),
        )
        completed_units = 0
        total_units = len(frontier) * (4**OVERTURE_ADAPTIVE_MAX_DEPTH)
        processed_queries = 0
        if progress_callback:
            progress_callback(completed_units, total_units, "Querying building cells")

        def fetch_cell(item: tuple[tuple[float, float, float, float], int]) -> list[dict[str, Any]]:
            cell, _depth = item
            return self.fetch_overture_buildings(*cell, limit=cell_query_limit)

        while frontier:
            next_frontier: list[tuple[tuple[float, float, float, float], int]] = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(max(1, query_workers), len(frontier))
            ) as executor:
                future_items = {executor.submit(fetch_cell, item): item for item in frontier}
                for future in concurrent.futures.as_completed(future_items):
                    cell, depth = future_items[future]
                    cell_elements = future.result()
                    if len(cell_elements) >= cell_query_limit and depth < OVERTURE_ADAPTIVE_MAX_DEPTH:
                        children = [
                            (subcell, depth + 1)
                            for subcell in split_geographic_bounds(*cell, columns=2, rows=2)
                        ]
                        next_frontier.extend(children)
                        phase = f"Subdividing saturated cell to level {depth + 1}"
                    else:
                        leaf_results.append(cell_elements)
                        completed_units += 4 ** (OVERTURE_ADAPTIVE_MAX_DEPTH - depth)
                        phase = f"Loaded building cell level {depth}"
                    processed_queries += 1
                    if progress_callback:
                        progress_callback(
                            completed_units,
                            total_units,
                            f"{phase} · {processed_queries} queries checked",
                        )
            frontier = next_frontier

        # If a very dense view exceeds the final global bound, retain data from every
        # leaf rather than allowing source order to empty the cells processed last.
        elements: list[dict[str, Any]] = []
        seen: set[str] = set()
        remaining = [deque(cell_elements) for cell_elements in leaf_results]
        while len(elements) < limit and any(remaining):
            for cell_elements in remaining:
                while cell_elements:
                    element = cell_elements.popleft()
                    identifier = f"{element.get('type', 'overture')}/{element.get('id', '')}"
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    elements.append(element)
                    break
                if len(elements) >= limit:
                    break
        return elements

    def build_terrain_grid(
        self,
        center_latitude: float,
        center_longitude: float,
        width_m: float,
        height_m: float,
        columns: int | None = None,
    ) -> tuple[int, int, list[float], int]:
        columns = columns or max(49, min(129, round(width_m / 175.0) + 1))
        rows = max(25, min(97, round(columns * height_m / max(1.0, width_m))))
        center_x, center_y = latlon_to_mercator(center_latitude, center_longitude)
        # width_m/height_m are true ground meters; Web Mercator itself inflates
        # distance by 1/cos(latitude), so the raw mercator span to query is
        # larger than the true-meter span by that same factor.
        scale = world_scale_factor(center_latitude)
        mercator_width = width_m / scale
        mercator_height = height_m / scale
        left = center_x - mercator_width / 2.0
        right = center_x + mercator_width / 2.0
        bottom = center_y - mercator_height / 2.0
        top = center_y + mercator_height / 2.0
        zoom = 14
        while zoom > 1:
            tx1, ty1 = mercator_to_tile(left, top, zoom)
            tx2, ty2 = mercator_to_tile(right, bottom, zoom)
            count = (math.floor(tx2) - math.floor(tx1) + 1) * (math.floor(ty2) - math.floor(ty1) + 1)
            if count <= 36:
                break
            zoom -= 1
        column_samples: list[tuple[int, int]] = []
        for column in range(columns):
            mercator_x = left + mercator_width * column / max(1, columns - 1)
            tile_x_float, _unused_y = mercator_to_tile(mercator_x, center_y, zoom)
            tile_x = math.floor(tile_x_float)
            pixel_x = max(0, min(255, int((tile_x_float - tile_x) * 256)))
            column_samples.append((tile_x, pixel_x))

        row_samples: list[tuple[int, int]] = []
        for row in range(rows):
            mercator_y = top - mercator_height * row / max(1, rows - 1)
            _unused_x, tile_y_float = mercator_to_tile(center_x, mercator_y, zoom)
            tile_y = math.floor(tile_y_float)
            pixel_y = max(0, min(255, int((tile_y_float - tile_y) * 256)))
            row_samples.append((tile_y, pixel_y))

        tile_keys = list(
            dict.fromkeys(
                (tile_x, tile_y)
                for tile_y, _pixel_y in row_samples
                for tile_x, _pixel_x in column_samples
            )
        )

        def load_tile(key: tuple[int, int]) -> Image.Image:
            data = self.get_tile_bytes("TerrainDEM", zoom, key[0], key[1])
            return Image.open(io.BytesIO(data)).convert("RGB")

        if len(tile_keys) == 1:
            images = {tile_keys[0]: load_tile(tile_keys[0])}
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(MAP_TILE_WORKERS, len(tile_keys))
            ) as executor:
                futures = {key: executor.submit(load_tile, key) for key in tile_keys}
                images = {key: futures[key].result() for key in tile_keys}

        pixels = {key: image.load() for key, image in images.items()}
        values: list[float] = []
        for tile_y, pixel_y in row_samples:
            for tile_x, pixel_x in column_samples:
                red, green, blue = pixels[(tile_x, tile_y)][pixel_x, pixel_y]
                values.append(red * 256.0 + green + blue / 256.0 - 32768.0)
        return columns, rows, values, zoom
