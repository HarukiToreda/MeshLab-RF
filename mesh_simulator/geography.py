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

from PIL import Image, ImageEnhance
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
MAP_TILE_WORKERS = 4
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


def latlon_to_world(
    latitude: float,
    longitude: float,
    center_latitude: float,
    center_longitude: float,
) -> tuple[float, float]:
    """Return unrestricted, center-relative Web Mercator coordinates."""
    x, y = latlon_to_mercator(latitude, longitude)
    center_x, center_y = _map_center_mercator(center_latitude, center_longitude)
    return x - center_x, center_y - y


def world_to_latlon(
    x: float,
    y: float,
    center_latitude: float,
    center_longitude: float,
) -> tuple[float, float]:
    """Convert unrestricted center-relative coordinates to latitude/longitude."""
    center_x, center_y = _map_center_mercator(center_latitude, center_longitude)
    return mercator_to_latlon(center_x + x, center_y - y)


def world_viewport_to_mercator_bounds(
    world_left: float,
    world_top: float,
    world_right: float,
    world_bottom: float,
    center_latitude: float,
    center_longitude: float,
) -> tuple[float, float, float, float]:
    """Convert an unrestricted canvas viewport into ordered Web Mercator bounds."""
    left, right = min(world_left, world_right), max(world_left, world_right)
    top, bottom = min(world_top, world_bottom), max(world_top, world_bottom)
    center_x, center_y = _map_center_mercator(center_latitude, center_longitude)
    return (
        center_x + left,
        center_y - top,
        center_x + right,
        center_y - bottom,
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


def choose_tile_zoom(screen_pixels_per_meter: float, max_zoom: int) -> int:
    ideal = math.log2(max(1e-9, screen_pixels_per_meter) * WEB_MERCATOR_WORLD_M / 256.0)
    # Prefer the next coarser level so an interactive viewport needs fewer community-hosted tiles.
    return max(1, min(max_zoom, int(math.floor(ideal))))


def grayscale_map_tile(data: bytes, pixel_size: int) -> Image.Image:
    source_rgba = Image.open(io.BytesIO(data)).convert("RGBA")
    white = Image.new("RGBA", source_rgba.size, (255, 255, 255, 255))
    source = Image.alpha_composite(white, source_rgba).convert("RGB")
    source = ImageEnhance.Contrast(source).enhance(1.15).convert("L")
    return source.resize((pixel_size, pixel_size), Image.Resampling.BILINEAR)


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
        for index in range(MAP_TILE_WORKERS):
            threading.Thread(target=self._tile_worker, name=f"MapTileWorker{index + 1}", daemon=True).start()

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
        return self.cache_root / layer / str(zoom) / str(x) / f"{y}.png"

    def get_tile_bytes(self, layer: str, zoom: int, x: int, y: int) -> bytes:
        definition = TILE_LAYERS[layer]
        maximum = 2**zoom
        x %= maximum
        if y < 0 or y >= maximum:
            raise ValueError("Tile is outside the Web Mercator world")
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
    def _fetch_overpass(
        south: float,
        west: float,
        north: float,
        east: float,
        selectors: list[str],
    ) -> list[dict[str, Any]]:
        bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
        query = (
            "[out:json][timeout:30][maxsize:134217728];"
            "("
            + "".join(f"way[{selector}]({bbox});" for selector in selectors)
            +
            ");"
            "out tags geom 1000;"
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
        left = center_x - width_m / 2.0
        right = center_x + width_m / 2.0
        bottom = center_y - height_m / 2.0
        top = center_y + height_m / 2.0
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
            mercator_x = left + width_m * column / max(1, columns - 1)
            tile_x_float, _unused_y = mercator_to_tile(mercator_x, center_y, zoom)
            tile_x = math.floor(tile_x_float)
            pixel_x = max(0, min(255, int((tile_x_float - tile_x) * 256)))
            column_samples.append((tile_x, pixel_x))

        row_samples: list[tuple[int, int]] = []
        for row in range(rows):
            mercator_y = top - height_m * row / max(1, rows - 1)
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
