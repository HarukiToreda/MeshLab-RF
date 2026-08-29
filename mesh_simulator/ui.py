from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import math
import os
import queue
import random
import threading
import time
import tkinter as tk
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont, ImageTk

from .geography import (
    MapDataService,
    OBSTACLE_IMPORT_MAX_AREA_M2,
    OVERTURE_VIEWPORT_BUILDING_LIMIT,
    TILE_LAYERS,
    WEB_MERCATOR_WORLD_M,
    choose_tile_zoom,
    decode_grayscale_tile,
    latlon_to_mercator,
    latlon_to_world,
    mercator_to_latlon,
    mercator_to_tile,
    obstacle_import_plan,
    tile_bounds_mercator,
    tile_size_m,
    world_scale_factor,
    world_viewport_to_mercator_bounds,
    world_to_latlon,
)
from .live_radio import LiveNode, LiveRadioClient, SerialPort, list_serial_ports
from .live_mesh import LiveMeshEngine, LiveMeshFrame, LiveMeshResult, LiveMeshTestResult, TRAFFIC_COLORS
from .model import (
    CORE_PORTS,
    HARDWARE_POWER_PROFILE_KEYS,
    MIN_DECODE_MARGIN_DB,
    BeaconProfile,
    HorizonPanorama,
    HorizonPoint,
    LiveMeshConfig,
    PathProfile,
    OBSTACLE_DEFAULTS,
    PRESETS,
    PRESET_DISPLAY_NAMES,
    REGION_BANDS,
    REBROADCAST_MODES,
    ROLE_COLORS,
    ROLES,
    Node,
    Obstacle,
    PacketConfig,
    PropagationModel,
    Scenario,
    SimEvent,
    SimulationEngine,
    SimulationResult,
    dbm_to_watts,
    dm_route_key,
    hardware_power_profile,
    meshtastic_default_frequency_mhz,
    new_id,
    preset_parameters,
    region_for_preset,
    region_preset_options,
    scenario_from_file,
    scenario_to_file,
)
from .survey import merge_survey_rows
from .survey_calibration import (
    SurveyCalibrationError,
    apply_building_calibration,
    fit_building_calibration,
)
from .survey_device import (
    DeviceCapture,
    DeviceInfo,
    SurveyExport,
    capture_device,
    query_device,
    read_measurements,
    save_captures,
)


BG = "#07101d"
PANEL = "#0e1929"
PANEL_2 = "#132238"
ENTRY = "#172840"
TEXT = "#e7eef9"
MUTED = "#8ca0ba"
ACCENT = "#27b3ff"
GREEN = "#31d58b"
AMBER = "#ffbd4a"
RED = "#fb6376"
BORDER = "#233752"
MAPLESS_BACKGROUND = "#ffffff"
HOP_COLORS = {
    0: "#27b3ff",
    1: "#31d58b",
    2: "#38bdf8",
    3: "#ffbd4a",
    4: "#c084fc",
    5: "#fb7185",
    6: "#f97316",
    7: "#a3e635",
}
METERS_PER_FOOT = 0.3048
METERS_PER_MILE = 1609.344
MIN_CANVAS_ZOOM = 0.001
MAX_CANVAS_ZOOM = 2048.0
MAX_CACHED_TILE_PIXELS = 1024
PACKET_LAYER_TAG = "packet-layer"
NODE_LAYER_TAG = "node-layer"
CURRENT_WAVE_TAG = "current-wave"
BEACON_TAG = "beacon-pulse"
BEACON_STATIC_TAG = "beacon-static"
BEACON_ANIMATION_TAG = "beacon-animation"
STATIC_COVERAGE_TAG = "static-coverage"
# How many obstacle-import tiles to fetch at once.  Each tile already fans its
# own cells across a small thread pool, so this multiplies overall throughput
# without overwhelming the Overture endpoint.
TILE_IMPORT_CONCURRENCY = 9
TILE_ADAPTIVE_QUERY_CONCURRENCY = 2
SCENE_TREE_OBSTACLE_PAGE_SIZE = 300
HUD_LAYER_TAG = "hud-layer"
GEOGRAPHIC_LAYER_TAG = "geographic-layer"
SELECTED_OBSTACLE_TAG = "selected-obstacle"
SURVEY_LAYER_TAG = "survey-layer"
SURVEY_PORT_NONE = "— Not selected —"
LIVE_TRAFFIC_PRESETS: dict[str, dict[str, Any]] = {
    "Default / slow": {
        "profile": "FIRMWARE_LIKE",
        "nodeinfo_interval_minutes": 180.0,
        "telemetry_interval_minutes": 60.0,
        "router_telemetry_interval_minutes": 720.0,
        "sensor_interval_minutes": 60.0,
        "message_interval_minutes": 120.0,
    },
    "Medium traffic": {
        "profile": "FIRMWARE_LIKE",
        "nodeinfo_interval_minutes": 90.0,
        "telemetry_interval_minutes": 30.0,
        "router_telemetry_interval_minutes": 360.0,
        "sensor_interval_minutes": 30.0,
        "message_interval_minutes": 60.0,
    },
    "High traffic": {
        "profile": "BUSY_10X",
        "nodeinfo_interval_minutes": 180.0,
        "telemetry_interval_minutes": 60.0,
        "router_telemetry_interval_minutes": 720.0,
        "sensor_interval_minutes": 60.0,
        "message_interval_minutes": 120.0,
    },
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def layout_node_labels(
    entries: list[tuple[str, str, float, float, bool]],
    canvas_width: int,
    canvas_height: int,
) -> dict[str, tuple[float, float, float, float, float, float]]:
    """Place outlined node labels around markers while avoiding nearby labels."""
    markers = {
        node_id: (
            x - (18 if infrastructure else 13),
            y - (18 if infrastructure else 13),
            x + (18 if infrastructure else 13),
            y + (18 if infrastructure else 13),
        )
        for node_id, _name, x, y, infrastructure in entries
    }
    occupied: list[tuple[float, float, float, float]] = []
    placements: dict[str, tuple[float, float, float, float, float, float]] = {}

    def overlap_area(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> float:
        width = min(first[2], second[2]) - max(first[0], second[0])
        height = min(first[3], second[3]) - max(first[1], second[1])
        return max(0.0, width) * max(0.0, height)

    for node_id, name, x, y, infrastructure in entries:
        width = clamp(len(name) * 7.2 + 12.0, 48.0, 240.0)
        height = 36.0 if infrastructure else 21.0
        half_width = width / 2.0
        half_height = height / 2.0
        marker_radius = 18.0 if infrastructure else 13.0
        vertical_distance = marker_radius + half_height + 6.0
        horizontal_distance = marker_radius + half_width + 7.0
        candidates = (
            (x, y + vertical_distance),
            (x, y - vertical_distance),
            (x + horizontal_distance, y),
            (x - horizontal_distance, y),
            (x + horizontal_distance * 0.72, y + vertical_distance * 0.78),
            (x - horizontal_distance * 0.72, y + vertical_distance * 0.78),
            (x + horizontal_distance * 0.72, y - vertical_distance * 0.78),
            (x - horizontal_distance * 0.72, y - vertical_distance * 0.78),
        )
        best: tuple[float, float, tuple[float, float, float, float]] | None = None
        for preference, (center_x, center_y) in enumerate(candidates):
            rectangle = (
                center_x - half_width,
                center_y - half_height,
                center_x + half_width,
                center_y + half_height,
            )
            label_overlap = sum(overlap_area(rectangle, other) for other in occupied)
            marker_overlap = sum(
                overlap_area(rectangle, marker)
                for other_id, marker in markers.items()
                if other_id != node_id
            )
            outside = (
                max(0.0, -rectangle[0])
                + max(0.0, -rectangle[1])
                + max(0.0, rectangle[2] - canvas_width)
                + max(0.0, rectangle[3] - canvas_height)
            )
            score = label_overlap * 1000.0 + marker_overlap * 120.0 + outside * 500.0 + preference
            if best is None or score < best[0]:
                best = (score, float(preference), rectangle)
        assert best is not None
        left, top, right, bottom = best[2]
        center_x = (left + right) / 2.0
        group_center_y = (top + bottom) / 2.0
        name_y = group_center_y - 8.0 if infrastructure else group_center_y
        placements[node_id] = (center_x, name_y, left, top, right, bottom)
        occupied.append((left, top, right, bottom))
    return placements


def format_distance_value(meters: float, unit_system: str) -> str:
    if unit_system == "Imperial":
        feet = meters / METERS_PER_FOOT
        if abs(feet) >= 5280:
            return f"{meters / METERS_PER_MILE:.2f} mi"
        return f"{feet:,.0f} ft"
    if abs(meters) >= 1000:
        return f"{meters / 1000:.2f} km"
    return f"{meters:,.0f} m"


def format_area_value(square_meters: float, unit_system: str) -> str:
    if unit_system == "Imperial":
        return f"{square_meters / (METERS_PER_MILE**2):.2f} mi²"
    return f"{square_meters / 1_000_000:.2f} km²"


def survey_bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def survey_value_known(value: object) -> bool:
    return value is not None and str(value).strip().lower() not in {"", "none", "null"}


def survey_float(value: object) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def survey_signal_color(measurement: dict[str, object]) -> str:
    if not survey_bool(measurement.get("forward_received")):
        return RED
    rssi = survey_float(measurement.get("forward_rssi_dbm"))
    if rssi is None:
        return MUTED
    if rssi >= -90.0:
        return GREEN
    if rssi >= -110.0:
        return AMBER
    return RED


def build_terrain_visual(
    columns: int,
    rows: int,
    values: list[float],
    width_m: float,
    height_m: float,
    unit_system: str = "Metric",
) -> Image.Image | None:
    """Create a smooth, road-free elevation drawing with labeled contours."""
    if columns < 2 or rows < 2 or len(values) != columns * rows:
        return None
    elevations = np.asarray(values, dtype=np.float32).reshape((rows, columns))
    finite = np.isfinite(elevations)
    if not finite.any():
        return None
    replacement = float(np.nanmedian(np.where(finite, elevations, np.nan)))
    elevations = np.where(finite, elevations, replacement)

    maximum_source_dimension = max(columns, rows)
    target_maximum = max(768, min(1400, maximum_source_dimension * 12))
    enlargement = target_maximum / maximum_source_dimension
    target_width = max(2, round(columns * enlargement))
    target_height = max(2, round(rows * enlargement))
    elevation_surface = Image.fromarray(elevations, mode="F").resize(
        (target_width, target_height),
        Image.Resampling.BICUBIC,
    )
    elevations = np.asarray(elevation_surface, dtype=np.float32)

    cell_x = max(1.0, width_m / max(1, target_width - 1))
    cell_y = max(1.0, height_m / max(1, target_height - 1))
    gradient_y, gradient_x = np.gradient(elevations, cell_y, cell_x)
    normal_x = -gradient_x
    normal_y = -gradient_y
    normal_z = np.ones_like(elevations)
    normal_length = np.sqrt(normal_x**2 + normal_y**2 + normal_z**2)
    # Very light northwest relief keeps contours readable without gray terrain bands.
    azimuth = math.radians(315.0)
    altitude = math.radians(42.0)
    light_x = math.cos(altitude) * math.sin(azimuth)
    light_y = -math.cos(altitude) * math.cos(azimuth)
    light_z = math.sin(altitude)
    illumination = (
        normal_x * light_x + normal_y * light_y + normal_z * light_z
    ) / np.maximum(normal_length, 1e-6)
    minimum = float(np.min(elevations))
    maximum = float(np.max(elevations))
    relief = (elevations - minimum) / max(1.0, maximum - minimum)
    grayscale = np.clip(239.0 + illumination * 8.0 - relief * 7.0, 218.0, 247.0)

    elevation_range = maximum - minimum
    display_range = elevation_range / METERS_PER_FOOT if unit_system == "Imperial" else elevation_range
    if unit_system == "Imperial":
        if display_range <= 300:
            display_interval = 20.0
        elif display_range <= 1000:
            display_interval = 50.0
        elif display_range <= 3000:
            display_interval = 100.0
        else:
            display_interval = 250.0
        contour_interval = display_interval * METERS_PER_FOOT
    else:
        if display_range <= 100:
            display_interval = 10.0
        elif display_range <= 300:
            display_interval = 20.0
        elif display_range <= 800:
            display_interval = 50.0
        else:
            display_interval = 100.0
        contour_interval = display_interval
    contour_bands = np.floor(elevations / contour_interval).astype(np.int32)
    contour_edges = np.zeros_like(contour_bands, dtype=bool)
    contour_edges[:, 1:] |= contour_bands[:, 1:] != contour_bands[:, :-1]
    contour_edges[1:, :] |= contour_bands[1:, :] != contour_bands[:-1, :]
    major_edges = contour_edges & (np.mod(contour_bands, 5) == 0)
    grayscale[contour_edges] = np.minimum(grayscale[contour_edges], 145.0)
    grayscale[major_edges] = 68.0
    major_thick = major_edges.copy()
    major_thick[:, 1:] |= major_edges[:, :-1]
    major_thick[1:, :] |= major_edges[:-1, :]
    grayscale[major_thick] = np.minimum(grayscale[major_thick], 82.0)

    visual = Image.fromarray(grayscale.astype(np.uint8), mode="L").convert("RGB")
    drawing = ImageDraw.Draw(visual)
    font = ImageFont.load_default(size=12)
    major_interval = contour_interval * 5.0
    first_level = math.ceil(minimum / major_interval) * major_interval
    last_level = math.floor(maximum / major_interval) * major_interval
    label_levels = np.arange(first_level, last_level + major_interval * 0.5, major_interval)
    placed: list[tuple[int, int]] = []
    for label_index, level in enumerate(label_levels[:16]):
        candidates = np.argwhere(
            contour_edges & (np.abs(elevations - level) <= contour_interval * 0.4)
        )
        if len(candidates) == 0:
            continue
        target_x = target_width * (0.25 + 0.25 * (label_index % 3))
        order = np.argsort(np.abs(candidates[:, 1] - target_x))
        chosen: tuple[int, int] | None = None
        for candidate_index in order[:200]:
            y, x = (int(value) for value in candidates[candidate_index])
            if 24 <= x <= target_width - 24 and 14 <= y <= target_height - 14 and all(
                (x - previous_x) ** 2 + (y - previous_y) ** 2 > 110**2
                for previous_x, previous_y in placed
            ):
                chosen = (x, y)
                break
        if chosen is None:
            continue
        placed.append(chosen)
        if unit_system == "Imperial":
            label = f"{round(level / METERS_PER_FOOT):,} ft"
        else:
            label = f"{round(level):,} m"
        drawing.text(
            chosen,
            label,
            anchor="mm",
            fill="#34383d",
            font=font,
            stroke_width=2,
            stroke_fill="#f1f1ed",
        )
    return visual


def decode_terrarium_elevations(data: bytes) -> np.ndarray:
    """Decode a Mapzen Terrarium PNG into a two-dimensional meter grid."""
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)
    return rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0


def sample_elevation_array(elevations: np.ndarray, tile_x: float, tile_y: float) -> float:
    """Bilinearly sample a decoded DEM tile using fractional tile coordinates."""
    height, width = elevations.shape
    pixel_x = clamp((tile_x - math.floor(tile_x)) * width, 0.0, width - 1.0)
    pixel_y = clamp((tile_y - math.floor(tile_y)) * height, 0.0, height - 1.0)
    x0, y0 = math.floor(pixel_x), math.floor(pixel_y)
    x1, y1 = min(width - 1, x0 + 1), min(height - 1, y0 + 1)
    fx, fy = pixel_x - x0, pixel_y - y0
    top = float(elevations[y0, x0]) * (1.0 - fx) + float(elevations[y0, x1]) * fx
    bottom = float(elevations[y1, x0]) * (1.0 - fx) + float(elevations[y1, x1]) * fx
    return top * (1.0 - fy) + bottom * fy


def spread_random_points(
    count: int,
    left: float,
    top: float,
    right: float,
    bottom: float,
    seed: int,
) -> list[tuple[float, float]]:
    """Place deterministic random points in separate viewport cells."""
    if count <= 0:
        return []
    left, right = min(left, right), max(left, right)
    top, bottom = min(top, bottom), max(top, bottom)
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)
    aspect = width / height
    columns = max(1, round(math.sqrt(count * aspect)))
    rows = max(1, math.ceil(count / columns))
    while columns * rows < count:
        rows += 1
    cells = [(column, row) for row in range(rows) for column in range(columns)]
    randomizer = random.Random(seed)
    randomizer.shuffle(cells)
    cell_width = width / columns
    cell_height = height / rows
    points: list[tuple[float, float]] = []
    for column, row in cells[:count]:
        jitter_x = randomizer.uniform(-0.28, 0.28)
        jitter_y = randomizer.uniform(-0.28, 0.28)
        points.append(
            (
                left + (column + 0.5 + jitter_x) * cell_width,
                top + (row + 0.5 + jitter_y) * cell_height,
            )
        )
    return points


def spread_random_points_in_regions(
    count: int,
    regions: list[tuple[float, float, float, float]],
    seed: int,
) -> list[tuple[float, float]]:
    """Distribute points between non-overlapping regions in proportion to usable area."""
    valid_regions = [
        (left, top, right, bottom)
        for left, top, right, bottom in regions
        if right - left >= 1.0 and bottom - top >= 1.0
    ]
    if count <= 0 or not valid_regions:
        return []
    areas = [(right - left) * (bottom - top) for left, top, right, bottom in valid_regions]
    total_area = sum(areas)
    exact_counts = [count * area / total_area for area in areas]
    allocations = [math.floor(value) for value in exact_counts]
    remaining = count - sum(allocations)
    fractional_order = sorted(
        range(len(valid_regions)),
        key=lambda index: (exact_counts[index] - allocations[index], areas[index]),
        reverse=True,
    )
    for index in fractional_order[:remaining]:
        allocations[index] += 1
    points: list[tuple[float, float]] = []
    for index, (region, allocation) in enumerate(zip(valid_regions, allocations)):
        if allocation:
            points.extend(spread_random_points(allocation, *region, seed=seed + index * 104_729))
    randomizer = random.Random(seed ^ 0x5A17)
    randomizer.shuffle(points)
    return points


def packet_path_node_ids(result: SimulationResult, target_id: str) -> list[str]:
    """Return the first-arrival route from the packet source to target."""
    if target_id not in result.reached:
        return []
    reverse_path: list[str] = []
    seen: set[str] = set()
    current = target_id
    while current and current not in seen:
        seen.add(current)
        reverse_path.append(current)
        current = str(result.reached.get(current, {}).get("via", ""))
    reverse_path.reverse()
    return reverse_path


def transmitters_without_new_receivers(
    transmitter_ids: list[str],
    wave: list[SimEvent],
) -> list[str]:
    """Return transmitters whose current hop produced no first-arrival receiver."""
    successful_transmitters = {
        event.peer_id
        for event in wave
        if event.peer_id and event.kind in {"RX", "OPAQUE"}
    }
    return [source_id for source_id in transmitter_ids if source_id not in successful_transmitters]


def first_hop_coverage_to_retain(
    hop: int,
    transmitter_ids: list[str],
    wave: list[SimEvent],
) -> list[str]:
    """Retain coverage only when the original source reaches nobody on hop one."""
    if hop != 1:
        return []
    return transmitters_without_new_receivers(transmitter_ids, wave)


def transmitter_ids_by_hop(result: SimulationResult) -> dict[int, list[str]]:
    """Return unique transmitters grouped in the order their hop ripple is shown."""
    grouped: dict[int, list[str]] = {}
    for event in result.events:
        if event.kind != "TX":
            continue
        hop = event.hop + 1
        grouped.setdefault(hop, [])
        if event.node_id not in grouped[hop]:
            grouped[hop].append(event.node_id)
    return grouped


def result_uses_coverage_ripples(result: SimulationResult) -> bool:
    """Only floods radiate coverage; a learned DM advances along directed hop lines."""
    return result.routing_mode != "DM_LEARNED"


def build_coverage_contours(
    scenario: Scenario,
    result: SimulationResult,
    model: PropagationModel | None = None,
    transmitter_ids: list[str] | None = None,
    max_range_m: float | None = None,
) -> dict[str, list[tuple[float, float, str]]]:
    """Sample a deterministic RF reception boundary around every transmitter.

    ``max_range_m`` caps how far each ray is traced.  The unobstructed link
    budget can span 100+ km, so an uncapped sweep across every transmitter and
    hop is what makes a busy flood freeze; the UI passes a viewport-sized cap.
    """
    nodes = {node.id: node for node in scenario.nodes}
    all_transmitter_ids = list(
        dict.fromkeys(event.node_id for event in result.events if event.kind == "TX" and event.node_id in nodes)
    )
    if transmitter_ids is None:
        transmitter_ids = all_transmitter_ids
    else:
        requested = set(transmitter_ids)
        transmitter_ids = [node_id for node_id in all_transmitter_ids if node_id in requested]
    transmitter_count = len(all_transmitter_ids)
    if transmitter_count <= 4:
        angular_samples = 96
    elif transmitter_count <= 12:
        angular_samples = 72
    elif transmitter_count <= 32:
        angular_samples = 48
    elif transmitter_count <= 96:
        angular_samples = 32
    elif transmitter_count <= 256:
        angular_samples = 24
    else:
        angular_samples = 16
    model = model or PropagationModel(scenario)

    # Use the exact same coverage computation as the beacon so the packet's
    # first-hop coverage and the beacon heatmap always agree in shape.  Each
    # beacon ray becomes one boundary point; "weakened" fades read as a soft
    # threshold edge, "blocked" as a hard stop.
    contours: dict[str, list[tuple[float, float, str]]] = {}
    for source_id in transmitter_ids:
        source = nodes[source_id]
        profile = model.beacon_profile(
            source, angular_samples=angular_samples, max_range_m=max_range_m, align_to_nodes=False
        )
        points: list[tuple[float, float, str]] = []
        for ray in profile.rays:
            boundary_kind = "blocked" if ray.kind == "blocked" else "threshold"
            points.append(
                (
                    source.x + math.cos(ray.angle) * ray.reach_m,
                    source.y + math.sin(ray.angle) * ray.reach_m,
                    boundary_kind,
                )
            )
        contours[source_id] = points
    return contours


class ScrollFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0, borderwidth=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event: tk.Event) -> None:
        if self.winfo_containing(event.x_root, event.y_root) in (self.canvas, self.body) or self._is_child(
            self.winfo_containing(event.x_root, event.y_root)
        ):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _is_child(self, widget: tk.Misc | None) -> bool:
        while widget:
            if widget == self.body:
                return True
            widget = widget.master
        return False


class MeshSimulatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MeshLab RF — Meshtastic Propagation Studio")
        self.root.geometry("1540x940")
        self.root.minsize(1100, 720)
        self.root.configure(bg=BG)
        self._set_icon()
        self._configure_style()

        self.scenario = Scenario(name="Untitled scenario")
        self.file_path: str | None = None
        self.dirty = False
        self.selected_id: str | None = None
        self.tool = "select"
        self.zoom = 1.0
        self.view_x = 0.0
        self.view_y = 0.0
        self.drag_start_screen: tuple[float, float] | None = None
        self.drag_start_world: tuple[float, float] | None = None
        self.drag_object_origin: tuple[float, ...] | None = None
        self.drag_object_points: list[list[float]] | None = None
        self.temp_obstacle: tuple[float, float, float, float] | None = None
        self.temp_forest_points: list[list[float]] = []
        self.pan_start: tuple[float, float] | None = None
        self.pan_origin: tuple[float, float] | None = None
        self.pan_last_screen: tuple[float, float] | None = None
        self.last_result: SimulationResult | None = None
        self.path_focus_id: str | None = None
        self.animation_waves: list[list[SimEvent]] = []
        self.animation_wave_hops: list[int] = []
        self.current_wave_hop = 0
        self.animation_index = 0
        self.animation_after: str | None = None
        self.animation_seen_edges: list[tuple[str, str, str, int]] = []
        self.retained_coverage_transmitters: list[tuple[int, str]] = []
        self.current_wave: list[SimEvent] = []
        self.animation_transmitters: dict[int, list[str]] = {}
        self.animation_contours: dict[str, list[tuple[float, float, str]]] = {}
        self.animation_revealed_nodes: set[str] = set()
        self.animation_progress = 0.0
        self.animation_frame = 0
        self.beacon_node_id: str | None = None
        self.beacon_profile: BeaconProfile | None = None
        self.beacon_segment_photo: ImageTk.PhotoImage | None = None
        self.beacon_segment_photo_key: tuple[object, ...] | None = None
        self.beacon_segment_source: Image.Image | None = None
        self.beacon_after: str | None = None
        self.beacon_phase = 0.0
        self.beacon_request_id = 0
        self.beacon_cancel = threading.Event()
        self.beacon_compute_queue: queue.Queue[tuple[int, Any, Any]] = queue.Queue()
        self.beacon_compute_after: str | None = None
        self.beacon_blocking_obstacles: list[Obstacle] = []
        self.beacon_weakening_obstacles: list[Obstacle] = []
        # Static (frozen, non-pulsing) coverage shown when a sent packet reaches
        # no other node -- same heatmap/colours as the beacon, but never animates.
        self.static_coverage_profile: BeaconProfile | None = None
        self.static_segment_photo: ImageTk.PhotoImage | None = None
        self.static_segment_photo_key: tuple[object, ...] | None = None
        self.static_coverage_blocking: list[Obstacle] = []
        self.static_coverage_weakening: list[Obstacle] = []
        self.static_coverage_cancel = threading.Event()
        self.static_coverage_request_id = 0
        self.static_coverage_queue: queue.Queue[tuple[int, Any, Any, str]] = queue.Queue()
        self.static_coverage_grow = 1.0   # one-shot beacon-style ripple phase
        self.static_coverage_after: str | None = None
        # When the zero-hop heatmap finishes expanding: freeze (reached nobody) or
        # clear it and continue the normal hop animation (reached another node).
        self.static_coverage_then_animate = False
        self.animation_frame_count = 1
        self.render_after: str | None = None
        self.zoom_render_after: str | None = None
        self.zoom_preview_after: str | None = None
        self.zoom_composite_source: Image.Image | None = None
        self.zoom_composite_source_key: tuple[int, int] | None = None
        self.zoom_preview_composite_active = False
        self.zoom_geographic_photo: ImageTk.PhotoImage | None = None
        self.zoom_beacon_photo: ImageTk.PhotoImage | None = None
        self.zoom_static_photo: ImageTk.PhotoImage | None = None
        self.zoom_composite_photo: ImageTk.PhotoImage | None = None
        self.zoom_preview_active_tags: set[str] = set()
        self._world_screen_transform: tuple[float, float, float, float] | None = None
        self._beacon_ripple_profile_id: int | None = None
        self._beacon_ripple_geometry: list[
            tuple[float, float, float, tuple[float, ...], tuple[bool, ...]]
        ] = []
        self.simulation_thread: threading.Thread | None = None
        self.simulation_updates: queue.Queue[tuple[int, str, Any]] = queue.Queue()
        self.simulation_request_id = 0
        self.simulation_contours_complete = True
        self.live_mesh_thread: threading.Thread | None = None
        self.live_mesh_cancel_event = threading.Event()
        self.live_mesh_updates: queue.Queue[tuple[int, Any, Any]] = queue.Queue()
        self.live_mesh_injections: queue.Queue[PacketConfig] = queue.Queue()
        self.live_mesh_enabled = tk.BooleanVar(value=False)
        self.live_mesh_tests: dict[int, LiveMeshTestResult] = {}
        self.live_mesh_snapshot: dict[str, Any] = {}
        self.live_path_test_id: int | None = None
        self.live_mesh_hidden_test_ids: set[int] = set()
        self.live_test_display_signature: tuple[Any, ...] | None = None
        self.live_results_last_refresh = 0.0
        self.live_mesh_request_id = 0
        self.live_mesh_result: LiveMeshResult | None = None
        self.live_mesh_frame_index = 0
        self.live_mesh_after: str | None = None
        self.live_mesh_recent_frames: list[LiveMeshFrame] = []
        self.live_mesh_history_frames: list[LiveMeshFrame] = []
        self.live_mesh_play_counts = {
            "tx": 0,
            "rx": 0,
            "collisions": 0,
            "dropped": 0,
            "throttled": 0,
        }
        self.live_mesh_preset_var = tk.StringVar(value="Default / slow")
        self.live_mesh_nodeinfo_var = tk.StringVar(value=str(self.scenario.live_mesh.nodeinfo_interval_minutes))
        self.live_mesh_telemetry_var = tk.StringVar(value=str(self.scenario.live_mesh.telemetry_interval_minutes))
        self.live_mesh_router_telemetry_var = tk.StringVar(value=str(self.scenario.live_mesh.router_telemetry_interval_minutes))
        self.live_mesh_sensor_var = tk.StringVar(value=str(self.scenario.live_mesh.sensor_interval_minutes))
        self.live_mesh_message_var = tk.StringVar(value=str(self.scenario.live_mesh.message_interval_minutes))
        self.live_mesh_status_var = tk.StringVar(value="Idle")
        self.mesh_graph_window: tk.Toplevel | None = None
        self.mesh_graph_canvas: tk.Canvas | None = None
        self.mesh_graph_info_var = tk.StringVar(value="")
        self.mesh_graph_delivery_var = tk.StringVar(value="")
        self.mesh_graph_show_nodeinfo = tk.BooleanVar(value=True)
        self.mesh_graph_show_telemetry = tk.BooleanVar(value=True)
        self.mesh_graph_show_sensor = tk.BooleanVar(value=True)
        self.mesh_graph_show_messages = tk.BooleanVar(value=True)
        self.mesh_graph_show_control = tk.BooleanVar(value=True)
        self.mesh_graph_show_collisions = tk.BooleanVar(value=True)
        self.mesh_graph_show_drops = tk.BooleanVar(value=True)
        self.mesh_graph_show_gated = tk.BooleanVar(value=True)
        self.mesh_graph_show_utilization = tk.BooleanVar(value=True)
        self.mesh_graph_refresh_after: str | None = None
        self.mesh_graph_last_refresh = 0.0
        self._bottom_dock_active: str | None = None
        self.horizon_canvas: tk.Canvas | None = None
        self.horizon_panorama: HorizonPanorama | None = None
        self.horizon_source_name = ""
        self.horizon_source_xy: tuple[float, float] | None = None
        self.map_picked_xy: tuple[float, float] | None = None
        self.map_picked_label: str | None = None
        self.horizon_show_buildings = tk.BooleanVar(value=True)
        self.horizon_show_forests = tk.BooleanVar(value=True)
        self.horizon_info_var = tk.StringVar(value="")
        self.horizon_redraw_after: str | None = None
        self._horizon_layout: tuple[float, float, float, float, float, float] | None = None
        self.horizon_view_center = 0.0
        self.horizon_view_span = self.HORIZON_DEFAULT_FOV_DEG
        self._horizon_drag_start: tuple[int, int, float] | None = None
        self._horizon_dragged = False
        self.profile_point_a: Node | tuple[float, float] | None = None
        self.path_profile_canvas: tk.Canvas | None = None
        self.path_profile_data: PathProfile | None = None
        self.path_profile_names = ("", "")
        self.path_profile_endpoints: tuple[tuple[float, float], tuple[float, float]] | None = None
        self.path_profile_info_var = tk.StringVar(value="")
        self.path_profile_redraw_after: str | None = None
        self._path_profile_layout: tuple[float, float, float, float, float, float] | None = None
        self.results_populated = True
        self.show_drops = tk.BooleanVar(value=True)
        self.map_visible = tk.BooleanVar(value=True)
        self.terrain_only_view = tk.BooleanVar(value=False)
        self.unit_system = tk.StringVar(value="Imperial")
        self.hop_line_vars = {hop: tk.BooleanVar(value=True) for hop in range(1, 8)}
        self.results_stale = False
        self.status_var = tk.StringVar(value="Ready")
        self.object_vars: dict[str, tk.Variable] = {}
        self.object_form_dirty = False
        self.env_vars: dict[str, tk.Variable] = {}
        self.packet_vars: dict[str, tk.Variable] = {}
        self.map_service = MapDataService()
        self.map_tile_bytes: dict[tuple[str, int, int, int], bytes] = {}
        self.map_tile_images: dict[tuple[str, int, int, int, int], Image.Image] = {}
        # Decoded once per tile, independent of the requested pixel size, so a
        # continuous zoom only ever pays for a cheap resize instead of
        # re-decoding + re-enhancing every tile on every settle-render -- that
        # repeated decode cost was long enough to flash the bare canvas
        # background between the old raster and the new one.
        self.map_tile_decoded: dict[tuple[str, int, int, int], Image.Image] = {}
        self.map_tile_failures: set[tuple[str, int, int, int]] = set()
        self.obstacle_layer_image: ImageTk.PhotoImage | None = None
        self.obstacle_layer_source: Image.Image | None = None
        self.obstacle_layer_source_key: tuple[object, ...] | None = None
        self.obstacle_layer_vectors: list[Obstacle] = []
        self._visible_obstacle_bounds: list[
            tuple[Obstacle, tuple[float, float, float, float]]
        ] = []
        self._obstacle_bounds_cache: dict[
            int,
            tuple[tuple[object, ...], tuple[float, float, float, float]],
        ] = {}
        self.node_label_layout: dict[str, tuple[float, float, float, float, float, float]] = {}
        self._scene_tree_signatures: dict[str, tuple[object, ...]] = {}
        self._scene_tree_imported_obstacle_limit = SCENE_TREE_OBSTACLE_PAGE_SIZE
        self.terrain_visual_source: Image.Image | None = None
        self.terrain_visual_key: tuple[Any, ...] | None = None
        self.terrain_tile_elevations: dict[tuple[int, int, int], np.ndarray] = {}
        self.geo_results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.map_search_var = tk.StringVar()
        self.map_layer_var = tk.StringVar(value=self.scenario.environment.map_layer)
        self.live_radio = LiveRadioClient()
        self.live_port_var = tk.StringVar()
        self.live_sync_var = tk.BooleanVar(value=True)
        self.live_status_var = tk.StringVar(value="Disconnected")
        self.live_ports: dict[str, SerialPort] = {}
        self.live_nodes: dict[int, LiveNode] = {}
        self.live_connection_ready = False
        self.survey_window: tk.Toplevel | None = None
        self.survey_ports: dict[str, SerialPort] = {}
        self.survey_port_var = tk.StringVar()
        self.survey_captures: dict[str, DeviceCapture] = {}
        self.survey_capture_attempts: set[str] = set()
        self.survey_devices: list[DeviceInfo] = []
        self.survey_measurements: list[dict[str, object]] = []
        self.survey_selected_index: int | None = None
        self.survey_export_path: Path | None = None
        self.survey_export_roles: set[str] = set()
        self.survey_worker: threading.Thread | None = None
        self.survey_updates: queue.Queue[tuple[str, object]] = queue.Queue()
        self.terrain_request_id = 0
        self.pending_terrain_rf_refresh: tuple[int, str | None, bool, bool] | None = None

        self._build_menu()
        self._build_toolbar()
        self._build_layout()
        self._bind_shortcuts()
        self.refresh_all()
        self.root.after(100, self._poll_map_services)
        self.root.after(100, self._poll_live_radio)
        self.root.after(600, self._load_startup_terrain)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _set_icon(self) -> None:
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(BG, to=(0, 0, 32, 32))
        icon.put("#1c9ee8", to=(6, 6, 26, 26))
        icon.put("#0b1d31", to=(9, 9, 23, 23))
        icon.put("#51d6ff", to=(14, 14, 18, 18))
        icon.put("#51d6ff", to=(4, 15, 10, 17))
        icon.put("#51d6ff", to=(22, 15, 28, 17))
        self.root.iconphoto(True, icon)
        self._icon_ref = icon

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=PANEL, foreground=TEXT, font=("Segoe UI", 9))
        style.configure("TFrame", background=PANEL)
        style.configure("Root.TFrame", background=BG)
        style.configure("Toolbar.TFrame", background="#0a1422")
        style.configure("TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 12), foreground="#f5f9ff")
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 9), foreground="#8fdcff")
        style.configure("Metric.TLabel", font=("Segoe UI Semibold", 15), foreground="#f7fbff")
        disabled_text = "#71839a"
        selected_indicator = "#168cd1"
        style.configure("TButton", background=PANEL_2, foreground=TEXT, borderwidth=0, padding=(9, 6))
        style.map(
            "TButton",
            background=[("active", "#1d3553"), ("pressed", "#244465"), ("disabled", "#101a29")],
            foreground=[("disabled", disabled_text)],
        )
        style.configure("Accent.TButton", background="#168cd1", foreground="white", padding=(12, 7))
        style.map(
            "Accent.TButton",
            background=[("active", "#22a9ef"), ("pressed", "#117ab7"), ("disabled", "#17415c")],
            foreground=[("disabled", "#9bb4c8")],
        )
        style.configure("Danger.TButton", background="#4b2130", foreground="#ffb6c0")
        style.map(
            "Danger.TButton",
            background=[("active", "#713044"), ("disabled", "#291923")],
            foreground=[("disabled", "#9b7880")],
        )
        style.configure("Tool.TButton", background="#0d1b2e", padding=(9, 7))
        style.configure("ActiveTool.TButton", background="#164c70", foreground="#dff6ff", padding=(9, 7))
        style.configure("Tool.TMenubutton", background="#0d1b2e", foreground=TEXT, padding=(9, 7))
        style.map(
            "Tool.TMenubutton",
            background=[("active", "#1d3553"), ("pressed", "#244465"), ("disabled", "#101a29")],
            foreground=[("disabled", disabled_text)],
            arrowcolor=[("disabled", disabled_text), ("!disabled", TEXT)],
        )
        style.configure(
            "TEntry",
            fieldbackground=ENTRY,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            padding=5,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", "#101a29")],
            foreground=[("disabled", disabled_text)],
            selectbackground=[("!disabled", "#1b638d")],
            selectforeground=[("!disabled", "#ffffff")],
        )
        style.configure("TCombobox", fieldbackground=ENTRY, background=ENTRY, foreground=TEXT, arrowcolor=TEXT, padding=4)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", ENTRY), ("disabled", "#101a29")],
            background=[("readonly", ENTRY), ("disabled", "#101a29")],
            foreground=[("readonly", TEXT), ("disabled", disabled_text)],
            selectbackground=[("readonly", ENTRY)],
            selectforeground=[("readonly", TEXT)],
            arrowcolor=[("disabled", disabled_text), ("!disabled", TEXT)],
        )
        style.configure(
            "TCheckbutton",
            background=PANEL,
            foreground=TEXT,
            indicatorbackground=ENTRY,
            indicatorforeground="#ffffff",
        )
        style.map(
            "TCheckbutton",
            background=[("active", PANEL_2)],
            foreground=[("disabled", disabled_text), ("!disabled", TEXT)],
            indicatorbackground=[("selected", selected_indicator), ("active", "#245178"), ("!selected", ENTRY)],
            indicatorforeground=[("selected", "#ffffff"), ("disabled", disabled_text)],
        )
        style.configure(
            "TRadiobutton",
            background=PANEL,
            foreground=TEXT,
            indicatorbackground=ENTRY,
            indicatorforeground="#ffffff",
        )
        style.map(
            "TRadiobutton",
            background=[("active", PANEL_2)],
            foreground=[("disabled", disabled_text), ("!disabled", TEXT)],
            indicatorbackground=[("selected", selected_indicator), ("active", "#245178"), ("!selected", ENTRY)],
            indicatorforeground=[("selected", "#ffffff"), ("disabled", disabled_text)],
        )
        style.configure("TNotebook", background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", background="#0c1726", foreground=MUTED, padding=(12, 7), borderwidth=0)
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL_2), ("active", "#152b45"), ("disabled", "#0a1320")],
            foreground=[("selected", TEXT), ("active", "#d9e8f8"), ("disabled", disabled_text)],
        )
        style.configure(
            "Treeview",
            background="#0b1625",
            fieldbackground="#0b1625",
            foreground="#dbe8f7",
            rowheight=24,
            borderwidth=0,
        )
        style.configure("Treeview.Heading", background="#15253a", foreground="#a9c1dc", relief="flat", padding=4)
        style.map(
            "Treeview.Heading",
            background=[("active", "#1d3553"), ("pressed", "#244465")],
            foreground=[("active", "#ffffff")],
        )
        style.map("Treeview", background=[("selected", "#174e72")], foreground=[("selected", "white")])
        style.configure("TPanedwindow", background=BORDER)
        style.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED)
        style.configure("Horizontal.TScrollbar", background=PANEL_2, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED)

    @staticmethod
    def _dark_menu(parent: tk.Misc, *, tearoff: bool = False) -> tk.Menu:
        """Menu whose indicators and every interaction state remain visible."""
        return tk.Menu(
            parent,
            tearoff=tearoff,
            bg=PANEL_2,
            fg=TEXT,
            activebackground="#1d4f73",
            activeforeground="#ffffff",
            selectcolor=ACCENT,
            disabledforeground="#71839a",
            borderwidth=1,
            relief="flat",
        )

    def _create_hop_lines_menu(self, parent: tk.Misc) -> tk.Menu:
        menu = self._dark_menu(parent)
        for hop in range(1, 8):
            menu.add_checkbutton(
                label=f"Hop {hop} lines",
                variable=self.hop_line_vars[hop],
                command=self.render_canvas,
                selectcolor=HOP_COLORS[hop],
            )
            menu.entryconfigure(hop - 1, foreground=HOP_COLORS[hop], activeforeground=HOP_COLORS[hop])
        menu.add_separator()
        menu.add_command(label="Show all hops", command=lambda: self.set_hop_lines(True))
        menu.add_command(label="Hide all hops", command=lambda: self.set_hop_lines(False))
        return menu

    def set_hop_lines(self, visible: bool) -> None:
        for variable in self.hop_line_vars.values():
            variable.set(visible)
        self.render_canvas()

    def _map_visibility_changed(self) -> None:
        state = "shown" if self.map_visible.get() else "hidden"
        self.status_var.set(f"Map tiles {state}")
        self.render_canvas()

    def _terrain_only_changed(self) -> None:
        enabled = self.terrain_only_view.get()
        env = self.scenario.environment
        if enabled and not env.terrain_values:
            self.status_var.set("Terrain-only view enabled · loading road-free elevation data…")
            if env.map_configured:
                self.load_topography()
        else:
            self.status_var.set(
                "Terrain-only view enabled · streets, highways, and labels hidden"
                if enabled
                else "Standard basemap restored"
            )
        self.render_canvas()

    def _units_changed(self, _event: tk.Event | None = None) -> None:
        self.refresh_all()
        if self.last_result is not None:
            self.populate_results()
        self.status_var.set(f"{self.unit_system.get()} units active")

    def _build_menu(self) -> None:
        menubar = self._dark_menu(self.root)
        file_menu = self._dark_menu(menubar)
        file_menu.add_command(label="New scenario", accelerator="Ctrl+N", command=self.new_scenario)
        file_menu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_scenario)
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_scenario)
        file_menu.add_command(label="Save as…", accelerator="Ctrl+Shift+S", command=self.save_scenario_as)
        file_menu.add_command(label="Export results CSV…", command=self.export_results)
        file_menu.add_command(label="Survey node export & viewer…", command=self.show_survey_viewer)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = self._dark_menu(menubar)
        edit_menu.add_command(label="Add node", accelerator="N", command=lambda: self.set_tool("node"))
        edit_menu.add_command(label="Drop beacon", accelerator="B", command=lambda: self.set_tool("beacon"))
        edit_menu.add_command(label="Add random nodesâ€¦", command=self.add_random_nodes)
        edit_menu.add_separator()
        edit_menu.add_command(label="Duplicate selected", accelerator="Ctrl+D", command=self.duplicate_selected)
        edit_menu.add_command(label="Delete selected", accelerator="Del", command=self.delete_selected)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = self._dark_menu(menubar)
        view_menu.add_command(label="Fit environment", accelerator="F", command=self.fit_view)
        view_menu.add_checkbutton(
            label="Show map tiles", variable=self.map_visible, command=self._map_visibility_changed
        )
        view_menu.add_checkbutton(
            label="Terrain only (hide roads and labels)",
            variable=self.terrain_only_view,
            command=self._terrain_only_changed,
        )
        view_menu.add_command(label="Refresh terrain data", command=self.load_topography)
        units_menu = self._dark_menu(view_menu)
        for units in ("Imperial", "Metric"):
            units_menu.add_radiobutton(
                label=units, value=units, variable=self.unit_system, command=self._units_changed
            )
        view_menu.add_cascade(label="Units", menu=units_menu)
        view_menu.add_checkbutton(label="Show failed receptions", variable=self.show_drops, command=self.render_canvas)
        self.view_hop_menu = self._create_hop_lines_menu(view_menu)
        view_menu.add_cascade(label="Hop line visibility", menu=self.view_hop_menu)
        view_menu.add_command(label="Clear packet traces", command=self.clear_results)
        menubar.add_cascade(label="View", menu=view_menu)

        sim_menu = self._dark_menu(menubar)
        sim_menu.add_command(label="Run packet", accelerator="Ctrl+Enter", command=self.run_simulation)
        sim_menu.add_command(label="Run live mesh traffic", command=self.start_live_mesh)
        sim_menu.add_command(label="Stop live mesh traffic", command=self.stop_live_mesh)
        sim_menu.add_separator()
        sim_menu.add_command(label="Pulse beacon from selected node", command=self.start_beacon)
        sim_menu.add_command(label="Stop beacon", command=self.stop_beacon)
        sim_menu.add_separator()
        sim_menu.add_command(label="Replay animation", command=self.replay_animation)
        sim_menu.add_command(label="Stop animation", command=self.stop_animation)
        menubar.add_cascade(label="Simulation", menu=sim_menu)

        help_menu = self._dark_menu(menubar)
        help_menu.add_command(label="Model assumptions", command=self.show_model_info)
        help_menu.add_command(label="About MeshLab RF", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, style="Toolbar.TFrame")
        bar.pack(fill="x")
        brand = ttk.Frame(bar, style="Toolbar.TFrame")
        brand.pack(side="left", padx=(12, 18), pady=7)
        tk.Label(
            brand,
            text="M",
            font=("Segoe UI Black", 13),
            bg="#168cd1",
            fg="white",
            width=2,
            height=1,
        ).pack(side="left", padx=(0, 7))
        ttk.Label(brand, text="MeshLab RF", style="Title.TLabel").pack(side="left")

        self.tool_buttons: dict[str, ttk.Button] = {}
        tools = [
            ("select", "↖  Select"),
            ("node", "●  Node"),
            ("beacon", "📡  Beacon"),
            ("horizon", "⛰  Horizon"),
            ("profile", "↔  Profile"),
        ]
        for key, label in tools:
            button = ttk.Button(bar, text=label, style="Tool.TButton", command=lambda k=key: self.set_tool(k))
            button.pack(side="left", padx=2, pady=6)
            self.tool_buttons[key] = button
        ttk.Button(bar, text="#  Random nodes", style="Tool.TButton", command=self.add_random_nodes).pack(
            side="left", padx=(6, 2), pady=6
        )
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=8)
        ttk.Button(bar, text="⌫  Delete", style="Tool.TButton", command=self.delete_selected).pack(side="left", padx=2)
        ttk.Button(bar, text="⊙  Fit", style="Tool.TButton", command=self.fit_view).pack(side="left", padx=2)
        ttk.Button(bar, text="◌  Mesh graph", style="Tool.TButton", command=self.show_mesh_graph).pack(
            side="left", padx=2
        )
        ttk.Button(bar, text="Survey logs", style="Tool.TButton", command=self.show_survey_viewer).pack(
            side="left", padx=2
        )
        self.send_button = ttk.Button(bar, text="▶  Send packet", style="Accent.TButton", command=self.run_simulation)
        self.send_button.pack(side="right", padx=12, pady=6)
        self.clear_hops_button = ttk.Button(
            bar, text="Clear hops", style="Tool.TButton", command=self.clear_results, state="disabled"
        )
        self.clear_hops_button.pack(side="right", padx=2)
        ttk.Button(
            bar,
            text="COM Radio",
            style="Tool.TButton",
            command=lambda: self.show_sidebar_tab("Live Radio"),
        ).pack(side="right", padx=2)
        hop_button = ttk.Menubutton(bar, text="Hop lines ▾", style="Tool.TMenubutton")
        self.toolbar_hop_menu = self._create_hop_lines_menu(hop_button)
        hop_button.configure(menu=self.toolbar_hop_menu)
        hop_button.pack(side="right", padx=2)
        self.live_mesh_toggle = ttk.Checkbutton(
            bar,
            text="Live mesh traffic",
            variable=self.live_mesh_enabled,
            command=self._live_mesh_toggle_changed,
        )
        self.live_mesh_toggle.pack(side="right", padx=8)
        self.set_tool("select")

    def _build_layout(self) -> None:
        self.workspace = ttk.Frame(self.root, style="Root.TFrame")
        self.workspace.pack(fill="both", expand=True)
        self.canvas_panel = ttk.Frame(self.workspace)
        self.canvas_panel.pack(side="left", fill="both", expand=True)
        self.sidebar = ttk.Frame(self.workspace, width=390)
        self.sidebar.pack_propagate(False)
        self.sidebar.pack(side="right", fill="y")
        self.sidebar_tabs = ttk.Notebook(self.sidebar)
        self.sidebar_tabs.pack(fill="both", expand=True)

        self.scene_panel = ttk.Frame(self.sidebar_tabs)
        self.property_panel = ttk.Frame(self.sidebar_tabs)
        self.property_panel.columnconfigure(0, weight=1)
        self.property_panel.rowconfigure(0, weight=1)
        self.object_scroll = ScrollFrame(self.property_panel)
        self.object_scroll.grid(row=0, column=0, sticky="nsew")
        self.object_apply_bar = ttk.Frame(self.property_panel, style="Toolbar.TFrame")
        ttk.Separator(self.object_apply_bar, orient="horizontal").pack(fill="x")
        apply_row = ttk.Frame(self.object_apply_bar, style="Toolbar.TFrame")
        apply_row.pack(fill="x", padx=10, pady=9)
        ttk.Label(apply_row, text="Changes ready", style="Muted.TLabel").pack(side="left")
        self.object_apply_button = ttk.Button(
            apply_row,
            text="Apply changes",
            style="Accent.TButton",
            command=self.apply_object,
        )
        self.object_apply_button.pack(side="right")
        self.object_apply_bar.grid(row=1, column=0, sticky="ew")
        self.object_apply_bar.grid_remove()
        self.environment_scroll = ScrollFrame(self.sidebar_tabs)
        self.packet_scroll = ScrollFrame(self.sidebar_tabs)
        self.live_panel = ttk.Frame(self.sidebar_tabs)
        self.results_panel = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(self.scene_panel, text="Scene")
        self.sidebar_tabs.add(self.property_panel, text="Properties")
        self.sidebar_tabs.add(self.environment_scroll, text="World")
        self.sidebar_tabs.add(self.packet_scroll, text="Packet")
        self.sidebar_tabs.add(self.live_panel, text="Live Radio")
        self.sidebar_tabs.add(self.results_panel, text="Results")
        self._build_scene_panel()
        self._build_live_panel()
        self._build_bottom_dock()
        self._build_canvas()
        self._build_results(self.results_panel)

        status = tk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            bg="#081321",
            fg=MUTED,
            padx=10,
            pady=4,
            font=("Segoe UI", 8),
        )
        status.pack(fill="x")

    def _build_bottom_dock(self) -> None:
        """The shared bottom-docked panel Horizon and Profile both render
        into -- one at a time, replacing each other -- instead of each
        popping its own floating Toplevel window. Built once, hidden until
        a tool has something to show. No separate title bar: each tool's own
        top row carries a close button instead, so the dock spends no extra
        height on a row that just repeats what's already on the toolbar."""
        self.bottom_dock = ttk.Frame(self.canvas_panel, style="Toolbar.TFrame", height=340)
        self.bottom_dock.pack_propagate(False)
        self.bottom_dock_body = ttk.Frame(self.bottom_dock, style="Root.TFrame")
        self.bottom_dock_body.pack(fill="both", expand=True)

    def _show_bottom_dock(self, tool: str) -> ttk.Frame:
        """Claim the dock for `tool`, clearing out whatever the other tool
        left behind first. Returns the empty body frame to build into."""
        previous = self._bottom_dock_active
        if previous == "horizon" and tool != "horizon":
            self._reset_horizon_state()
        elif previous == "profile" and tool != "profile":
            self._reset_path_profile_state()
        for child in self.bottom_dock_body.winfo_children():
            child.destroy()
        self._bottom_dock_active = tool
        if not self.bottom_dock.winfo_ismapped():
            self.bottom_dock.pack(side="bottom", fill="x")
        return self.bottom_dock_body

    def _hide_bottom_dock(self) -> None:
        if self.bottom_dock.winfo_ismapped():
            self.bottom_dock.pack_forget()
        self._bottom_dock_active = None

    def _close_bottom_dock(self) -> None:
        active = self._bottom_dock_active
        if active == "horizon":
            self._close_horizon_window()
        elif active == "profile":
            self._close_path_profile_window()
        else:
            self._hide_bottom_dock()

    def show_sidebar_tab(self, name: str) -> None:
        tabs = {"Scene": 0, "Properties": 1, "World": 2, "Packet": 3, "Live Radio": 4, "Results": 5}
        self.sidebar_tabs.select(tabs[name])
        self.root.after_idle(self.render_canvas)

    def show_mesh_graph(self) -> None:
        """Open a separate time-series view of the shared mesh channel."""
        if self.mesh_graph_window is not None and self.mesh_graph_window.winfo_exists():
            self.mesh_graph_window.deiconify()
            self.mesh_graph_window.lift()
            self._refresh_mesh_graph()
            return

        window = tk.Toplevel(self.root)
        self.mesh_graph_window = window
        window.title("MeshLab RF — Mesh traffic graph")
        window.geometry("1180x780")
        window.minsize(720, 520)
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", self._close_mesh_graph)

        controls = ttk.Frame(window, style="Toolbar.TFrame")
        controls.pack(fill="x")
        ttk.Button(controls, text="Refresh", style="Tool.TButton", command=self._refresh_mesh_graph).pack(
            side="left", padx=(8, 6), pady=6
        )
        packet_toggles = [
            ("NodeInfo", self.mesh_graph_show_nodeinfo),
            ("Telemetry", self.mesh_graph_show_telemetry),
            ("Sensor", self.mesh_graph_show_sensor),
            ("Messages", self.mesh_graph_show_messages),
            ("Tests / replies", self.mesh_graph_show_control),
        ]
        for label, variable in packet_toggles:
            ttk.Checkbutton(controls, text=label, variable=variable, command=self._refresh_mesh_graph).pack(
                side="left", padx=5
            )

        impact_controls = ttk.Frame(window, style="Toolbar.TFrame")
        impact_controls.pack(fill="x")
        ttk.Label(impact_controls, text="Delivery impact", style="Muted.TLabel").pack(
            side="left", padx=(10, 8), pady=4
        )
        impact_toggles = [
            ("Collisions", self.mesh_graph_show_collisions),
            ("RF drops", self.mesh_graph_show_drops),
            ("Channel-gated", self.mesh_graph_show_gated),
            ("Channel utilization", self.mesh_graph_show_utilization),
        ]
        for label, variable in impact_toggles:
            ttk.Checkbutton(impact_controls, text=label, variable=variable, command=self._refresh_mesh_graph).pack(
                side="left", padx=5
            )

        ttk.Label(
            window,
            textvariable=self.mesh_graph_info_var,
            style="Muted.TLabel",
            anchor="w",
            wraplength=1100,
            justify="left",
        ).pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(
            window,
            textvariable=self.mesh_graph_delivery_var,
            style="Muted.TLabel",
            anchor="w",
            wraplength=1100,
            justify="left",
        ).pack(fill="x", padx=12, pady=(0, 4))
        ttk.Label(
            window,
            text="Packet-type lines are RF transmissions per 2-second live frame. Collision and channel-gated lines identify traffic pressure; RF drops identify the separate link, terrain, or obstacle limitation. The graph retains up to one hour of real-time history.",
            style="Muted.TLabel",
            anchor="w",
            wraplength=1100,
        ).pack(fill="x", padx=12, pady=(0, 7))

        self.mesh_graph_canvas = tk.Canvas(window, bg=BG, highlightthickness=0, borderwidth=0)
        self.mesh_graph_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.mesh_graph_canvas.bind("<Configure>", self._schedule_mesh_graph_refresh)
        self._refresh_mesh_graph()

    def _close_mesh_graph(self) -> None:
        if self.mesh_graph_refresh_after is not None:
            self.root.after_cancel(self.mesh_graph_refresh_after)
            self.mesh_graph_refresh_after = None
        if self.mesh_graph_window is not None and self.mesh_graph_window.winfo_exists():
            self.mesh_graph_window.destroy()
        self.mesh_graph_window = None
        self.mesh_graph_canvas = None

    def _schedule_mesh_graph_refresh(self, _event: tk.Event | None = None) -> None:
        if self.mesh_graph_refresh_after is not None:
            self.root.after_cancel(self.mesh_graph_refresh_after)
        # Recreating thousands of Canvas coordinates for a full history at
        # every live heartbeat eventually starves the map.  Half-second graph
        # updates are visually live while keeping the RF worker independent.
        wait_ms = max(0, int(500 - (time.monotonic() - self.mesh_graph_last_refresh) * 1000))
        self.mesh_graph_refresh_after = self.root.after(wait_ms, self._refresh_mesh_graph)

    def _refresh_mesh_graph(self) -> None:
        self.mesh_graph_refresh_after = None
        canvas = self.mesh_graph_canvas
        if (
            canvas is None
            or self.mesh_graph_window is None
            or not self.mesh_graph_window.winfo_exists()
        ):
            return
        self.mesh_graph_last_refresh = time.monotonic()
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        self._render_mesh_traffic_chart(canvas, width, height)
        return
        nodes = list(self.scenario.nodes)
        if not nodes:
            canvas.create_text(width / 2, height / 2, text="Add nodes to view the mesh graph", fill=MUTED, font=("Segoe UI", 13))
            self.mesh_graph_info_var.set("No nodes in this scenario.")
            return

        positions = self._graph_node_positions(nodes, width, height)
        viable, weak = self._mesh_graph_links()
        if self.mesh_graph_show_weak_links.get():
            for source, target, _link in weak:
                x1, y1 = positions[source.id]
                x2, y2 = positions[target.id]
                canvas.create_line(x1, y1, x2, y2, fill="#8b3b4b", width=1, dash=(3, 4), tags="graph-weak")
        if self.mesh_graph_show_links.get():
            for source, target, link in viable:
                x1, y1 = positions[source.id]
                x2, y2 = positions[target.id]
                shade = "#177d5d" if link.margin_db >= 8.0 else "#b58a34"
                canvas.create_line(x1, y1, x2, y2, fill=shade, width=1, tags="graph-link")

        if self.mesh_graph_show_routes.get():
            for route in self.scenario.learned_routes.values():
                for source_id, target_id in zip(route, route[1:]):
                    if source_id in positions and target_id in positions:
                        x1, y1 = positions[source_id]
                        x2, y2 = positions[target_id]
                        canvas.create_line(x1, y1, x2, y2, fill="#c084fc", width=3, dash=(7, 3), tags="graph-route")

        if self.mesh_graph_show_packet.get() and self.last_result is not None:
            for node_id, arrival in self.last_result.reached.items():
                via_id = str(arrival.get("via", ""))
                if not via_id or via_id not in positions or node_id not in positions:
                    continue
                x1, y1 = positions[via_id]
                x2, y2 = positions[node_id]
                canvas.create_line(
                    x1, y1, x2, y2,
                    fill=HOP_COLORS.get(int(arrival.get("hop", 0)), ACCENT), width=4,
                    tags="graph-packet",
                )

        live_load = self.live_mesh_snapshot.get("node_utilization", {}) if self.mesh_graph_show_live_load.get() else {}
        reached = self.last_result.reached if self.last_result is not None else {}
        for node in nodes:
            x, y = positions[node.id]
            fill = ROLE_COLORS.get(node.role, ACCENT) if node.online else "#667085"
            outline = TEXT if node.id == self.selected_id else "#06101b"
            if node.online and self.last_result is not None and node.id not in reached:
                fill = "#64748b"
            utilization = float(live_load.get(node.id, 0.0))
            if utilization > 0.0:
                ring = RED if utilization >= 40.0 else AMBER if utilization >= 25.0 else GREEN
                canvas.create_oval(x - 14, y - 14, x + 14, y + 14, outline=ring, width=3, tags=("graph-load", f"graph-node:{node.id}"))
            canvas.create_oval(
                x - 8, y - 8, x + 8, y + 8,
                fill=fill, outline=outline, width=3 if node.id == self.selected_id else 1,
                tags=("graph-node", f"graph-node:{node.id}"),
            )
            if self.mesh_graph_show_labels.get():
                label = node.name or node.id
                if self.mesh_graph_show_roles.get():
                    label = f"{label}\n{node.role}"
                if utilization > 0.0 and self.mesh_graph_show_live_load.get():
                    label = f"{label}\n{utilization:.1f}% ch util"
                canvas.create_text(
                    x, y + 13, text=label, fill=TEXT if node.online else MUTED,
                    font=("Segoe UI", 9, "bold"), anchor="n", justify="center",
                    tags=("graph-label", f"graph-node:{node.id}"),
                )

        online_count = sum(node.online for node in nodes)
        self.mesh_graph_info_var.set(
            f"{len(nodes):,} nodes · {online_count:,} online · {len(viable):,} decodable RF links"
            + (f" · {len(weak):,} weak/blocked nearby links" if self.mesh_graph_show_weak_links.get() else "")
            + (f" · {len(self.scenario.learned_routes):,} learned DM routes" if self.scenario.learned_routes else "")
        )

    def show_horizon_panorama(self, point: Node | tuple[float, float]) -> None:
        """Compute and open the 360° terrain/obstacle skyline from one clicked
        point -- an existing node (its own real antenna height, not the
        ground) if that's what was clicked, otherwise a throwaway
        auto-grounded point. A geometric horizon sweep (no radio math) stays
        fast even over a large, obstacle-heavy scene, so this runs
        synchronously."""
        node, label = self._resolve_map_point(point)
        self.horizon_source_name = label
        self.horizon_source_xy = (node.x, node.y)
        model = PropagationModel(self.scenario)
        self.horizon_panorama = model.horizon_panorama(node, max_range_m=self._coverage_range_cap())
        self.horizon_view_center = 0.0
        self.horizon_view_span = self.HORIZON_DEFAULT_FOV_DEG
        self.status_var.set(f"Horizon panorama ready for {label}")
        self._open_horizon_window()
        self.schedule_render()

    def _open_horizon_window(self) -> None:
        if self._bottom_dock_active == "horizon" and self.horizon_canvas is not None:
            self._refresh_horizon_panorama()
            return

        window = self._show_bottom_dock("horizon")

        controls = ttk.Frame(window, style="Toolbar.TFrame")
        controls.pack(fill="x")
        ttk.Button(
            controls, text="◀", style="Tool.TButton", width=3,
            command=lambda: self._pan_horizon(-1),
        ).pack(side="left", padx=(8, 2), pady=6)
        ttk.Button(
            controls, text="▶", style="Tool.TButton", width=3,
            command=lambda: self._pan_horizon(1),
        ).pack(side="left", padx=2, pady=6)
        ttk.Button(
            controls, text="Zoom in", style="Tool.TButton", command=lambda: self._zoom_horizon(0.5)
        ).pack(side="left", padx=(10, 2), pady=6)
        ttk.Button(
            controls, text="Zoom out", style="Tool.TButton", command=lambda: self._zoom_horizon(2.0)
        ).pack(side="left", padx=2, pady=6)
        ttk.Button(
            controls, text="Full 360°", style="Tool.TButton", command=self._reset_horizon_view
        ).pack(side="left", padx=2, pady=6)
        ttk.Checkbutton(
            controls, text="Buildings", variable=self.horizon_show_buildings,
            command=self._refresh_horizon_panorama,
        ).pack(side="left", padx=(14, 2), pady=6)
        ttk.Checkbutton(
            controls, text="Forests", variable=self.horizon_show_forests,
            command=self._refresh_horizon_panorama,
        ).pack(side="left", padx=2, pady=6)
        ttk.Button(
            controls, text="✕", style="Tool.TButton", width=3, command=self._close_bottom_dock,
        ).pack(side="right", padx=(2, 8), pady=6)
        ttk.Label(
            controls,
            text="Drag to spin, scroll to zoom, click a feature to locate it on the map.",
            style="Muted.TLabel",
        ).pack(side="left", padx=12)

        ttk.Label(
            window,
            textvariable=self.horizon_info_var,
            style="Muted.TLabel",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(4, 4))

        self.horizon_canvas = tk.Canvas(window, bg=MAPLESS_BACKGROUND, highlightthickness=0, borderwidth=0)
        self.horizon_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.horizon_canvas.bind("<Configure>", self._schedule_horizon_redraw)
        self.horizon_canvas.bind("<Motion>", self._horizon_canvas_motion)
        self.horizon_canvas.bind("<ButtonPress-1>", self._horizon_drag_start_event)
        self.horizon_canvas.bind("<B1-Motion>", self._horizon_drag_motion)
        self.horizon_canvas.bind("<ButtonRelease-1>", self._horizon_click_release)
        self.horizon_canvas.bind("<MouseWheel>", self._horizon_mousewheel)
        self.horizon_canvas.configure(cursor="fleur")

        ttk.Label(
            window,
            text=(
                "Terrain shaded by distance, obstacles coloured by kind. Red markers show bearings "
                "where something blocks a level shot nearby."
            ),
            style="Muted.TLabel",
            anchor="w",
            wraplength=620,
        ).pack(fill="x", padx=12, pady=(0, 8))
        self._refresh_horizon_panorama()

    def _reset_horizon_state(self) -> None:
        if self.horizon_redraw_after is not None:
            try:
                self.root.after_cancel(self.horizon_redraw_after)
            except tk.TclError:
                pass
            self.horizon_redraw_after = None
        self.horizon_canvas = None
        self.horizon_source_xy = None
        self.map_picked_xy = None
        self.map_picked_label = None
        self.schedule_render()

    def _close_horizon_window(self) -> None:
        self._reset_horizon_state()
        if self._bottom_dock_active == "horizon":
            self._hide_bottom_dock()

    def _schedule_horizon_redraw(self, _event: tk.Event | None = None) -> None:
        if self.horizon_redraw_after is not None:
            try:
                self.root.after_cancel(self.horizon_redraw_after)
            except tk.TclError:
                pass
        self.horizon_redraw_after = self.root.after(80, self._refresh_horizon_panorama)

    def _pan_horizon(self, direction: int) -> None:
        self.horizon_view_center = (self.horizon_view_center + direction * self.horizon_view_span * 0.6) % 360.0
        self._refresh_horizon_panorama()

    def _zoom_horizon(self, factor: float) -> None:
        self.horizon_view_span = max(20.0, min(360.0, self.horizon_view_span * factor))
        self._refresh_horizon_panorama()

    def _reset_horizon_view(self) -> None:
        self.horizon_view_center = 0.0
        self.horizon_view_span = self.HORIZON_DEFAULT_FOV_DEG
        self._refresh_horizon_panorama()

    def _horizon_drag_start_event(self, event: tk.Event) -> None:
        self._horizon_drag_start = (event.x, event.y, self.horizon_view_center)
        self._horizon_dragged = False

    def _horizon_drag_motion(self, event: tk.Event) -> None:
        if self._horizon_drag_start is None:
            return
        layout = self._horizon_layout
        if layout is None:
            return
        plot_left, plot_right, *_rest = layout
        plot_width = max(1.0, plot_right - plot_left)
        start_x, start_y, start_center = self._horizon_drag_start
        if abs(event.x - start_x) > 3 or abs(event.y - start_y) > 3:
            self._horizon_dragged = True
        # Dragging right reveals bearings that were to the left of centre.
        delta_bearing = (event.x - start_x) / plot_width * self.horizon_view_span
        self.horizon_view_center = (start_center - delta_bearing) % 360.0
        self._refresh_horizon_panorama()

    def _horizon_mousewheel(self, event: tk.Event) -> None:
        self._zoom_horizon(0.85 if event.delta > 0 else 1 / 0.85)

    def _horizon_click_release(self, event: tk.Event) -> None:
        dragged = self._horizon_dragged
        self._horizon_drag_start = None
        self._horizon_dragged = False
        if dragged:
            return
        self._pick_horizon_point(event.x, event.y)

    def _refresh_horizon_panorama(self) -> None:
        self.horizon_redraw_after = None
        canvas = self.horizon_canvas
        panorama = self.horizon_panorama
        if canvas is None or panorama is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        self._draw_horizon_panorama(canvas, width, height, panorama)
        self.horizon_info_var.set(f"From {self.horizon_source_name}")
        # Keep the map's direction cone in sync with pans/zooms of this view.
        self.schedule_render()

    @staticmethod
    def _draw_horizon_curve(
        canvas: tk.Canvas,
        visible: list[tuple[float, "HorizonPoint"]],
        point_xy: Callable[[float, float], tuple[float, float]],
        color_fn: Callable[["HorizonPoint"], str],
        width_fn: Callable[["HorizonPoint"], float],
        dash: tuple[int, int] | None = None,
    ) -> None:
        """Draw one polyline per contiguous same-colour/width run.

        Real terrain is jagged, not flowing -- spline-smoothing the sampled
        points erased the actual relief detail (small ridges, individual
        rooflines) instead of preserving it, which read as artificially flat.
        One continuous unsmoothed polyline per run still avoids the seams
        many separate two-point segments left at run boundaries.
        """
        if len(visible) < 2:
            return
        runs: list[tuple[str, float, list[float]]] = []
        for bearing, point in visible:
            color, width = color_fn(point), width_fn(point)
            coords = list(point_xy(bearing, point.angle_deg))
            if runs and runs[-1][0] == color and runs[-1][1] == width:
                runs[-1][2].extend(coords)
            else:
                if runs:
                    coords = runs[-1][2][-2:] + coords
                runs.append((color, width, coords))
        for color, width, coords in runs:
            if len(coords) >= 4:
                if dash:
                    canvas.create_line(*coords, fill=color, width=width, dash=dash)
                else:
                    canvas.create_line(*coords, fill=color, width=width, capstyle=tk.ROUND, joinstyle=tk.ROUND)

    def _draw_horizon_natural_run(
        self,
        canvas: tk.Canvas,
        top_pts: list[tuple[float, float]],
        ground_pts: list[tuple[float, float]],
        color: str,
    ) -> None:
        """A mountain, body of water, or other non-boxy/non-canopy feature:
        one coherent 3D block following its own real (organically sloped)
        silhouette -- a lit roofline band and one shadowed trailing edge,
        not a flat cutout. Shading is applied once per run, not once per
        sample, so the block reads as one solid shape and the only strong
        edges are real boundaries -- where this run starts and ends -- not
        sampling artifacts partway through it."""
        fill_poly: list[float] = []
        for x, y in top_pts:
            fill_poly.extend((x, y))
        for x, y in reversed(ground_pts):
            fill_poly.extend((x, y))
        canvas.create_polygon(*fill_poly, fill=color, outline="")

        cap = 4.0
        cap_poly: list[float] = []
        for x, y in top_pts:
            cap_poly.extend((x, y))
        for x, y in reversed(top_pts):
            cap_poly.extend((x, y + cap))
        canvas.create_polygon(*cap_poly, fill=self._lighten(color), outline="")

        last_x, last_top_y = top_pts[-1]
        last_ground_y = ground_pts[-1][1]
        prev_x = top_pts[-2][0] if len(top_pts) >= 2 else last_x - 6.0
        side = min(4.0, max(0.0, last_x - prev_x) * 0.5)
        if side > 0:
            inset_x = last_x - side
            canvas.create_polygon(
                inset_x, last_top_y, last_x, last_top_y, last_x, last_ground_y, inset_x, last_ground_y,
                fill=self._darken(color), outline="",
            )

        canvas.create_polygon(*fill_poly, fill="", outline=self._darken(color, 45), width=1)

    @staticmethod
    def _interp_y(points: list[tuple[float, float]], x: float) -> float:
        """Linear-interpolate the y of a sorted-by-x point list at x, so a
        block's edges can land at chunk boundaries that fall between
        actual sampled points instead of only at sample positions."""
        if not points:
            return 0.0
        if x <= points[0][0]:
            return points[0][1]
        if x >= points[-1][0]:
            return points[-1][1]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= x <= x1:
                if x1 == x0:
                    return y0
                t = (x - x0) / (x1 - x0)
                return y0 + (y1 - y0) * t
        return points[-1][1]

    def _draw_horizon_flat_block(
        self,
        canvas: tk.Canvas,
        x1: float, x2: float,
        roof_y: float,
        ground_y1: float, ground_y2: float,
        color: str,
    ) -> None:
        """One real rectangular block, seen from the side, the way one face
        of a cuboid actually looks: a flat roofline, a lit cap catching the
        light, and a single shadowed edge -- no internal grid or division
        that could read as more than one structure."""
        if x2 - x1 < 0.5:
            return
        canvas.create_polygon(x1, roof_y, x2, roof_y, x2, ground_y2, x1, ground_y1, fill=color, outline="")
        avg_height = max(1.0, ((ground_y1 - roof_y) + (ground_y2 - roof_y)) / 2.0)
        cap = min(4.0, avg_height * 0.25)
        if cap > 0:
            canvas.create_rectangle(x1, roof_y, x2, roof_y + cap, fill=self._lighten(color), outline="")
        side = min(8.0, (x2 - x1) * 0.25)
        if side > 0:
            canvas.create_polygon(
                x2 - side, roof_y, x2, roof_y, x2, ground_y2, x2 - side, ground_y2,
                fill=self._darken(color), outline="",
            )
        canvas.create_polygon(
            x1, roof_y, x2, roof_y, x2, ground_y2, x1, ground_y1,
            fill="", outline=self._darken(color, 55), width=1,
        )

    def _draw_horizon_block_run(
        self,
        canvas: tk.Canvas,
        top_pts: list[tuple[float, float]],
        ground_pts: list[tuple[float, float]],
        color: str,
    ) -> None:
        """Buildings/walls: the whole run is ONE real building drawn as ONE
        rectangular block -- a flat roofline at the structure's actual peak
        height, spanning its full real width -- never chopped into internal
        chunks, which read as several separate buildings instead of one.
        A hairline gap is trimmed off each end so that two real, distinct
        buildings standing right next to each other (a different source
        object, even with the same colour) still show a sliver of the
        ground between them instead of fusing into one solid mass."""
        x0, x1 = top_pts[0][0], top_pts[-1][0]
        gap = 1.0
        if x1 - x0 > gap * 2:
            x0 += gap * 0.5
            x1 -= gap * 0.5
        roof_y = min(y for x, y in top_pts if x0 - 0.5 <= x <= x1 + 0.5) if x1 > x0 else min(y for _x, y in top_pts)
        ground_y1 = self._interp_y(ground_pts, x0)
        ground_y2 = self._interp_y(ground_pts, x1)
        self._draw_horizon_flat_block(canvas, x0, x1, roof_y, ground_y1, ground_y2, color)

    def _draw_horizon_forest_run(
        self,
        canvas: tk.Canvas,
        top_pts: list[tuple[float, float]],
        ground_pts: list[tuple[float, float]],
        color: str,
    ) -> None:
        """Forest: one dense row of trees, every one rooted at the real
        ground line, each sized to reach up toward the real recorded
        canopy height at its own spot -- tall trees where the canopy is
        tall, short ones where it's short. Height varies; the base never
        leaves the ground, so nothing floats."""
        x0, x1 = top_pts[0][0], top_pts[-1][0]
        span = x1 - x0
        if span <= 0.5:
            return
        tree_w = 11.0
        columns = min(90, max(1, round(span / tree_w)))
        col_w = span / columns
        for col in range(columns):
            cx = x0 + col_w * (col + 0.5)
            top_y = self._interp_y(top_pts, cx)
            ground_y = self._interp_y(ground_pts, cx)
            available = max(6.0, ground_y - top_y)
            radius = max(3.0, min(10.0, available / 2.3, col_w * 0.75))
            radius *= 0.82 if col % 3 == 1 else 1.0
            self._draw_tree_icon(canvas, cx, ground_y, radius, color)

    @staticmethod
    def _horizon_layer_color(fraction: float) -> str:
        """Near-to-far depth tint: light teal up close, indigo at range --
        purely for plain terrain; obstacle segments keep their own kind colour."""
        near, far = (0x8a, 0xe0, 0xc9), (0x33, 0x33, 0x8a)
        r = round(near[0] + (far[0] - near[0]) * fraction)
        g = round(near[1] + (far[1] - near[1]) * fraction)
        b = round(near[2] + (far[2] - near[2]) * fraction)
        return f"#{r:02x}{g:02x}{b:02x}"

    @staticmethod
    def _visible_horizon_points(
        points: list[HorizonPoint], low: float, high: float
    ) -> list[tuple[float, HorizonPoint]]:
        """(effective_bearing, point) pairs unwrapped and sorted for the
        current [low, high) view window, including a little slack past each
        edge so segments crossing the boundary still draw correctly."""
        if not points:
            return []
        step = 360.0 / len(points)
        slack = step * 1.5
        result: list[tuple[float, HorizonPoint]] = []
        for point in points:
            k_start = math.floor((low - slack - point.bearing_deg) / 360.0)
            k_end = math.ceil((high + slack - point.bearing_deg) / 360.0)
            for k in range(k_start, k_end + 1):
                effective = point.bearing_deg + 360.0 * k
                if low - slack <= effective <= high + slack:
                    result.append((effective, point))
        result.sort(key=lambda item: item[0])
        return result

    def _apply_horizon_kind_filter(
        self,
        visible: list[tuple[float, HorizonPoint]],
        show_buildings: bool,
        show_forests: bool,
    ) -> list[tuple[float, HorizonPoint]]:
        """A hidden Building/Wall or Forest doesn't just disappear -- it
        reverts to the plain terrain silhouette sitting under it (using its
        own ground_angle_deg), the same skyline you'd see if it weren't
        there, rather than leaving a hole or a still-coloured outline."""
        if show_buildings and show_forests:
            return visible
        result: list[tuple[float, HorizonPoint]] = []
        for bearing, point in visible:
            hidden = (point.kind in ("Building", "Wall") and not show_buildings) or (
                point.kind == "Forest" and not show_forests
            )
            if hidden:
                result.append((
                    bearing,
                    HorizonPoint(
                        point.bearing_deg, point.ground_angle_deg, point.distance_m,
                        "terrain", self.HORIZON_SILHOUETTE_COLOR, point.ground_angle_deg, None,
                    ),
                ))
            else:
                result.append((bearing, point))
        return result

    def _draw_horizon_panorama(
        self, canvas: tk.Canvas, width: int, height: int, panorama: HorizonPanorama
    ) -> None:
        margin_left, margin_right = 46, 16
        margin_top, margin_bottom = 40, 34
        plot_left, plot_right = margin_left, width - margin_right
        plot_top, plot_bottom = margin_top, height - margin_bottom
        plot_width = max(1.0, plot_right - plot_left)
        plot_height = max(1.0, plot_bottom - plot_top)

        span = max(20.0, min(360.0, self.horizon_view_span))
        low, high = self.horizon_view_center - span / 2.0, self.horizon_view_center + span / 2.0

        # The vertical range has to fit everything that actually gets drawn --
        # the envelope alone isn't enough, since a nearer depth layer can dip
        # well below it (a valley, low ground between here and the ridge that
        # forms the skyline) and would otherwise be clipped off the bottom of
        # the chart instead of shown.
        angles = [point.angle_deg for point in panorama.points]
        for layer in panorama.layers:
            angles.extend(point.angle_deg for point in layer.points)
        angle_min = min(angles + [0.0])
        angle_max = max(angles + [0.0])
        angle_span = max(4.0, (angle_max - angle_min) * 1.2)
        angle_center = (angle_max + angle_min) / 2.0
        angle_min, angle_max = angle_center - angle_span / 2.0, angle_center + angle_span / 2.0

        def point_xy(effective_bearing: float, angle: float) -> tuple[float, float]:
            px = plot_left + (effective_bearing - low) / (high - low) * plot_width
            py = plot_bottom - (angle - angle_min) / (angle_max - angle_min) * plot_height
            return px, py

        self._horizon_layout = (plot_left, plot_right, plot_top, plot_bottom, angle_min, angle_max)

        show_buildings = self.horizon_show_buildings.get()
        show_forests = self.horizon_show_forests.get()
        visible_envelope = self._apply_horizon_kind_filter(
            self._visible_horizon_points(panorama.points, low, high), show_buildings, show_forests,
        )

        # Depth layers (farthest first, so nearer layers draw on top and
        # correctly occlude them): each is its own real, continuously varying
        # filled silhouette -- no shared flat baseline standing in for the
        # actual ground anywhere. A building inside any layer still rises
        # only from that layer's own real ground at that bearing, never a
        # bar dropping straight to the chart floor.
        layer_count = max(1, len(panorama.layers))
        # Collected here as every run is drawn, then combined with the
        # envelope peaks below -- a building can be clearly visible in its
        # own depth layer without ever winning the overall envelope (a
        # farther mountain at the same bearing can out-angle it), and it
        # still deserves a height label.
        run_peaks: list[tuple[float, HorizonPoint]] = []
        for layer_index, layer in enumerate(reversed(panorama.layers)):
            depth_fraction = 1.0 - layer_index / max(1, layer_count - 1)
            depth_color = self._horizon_layer_color(depth_fraction)
            visible = self._apply_horizon_kind_filter(
                self._visible_horizon_points(layer.points, low, high), show_buildings, show_forests,
            )
            if len(visible) < 2:
                continue
            ground_polygon: list[float] = []
            for bearing, point in visible:
                ground_polygon.extend(point_xy(bearing, point.ground_angle_deg))
            layer_first_x, _ = point_xy(visible[0][0], angle_min)
            layer_last_x, _ = point_xy(visible[-1][0], angle_min)
            canvas.create_polygon(
                layer_first_x, plot_bottom, *ground_polygon, layer_last_x, plot_bottom,
                fill=depth_color, outline=self._darken(depth_color, 25), width=1,
            )
            # Group consecutive same-colour, non-terrain samples into one
            # run per building/mountain/forest before shading -- shading
            # every tiny bearing-sample segment individually turned each
            # building into a strobe of near-identical light/dark slices,
            # which drowned out the real colour-boundary between one
            # building and the next instead of clarifying it.
            visible_count = len(visible)
            index = 0
            while index < visible_count:
                point_i = visible[index][1]
                if point_i.kind == "terrain":
                    index += 1
                    continue
                run_end = index
                while (
                    run_end + 1 < visible_count
                    and visible[run_end + 1][1].kind == point_i.kind
                    and visible[run_end + 1][1].color == point_i.color
                    and visible[run_end + 1][1].source_id == point_i.source_id
                ):
                    run_end += 1
                if run_end + 1 >= visible_count:
                    break
                run_indices = range(index, run_end + 2)
                run_points = [visible[k] for k in run_indices]
                top_pts = [point_xy(b, p.angle_deg) for b, p in run_points]
                ground_pts = [point_xy(b, p.ground_angle_deg) for b, p in run_points]
                if point_i.kind in ("Building", "Wall"):
                    self._draw_horizon_block_run(canvas, top_pts, ground_pts, point_i.color)
                elif point_i.kind == "Forest":
                    self._draw_horizon_forest_run(canvas, top_pts, ground_pts, point_i.color)
                else:
                    self._draw_horizon_natural_run(canvas, top_pts, ground_pts, point_i.color)
                run_peaks.append(max(run_points[:-1], key=lambda item: item[1].angle_deg))
                index = run_end + 1

        # Overall skyline outline on top: the true tallest-per-bearing
        # boundary against the sky, in each point's own colour (terrain
        # stays one uniform tone -- its jagged shape alone reads as terrain;
        # a building/forest/mountain keeps its own colour so it doesn't read
        # as just another bump). The layers above already provide every
        # filled shape, so this is a stroke only, not a second fill.
        self._draw_horizon_curve(
            canvas, visible_envelope, point_xy,
            lambda point: point.color if point.kind != "terrain" else self.HORIZON_SILHOUETTE_COLOR,
            lambda point: 2.5 if point.kind != "terrain" else 2,
        )

        # Height markers: label the most prominent peaks with how far above
        # eye level they rise, so "how tall is that" has a real answer.
        # Every drawn run contributes its own peak (run_peaks, gathered
        # above) alongside the overall envelope's local maxima -- a
        # building can be clearly visible in a nearer depth layer without
        # ever winning the envelope (a farther mountain at the same
        # bearing can out-angle it) and still needs a label of its own.
        peaks: list[tuple[float, HorizonPoint]] = list(run_peaks)
        for index in range(1, len(visible_envelope) - 1):
            _prev_bearing, prev_point = visible_envelope[index - 1]
            bearing, point = visible_envelope[index]
            _next_bearing, next_point = visible_envelope[index + 1]
            if point.angle_deg > 1.0 and point.angle_deg >= prev_point.angle_deg and point.angle_deg >= next_point.angle_deg:
                peaks.append((bearing, point))
        peaks.sort(key=lambda item: item[1].angle_deg, reverse=True)
        labeled_bearings: list[float] = []
        for bearing, point in peaks:
            if len(labeled_bearings) >= 10:
                break
            if any(abs(bearing - existing) < span * 0.06 for existing in labeled_bearings):
                continue
            labeled_bearings.append(bearing)
            height_above_eye = point.distance_m * math.tan(math.radians(point.angle_deg))
            label_x, label_y = point_xy(bearing, point.angle_deg)
            canvas.create_text(
                label_x, label_y - 8,
                text=f"+{self.format_distance(height_above_eye)}",
                fill="black", font=("Segoe UI", 8, "bold"), anchor="s",
            )

        # Other mesh nodes that are actually visible from here -- geometry
        # only, not radio budget -- so "is that node visible from this
        # point" has a direct answer right on the chart, not just a link
        # check buried in the simulation results.
        for marker in panorama.visible_nodes:
            marker_bearing = None
            for candidate in (marker.bearing_deg, marker.bearing_deg + 360.0, marker.bearing_deg - 360.0):
                if low - 1.0 <= candidate <= high + 1.0:
                    marker_bearing = candidate
                    break
            if marker_bearing is None:
                continue
            mx, my = point_xy(marker_bearing, marker.angle_deg)
            canvas.create_polygon(
                mx, my - 9, mx - 6, my + 4, mx + 6, my + 4,
                fill="#76dcff", outline=self._darken("#76dcff", 35), width=1,
            )
            canvas.create_text(
                mx, my - 12, text=marker.name,
                fill="black", font=("Segoe UI Semibold", 8, "bold"), anchor="s",
            )

        # Eye-level reference line, with markers where something NEARBY
        # crosses it -- a distant peak is already obviously above the line
        # from its silhouette alone; the marker's job is flagging a close
        # obstruction that isn't otherwise obvious, so it's gated on
        # distance, not just height.
        _zero_x1, zero_y = point_xy(low, 0.0)
        canvas.create_line(plot_left, zero_y, plot_right, zero_y, fill="#c026d3", width=2)
        near_threshold_m = panorama.max_range_m * 0.25
        for bearing, point in visible_envelope:
            if point.angle_deg <= 0.0 or point.distance_m > near_threshold_m:
                continue
            marker_x, _marker_y = point_xy(bearing, 0.0)
            canvas.create_polygon(
                marker_x - 5, zero_y - 9, marker_x + 5, zero_y - 9, marker_x, zero_y - 1,
                fill="#ef4444", outline="",
            )

        # Compass labels, only the ones currently in view. A label exactly
        # opposite the view centre (e.g. S when centred on N at full 360°
        # span) sits at both edges at once, so check every wrapped position
        # that could fall in range, not just the nearest one.
        for base_bearing, label in ((0.0, "N"), (90.0, "E"), (180.0, "S"), (270.0, "W")):
            k_center = round((self.horizon_view_center - base_bearing) / 360.0)
            for k in (k_center - 1, k_center, k_center + 1):
                effective = base_bearing + 360.0 * k
                if not (low <= effective <= high):
                    continue
                label_x, _label_y = point_xy(effective, angle_max)
                canvas.create_text(label_x, plot_top - 10, text=label, fill="black", font=("Segoe UI Semibold", 10))
                canvas.create_line(label_x, plot_top, label_x, plot_bottom, fill="#d0d0d0", width=1)

        # Elevation-angle axis.
        for tick in (angle_min, angle_center, angle_max):
            _tick_x, tick_y = point_xy(low, tick)
            canvas.create_line(plot_left, tick_y, plot_right, tick_y, fill="#e2e2e2", width=1)
            canvas.create_text(
                plot_left - 6, tick_y, text=f"{tick:+.0f}°", fill="black", anchor="e", font=("Segoe UI", 8)
            )

        exaggeration = (plot_height / (angle_max - angle_min)) / max(1e-6, plot_width / span)
        canvas.create_text(
            plot_left,
            height - 14,
            text=f"(vertical scale exaggerated {exaggeration:.1f}× · {span:.0f}° in view)",
            fill="black",
            anchor="w",
            font=("Segoe UI", 8),
        )
        canvas.create_text(
            plot_right,
            height - 14,
            text=f"max range {self.format_distance(panorama.max_range_m)}",
            fill="black",
            anchor="e",
            font=("Segoe UI", 8),
        )

        # Depth-layer key: the dashed near->far colour ramp is the only
        # colour coding left (the silhouette itself is one uniform tone),
        # so it's what needs explaining here.
        legend_x = plot_right
        legend_y = plot_top - 27
        for label, color in (("near", self._horizon_layer_color(0.0)), ("far", self._horizon_layer_color(1.0))):
            canvas.create_text(legend_x, legend_y, text=label, fill="black", anchor="e", font=("Segoe UI", 8))
            width_estimate = 10 + len(label) * 5.5
            canvas.create_line(
                legend_x - width_estimate - 12, legend_y, legend_x - width_estimate - 2, legend_y,
                fill=color, width=1, dash=(2, 3),
            )
            legend_x -= width_estimate + 16

        # Kind key: shape (flat-roofed blocks for buildings, scalloped
        # canopy bumps for forest, sloped natural silhouette for everything
        # else) is what makes a feature recognisable at a glance now, but
        # naming the colour-to-kind mapping still saves a guess.
        kind_colors: dict[str, str] = {}
        for point in panorama.points:
            if point.kind != "terrain" and point.kind not in kind_colors:
                kind_colors[point.kind] = point.color
        for layer in panorama.layers:
            for point in layer.points:
                if point.kind != "terrain" and point.kind not in kind_colors:
                    kind_colors[point.kind] = point.color
        if kind_colors:
            kind_legend_x = plot_right
            kind_legend_y = plot_top - 12
            for kind in reversed(list(kind_colors.keys())):
                color = kind_colors[kind]
                canvas.create_text(kind_legend_x, kind_legend_y, text=kind, fill="black", anchor="e", font=("Segoe UI", 8))
                width_estimate = 10 + len(kind) * 5.5
                canvas.create_rectangle(
                    kind_legend_x - width_estimate - 12, kind_legend_y - 5,
                    kind_legend_x - width_estimate - 2, kind_legend_y + 5,
                    fill=color, outline=self._darken(color, 40),
                )
                kind_legend_x -= width_estimate + 16

    def _horizon_point_at(self, x: float, y: float) -> tuple[float, HorizonPoint] | None:
        """Whichever shape is actually visible under (x, y). Depth layers
        are checked nearest first -- the same order the chart draws them,
        nearest on top -- and the first one whose own silhouette (its real
        ground line up to its own height) actually covers this pixel wins,
        so clicking a near feature standing in front of a farther mountain
        reports the near feature, not whichever candidate's angle happens
        to be numerically closest. Falls back to closest-angle only when
        nothing's silhouette covers the click (open sky)."""
        panorama = self.horizon_panorama
        layout = self._horizon_layout
        if panorama is None or layout is None or not panorama.points:
            return None
        plot_left, plot_right, plot_top, plot_bottom, angle_min, angle_max = layout
        plot_width = max(1.0, plot_right - plot_left)
        plot_height = max(1.0, plot_bottom - plot_top)
        angle_range = max(1e-6, angle_max - angle_min)
        view_span = max(20.0, min(360.0, self.horizon_view_span))
        low = self.horizon_view_center - view_span / 2.0
        fraction = max(0.0, min(1.0, (x - plot_left) / plot_width))
        bearing = (low + fraction * view_span) % 360.0

        bearing_count = len(panorama.points)
        index = min(
            range(bearing_count),
            key=lambda i: min(
                abs(panorama.points[i].bearing_deg - bearing),
                360.0 - abs(panorama.points[i].bearing_deg - bearing),
            ),
        )

        def angle_to_y(angle: float) -> float:
            return plot_bottom - (angle - angle_min) / angle_range * plot_height

        show_buildings = self.horizon_show_buildings.get()
        show_forests = self.horizon_show_forests.get()

        # panorama.layers is ordered near-to-far, matching the chart's own
        # nearest-drawn-last-on-top rule -- so checking it in this order
        # and taking the first covering hit reproduces real occlusion.
        for layer in panorama.layers:
            if index >= len(layer.points):
                continue
            raw_point = layer.points[index]
            filtered_point = self._apply_horizon_kind_filter(
                [(raw_point.bearing_deg, raw_point)], show_buildings, show_forests,
            )[0][1]
            top_y = angle_to_y(filtered_point.angle_deg)
            ground_y = angle_to_y(filtered_point.ground_angle_deg)
            span_top, span_bottom = min(top_y, ground_y), max(top_y, ground_y)
            if span_top - 2.0 <= y <= span_bottom + 2.0:
                return filtered_point.bearing_deg, filtered_point

        candidates: list[tuple[float, HorizonPoint]] = [(panorama.points[index].bearing_deg, panorama.points[index])]
        for layer in panorama.layers:
            if index < len(layer.points):
                candidates.append((layer.points[index].bearing_deg, layer.points[index]))
        candidates = self._apply_horizon_kind_filter(candidates, show_buildings, show_forests)
        return min(candidates, key=lambda item: abs(angle_to_y(item[1].angle_deg) - y))

    def _horizon_canvas_motion(self, event: tk.Event) -> None:
        found = self._horizon_point_at(event.x, event.y)
        if found is None:
            return
        bearing, point = found
        compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(bearing / 45.0) % 8]
        kind_label = "Terrain" if point.kind == "terrain" else point.kind
        self.horizon_info_var.set(
            f"From {self.horizon_source_name} · {bearing:.0f}° {compass} · "
            f"{point.angle_deg:+.1f}° elevation angle · {self.format_distance(point.distance_m)} · {kind_label} "
            f"· click to locate on map"
        )

    def _pick_horizon_point(self, x: int, y: int) -> None:
        """Clicking a spot on the panorama drops a marker on the main map
        at that real-world location, so 'what is that shape' has a
        concrete, verifiable answer instead of just a colour/label guess."""
        if self.horizon_source_xy is None:
            return
        found = self._horizon_point_at(x, y)
        if found is None:
            return
        bearing, point = found
        source_x, source_y = self.horizon_source_xy
        angle_rad = math.radians(bearing)
        dx, dy = math.sin(angle_rad), -math.cos(angle_rad)
        world_x = source_x + dx * point.distance_m
        world_y = source_y + dy * point.distance_m
        compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(bearing / 45.0) % 8]
        kind_label = "Terrain" if point.kind == "terrain" else point.kind
        self._locate_on_map(
            world_x, world_y,
            label=f"{kind_label} · {self.format_distance(point.distance_m)} {compass}",
            status=f"Located on map: {kind_label} · {self.format_distance(point.distance_m)} at {bearing:.0f}° {compass}",
        )

    def _locate_on_map(self, world_x: float, world_y: float, label: str, status: str) -> None:
        """Shared by Horizon and Profile: drop a marker on the main map at a
        real-world location clicked in either chart, so 'what is that
        exactly' has a concrete, verifiable answer, and recentre the view
        if the spot isn't already comfortably visible."""
        self.map_picked_xy = (world_x, world_y)
        self.map_picked_label = label
        self.status_var.set(status)
        if hasattr(self, "canvas") and self.canvas.winfo_width() > 1 and self.canvas.winfo_height() > 1:
            left, top = self.screen_to_world(0, 0)
            right, bottom = self.screen_to_world(self.canvas.winfo_width(), self.canvas.winfo_height())
            margin_x = (right - left) * 0.1
            margin_y = (bottom - top) * 0.1
            if not (
                left + margin_x <= world_x <= right - margin_x
                and top + margin_y <= world_y <= bottom - margin_y
            ):
                visible_width = self.canvas.winfo_width() / max(1e-9, self._base_scale() * self.zoom)
                visible_height = self.canvas.winfo_height() / max(1e-9, self._base_scale() * self.zoom)
                self.view_x = world_x - visible_width / 2.0
                self.view_y = world_y - visible_height / 2.0
        self.schedule_render()

    def _draw_map_picked_marker(self, c: tk.Canvas) -> None:
        if self.map_picked_xy is None:
            return
        world_x, world_y = self.map_picked_xy
        marker_x, marker_y = self.world_to_screen(world_x, world_y)
        c.create_line(marker_x - 11, marker_y, marker_x + 11, marker_y, fill="#facc15", width=2)
        c.create_line(marker_x, marker_y - 11, marker_x, marker_y + 11, fill="#facc15", width=2)
        c.create_oval(marker_x - 9, marker_y - 9, marker_x + 9, marker_y + 9, outline="#facc15", width=2)
        if self.map_picked_label:
            c.create_text(
                marker_x, marker_y - 17, text=self.map_picked_label,
                fill="#facc15", font=("Segoe UI Semibold", 9), anchor="s",
            )

    def _draw_horizon_view_cone(self, c: tk.Canvas) -> None:
        """Show where the open Horizon panorama is standing, and a wedge for
        exactly which bearings its current view spans -- so panning/zooming
        that window is visible on the map too, not just in its own canvas."""
        if self.horizon_source_xy is None:
            return
        source_x, source_y = self.horizon_source_xy
        span = max(20.0, min(360.0, self.horizon_view_span))
        low = self.horizon_view_center - span / 2.0
        cone_length = self._coverage_range_cap() * 0.6

        source_sx, source_sy = self.world_to_screen(source_x, source_y)
        c.create_oval(
            source_sx - 6, source_sy - 6, source_sx + 6, source_sy + 6,
            outline="#76dcff", width=2, fill="#0b1220",
        )

        if span < 359.9:
            steps = max(2, round(span / 6.0))
            arc_points: list[float] = []
            for step in range(steps + 1):
                bearing = low + span * step / steps
                angle_rad = math.radians(bearing)
                dx, dy = math.sin(angle_rad), -math.cos(angle_rad)
                world_x = source_x + dx * cone_length
                world_y = source_y + dy * cone_length
                arc_points.extend(self.world_to_screen(world_x, world_y))
            c.create_polygon(
                source_sx, source_sy, *arc_points,
                fill="#76dcff", stipple="gray25", outline="",
            )
            c.create_line(
                source_sx, source_sy, arc_points[0], arc_points[1],
                fill="#76dcff", width=1, dash=(3, 2),
            )
            c.create_line(
                source_sx, source_sy, arc_points[-2], arc_points[-1],
                fill="#76dcff", width=1, dash=(3, 2),
            )
            c.create_line(*arc_points, fill="#76dcff", width=2)
        else:
            c.create_oval(
                source_sx - cone_length * (self._base_scale() * self.zoom),
                source_sy - cone_length * (self._base_scale() * self.zoom),
                source_sx + cone_length * (self._base_scale() * self.zoom),
                source_sy + cone_length * (self._base_scale() * self.zoom),
                outline="#76dcff", width=1, dash=(3, 2),
            )

    def _resolve_map_point(self, point: Node | tuple[float, float]) -> tuple[Node, str]:
        """Shared by Horizon and Profile: a clicked point may be an existing
        node (use it exactly, with its own real antenna height/elevation,
        not the ground) or bare ground (a throwaway point with auto-grounded
        elevation and the default antenna height)."""
        if isinstance(point, Node):
            return point, point.name
        x, y = point
        node = Node(x=x, y=y)
        self._set_auto_node_elevation(node)
        env = self.scenario.environment
        if env.map_configured:
            latitude, longitude = world_to_latlon(x, y, env.map_center_lat, env.map_center_lon)
            label = f"{latitude:.5f}, {longitude:.5f}"
        else:
            label = f"X {self.format_distance(x)} · Y {self.format_distance(y)}"
        return node, label

    def show_path_profile(
        self, point_a: Node | tuple[float, float], point_b: Node | tuple[float, float]
    ) -> None:
        node_a, name_a = self._resolve_map_point(point_a)
        node_b, name_b = self._resolve_map_point(point_b)
        self.path_profile_names = (name_a, name_b)
        self.path_profile_endpoints = ((node_a.x, node_a.y), (node_b.x, node_b.y))
        model = PropagationModel(self.scenario)
        self.path_profile_data = model.path_profile(node_a, node_b)
        self.status_var.set(f"Path profile ready: {name_a} → {name_b}")
        self._open_path_profile_window()

    def _open_path_profile_window(self) -> None:
        if self._bottom_dock_active == "profile" and self.path_profile_canvas is not None:
            self._refresh_path_profile()
            return

        window = self._show_bottom_dock("profile")

        info_row = ttk.Frame(window, style="Toolbar.TFrame")
        info_row.pack(fill="x")
        ttk.Label(
            info_row,
            textvariable=self.path_profile_info_var,
            style="Muted.TLabel",
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=12, pady=(8, 4))
        ttk.Button(
            info_row, text="✕", style="Tool.TButton", width=3, command=self._close_bottom_dock,
        ).pack(side="right", padx=(2, 8), pady=(6, 2))

        self.path_profile_canvas = tk.Canvas(window, bg=MAPLESS_BACKGROUND, highlightthickness=0, borderwidth=0)
        self.path_profile_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.path_profile_canvas.bind("<Configure>", self._schedule_path_profile_redraw)
        self.path_profile_canvas.bind("<Button-1>", self._profile_canvas_click)

        ttk.Label(
            window,
            text=(
                "Ground and obstacles between the two points, to scale. The dashed line is the "
                "straight radio path the simulator itself tests between their antenna heights. "
                "Click a spot to locate it on the map."
            ),
            style="Muted.TLabel",
            anchor="w",
            wraplength=620,
        ).pack(fill="x", padx=12, pady=(0, 8))
        self._refresh_path_profile()

    def _reset_path_profile_state(self) -> None:
        if self.path_profile_redraw_after is not None:
            try:
                self.root.after_cancel(self.path_profile_redraw_after)
            except tk.TclError:
                pass
            self.path_profile_redraw_after = None
        self.path_profile_canvas = None
        self.map_picked_xy = None
        self.map_picked_label = None

    def _close_path_profile_window(self) -> None:
        self._reset_path_profile_state()
        if self._bottom_dock_active == "profile":
            self._hide_bottom_dock()

    def _schedule_path_profile_redraw(self, _event: tk.Event | None = None) -> None:
        if self.path_profile_redraw_after is not None:
            try:
                self.root.after_cancel(self.path_profile_redraw_after)
            except tk.TclError:
                pass
        self.path_profile_redraw_after = self.root.after(80, self._refresh_path_profile)

    def _refresh_path_profile(self) -> None:
        self.path_profile_redraw_after = None
        canvas = self.path_profile_canvas
        profile = self.path_profile_data
        if canvas is None or profile is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        self._draw_path_profile(canvas, width, height, profile)
        name_a, name_b = self.path_profile_names
        if not profile.compatible:
            status = f"Blocked: {profile.reason}"
        elif profile.margin_db < MIN_DECODE_MARGIN_DB:
            status = f"Too weak to decode · {profile.margin_db:+.1f} dB margin · {profile.total_loss_db:.1f} dB obstruction loss"
        else:
            status = f"Link viable · {profile.margin_db:+.1f} dB margin · {profile.total_loss_db:.1f} dB obstruction loss"
        self.path_profile_info_var.set(
            f"{name_a} → {name_b} · {self.format_distance(profile.distance_m)} · {status}"
        )

    def _profile_canvas_click(self, event: tk.Event) -> None:
        self._pick_profile_point(event.x)

    def _pick_profile_point(self, x: int) -> None:
        """Clicking a spot on the profile drops a marker on the main map at
        that real-world location along the A-to-B line, so 'where exactly
        is that dip/obstacle' has a concrete answer."""
        layout = self._path_profile_layout
        profile = self.path_profile_data
        endpoints = self.path_profile_endpoints
        if layout is None or profile is None or endpoints is None:
            return
        plot_left, plot_right, *_rest = layout
        pixel_span = max(1.0, plot_right - plot_left)
        fraction = max(0.0, min(1.0, (x - plot_left) / pixel_span))
        distance_along = fraction * profile.distance_m
        (ax, ay), (bx, by) = endpoints
        world_x = ax + (bx - ax) * fraction
        world_y = ay + (by - ay) * fraction
        name_a, name_b = self.path_profile_names
        self._locate_on_map(
            world_x, world_y,
            label=f"{self.format_distance(distance_along)} from {name_a}",
            status=f"Located on map: {self.format_distance(distance_along)} along the path from {name_a} to {name_b}",
        )

    def _draw_path_profile(self, canvas: tk.Canvas, width: int, height: int, profile: PathProfile) -> None:
        margin_left, margin_right = 56, 16
        margin_top, margin_bottom = 34, 30
        plot_left, plot_right = margin_left, width - margin_right
        plot_top, plot_bottom = margin_top, height - margin_bottom
        plot_width = max(1.0, plot_right - plot_left)
        plot_height = max(1.0, plot_bottom - plot_top)

        elevations = [elevation for _distance, elevation in profile.terrain_samples if elevation is not None]
        elevations.extend([profile.source_antenna_z, profile.target_antenna_z])
        for obstacle in profile.obstacles:
            elevations.extend((obstacle.base_elevation_m, obstacle.top_elevation_m))
        elevation_min = min(elevations)
        elevation_max = max(elevations)
        pad = max(2.0, (elevation_max - elevation_min) * 0.12)
        elevation_min -= pad
        elevation_max += pad
        distance = max(1.0, profile.distance_m)

        def point_xy(distance_m: float, elevation_m: float) -> tuple[float, float]:
            px = plot_left + (distance_m / distance) * plot_width
            py = plot_bottom - (elevation_m - elevation_min) / (elevation_max - elevation_min) * plot_height
            return px, py

        self._path_profile_layout = (plot_left, plot_right, plot_top, plot_bottom, elevation_min, elevation_max)

        # Ground fill beneath the sampled terrain, skipping any gaps with no data.
        run: list[float] = []
        for sample_distance, elevation in profile.terrain_samples:
            if elevation is None:
                if len(run) >= 4:
                    canvas.create_polygon(
                        run[0], plot_bottom, *run, run[-2], plot_bottom, fill="#d8dde3", outline=""
                    )
                run = []
                continue
            run.extend(point_xy(sample_distance, elevation))
        if len(run) >= 4:
            canvas.create_polygon(run[0], plot_bottom, *run, run[-2], plot_bottom, fill="#d8dde3", outline="")
        coordinates = [
            coordinate
            for sample_distance, elevation in profile.terrain_samples
            if elevation is not None
            for coordinate in point_xy(sample_distance, elevation)
        ]
        if len(coordinates) >= 4:
            canvas.create_line(*coordinates, fill=PropagationModel.TERRAIN_HORIZON_COLOR, width=2)

        # Obstacle footprints, shaded to read as solid blocks on the ground.
        for obstacle in profile.obstacles:
            x1, y1 = point_xy(obstacle.start_m, obstacle.top_elevation_m)
            x2, y2 = point_xy(obstacle.end_m, obstacle.base_elevation_m)
            if obstacle.kind == "Forest":
                self._draw_profile_forest_block(canvas, x1, y1, x2, y2, obstacle.color)
            else:
                self._draw_shaded_block(canvas, x1, y1, x2, y2, obstacle.color)

        # The straight radio path the simulator tests between the two antennas.
        los_x1, los_y1 = point_xy(0.0, profile.source_antenna_z)
        los_x2, los_y2 = point_xy(distance, profile.target_antenna_z)
        canvas.create_line(los_x1, los_y1, los_x2, los_y2, fill="#c026d3", width=2, dash=(6, 3))
        name_a, name_b = self.path_profile_names
        for x, y, label, anchor in ((los_x1, los_y1, name_a, "w"), (los_x2, los_y2, name_b, "e")):
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#c026d3", outline="white")
            canvas.create_text(
                x, y - 12, text=label, fill="black", font=("Segoe UI Semibold", 10), anchor=anchor,
            )

        # Elevation axis.
        for tick in (elevation_min + pad, (elevation_min + elevation_max) / 2.0, elevation_max - pad):
            _tick_x, tick_y = point_xy(0.0, tick)
            canvas.create_line(plot_left, tick_y, plot_right, tick_y, fill="#e2e2e2", width=1)
            canvas.create_text(
                plot_left - 6, tick_y, text=self.format_distance(tick), fill="black", anchor="e", font=("Segoe UI", 8)
            )

        # Distance axis.
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            tick_x, _tick_y = point_xy(distance * fraction, elevation_min)
            canvas.create_text(
                tick_x, plot_bottom + 8, text=self.format_distance(distance * fraction),
                fill="black", anchor="n", font=("Segoe UI", 8),
            )

        legend_kinds = list(dict.fromkeys(obstacle.kind for obstacle in profile.obstacles))
        legend_x = plot_right
        legend_y = plot_top - 16
        for kind in reversed(legend_kinds):
            color = next(obstacle.color for obstacle in profile.obstacles if obstacle.kind == kind)
            canvas.create_text(legend_x, legend_y, text=kind, fill="black", anchor="e", font=("Segoe UI", 8))
            width_estimate = 10 + len(kind) * 5.5
            canvas.create_rectangle(
                legend_x - width_estimate - 12, legend_y - 5, legend_x - width_estimate - 2, legend_y + 5,
                fill=color, outline="",
            )
            legend_x -= width_estimate + 16

    def _render_mesh_traffic_chart(self, canvas: tk.Canvas, width: float, height: float) -> None:
        frames = list(self.live_mesh_history_frames)
        if not frames and self.live_mesh_result is not None:
            frames = list(self.live_mesh_result.frames)
        if not frames and self.last_result is not None:
            buckets: dict[int, LiveMeshFrame] = {}
            for event in self.last_result.events:
                index = int(max(0.0, event.time_ms) // 1_000.0)
                frame = buckets.setdefault(index, LiveMeshFrame(time_ms=index * 1_000.0))
                if event.kind == "TX":
                    frame.transmission_count += 1
                elif event.kind == "RX":
                    frame.reception_count += 1
                elif event.kind == "COLLISION":
                    frame.collision_count += 1
                elif event.kind in {"DROP", "RF DROP"}:
                    frame.drop_count += 1
            frames = [buckets[index] for index in sorted(buckets)]
        if not frames:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Start Live mesh traffic to plot mesh activity over time",
                fill=MUTED,
                font=("Segoe UI", 13),
            )
            self.mesh_graph_info_var.set("No live-mesh history yet.")
            self.mesh_graph_delivery_var.set(
                "Send a test packet while Live mesh traffic is on to see whether traffic, rather than RF coverage, prevented delivery."
            )
            return

        # Preserve the full one-hour accounting history, but aggregate it to
        # fewer than a screenful of samples before asking Tk to redraw lines.
        # This keeps an open graph from becoming progressively more expensive.
        max_chart_frames = max(160, int(width * 0.65))
        if len(frames) > max_chart_frames:
            bucket_size = math.ceil(len(frames) / max_chart_frames)
            reduced: list[LiveMeshFrame] = []
            for start in range(0, len(frames), bucket_size):
                group = frames[start:start + bucket_size]
                aggregate = LiveMeshFrame(time_ms=group[-1].time_ms)
                for frame in group:
                    aggregate.transmission_count += frame.transmission_count
                    aggregate.reception_count += frame.reception_count
                    aggregate.collision_count += frame.collision_count
                    aggregate.drop_count += frame.drop_count
                    aggregate.throttle_count += frame.throttle_count
                    aggregate.peak_channel_utilization = max(
                        aggregate.peak_channel_utilization, frame.peak_channel_utilization
                    )
                    for target, source in (
                        (aggregate.traffic_transmissions, frame.traffic_transmissions),
                        (aggregate.traffic_collisions, frame.traffic_collisions),
                        (aggregate.traffic_drops, frame.traffic_drops),
                        (aggregate.traffic_throttles, frame.traffic_throttles),
                    ):
                        for kind, count in source.items():
                            target[kind] = target.get(kind, 0) + count
                reduced.append(aggregate)
            frames = reduced

        def packet_total(frame: LiveMeshFrame, kinds: tuple[str, ...]) -> int:
            return sum(frame.traffic_transmissions.get(kind, 0) for kind in kinds)

        # Animation lists are capped for smooth drawing.  These dictionaries
        # retain the complete packet accounting needed for traffic diagnosis.
        series = [
            ("NodeInfo", TRAFFIC_COLORS["NODEINFO"], self.mesh_graph_show_nodeinfo.get(), lambda frame: packet_total(frame, ("NODEINFO",)), "count"),
            ("Telemetry", TRAFFIC_COLORS["TELEMETRY"], self.mesh_graph_show_telemetry.get(), lambda frame: packet_total(frame, ("TELEMETRY",)), "count"),
            ("Sensor", TRAFFIC_COLORS["SENSOR"], self.mesh_graph_show_sensor.get(), lambda frame: packet_total(frame, ("SENSOR",)), "count"),
            ("Messages", TRAFFIC_COLORS["MESSAGE"], self.mesh_graph_show_messages.get(), lambda frame: packet_total(frame, ("MESSAGE",)), "count"),
            ("Tests / replies", "#f8fafc", self.mesh_graph_show_control.get(), lambda frame: packet_total(frame, ("TEST", "ACK", "RESPONSE", "NAK")), "count"),
            ("Collisions", "#fb6376", self.mesh_graph_show_collisions.get(), lambda frame: frame.collision_count, "count"),
            ("RF drops", "#ffbd4a", self.mesh_graph_show_drops.get(), lambda frame: frame.drop_count, "count"),
            ("Channel-gated", "#c084fc", self.mesh_graph_show_gated.get(), lambda frame: frame.throttle_count, "count"),
            ("Channel utility", "#f8fafc", self.mesh_graph_show_utilization.get(), lambda frame: frame.peak_channel_utilization, "percent"),
        ]
        left, right, top, bottom = 66.0, width - 66.0, 54.0, height - 56.0
        chart_width, chart_height = max(1.0, right - left), max(1.0, bottom - top)
        count_max = max(
            1,
            *(max(float(value_for(frame)) for frame in frames) for _label, _color, _visible, value_for, kind in series if kind == "count"),
        )
        for step in range(5):
            y = top + chart_height * step / 4.0
            value = count_max * (4 - step) / 4.0
            canvas.create_line(left, y, right, y, fill="#20334b", width=1)
            canvas.create_text(left - 8, y, text=f"{value:.0f}", fill=MUTED, anchor="e", font=("Segoe UI", 8))
            canvas.create_text(right + 8, y, text=f"{(4 - step) * 25:.0f}%", fill=MUTED, anchor="w", font=("Segoe UI", 8))

        for percent, color, label in ((25.0, "#d6a442", "25% polite"), (40.0, "#ef6a75", "40% max")):
            y = bottom - percent / 100.0 * chart_height
            canvas.create_line(left, y, right, y, fill=color, width=1, dash=(4, 4))
            canvas.create_text(right - 4, y - 3, text=label, fill=color, anchor="se", font=("Segoe UI", 8, "bold"))

        canvas.create_text(left, top - 19, text="RF transmissions / 2 s frame", fill=MUTED, anchor="w", font=("Segoe UI", 8))
        canvas.create_text(right, top - 19, text="channel utilization", fill=MUTED, anchor="e", font=("Segoe UI", 8))

        start_ms, end_ms = frames[0].time_ms, frames[-1].time_ms
        span_ms = max(1.0, end_ms - start_ms)
        for step in range(5):
            x = left + chart_width * step / 4.0
            time_ms = start_ms + span_ms * step / 4.0
            if time_ms >= 3_600_000:
                label = f"{time_ms / 3_600_000:.1f}h"
            elif time_ms >= 60_000:
                label = f"{time_ms / 60_000:.1f}m"
            else:
                label = f"{time_ms / 1000:.0f}s"
            canvas.create_line(x, top, x, bottom, fill="#172a41", width=1)
            canvas.create_text(x, bottom + 18, text=label, fill=MUTED, anchor="n", font=("Segoe UI", 8))

        legend_x = left
        legend_y = 16.0
        for label, color, visible, value_for, scale_kind in series:
            if not visible:
                continue
            legend_width = max(84.0, 32.0 + len(label) * 6.2)
            if legend_x + legend_width > right:
                legend_x = left
                legend_y += 18.0
            coordinates: list[float] = []
            for frame in frames:
                x = left + (frame.time_ms - start_ms) / span_ms * chart_width
                value = float(value_for(frame))
                scale = 100.0 if scale_kind == "percent" else float(count_max)
                y = bottom - clamp(value / scale, 0.0, 1.0) * chart_height
                coordinates.extend((x, y))
            if len(coordinates) >= 4:
                canvas.create_line(*coordinates, fill=color, width=2, dash=(5, 3) if scale_kind == "percent" else ())
            elif coordinates:
                x, y = coordinates
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")
            canvas.create_line(legend_x, legend_y, legend_x + 16, legend_y, fill=color, width=3)
            canvas.create_text(legend_x + 20, legend_y, text=label, fill=TEXT, anchor="w", font=("Segoe UI", 9))
            legend_x += legend_width

        snapshot = self.live_mesh_snapshot
        type_totals = {
            "NodeInfo": sum(packet_total(frame, ("NODEINFO",)) for frame in frames),
            "Telemetry": sum(packet_total(frame, ("TELEMETRY",)) for frame in frames),
            "Sensor": sum(packet_total(frame, ("SENSOR",)) for frame in frames),
            "Messages": sum(packet_total(frame, ("MESSAGE",)) for frame in frames),
            "Tests/replies": sum(packet_total(frame, ("TEST", "ACK", "RESPONSE", "NAK")) for frame in frames),
        }
        self.mesh_graph_info_var.set(
            f"{len(frames):,} frames · T+{end_ms / 1000:.1f}s · "
            f"{snapshot.get('transmissions', self.live_mesh_play_counts['tx']):,} total TX · "
            f"{snapshot.get('receptions', self.live_mesh_play_counts['rx']):,} total RX · "
            f"{snapshot.get('collisions', self.live_mesh_play_counts['collisions']):,} collisions · "
            f"{snapshot.get('dropped', self.live_mesh_play_counts['dropped']):,} RF drops"
        )
        self.mesh_graph_info_var.set(
            f"{len(frames):,} frames | T+{end_ms / 1000:.1f}s | "
            + " | ".join(f"{label} {count:,}" for label, count in type_totals.items())
            + f" | {sum(frame.collision_count for frame in frames):,} collisions"
            + f" | {sum(frame.throttle_count for frame in frames):,} channel-gated"
            + f" | {sum(frame.drop_count for frame in frames):,} RF drops"
        )
        selected_test = self.live_mesh_tests.get(self.live_path_test_id or -1)
        if selected_test is None and self.live_mesh_tests:
            selected_test = self.live_mesh_tests[max(self.live_mesh_tests)]
        if selected_test is None:
            self.mesh_graph_delivery_var.set(
                "Traffic diagnosis: collisions and channel-gated attempts are load-related. RF drops are link, terrain, or obstacle losses and are shown separately."
            )
        else:
            waits = sum(1 for event in selected_test.events if event.kind == "CHANNEL WAIT")
            collision_events = sum(1 for event in selected_test.events if event.kind.endswith("COLLISION"))
            rf_events = sum(1 for event in selected_test.events if event.kind.endswith("RF DROP"))
            failure = next(
                (event.detail for event in reversed(selected_test.events)
                 if event.kind in {"RESULT", "ACK FAILED", "RESPONSE FAILED", "HOP LIMIT", "NO RELAY"} and event.detail),
                "",
            )
            diagnosis = (
                f"Test #{selected_test.test_id}: {selected_test.status}. "
                f"Traffic impact: {collision_events} collision(s), {waits} channel wait(s). "
                f"RF impact: {rf_events} link/terrain drop(s)."
            )
            if failure:
                diagnosis += f" Outcome: {failure}"
            self.mesh_graph_delivery_var.set(diagnosis)

    def _build_scene_panel(self) -> None:
        header = ttk.Frame(self.scene_panel)
        header.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(header, text="SCENE", style="Section.TLabel").pack(side="left")
        ttk.Button(header, text="+", width=3, command=lambda: self.set_tool("node")).pack(side="right")
        self.scene_tree = ttk.Treeview(self.scene_panel, show="tree", selectmode="browse")
        self.scene_tree.pack(fill="both", expand=True, padx=8)
        self.scene_tree.bind("<<TreeviewSelect>>", self._scene_tree_select)
        hint = tk.Label(
            self.scene_panel,
            text="Drag items on the map.\nMouse wheel zooms · right drag pans.",
            justify="left",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 8),
            padx=10,
            pady=9,
        )
        hint.pack(fill="x")

    def _build_live_panel(self) -> None:
        header = ttk.Frame(self.live_panel)
        header.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(header, text="MESHTASTIC SERIAL", style="Section.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.live_status_var, style="Muted.TLabel").pack(side="right")

        connection = ttk.Frame(self.live_panel)
        connection.pack(fill="x", padx=10, pady=(2, 6))
        self.live_port_picker = ttk.Combobox(
            connection,
            textvariable=self.live_port_var,
            state="readonly",
        )
        self.live_port_picker.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.live_port_picker.bind("<Button-1>", self.refresh_live_ports)
        self.live_connect_button = ttk.Button(
            connection, text="Connect", style="Accent.TButton", command=self.connect_live_radio
        )
        self.live_connect_button.grid(row=1, column=0, sticky="ew", padx=(0, 3))
        self.live_disconnect_button = ttk.Button(
            connection, text="Disconnect", command=self.disconnect_live_radio, state="disabled"
        )
        self.live_disconnect_button.grid(row=1, column=1, sticky="ew", padx=(3, 0))
        for column in range(2):
            connection.columnconfigure(column, weight=1)

        options = ttk.Frame(self.live_panel)
        options.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Checkbutton(
            options,
            text="Continuously update plotted nodes",
            variable=self.live_sync_var,
        ).pack(side="left")
        self.live_fit_button = ttk.Button(options, text="Fit nodes", command=self.fit_live_nodes, state="disabled")
        self.live_fit_button.pack(side="right")

        self.live_nodes_tree = self._tree(
            self.live_panel,
            [
                ("node", "Node", 145),
                ("id", "ID", 88),
                ("position", "Position / altitude", 155),
                ("hops", "Hops", 48),
                ("snr", "SNR", 60),
                ("heard", "Last heard", 100),
            ],
        )
        self.live_nodes_tree.bind("<<TreeviewSelect>>", self._live_tree_select)
        ttk.Label(
            self.live_panel,
            text="Read-only: connecting downloads the radio's known node database. "
            "Nodes without a shared position remain listed here but are not placed on the map.",
            style="Muted.TLabel",
            wraplength=355,
            justify="left",
        ).pack(fill="x", padx=10, pady=8)
        self.refresh_live_ports()

    def refresh_live_ports(self, _event: tk.Event | None = None) -> None:
        previous_device = ""
        selected = self.live_ports.get(self.live_port_var.get())
        if selected is not None:
            previous_device = selected.device
        try:
            ports = list_serial_ports()
        except Exception as error:
            self.live_status_var.set("Port scan failed")
            self.status_var.set(f"Could not enumerate COM ports: {error}")
            return
        self.live_ports = {port.label: port for port in ports}
        labels = list(self.live_ports)
        self.live_port_picker.configure(values=labels)
        matching_label = next(
            (label for label, port in self.live_ports.items() if port.device == previous_device),
            "",
        )
        if matching_label:
            self.live_port_var.set(matching_label)
        elif labels:
            self.live_port_var.set(labels[0])
        else:
            self.live_port_var.set("")
        if not self.live_radio.connected and not self.live_radio.connecting:
            self.live_status_var.set(f"{len(ports)} COM port{'s' if len(ports) != 1 else ''}")

    def connect_live_radio(self) -> None:
        port = self.live_ports.get(self.live_port_var.get())
        if port is None:
            messagebox.showinfo(
                "Choose a COM port",
                "Connect the radio by USB, then choose its COM port from the dropdown.",
                parent=self.root,
            )
            return
        self.live_connection_ready = False
        self.live_connect_button.configure(state="disabled")
        self.live_disconnect_button.configure(state="normal")
        self.live_port_picker.configure(state="disabled")
        self.live_status_var.set(f"Connecting {port.device}…")
        self.status_var.set(f"Connecting read-only to Meshtastic on {port.device} and downloading its node database…")
        try:
            self.live_radio.connect(port.device)
        except RuntimeError as error:
            self._set_live_disconnected_controls()
            messagebox.showerror("Could not connect", str(error), parent=self.root)

    def disconnect_live_radio(self) -> None:
        self.live_status_var.set("Disconnecting…")
        self.live_disconnect_button.configure(state="disabled")
        self.live_connection_ready = False
        self.live_radio.disconnect()

    def _set_live_disconnected_controls(self) -> None:
        self.live_connect_button.configure(state="normal")
        self.live_disconnect_button.configure(state="disabled")
        self.live_port_picker.configure(state="readonly")

    def _poll_live_radio(self) -> None:
        latest_nodes: dict[int, LiveNode] = {}
        initial_connection = False
        connected_port = ""
        redraw_tree = False
        while True:
            try:
                event, payload = self.live_radio.events.get_nowait()
            except queue.Empty:
                break
            if event == "node":
                latest_nodes[payload.node_num] = payload
                redraw_tree = True
            elif event == "connected":
                connected_port = str(payload["port"])
                for node in payload["nodes"]:
                    latest_nodes[node.node_num] = node
                self.live_connection_ready = True
                initial_connection = True
                redraw_tree = True
                self.live_disconnect_button.configure(state="normal")
                self.live_fit_button.configure(state="normal")
            elif event == "error":
                self.live_connection_ready = False
                self._set_live_disconnected_controls()
                self.live_status_var.set("Connection failed")
                error = payload["error"]
                self.status_var.set(f"Meshtastic connection failed on {payload['port']}: {error}")
                messagebox.showerror(
                    "Meshtastic connection failed",
                    f"Could not open {payload['port']}.\n\n{error}\n\n"
                    "Close any other Meshtastic application using that COM port and try again.",
                    parent=self.root,
                )
            elif event in {"disconnected", "lost"}:
                self.live_connection_ready = False
                self._set_live_disconnected_controls()
                self.live_status_var.set("Disconnected")
                verb = "Connection lost" if event == "lost" else "Disconnected"
                self.status_var.set(f"{verb} from {payload['port']} · plotted nodes were retained")
            elif event == "close_error":
                self.status_var.set(f"Serial port closed with a warning: {payload['error']}")

        self.live_nodes.update(latest_nodes)
        if self.live_connection_ready and self.live_sync_var.get():
            if initial_connection:
                self._apply_live_nodes(list(self.live_nodes.values()), reframe=True)
            elif latest_nodes:
                self._apply_live_nodes(list(latest_nodes.values()), reframe=False)
        if initial_connection:
            positioned = sum(node.has_position for node in self.live_nodes.values())
            total = len(self.live_nodes)
            self.live_status_var.set(f"Connected {connected_port}")
            self.status_var.set(
                f"Connected to {connected_port} · {total} known nodes · {positioned} plotted with shared positions"
            )
        if redraw_tree:
            self._refresh_live_tree()
        try:
            self.root.after(150, self._poll_live_radio)
        except tk.TclError:
            pass

    def _apply_live_nodes(self, snapshots: list[LiveNode], reframe: bool) -> None:
        positioned = [snapshot for snapshot in snapshots if snapshot.has_position]
        if reframe and positioned:
            self._reframe_live_area(positioned)

        # A Meshtastic node number is its stable identity. Reconnecting must
        # update that map object, even if an older build accidentally left more
        # than one object with the same number in the scenario.
        groups: dict[int, list[Node]] = {}
        for candidate in self.scenario.nodes:
            groups.setdefault(candidate.node_num, []).append(candidate)
        referenced_ids = {
            self.scenario.packet.source_id,
            self.scenario.packet.destination_id,
            self.selected_id,
        }
        by_number: dict[int, Node] = {}
        duplicate_to_keeper: dict[str, str] = {}
        for node_num, candidates in groups.items():
            keeper = max(
                candidates,
                key=lambda candidate: (
                    candidate.id == self.selected_id,
                    candidate.id in referenced_ids,
                    bool(candidate.live_port),
                    -self.scenario.nodes.index(candidate),
                ),
            )
            by_number[node_num] = keeper
            for candidate in candidates:
                if candidate is not keeper:
                    duplicate_to_keeper[candidate.id] = keeper.id
        if duplicate_to_keeper:
            self.scenario.nodes = [
                node for node in self.scenario.nodes if node.id not in duplicate_to_keeper
            ]
            self.scenario.packet.source_id = duplicate_to_keeper.get(
                self.scenario.packet.source_id,
                self.scenario.packet.source_id,
            )
            self.scenario.packet.destination_id = duplicate_to_keeper.get(
                self.scenario.packet.destination_id,
                self.scenario.packet.destination_id,
            )
            self.selected_id = duplicate_to_keeper.get(self.selected_id, self.selected_id)

        created = False
        changed = bool(duplicate_to_keeper)
        for snapshot in snapshots:
            node = by_number.get(snapshot.node_num)
            node_was_created = False
            if node is None and not snapshot.has_position:
                continue
            if node is None:
                node = Node(node_num=snapshot.node_num)
                self.scenario.nodes.append(node)
                by_number[node.node_num] = node
                created = True
                node_was_created = True
            else:
                # Metadata, role, hardware, signal statistics, and a newly
                # available position all belong to this existing object.
                changed = True
            role = snapshot.role if snapshot.role in ROLES else "CLIENT"
            node.name = snapshot.name
            node.role = role
            node.favorite = snapshot.favorite
            node.online = True
            node.live_port = self.live_radio.port
            node.hardware_model = snapshot.hardware_model
            if node_was_created:
                profile = hardware_power_profile(snapshot.hardware_model)
                node.power_profile = profile.key
                node.tx_power_dbm = profile.recommended_dbm
            node.last_heard = snapshot.last_heard
            node.live_snr_db = snapshot.snr_db
            node.hops_away = snapshot.hops_away
            node.position_precision_bits = snapshot.precision_bits
            self._record_live_altitude(node, snapshot)
            details = [f"Live Meshtastic node from {self.live_radio.port}"]
            if snapshot.hardware_model and snapshot.hardware_model != "UNSET":
                details.append(f"hardware {snapshot.hardware_model}")
            if snapshot.hops_away is not None:
                details.append(f"{snapshot.hops_away} hops away")
            node.notes = " · ".join(details)
            if snapshot.has_position:
                node.x, node.y = latlon_to_world(
                    float(snapshot.latitude),
                    float(snapshot.longitude),
                    self.scenario.environment.map_center_lat,
                    self.scenario.environment.map_center_lon,
                )
                self._apply_live_node_elevation(node)
                changed = True

        if not self.scenario.packet.source_id:
            local = next(
                (
                    by_number[snapshot.node_num]
                    for snapshot in snapshots
                    if snapshot.hops_away == 0 and snapshot.node_num in by_number
                ),
                None,
            )
            if local is not None:
                self.scenario.packet.source_id = local.id
            elif created and self.scenario.nodes:
                self.scenario.packet.source_id = self.scenario.nodes[0].id
        if not (created or changed):
            return
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_scene_tree()
        if created:
            self._build_packet_form()
        if isinstance(self.get_selected(), Node):
            self._build_object_form()
        self.render_canvas()
        if reframe:
            self.fit_view()
            self.load_topography()

    def _reframe_live_area(self, snapshots: list[LiveNode]) -> None:
        env = self.scenario.environment
        old_configured = env.map_configured
        old_reference = (env.map_center_lat, env.map_center_lon)
        node_geo: dict[str, tuple[float, float]] = {}
        obstacle_geo: dict[str, dict[str, Any]] = {}
        geographic_points = [
            (float(snapshot.latitude), float(snapshot.longitude))
            for snapshot in snapshots
            if snapshot.has_position
        ]

        if old_configured:
            for node in self.scenario.nodes:
                coordinates = world_to_latlon(node.x, node.y, *old_reference)
                node_geo[node.id] = coordinates
                geographic_points.append(coordinates)
            for obstacle in self.scenario.obstacles:
                data: dict[str, Any] = {
                    "corner1": world_to_latlon(obstacle.x1, obstacle.y1, *old_reference),
                    "corner2": world_to_latlon(obstacle.x2, obstacle.y2, *old_reference),
                    "points": [world_to_latlon(point[0], point[1], *old_reference) for point in obstacle.points],
                }
                obstacle_geo[obstacle.id] = data
                geographic_points.extend([data["corner1"], data["corner2"], *data["points"]])

        mercator_points = [latlon_to_mercator(latitude, longitude) for latitude, longitude in geographic_points]
        if not mercator_points:
            return
        min_x = min(point[0] for point in mercator_points)
        max_x = max(point[0] for point in mercator_points)
        min_y = min(point[1] for point in mercator_points)
        max_y = max(point[1] for point in mercator_points)
        span_x = max_x - min_x
        span_y = max_y - min_y
        env.map_center_lat, env.map_center_lon = mercator_to_latlon(
            (min_x + max_x) / 2.0,
            (min_y + max_y) / 2.0,
        )
        mercator_scale = world_scale_factor(env.map_center_lat)
        env.initial_view_width_m = max(6_000.0, span_x * mercator_scale * 1.35)
        env.initial_view_height_m = max(4_200.0, span_y * mercator_scale * 1.35)
        env.map_configured = True
        env.map_layer = self.map_layer_var.get() if self.map_layer_var.get() in TILE_LAYERS else "Topographic"
        self._clear_terrain_grid()
        self.map_visible.set(True)
        self.map_tile_images.clear()
        self.map_tile_decoded.clear()
        self.map_tile_failures.clear()

        if old_configured:
            for node in self.scenario.nodes:
                latitude, longitude = node_geo[node.id]
                node.x, node.y = latlon_to_world(
                    latitude,
                    longitude,
                    env.map_center_lat,
                    env.map_center_lon,
                )
            by_id = {obstacle.id: obstacle for obstacle in self.scenario.obstacles}
            for obstacle_id, data in obstacle_geo.items():
                obstacle = by_id[obstacle_id]
                obstacle.x1, obstacle.y1 = latlon_to_world(
                    *data["corner1"],
                    env.map_center_lat,
                    env.map_center_lon,
                )
                obstacle.x2, obstacle.y2 = latlon_to_world(
                    *data["corner2"],
                    env.map_center_lat,
                    env.map_center_lon,
                )
                obstacle.points = [
                    list(
                        latlon_to_world(
                            *point,
                            env.map_center_lat,
                            env.map_center_lon,
                        )
                    )
                    for point in data["points"]
                ]

    def fit_live_nodes(self) -> None:
        positioned = [node for node in self.live_nodes.values() if node.has_position]
        if not positioned:
            messagebox.showinfo(
                "No shared positions",
                "The connected radio does not currently know a valid position for any listed node.",
                parent=self.root,
            )
            return
        self._reframe_live_area(positioned)
        self._apply_live_nodes(positioned, reframe=False)
        self.refresh_all()
        self.fit_view()
        self.status_var.set(f"Map fitted to {len(positioned)} live node positions")

    def _refresh_live_tree(self) -> None:
        self.live_nodes_tree.delete(*self.live_nodes_tree.get_children())
        now = int(time.time())
        for snapshot in sorted(self.live_nodes.values(), key=lambda item: (item.name.lower(), item.node_num)):
            if snapshot.last_heard:
                age_seconds = max(0, now - snapshot.last_heard)
                if age_seconds < 60:
                    heard = f"{age_seconds}s ago"
                elif age_seconds < 3600:
                    heard = f"{age_seconds // 60}m ago"
                elif age_seconds < 86_400:
                    heard = f"{age_seconds // 3600}h ago"
                else:
                    heard = f"{age_seconds // 86_400}d ago"
            else:
                heard = "—"
            if snapshot.has_position and snapshot.altitude_m is not None:
                position_status = f"Mapped · {self.format_distance(snapshot.altitude_m)} MSL"
            elif snapshot.has_position and snapshot.altitude_hae_m is not None:
                position_status = "Mapped · HAE only"
            elif snapshot.has_position:
                position_status = "Mapped · terrain altitude"
            else:
                position_status = "No position"
            values = (
                snapshot.name,
                f"!{snapshot.node_num:08x}",
                position_status,
                snapshot.hops_away if snapshot.hops_away is not None else "—",
                f"{snapshot.snr_db:.1f} dB" if snapshot.snr_db is not None else "—",
                heard,
            )
            tag = "mapped" if snapshot.has_position else "unlocated"
            self.live_nodes_tree.insert("", "end", iid=f"live-{snapshot.node_num}", values=values, tags=(tag,))
        self.live_nodes_tree.tag_configure("mapped", foreground="#dbe8f7")
        self.live_nodes_tree.tag_configure("unlocated", foreground="#8293a8")

    def _live_tree_select(self, _event: tk.Event) -> None:
        selection = self.live_nodes_tree.selection()
        if not selection:
            return
        try:
            node_num = int(selection[0].removeprefix("live-"))
        except ValueError:
            return
        plotted = next((node for node in self.scenario.nodes if node.node_num == node_num), None)
        if plotted is not None:
            self.select(plotted.id)

    def _build_canvas(self) -> None:
        self.canvas = tk.Canvas(self.canvas_panel, bg=BG, highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        search = tk.Frame(self.canvas, bg="#081321", highlightbackground=BORDER, highlightthickness=1)
        search.place(x=16, y=14)
        search.columnconfigure(0, weight=1)
        entry = ttk.Entry(search, textvariable=self.map_search_var, width=24)
        entry.grid(row=0, column=0, padx=7, pady=(7, 3), sticky="ew")
        entry.bind("<Return>", lambda _event: self.search_map())
        self.map_search_button = ttk.Button(search, text="Search map", command=self.search_map)
        self.map_search_button.grid(row=1, column=0, padx=7, pady=3, sticky="ew")
        self.map_canvas_toggle = tk.Checkbutton(
            search,
            text="Show map tiles",
            variable=self.map_visible,
            command=self._map_visibility_changed,
            bg="#081321",
            fg=TEXT,
            activebackground="#081321",
            activeforeground=TEXT,
            selectcolor="#168cd1",
            disabledforeground="#71839a",
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI Semibold", 9),
        )
        self.map_canvas_toggle.grid(row=2, column=0, padx=7, pady=(3, 1), sticky="w")
        self.terrain_only_toggle = tk.Checkbutton(
            search,
            text="Terrain only · hide streets, highways, and labels",
            variable=self.terrain_only_view,
            command=self._terrain_only_changed,
            bg="#081321",
            fg=TEXT,
            activebackground="#081321",
            activeforeground=TEXT,
            selectcolor="#168cd1",
            disabledforeground="#71839a",
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI Semibold", 9),
            justify="left",
            wraplength=205,
        )
        self.terrain_only_toggle.grid(row=3, column=0, padx=7, pady=(1, 4), sticky="w")
        self.osm_import_button = ttk.Button(search, text="Import obstacles", command=self.import_osm_obstacles)
        self.osm_import_button.grid(row=4, column=0, padx=7, pady=(3, 7), sticky="ew")
        self.obstacle_progress_frame = tk.Frame(search, bg="#081321")
        self.obstacle_progress_frame.grid(row=5, column=0, padx=7, pady=(0, 7), sticky="ew")
        self.obstacle_progress_frame.columnconfigure(0, weight=1)
        self.obstacle_progress_var = tk.StringVar(value="")
        ttk.Label(
            self.obstacle_progress_frame,
            textvariable=self.obstacle_progress_var,
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.obstacle_progress_bar = ttk.Progressbar(
            self.obstacle_progress_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            length=205,
        )
        self.obstacle_progress_bar.grid(row=1, column=0, sticky="ew")
        self.obstacle_progress_frame.grid_remove()
        self.canvas.bind("<Configure>", self._canvas_configured)
        self.canvas.bind("<ButtonPress-1>", self._canvas_down)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_up)
        self.canvas.bind("<ButtonPress-3>", self._pan_down)
        self.canvas.bind("<B3-Motion>", self._pan_drag)
        self.canvas.bind("<ButtonRelease-3>", lambda _e: self._pan_end())
        self.canvas.bind("<ButtonPress-2>", self._pan_down)
        self.canvas.bind("<B2-Motion>", self._pan_drag)
        self.canvas.bind("<ButtonRelease-2>", lambda _e: self._pan_end())
        self.canvas.bind("<MouseWheel>", self._canvas_wheel)
        self.canvas.bind("<Motion>", self._canvas_motion)

    def _poll_map_services(self) -> None:
        redraw = False
        terrain_arrived = False
        while True:
            try:
                key, result = self.map_service.tile_results.get_nowait()
            except queue.Empty:
                break
            if isinstance(result, Exception):
                self.map_tile_failures.add(key)
                self.status_var.set(f"Map tile unavailable: {result}")
            else:
                self.map_tile_bytes[key] = result
                if key[0] == "TerrainDEM":
                    terrain_arrived = True
                redraw = True
        if terrain_arrived:
            self._refresh_auto_node_elevations()
        while True:
            try:
                operation, result = self.geo_results.get_nowait()
            except queue.Empty:
                break
            if operation == "search":
                self._apply_map_search(result)
            elif operation == "terrain":
                self._apply_topography(result)
            elif operation == "terrain_error":
                request_id, error = result
                if request_id == self.terrain_request_id:
                    pending = self.pending_terrain_rf_refresh
                    if pending is not None and pending[0] == request_id:
                        self.pending_terrain_rf_refresh = None
                        self._refresh_active_rf_after_scene_change(
                            active_beacon_id=pending[1],
                            restart_live_mesh=pending[2],
                            restart_packet=pending[3],
                        )
                    self.status_var.set(f"Terrain loading failed: {error}")
                    messagebox.showerror("Terrain loading failed", str(error), parent=self.root)
            elif operation == "obstacles":
                self._apply_osm_obstacles(result)
            elif operation == "obstacle_progress":
                self._set_obstacle_progress(
                    str(result.get("text", "Importing obstacles…")),
                    float(result.get("value", 0.0)),
                    bool(result.get("indeterminate", False)),
                )
            elif operation == "error":
                source, error = result
                self.map_search_button.configure(state="normal")
                self.osm_import_button.configure(state="normal")
                if source == "Obstacle import":
                    self._hide_obstacle_progress()
                self.status_var.set(f"{source} failed: {error}")
                messagebox.showerror(f"{source} failed", str(error), parent=self.root)
        if redraw:
            self._invalidate_geographic_layer()
            self.schedule_render()
        try:
            self.root.after(100, self._poll_map_services)
        except tk.TclError:
            pass

    def _set_obstacle_progress(self, text: str, value: float, indeterminate: bool = False) -> None:
        self.obstacle_progress_frame.grid()
        self.obstacle_progress_var.set(text)
        self.obstacle_progress_bar.stop()
        if indeterminate:
            self.obstacle_progress_bar.configure(mode="indeterminate")
            self.obstacle_progress_bar.start(12)
        else:
            self.obstacle_progress_bar.configure(mode="determinate", maximum=100)
            self.obstacle_progress_bar["value"] = max(0.0, min(100.0, value))

    def _hide_obstacle_progress(self) -> None:
        self.obstacle_progress_bar.stop()
        self.obstacle_progress_bar.configure(mode="determinate")
        self.obstacle_progress_bar["value"] = 0
        self.obstacle_progress_var.set("")
        self.obstacle_progress_frame.grid_remove()

    def search_map(self) -> None:
        query_text = self.map_search_var.get().strip()
        if not query_text:
            return
        self.map_search_button.configure(state="disabled")
        self.status_var.set(f"Searching OpenStreetMap for {query_text}…")

        def worker() -> None:
            try:
                result = self.map_service.geocode(query_text)
                self.geo_results.put(("search", (query_text, result)))
            except Exception as error:
                self.geo_results.put(("error", ("Map search", error)))

        threading.Thread(target=worker, name="MapSearch", daemon=True).start()

    def _apply_map_search(self, result: tuple[str, dict[str, Any]]) -> None:
        query_text, location = result
        env = self.scenario.environment
        latitude = float(location["lat"])
        longitude = float(location["lon"])
        bounds = [float(value) for value in location.get("boundingbox", [])]
        if len(bounds) == 4:
            south, north, west, east = bounds
            west_x, south_y = latlon_to_mercator(south, west)
            east_x, north_y = latlon_to_mercator(north, east)
            mercator_scale = world_scale_factor(latitude)
            requested_width = abs(east_x - west_x) * mercator_scale * 1.25
            requested_height = abs(north_y - south_y) * mercator_scale * 1.25
        else:
            requested_width, requested_height = 10_000.0, 7_000.0
        env.initial_view_width_m = max(6_000.0, requested_width)
        env.initial_view_height_m = max(4_200.0, requested_height)
        env.map_center_lat = latitude
        env.map_center_lon = longitude
        env.map_configured = True
        self.map_visible.set(True)
        env.map_layer = self.map_layer_var.get() if self.map_layer_var.get() in TILE_LAYERS else "Topographic"
        self._clear_terrain_grid()
        self.map_tile_failures.clear()
        self.map_tile_images.clear()
        self.map_tile_decoded.clear()
        self.map_search_button.configure(state="normal")
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_all()
        self.fit_view()
        display_name = str(location.get("display_name", query_text))
        self.status_var.set(
            f"Map centered on {display_name} · initial view "
            f"{self.format_distance(env.initial_view_width_m)} × "
            f"{self.format_distance(env.initial_view_height_m)}"
        )
        self.load_topography()

    def _clear_terrain_grid(self) -> None:
        env = self.scenario.environment
        env.terrain_columns = 0
        env.terrain_rows = 0
        env.terrain_values = []
        env.terrain_source = ""
        env.terrain_left_m = 0.0
        env.terrain_top_m = 0.0
        env.terrain_width_m = 0.0
        env.terrain_height_m = 0.0
        self.terrain_visual_key = None
        self.terrain_visual_source = None

    def _terrain_request_bounds(self) -> tuple[float, float, float, float]:
        """Cover the viewport and RF objects without imposing a world boundary."""
        if hasattr(self, "canvas") and self.canvas.winfo_width() > 1 and self.canvas.winfo_height() > 1:
            first = self.screen_to_world(0, 0)
            second = self.screen_to_world(self.canvas.winfo_width(), self.canvas.winfo_height())
            left, right = sorted((first[0], second[0]))
            top, bottom = sorted((first[1], second[1]))
        else:
            env = self.scenario.environment
            left, top = -env.initial_view_width_m / 2.0, -env.initial_view_height_m / 2.0
            right, bottom = env.initial_view_width_m / 2.0, env.initial_view_height_m / 2.0
        scene_left, scene_top, scene_right, scene_bottom = self._scene_bounds()
        left, top = min(left, scene_left), min(top, scene_top)
        right, bottom = max(right, scene_right), max(bottom, scene_bottom)
        padding_x = max(100.0, (right - left) * 0.03)
        padding_y = max(100.0, (bottom - top) * 0.03)
        return left - padding_x, top - padding_y, right + padding_x, bottom + padding_y

    def _terrain_covers(self, x: float, y: float) -> bool:
        env = self.scenario.environment
        if not env.terrain_values:
            return False
        left, top, right, bottom = env.terrain_bounds()
        return left <= x <= right and top <= y <= bottom

    def load_topography(self) -> None:
        env = self.scenario.environment
        if not env.map_configured:
            messagebox.showinfo("Search first", "Search for a real-world location before loading terrain.", parent=self.root)
            return
        self.terrain_request_id += 1
        request_id = self.terrain_request_id
        self.status_var.set("Loading and sampling global terrain elevation…")
        left, top, right, bottom = self._terrain_request_bounds()
        center_lat, center_lon = world_to_latlon(
            (left + right) / 2.0,
            (top + bottom) / 2.0,
            env.map_center_lat,
            env.map_center_lon,
        )
        parameters = (
            env.map_center_lat,
            env.map_center_lon,
            left,
            top,
            right,
            bottom,
            center_lat,
            center_lon,
        )

        def worker() -> None:
            try:
                result = self.map_service.build_terrain_grid(
                    center_lat,
                    center_lon,
                    max(1.0, right - left),
                    max(1.0, bottom - top),
                )
                self.geo_results.put(("terrain", (request_id, parameters, result)))
            except Exception as error:
                self.geo_results.put(("terrain_error", (request_id, error)))

        threading.Thread(target=worker, name="TerrainLoader", daemon=True).start()

    def _load_startup_terrain(self) -> None:
        env = self.scenario.environment
        if env.map_configured and not env.terrain_values and not (
            self.simulation_thread and self.simulation_thread.is_alive()
        ):
            self.load_topography()

    def _cached_dem_elevation(self, x: float, y: float) -> float | None:
        """Return the most detailed cached DEM value for a world position."""
        env = self.scenario.environment
        if not env.map_configured:
            return None
        center_x, center_y = latlon_to_mercator(env.map_center_lat, env.map_center_lon)
        mercator_scale = world_scale_factor(env.map_center_lat)
        mercator_x = center_x + x / mercator_scale
        mercator_y = center_y - y / mercator_scale
        zooms = sorted(
            {key[1] for key in self.map_tile_bytes if key[0] == "TerrainDEM"},
            reverse=True,
        )
        for zoom in zooms:
            tile_x_float, tile_y_float = mercator_to_tile(mercator_x, mercator_y, zoom)
            raw_tile_x, tile_y = math.floor(tile_x_float), math.floor(tile_y_float)
            maximum = 2**zoom
            if tile_y < 0 or tile_y >= maximum:
                continue
            tile_x = raw_tile_x % maximum
            key = ("TerrainDEM", zoom, tile_x, tile_y)
            data = self.map_tile_bytes.get(key)
            if data is None:
                continue
            elevation_key = (zoom, tile_x, tile_y)
            elevations = self.terrain_tile_elevations.get(elevation_key)
            if elevations is None:
                try:
                    elevations = decode_terrarium_elevations(data)
                except Exception:
                    self.map_tile_failures.add(key)
                    continue
                self.terrain_tile_elevations[elevation_key] = elevations
            return sample_elevation_array(elevations, tile_x_float, tile_y_float)
        return None

    def _ground_elevation_at(self, x: float, y: float) -> float | None:
        exact = self._cached_dem_elevation(x, y)
        if exact is not None:
            return exact
        return self.scenario.environment.ground_elevation(x, y)

    @staticmethod
    def _validate_reported_altitude(node: Node, ground_elevation_m: float | None) -> bool:
        """Correct impossible absolute altitudes without discarding the node position."""
        was_usable = node.reported_altitude_usable
        old_status = node.reported_altitude_status
        reported = node.reported_altitude_m
        if reported is None or not math.isfinite(reported):
            node.reported_altitude_status = ""
        elif ground_elevation_m is None or not math.isfinite(ground_elevation_m):
            # Defer the physical check until terrain exists at this position.
            node.reported_altitude_usable = True
            node.reported_altitude_status = ""
        elif reported <= ground_elevation_m:
            below_m = ground_elevation_m - reported
            node.reported_altitude_usable = False
            node.reported_altitude_status = (
                f"Corrected upward: reported altitude was {below_m:.1f} m below local terrain. "
                "Placed at terrain elevation + antenna AGL; latitude/longitude unchanged."
            )
        else:
            node.reported_altitude_usable = True
            node.reported_altitude_status = ""
        return (
            was_usable != node.reported_altitude_usable
            or old_status != node.reported_altitude_status
        )

    def _set_auto_node_elevation(self, node: Node) -> bool:
        elevation = self._ground_elevation_at(node.x, node.y)
        if elevation is None or not math.isfinite(elevation):
            validation_ground = node.elevation_m if node.elevation_override else None
            return self._validate_reported_altitude(node, validation_ground)
        changed = self._validate_reported_altitude(node, elevation)
        if node.elevation_override:
            return changed
        if math.isclose(node.elevation_m, elevation, rel_tol=0.0, abs_tol=0.001):
            return changed
        node.elevation_m = elevation
        return True

    def _apply_live_node_elevation(self, node: Node) -> None:
        """Keep DEM ground and live absolute radio altitude as separate quantities."""
        if self._set_auto_node_elevation(node):
            return
        # If terrain has not loaded yet, retain a sensible ground estimate for
        # display. RF height still uses the reported absolute MSL altitude
        # directly through Node.antenna_z and therefore never double-counts AGL.
        if (
            not node.elevation_override
            and node.reported_altitude_m is not None
            and math.isfinite(node.reported_altitude_m)
            and self._ground_elevation_at(node.x, node.y) is None
        ):
            node.elevation_m = node.reported_altitude_m - node.antenna_height_m

    @staticmethod
    def _record_live_altitude(node: Node, snapshot: LiveNode) -> None:
        """Replace, rather than retain, live altitude metadata on every node update."""
        node.reported_altitude_m = snapshot.altitude_m
        node.reported_altitude_hae_m = snapshot.altitude_hae_m
        node.reported_altitude_source = snapshot.altitude_source
        node.reported_altitude_accuracy_m = snapshot.altitude_accuracy_m
        node.reported_altitude_usable = snapshot.altitude_m is not None
        node.reported_altitude_status = ""

    def _refresh_auto_node_elevations(self) -> None:
        changed = False
        for node in self.scenario.nodes:
            changed = self._set_auto_node_elevation(node) or changed
        if not changed:
            return
        self.mark_dirty()
        self._mark_results_stale()
        if isinstance(self.get_selected(), Node):
            self._build_object_form()

    def _apply_topography(
        self,
        payload: tuple[
            int,
            tuple[float, ...],
            tuple[int, int, list[float], int],
        ],
    ) -> None:
        request_id, parameters, result = payload
        env = self.scenario.environment
        if (
            request_id != self.terrain_request_id
            or parameters[0] != env.map_center_lat
            or parameters[1] != env.map_center_lon
        ):
            return
        pending = self.pending_terrain_rf_refresh
        if pending is not None and pending[0] == request_id:
            self.pending_terrain_rf_refresh = None
            active_beacon_id = self.beacon_node_id or pending[1]
            restart_live_mesh = self._live_mesh_running() or pending[2]
            restart_packet = pending[3]
        else:
            active_beacon_id = self.beacon_node_id
            restart_live_mesh = self._live_mesh_running()
            restart_packet = self._standalone_packet_active()
        columns, rows, values, zoom = result
        _map_lat, _map_lon, left, top, right, bottom, _center_lat, _center_lon = parameters
        env.terrain_columns = columns
        env.terrain_rows = rows
        env.terrain_values = values
        env.terrain_source = f"Mapzen/AWS Terrain Tiles z{zoom}"
        env.terrain_left_m = left
        env.terrain_top_m = top
        env.terrain_width_m = max(1.0, right - left)
        env.terrain_height_m = max(1.0, bottom - top)
        self.terrain_visual_key = None
        self.terrain_visual_source = None
        for node in self.scenario.nodes:
            self._set_auto_node_elevation(node)
        for obstacle in self.scenario.obstacles:
            x1, y1, x2, y2 = obstacle.normalized()
            elevation = env.ground_elevation((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            if elevation is not None:
                obstacle.base_elevation_m = elevation
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_all()
        self.status_var.set(
            f"Terrain loaded: {self.format_distance(min(values))}–"
            f"{self.format_distance(max(values))} elevation · "
            f"{columns}×{rows} elevation samples"
        )

        self._refresh_active_rf_after_scene_change(
            active_beacon_id=active_beacon_id,
            restart_live_mesh=restart_live_mesh,
            restart_packet=restart_packet,
        )

    def import_osm_obstacles(self) -> None:
        env = self.scenario.environment
        if not env.map_configured:
            messagebox.showinfo("Search first", "Search for a real-world location before importing obstacles.", parent=self.root)
            return
        left, top = self.screen_to_world(0, 0)
        right, bottom = self.screen_to_world(self.canvas.winfo_width(), self.canvas.winfo_height())
        left, right = min(left, right), max(left, right)
        top, bottom = min(top, bottom), max(top, bottom)
        full_width_m = max(1.0, right - left)
        full_height_m = max(1.0, bottom - top)
        full_area_m2 = full_width_m * full_height_m
        if full_area_m2 <= 0:
            return
        cap = OBSTACLE_IMPORT_MAX_AREA_M2

        # Cover the whole view by tiling it into complete <=cap imports and merging
        # them -- no dead spots and no "zoom in" prompt.  Bounded to a 3x3 grid so a
        # very wide view maps its central region rather than downloading endlessly.
        if full_area_m2 <= cap:
            tiles = [(left, top, right, bottom)]
        else:
            tile_side = math.sqrt(cap)
            max_axis = 3
            columns = min(max_axis, max(1, math.ceil(full_width_m / tile_side)))
            rows_axis = min(max_axis, max(1, math.ceil(full_height_m / tile_side)))
            covered_width = min(full_width_m, columns * tile_side)
            covered_height = min(full_height_m, rows_axis * tile_side)
            center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
            base_left = center_x - covered_width / 2.0
            base_top = center_y - covered_height / 2.0
            tile_w = covered_width / columns
            tile_h = covered_height / rows_axis
            tiles = [
                (base_left + i * tile_w, base_top + j * tile_h,
                 base_left + (i + 1) * tile_w, base_top + (j + 1) * tile_h)
                for i in range(columns)
                for j in range(rows_axis)
            ]

        covered_left = min(t[0] for t in tiles)
        covered_top = min(t[1] for t in tiles)
        covered_right = max(t[2] for t in tiles)
        covered_bottom = max(t[3] for t in tiles)
        covered_area_m2 = (covered_right - covered_left) * (covered_bottom - covered_top)

        # Each tile is a complete import with its own query plan and budget.
        tile_jobs: list[tuple[float, float, float, float, int, int, int]] = []
        total_building_limit = 0
        for tile_left, tile_top, tile_right, tile_bottom in tiles:
            _columns_c, _rows_c, tile_limit, _sampled = obstacle_import_plan(
                max(1.0, tile_right - tile_left), max(1.0, tile_bottom - tile_top)
            )
            total_building_limit += tile_limit
            north, west = world_to_latlon(tile_left, tile_top, env.map_center_lat, env.map_center_lon)
            south, east = world_to_latlon(tile_right, tile_bottom, env.map_center_lat, env.map_center_lon)
            # Start with one complete request per top-level tile. Overture returns
            # every footprint below tile_limit; only a genuinely saturated tile
            # needs adaptive subdivision. The former unconditional 2x2 split made
            # a normal nine-tile import perform 36 remote requests.
            tile_jobs.append((south, west, north, east, 1, 1, tile_limit))

        forest_north, forest_west = world_to_latlon(covered_left, covered_top, env.map_center_lat, env.map_center_lon)
        forest_south, forest_east = world_to_latlon(covered_right, covered_bottom, env.map_center_lat, env.map_center_lon)

        self.osm_import_button.configure(state="disabled")
        self._set_obstacle_progress("Preparing geographic cells…", 2.0)
        if len(tile_jobs) > 1:
            self.status_var.set(
                f"Importing {self.format_area(covered_area_m2)} across {len(tile_jobs)} tiles…"
            )
        else:
            self.status_var.set(f"Importing the full visible {self.format_area(covered_area_m2)}…")

        def worker() -> None:
            try:
                warnings: list[str] = []
                combined: dict[str, dict[str, Any]] = {}
                building_source = "Overture"
                total = len(tile_jobs)
                lock = threading.Lock()
                completed = [0]

                def fetch_forests() -> list[dict[str, Any]]:
                    return self.map_service.fetch_osm_forests(
                        forest_south, forest_west, forest_north, forest_east
                    )

                def fetch_tile(job: tuple[float, float, float, float, int, int, int]):
                    south, west, north, east, columns_c, rows_c, tile_limit = job
                    source = "Overture"
                    try:
                        elements = self.map_service.fetch_overture_buildings_for_viewport(
                            south, west, north, east,
                            limit=tile_limit, columns=columns_c, rows=rows_c,
                            query_workers=TILE_ADAPTIVE_QUERY_CONCURRENCY,
                        )
                    except Exception as overture_error:
                        try:
                            elements = self.map_service.fetch_osm_obstacles(south, west, north, east)
                            source = "OSM fallback"
                            with lock:
                                warnings.append(f"Overture unavailable for a tile: {overture_error}")
                        except Exception as osm_error:
                            with lock:
                                warnings.append(f"A tile failed to import: {osm_error}")
                            elements = []
                    with lock:
                        completed[0] += 1
                        self.geo_results.put(
                            (
                                "obstacle_progress",
                                {
                                    "value": 5.0 + 78.0 * completed[0] / max(1, total),
                                    "text": (
                                        f"Loaded tile {completed[0]}/{total} · "
                                        f"{len(elements):,} buildings"
                                    ),
                                },
                            )
                        )
                    return elements, source

                # Forests use a separate provider and used to begin only after every
                # building query completed. Start that request beside the bounded
                # building pool so its latency is normally hidden.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as forest_executor:
                    forest_future = forest_executor.submit(fetch_forests)
                    # Each tile fans its cells across a small pool; the outer limit
                    # keeps aggregate remote work bounded.
                    if total <= 1:
                        tile_results = [fetch_tile(job) for job in tile_jobs]
                    else:
                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(TILE_IMPORT_CONCURRENCY, total)
                        ) as executor:
                            tile_results = list(executor.map(fetch_tile, tile_jobs))
                    try:
                        forests = forest_future.result()
                    except Exception as forest_error:
                        forests = []
                        warnings.append(f"OSM forests unavailable: {forest_error}")
                for elements, source in tile_results:
                    if source == "OSM fallback":
                        building_source = "OSM fallback"
                    for element in elements:
                        key = f"{element.get('type', 'overture')}/{element.get('id', '')}"
                        combined.setdefault(key, element)

                self.geo_results.put(
                    (
                        "obstacles",
                        {
                            "elements": list(combined.values()) + forests,
                            "building_source": building_source,
                            "warnings": warnings,
                            # Keep every merged building -- the per-tile caps already
                            # bounded the totals.
                            "building_limit": max(total_building_limit, len(combined)),
                        },
                    )
                )
            except Exception as error:
                self.geo_results.put(("error", ("Obstacle import", error)))

        threading.Thread(target=worker, name="ObstacleImport", daemon=True).start()

    # A footprint with no real height or floor count anywhere in the source
    # dataset gets this instead of the generic OBSTACLE_DEFAULTS height (12 m,
    # ~4 storeys) -- a single unverified building silently modeled as a
    # 4-storey structure was enough on its own to mark an otherwise-clear
    # long link as blocked. 6 m (~2 storeys) is a more typical guess for an
    # arbitrary building when nothing in the data says otherwise.
    OSM_BUILDING_HEIGHT_FALLBACK_M = 6.0

    @staticmethod
    def _osm_height(tags: dict[str, str], default: float) -> float:
        raw_height = tags.get("height", "").strip().lower()
        try:
            if raw_height.endswith("ft"):
                return max(1.0, float(raw_height[:-2].strip()) * 0.3048)
            if raw_height.endswith("m"):
                raw_height = raw_height[:-1].strip()
            if raw_height:
                return max(1.0, float(raw_height.replace(",", ".")))
        except ValueError:
            pass
        try:
            return max(1.0, float(tags.get("building:levels", "")) * 3.0)
        except ValueError:
            return default

    def _apply_osm_obstacles(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        if isinstance(payload, dict):
            elements = list(payload.get("elements", []))
            building_source = str(payload.get("building_source", "Overture"))
            warnings = [str(value) for value in payload.get("warnings", [])]
            building_limit = int(payload.get("building_limit", OVERTURE_VIEWPORT_BUILDING_LIMIT))
        else:
            elements = payload
            building_source = "OSM"
            warnings = []
            building_limit = OVERTURE_VIEWPORT_BUILDING_LIMIT
        env = self.scenario.environment
        active_beacon_id = self.beacon_node_id
        restart_live_mesh = self._live_mesh_running()
        restart_packet = self._standalone_packet_active()
        existing = {obstacle.osm_id for obstacle in self.scenario.obstacles if obstacle.osm_id}
        added = 0
        added_buildings = 0
        added_forests = 0
        skipped = 0
        imported_need_terrain = False
        last_progress_update = 0.0
        self._set_obstacle_progress(f"Adding {len(elements):,} obstacle shapes…", 88.0)
        for element_index, element in enumerate(elements, start=1):
            now = time.perf_counter()
            if (
                element_index == 1
                or element_index == len(elements)
                or now - last_progress_update >= 0.1
            ):
                percent = 88.0 + 11.0 * element_index / max(1, len(elements))
                self._set_obstacle_progress(
                    f"Adding obstacle shapes · {element_index:,}/{len(elements):,}",
                    percent,
                )
                self.root.update_idletasks()
                last_progress_update = now
            osm_id = f"{element.get('type', 'way')}/{element.get('id', '')}"
            if osm_id in existing:
                continue
            tags = element.get("tags", {})
            geometry = element.get("geometry", [])
            points = [
                list(
                    latlon_to_world(
                        float(point["lat"]),
                        float(point["lon"]),
                        env.map_center_lat,
                        env.map_center_lon,
                    )
                )
                for point in geometry
                if "lat" in point and "lon" in point
            ]
            if len(points) < 3:
                skipped += 1
                continue
            is_building = "building" in tags
            is_forest = tags.get("landuse") == "forest" or tags.get("natural") == "wood"
            if not is_building and not is_forest:
                continue
            if is_building and added_buildings >= building_limit:
                skipped += 1
                continue
            if is_forest and added_forests >= 500:
                skipped += 1
                continue
            kind = "Building" if is_building else "Forest"
            color, attenuation, default_height, per_100, behavior, max_beyond = OBSTACLE_DEFAULTS[kind]
            if is_building:
                default_height = self.OSM_BUILDING_HEIGHT_FALLBACK_M
            x_values = [point[0] for point in points]
            y_values = [point[1] for point in points]
            center_x = sum(x_values) / len(x_values)
            center_y = sum(y_values) / len(y_values)
            base_elevation = env.terrain_elevation(center_x, center_y) or 0.0
            if not imported_need_terrain and not self._terrain_covers(center_x, center_y):
                imported_need_terrain = True
            feature_source = "Overture" if element.get("type") == "overture" else "OSM"
            name = tags.get("name") or f"{feature_source} {kind.lower()} {element.get('id', '')}"
            obstacle = Obstacle(
                name=name,
                kind=kind,
                x1=min(x_values),
                y1=min(y_values),
                x2=max(x_values),
                y2=max(y_values),
                height_m=self._osm_height(tags, default_height),
                base_elevation_m=base_elevation,
                attenuation_db=attenuation,
                loss_per_100m_db=per_100,
                behavior=behavior,
                max_range_beyond_m=max_beyond,
                shape="polygon",
                points=points,
                osm_id=osm_id,
                color=color,
            )
            self.scenario.obstacles.append(obstacle)
            existing.add(osm_id)
            added += 1
            if kind == "Building":
                added_buildings += 1
            else:
                added_forests += 1
        self.osm_import_button.configure(state="normal")
        if added:
            if imported_need_terrain:
                # Do not publish a temporary beacon/link profile with zero or
                # partial obstacle elevations. Resume every active RF engine only
                # after the expanded terrain grid has finalized the same scene a
                # newly dropped beacon would use.
                self._suspend_active_rf_for_scene_change(
                    active_beacon_id=active_beacon_id,
                    stop_live_mesh=restart_live_mesh,
                )
            self.selected_id = None
            self.mark_dirty()
            self._mark_results_stale()
            self._refresh_scene_change(geographic=True)
        suffix = f" · {skipped} skipped/capped" if skipped else ""
        warning_suffix = f" · {'; '.join(warnings)}" if warnings else ""
        self.status_var.set(
            f"Imported {added_buildings} {building_source} buildings and "
            f"{added_forests} OSM forests{suffix}{warning_suffix}"
        )
        self._hide_obstacle_progress()
        if added:
            if imported_need_terrain:
                self.load_topography()
                self.pending_terrain_rf_refresh = (
                    self.terrain_request_id,
                    active_beacon_id,
                    restart_live_mesh,
                    restart_packet,
                )
                self.status_var.set("Obstacles loaded · finalizing terrain before RF recalculation…")
            else:
                self._refresh_active_rf_after_scene_change(
                    active_beacon_id=active_beacon_id,
                    restart_live_mesh=restart_live_mesh,
                    restart_packet=restart_packet,
                )

    def _build_results(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=10, pady=(7, 3))
        ttk.Label(top, text="SIMULATION RESULTS", style="Section.TLabel").pack(side="left")
        self.result_status = ttk.Label(top, text="No packet sent", style="Muted.TLabel")
        self.result_status.pack(side="left", padx=12)
        ttk.Button(top, text="Replay", command=self.replay_animation).pack(side="right", padx=3)
        ttk.Button(top, text="Clear", command=self.clear_results).pack(side="right", padx=3)

        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        metrics = ttk.Frame(body)
        metrics.pack(fill="x", pady=(0, 5))
        self.metric_vars: dict[str, tk.StringVar] = {}
        for index, (key, title) in enumerate([
            ("reached", "Nodes reached"),
            ("tx", "Transmissions"),
            ("range", "Farthest reach"),
            ("airtime", "Mesh airtime"),
        ]):
            card = tk.Frame(metrics, bg="#101f32", highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=2, pady=2)
            metrics.columnconfigure(index % 2, weight=1)
            tk.Label(card, text=title, bg="#101f32", fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(
                fill="x", padx=9, pady=(5, 0)
            )
            var = tk.StringVar(value="—")
            self.metric_vars[key] = var
            tk.Label(card, textvariable=var, bg="#101f32", fg=TEXT, font=("Segoe UI Semibold", 13), anchor="w").pack(
                fill="x", padx=9, pady=(0, 5)
            )

        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        self.results_notebook = notebook
        packet_frame = ttk.Frame(notebook)
        details_bar = ttk.Frame(packet_frame)
        details_bar.pack(fill="x", padx=5, pady=(5, 2))
        ttk.Label(details_bar, text="Show", style="Muted.TLabel").pack(side="left", padx=(2, 5))
        self.packet_results_view = tk.StringVar(value="Timeline")
        view_picker = ttk.Combobox(
            details_bar,
            textvariable=self.packet_results_view,
            values=["Timeline", "Delivery", "Links"],
            state="readonly",
            width=13,
        )
        view_picker.pack(side="left")
        view_picker.bind("<<ComboboxSelected>>", self._select_packet_results_view)
        details_stack = ttk.Frame(packet_frame)
        details_stack.pack(fill="both", expand=True)
        events_frame = ttk.Frame(details_stack)
        nodes_frame = ttk.Frame(details_stack)
        links_frame = ttk.Frame(details_stack)
        self.packet_result_frames = {
            "Timeline": events_frame,
            "Delivery": nodes_frame,
            "Links": links_frame,
        }
        live_frame = ttk.Frame(notebook)
        self.live_results_frame = live_frame
        notebook.add(packet_frame, text="Packet results")
        notebook.add(live_frame, text="Live traffic")

        self.events_tree = self._tree(
            events_frame,
            [
                ("time", "Time", 76),
                ("event", "Event", 90),
                ("node", "Node", 140),
                ("peer", "From / via", 140),
                ("hop", "Hop", 45),
                ("rssi", "RSSI", 65),
                ("snr", "SNR", 60),
                ("margin", "Margin", 65),
                ("detail", "Detail", 300),
            ],
        )
        self.nodes_tree = self._tree(
            nodes_frame,
            [
                ("node", "Node", 180),
                ("role", "Role", 120),
                ("status", "Status", 95),
                ("time", "Arrival", 75),
                ("hop", "Hop", 45),
                ("via", "Via", 150),
                ("rssi", "RSSI", 70),
                ("margin", "Margin", 75),
            ],
        )
        self.links_tree = self._tree(
            links_frame,
            [
                ("source", "Transmitter", 145),
                ("target", "Receiver", 145),
                ("distance", "Distance", 85),
                ("rssi", "RSSI", 70),
                ("snr", "SNR", 65),
                ("margin", "Margin", 70),
                ("chance", "Chance", 65),
                ("obstacles", "Obstruction loss", 125),
                ("reason", "Compatibility", 180),
            ],
        )
        self._select_packet_results_view()
        self.live_summary_var = tk.StringVar(value="Start Live mesh traffic to model background packets.")
        ttk.Label(live_frame, textvariable=self.live_summary_var, style="Muted.TLabel", wraplength=350, justify="left").pack(
            anchor="w", padx=8, pady=(7, 4)
        )
        live_tests_frame = ttk.LabelFrame(live_frame, text="Injected tests")
        live_tests_frame.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        live_detail_frame = ttk.LabelFrame(live_frame, text="Selected test: why / event detail")
        live_detail_frame.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.live_tests_tree = self._tree(
            live_tests_frame,
            [
                ("id", "Test", 48), ("status", "Result", 150), ("source", "Source", 105),
                ("destination", "Destination", 105), ("reached", "Received", 70), ("tx", "TX", 50),
                ("collisions", "Collisions", 70), ("drops", "RF drops", 65),
            ],
        )
        self.live_tests_tree.bind("<<TreeviewSelect>>", self._live_test_selected)
        self.live_detail_tree = self._tree(
            live_detail_frame,
            [
                ("time", "Time", 65), ("event", "Outcome", 95), ("node", "Node", 115),
                ("via", "Via", 110), ("hop", "Hop", 42), ("rssi", "RSSI", 60),
                ("margin", "Margin", 65), ("detail", "Why", 350),
            ],
        )

    def _tree(self, parent: ttk.Frame, columns: list[tuple[str, str, int]]) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[column[0] for column in columns], show="headings")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=40, stretch=key in {"detail", "reason", "obstacles"})
        return tree

    def _select_packet_results_view(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "packet_result_frames"):
            return
        selected = self.packet_results_view.get()
        for name, frame in self.packet_result_frames.items():
            frame.pack_forget()
            if name == selected:
                frame.pack(fill="both", expand=True)

    def _refresh_live_results(self) -> None:
        if not hasattr(self, "live_tests_tree"):
            return
        now = time.monotonic()
        if now - self.live_results_last_refresh < 0.25:
            return
        self.live_results_last_refresh = now
        snapshot = self.live_mesh_snapshot
        if snapshot:
            self.live_summary_var.set(
                f"Live background: {snapshot.get('transmissions', 0):,} TX · "
                f"{snapshot.get('collisions', 0):,} collisions · {snapshot.get('dropped', 0):,} RF drops · "
                f"{snapshot.get('throttled', 0):,} channel-gated · peak {snapshot.get('peak', 0.0):.1f}%"
            )
        # Rebuilding this small summary table is cheap, but rebuilding the
        # selected test's full event log and map overlay on every 250 ms live
        # update is not.  Preserve the selected test and only refresh its
        # detail/map when its result has actually changed.
        selected = self.live_tests_tree.selection()
        selected_item = selected[0] if selected else ""
        self.live_tests_tree.delete(*self.live_tests_tree.get_children())
        names = {node.id: node.name for node in self.scenario.nodes}
        visible_tests = [
            (test_id, test)
            for test_id, test in sorted(self.live_mesh_tests.items(), reverse=True)
            if test_id not in self.live_mesh_hidden_test_ids
        ]
        for test_id, test in visible_tests:
            self.live_tests_tree.insert(
                "", "end", iid=f"live-test-{test_id}", values=(
                    f"#{test_id}", test.status, names.get(test.packet.source_id, test.packet.source_id),
                    "BROADCAST" if test.packet.destination_id == "BROADCAST" else names.get(test.packet.destination_id, test.packet.destination_id),
                    max(0, len(test.reached) - 1), test.transmissions, test.collisions, test.dropped,
                )
            )
        children = self.live_tests_tree.get_children()
        if not children:
            self.live_test_display_signature = None
            return

        if not selected_item or not self.live_tests_tree.exists(selected_item):
            preferred = (
                f"live-test-{self.live_path_test_id}"
                if self.live_path_test_id is not None else ""
            )
            selected_item = preferred if preferred and self.live_tests_tree.exists(preferred) else children[0]
        self.live_tests_tree.selection_set(selected_item)
        try:
            selected_test_id = int(str(selected_item).rsplit("-", 1)[-1])
        except ValueError:
            return
        selected_test = self.live_mesh_tests.get(selected_test_id)
        if selected_test is not None and self._live_test_signature(selected_test) != self.live_test_display_signature:
            self._live_test_selected()

    def _live_test_selected(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "live_detail_tree"):
            return
        self.live_detail_tree.delete(*self.live_detail_tree.get_children())
        selected = self.live_tests_tree.selection() if hasattr(self, "live_tests_tree") else ()
        if not selected:
            return
        try:
            test_id = int(str(selected[0]).rsplit("-", 1)[-1])
        except ValueError:
            return
        test = self.live_mesh_tests.get(test_id)
        if test is None:
            return
        # _refresh_live_results rebuilds the compact summary table.  Tk emits
        # <<TreeviewSelect>> when that code restores the same selection, so
        # do not rebuild hundreds of canvas labels and detail rows unless the
        # user picked another test or this test actually received new data.
        signature = self._live_test_signature(test)
        if test_id == self.live_path_test_id and signature == self.live_test_display_signature:
            return
        self.live_path_test_id = test_id
        self._present_live_test_as_packet_result(test)
        names = {node.id: node.name for node in self.scenario.nodes}
        events = test.events
        if len(events) > 500:
            events = [*events[:100], *events[-400:]]
            self.live_detail_tree.insert(
                "", "end", values=("…", "DETAIL CAPPED", "", "", "", "", "", f"Showing 500 of {len(test.events):,} events"),
            )
        for event in events:
            self.live_detail_tree.insert(
                "", "end", values=(
                    f"{event.time_ms / 1000:.1f}s", event.kind, names.get(event.node_id, event.node_id),
                    names.get(event.peer_id, event.peer_id), event.hop,
                    f"{event.rssi_dbm:.1f}" if event.rssi_dbm else "—",
                    f"{event.margin_db:.1f}" if event.rssi_dbm else "—", event.detail,
                )
            )
        self.live_test_display_signature = signature
        # The geographic map and obstacle raster have not changed.  Redraw
        # just the packet/node layers; composing tiles again here made an
        # in-flight dense test stall the UI every live refresh.
        self._render_simulation_layers()

    @staticmethod
    def _live_test_signature(test: LiveMeshTestResult) -> tuple[Any, ...]:
        return (
            test.test_id, test.status, len(test.events), len(test.reached), test.transmissions,
            test.receptions, test.collisions, test.dropped, test.complete, test.routing_mode,
            tuple(test.learned_route), test.acknowledged,
        )

    def _present_live_test_as_packet_result(self, test: LiveMeshTestResult) -> None:
        """Use the established packet renderer for a live-injected message."""
        result = SimulationResult(
            reached={node_id: dict(arrival) for node_id, arrival in test.reached.items()},
            transmissions=test.transmissions,
            receptions=test.receptions,
            decoded=test.receptions,
            collisions=test.collisions,
            dropped=test.dropped,
            routing_mode=test.routing_mode,
            route_key=test.route_key,
            learned_route=list(test.learned_route),
            invalidated_route_key=test.invalidated_route_key,
            acknowledged=test.acknowledged,
        )
        source = next((node for node in self.scenario.nodes if node.id == test.packet.source_id), None)
        # The live engine retains every duplicate/collision for accounting, but
        # the map should match the normal simulator: one first-arrival edge per
        # reached node.  This stays responsive for dense, high-hop floods.
        for node_id, arrival in result.reached.items():
            via_id = str(arrival.get("via", ""))
            if not via_id:
                continue
            result.events.append(SimEvent(
                float(arrival.get("time_ms", 0.0)), "RX", node_id, via_id,
                int(arrival.get("hop", 0)), detail="first live arrival",
            ))
        if source is not None:
            result.max_distance_m = max(
                (math.hypot(node.x - source.x, node.y - source.y) for node in self.scenario.nodes if node.id in result.reached),
                default=0.0,
            )
        result.duration_ms = max((event.time_ms for event in result.events), default=0.0)
        self.last_result = result
        self.animation_seen_edges = [
            (event.peer_id, event.node_id, event.kind, event.hop)
            for event in result.events
            if event.kind == "RX" and event.peer_id
        ]
        self.animation_revealed_nodes = set(result.reached)
        self.results_populated = False
        self.results_stale = False
        self._update_result_metrics()
        if hasattr(self, "result_status"):
            self.result_status.configure(text=f"Live test #{test.test_id}: {test.status}")
        if hasattr(self, "clear_hops_button"):
            self.clear_hops_button.configure(state="normal")

    def _append_live_event_log(self, frame: LiveMeshFrame | None = None, detail: str = "") -> None:
        """Mirror bounded live activity into the familiar Event timeline."""
        if not hasattr(self, "events_tree"):
            return
        names = {node.id: node.name for node in self.scenario.nodes}
        time_text = (
            f"{frame.time_ms / 1000:.1f}s" if frame is not None else
            f"{float(self.live_mesh_snapshot.get('time_ms', 0.0)) / 1000:.1f}s"
        )
        rows: list[tuple[str, str, str, int, str]] = []
        if detail:
            rows.append(("LIVE", "", "", 0, detail))
        if frame is not None:
            rows.extend(("LIVE TX", source_id, "", 0, kind.title()) for source_id, kind in frame.transmitters)
            rows.extend(
                ("LIVE RX", target_id, source_id, hop, f"{kind.title()} received")
                for source_id, target_id, kind, hop in frame.receptions
            )
            rows.extend(("COLLISION", node_id, "", 0, "overlapping live traffic") for node_id in frame.collisions)
            rows.extend(("CHANNEL GATED", node_id, "", 0, "local channel utilization gate") for node_id in frame.throttled)
        for event, node_id, peer_id, hop, reason in rows:
            self.events_tree.insert(
                "", "end",
                values=(time_text, event, names.get(node_id, node_id), names.get(peer_id, peer_id), hop, "—", "—", "—", reason),
            )
        children = self.events_tree.get_children()
        for item in children[:-500]:
            self.events_tree.delete(item)
        if rows:
            self.events_tree.see(self.events_tree.get_children()[-1])

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-n>", lambda _e: self.new_scenario())
        self.root.bind("<Control-o>", lambda _e: self.open_scenario())
        self.root.bind("<Control-s>", lambda _e: self.save_scenario())
        self.root.bind("<Control-Shift-S>", lambda _e: self.save_scenario_as())
        self.root.bind("<Control-d>", lambda _e: self.duplicate_selected())
        self.root.bind("<Delete>", lambda _e: self.delete_selected())
        self.root.bind("<Control-Return>", lambda _e: self.run_simulation())
        self.root.bind("<Key-f>", lambda _e: self.fit_view())
        self.root.bind("<Escape>", lambda _e: self.set_tool("select"))
        self.root.bind("<Key-n>", lambda _e: self.set_tool("node"))
        self.root.bind("<Key-b>", lambda _e: self.set_tool("beacon"))

    def _form_header(self, parent: ttk.Frame, title: str, subtitle: str = "") -> None:
        ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 2))
        if subtitle:
            ttk.Label(parent, text=subtitle, style="Muted.TLabel", wraplength=300, justify="left").pack(
                anchor="w", padx=12, pady=(0, 8)
            )

    def _section(self, parent: ttk.Frame, title: str) -> ttk.Frame:
        ttk.Label(parent, text=title.upper(), style="Section.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        frame = ttk.Frame(parent)
        frame.pack(fill="x", padx=12)
        frame.columnconfigure(1, weight=1)
        return frame

    def _field(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.Variable,
        values: list[str] | None = None,
        width: int = 14,
    ) -> tk.Widget:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        if values is None:
            widget: tk.Widget = ttk.Entry(parent, textvariable=variable, width=width)
        else:
            widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=width)
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        return widget

    def _check(self, parent: ttk.Frame, row: int, label: str, variable: tk.Variable) -> None:
        ttk.Checkbutton(parent, text=label, variable=variable).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)

    def _clear_frame(self, frame: ttk.Frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _build_object_form(self) -> None:
        body = self.object_scroll.body
        self._clear_frame(body)
        self.object_vars = {}
        self._set_object_form_clean()
        obj = self.get_selected()
        if obj is None:
            self._form_header(body, "Nothing selected", "Select a node or obstruction on the map to edit all of its settings.")
            ttk.Button(body, text="Add a node", style="Accent.TButton", command=lambda: self.set_tool("node")).pack(
                anchor="w", padx=12, pady=8
            )
            return
        if isinstance(obj, Node):
            self._build_node_form(body, obj)
        else:
            self._build_obstacle_form(body, obj)
        self._watch_object_form_changes()

    def _watch_object_form_changes(self) -> None:
        for key, variable in self.object_vars.items():
            if key != "power_summary":
                variable.trace_add("write", self._object_form_value_changed)

    def _object_form_value_changed(self, *_args: object) -> None:
        if not self.object_form_dirty and self.get_selected() is not None:
            self.object_form_dirty = True
            self.object_apply_bar.grid()

    def _set_object_form_clean(self) -> None:
        self.object_form_dirty = False
        apply_bar = getattr(self, "object_apply_bar", None)
        if apply_bar is not None:
            apply_bar.grid_remove()

    def _build_node_form(self, body: ttk.Frame, node: Node) -> None:
        self._form_header(body, node.name, f"Node !{node.node_num:08x} · {node.role}")
        env = self.scenario.environment
        if env.map_configured:
            latitude, longitude = world_to_latlon(node.x, node.y, env.map_center_lat, env.map_center_lon)
        else:
            latitude, longitude = 0.0, 0.0
        values: dict[str, Any] = {
            "name": node.name,
            "node_num": f"{node.node_num:08x}",
            "role": node.role,
            "rebroadcast_mode": node.rebroadcast_mode,
            "online": node.online,
            "favorite": node.favorite,
            "x": self._display_length(node.x),
            "y": self._display_length(node.y),
            "elevation_m": self._display_length(node.elevation_m),
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "elevation_override": node.elevation_override,
            "antenna_height_m": self._display_length(node.antenna_height_m),
            "use_live_altitude": node.use_live_altitude,
            "power_profile": node.power_profile,
            "tx_power_dbm": node.tx_power_dbm,
            "antenna_gain_dbi": node.antenna_gain_dbi,
            "cable_loss_db": node.cable_loss_db,
            "noise_figure_db": node.noise_figure_db,
            "region": node.radio.region,
            "preset": node.radio.preset,
            "frequency_mhz": node.radio.frequency_mhz,
            "bandwidth_khz": node.radio.bandwidth_khz,
            "spreading_factor": node.radio.spreading_factor,
            "coding_rate": node.radio.coding_rate,
            "channel": node.channel,
            "notes": node.notes,
        }
        for key, value in values.items():
            self.object_vars[key] = tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value))

        section = self._section(body, "Identity & behavior")
        self._field(section, 0, "Name", self.object_vars["name"])
        self._field(section, 1, "Node number (hex)", self.object_vars["node_num"])
        self._field(section, 2, "Firmware role", self.object_vars["role"], ROLES)
        self._field(section, 3, "Rebroadcast", self.object_vars["rebroadcast_mode"], REBROADCAST_MODES)
        self._field(section, 4, "Channel / PSK name", self.object_vars["channel"])
        self._check(section, 5, "Online / powered", self.object_vars["online"])
        self._check(section, 6, "Favorite node", self.object_vars["favorite"])

        section = self._section(body, "Position & installation height")
        length_unit = self._length_unit()
        if env.map_configured:
            self._field(section, 0, "Latitude", self.object_vars["latitude"])
            self._field(section, 1, "Longitude", self.object_vars["longitude"])
        else:
            self._field(section, 0, f"X ({length_unit})", self.object_vars["x"])
            self._field(section, 1, f"Y ({length_unit})", self.object_vars["y"])
        self._field(section, 2, f"Terrain elevation MSL ({length_unit})", self.object_vars["elevation_m"])
        self._check(section, 3, "Manually override terrain elevation", self.object_vars["elevation_override"])
        self._field(
            section,
            4,
            f"Installation height AGL ({length_unit})",
            self.object_vars["antenna_height_m"],
        )
        self._check(
            section,
            5,
            "Use valid reported radio altitude when available",
            self.object_vars["use_live_altitude"],
        )
        ttk.Label(
            section,
            text="AGL is the antenna's height above the local terrain elevation.",
            style="Muted.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 4))
        if node.reported_altitude_m is not None:
            accuracy = (
                f" · estimated ±{self.format_distance(node.reported_altitude_accuracy_m)}"
                if node.reported_altitude_accuracy_m is not None
                else ""
            )
            status = (
                f"\n{node.reported_altitude_status}"
                if node.reported_altitude_status
                else ""
            )
            ttk.Label(
                section,
                text=(
                    f"Radio altitude {self.format_distance(node.reported_altitude_m)} MSL"
                    f" · {node.reported_altitude_source or 'source unknown'}{accuracy}{status}"
                ),
                style="Muted.TLabel",
                wraplength=285,
                justify="left",
            ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 4))
        elif node.reported_altitude_hae_m is not None:
            ttk.Label(
                section,
                text=(
                    f"Radio supplied HAE {self.format_distance(node.reported_altitude_hae_m)}, "
                    "but no geoidal separation; using terrain + antenna AGL."
                ),
                style="Muted.TLabel",
                wraplength=285,
                justify="left",
            ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 4))

        section = self._section(body, "LoRa modem")
        region_widget = self._field(section, 0, "Regulatory region", self.object_vars["region"], list(REGION_BANDS))
        region_widget.bind("<<ComboboxSelected>>", self._region_preview)
        preset_widget = self._field(
            section,
            1,
            "Firmware preset",
            self.object_vars["preset"],
            list(region_preset_options(node.radio.region)),
        )
        self.object_preset_widget = preset_widget
        preset_widget.bind("<<ComboboxSelected>>", self._preset_preview)
        self._field(section, 2, "Frequency (MHz)", self.object_vars["frequency_mhz"])
        self._field(section, 3, "Bandwidth (kHz)", self.object_vars["bandwidth_khz"])
        self._field(section, 4, "Spreading factor", self.object_vars["spreading_factor"])
        self._field(section, 5, "Coding rate 4/", self.object_vars["coding_rate"])

        section = self._section(body, "RF chain")
        profile_widget = self._field(
            section, 0, "Device / radio power", self.object_vars["power_profile"], HARDWARE_POWER_PROFILE_KEYS
        )
        profile_widget.bind("<<ComboboxSelected>>", self._hardware_power_preview)
        self._field(section, 1, "Conducted TX (dBm)", self.object_vars["tx_power_dbm"])
        self._field(section, 2, "Antenna gain (dBi)", self.object_vars["antenna_gain_dbi"])
        self._field(section, 3, "Cable loss (dB)", self.object_vars["cable_loss_db"])
        self._field(section, 4, "Noise figure (dB)", self.object_vars["noise_figure_db"])
        self.object_vars["power_summary"] = tk.StringVar()
        self.object_vars["tx_power_dbm"].trace_add("write", lambda *_args: self._refresh_power_summary())
        self._refresh_power_summary()
        ttk.Label(
            section,
            textvariable=self.object_vars["power_summary"],
            style="Muted.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 2))
        sensitivity = PropagationModel.sensitivity(node)
        ttk.Label(section, text=f"Calculated sensitivity  {sensitivity:.1f} dBm", style="Muted.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(5, 2)
        )
        ttk.Label(
            section,
            text="Power is total conducted output before antenna gain. Regional legal limits still apply.",
            style="Muted.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 2))

        if node.live_port:
            section = self._section(body, "Live radio data")
            live_details = [f"Source {node.live_port}"]
            if node.hardware_model:
                live_details.append(f"Hardware {node.hardware_model}")
            if node.hops_away is not None:
                live_details.append(f"Hops away {node.hops_away}")
            if node.live_snr_db is not None:
                live_details.append(f"Last SNR {node.live_snr_db:.1f} dB")
            if node.last_heard:
                live_details.append(f"Last heard {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(node.last_heard))}")
            ttk.Label(
                section,
                text=" · ".join(live_details),
                style="Muted.TLabel",
                wraplength=320,
                justify="left",
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(3, 2))

        section = self._section(body, "Notes")
        self._field(section, 0, "Description", self.object_vars["notes"])
        self._form_actions(body)

    def _preset_preview(self, _event: tk.Event | None = None) -> None:
        preset = str(self.object_vars["preset"].get())
        if preset != "CUSTOM" and preset in PRESETS:
            region_var = self.object_vars.get("region")
            region = str(region_var.get()) if region_var is not None else "US"
            region = region_for_preset(region, preset)
            if region_var is not None:
                region_var.set(region)
            preset_widget = getattr(self, "object_preset_widget", None)
            if preset_widget is not None:
                preset_widget.configure(values=list(region_preset_options(region)))
            bw, sf, cr = preset_parameters(preset, region)
            self.object_vars["bandwidth_khz"].set(str(bw))
            self.object_vars["spreading_factor"].set(str(sf))
            self.object_vars["coding_rate"].set(str(cr))
            channel_name = str(self.object_vars["channel"].get()).strip()
            if not channel_name or channel_name in PRESET_DISPLAY_NAMES.values():
                channel_name = PRESET_DISPLAY_NAMES[preset]
                self.object_vars["channel"].set(channel_name)
            frequency = meshtastic_default_frequency_mhz(preset, channel_name, region=region)
            self.object_vars["frequency_mhz"].set(f"{frequency:.6f}".rstrip("0").rstrip("."))

    def _region_preview(self, _event: tk.Event | None = None) -> None:
        region = str(self.object_vars["region"].get())
        if region not in REGION_BANDS:
            region = "US"
            self.object_vars["region"].set(region)
        options = region_preset_options(region)
        preset_widget = getattr(self, "object_preset_widget", None)
        if preset_widget is not None:
            preset_widget.configure(values=list(options))
        current_preset = str(self.object_vars["preset"].get())
        if current_preset != "CUSTOM" and current_preset not in REGION_BANDS[region].presets:
            self.object_vars["preset"].set(REGION_BANDS[region].default_preset)
        self._preset_preview()

    def _hardware_power_preview(self, _event: tk.Event | None = None) -> None:
        profile = hardware_power_profile(str(self.object_vars["power_profile"].get()))
        self.object_vars["tx_power_dbm"].set(f"{profile.recommended_dbm:g}")
        self._refresh_power_summary()

    def _refresh_power_summary(self) -> None:
        summary_var = self.object_vars.get("power_summary")
        if summary_var is None:
            return
        profile = hardware_power_profile(str(self.object_vars["power_profile"].get()))
        try:
            power_dbm = float(self.object_vars["tx_power_dbm"].get())
            watts = dbm_to_watts(power_dbm)
            power_text = f"{watts:.2f} W" if watts >= 1.0 else f"{watts * 1000:.0f} mW"
        except (ValueError, TypeError):
            power_text = "Enter a valid dBm value"
        maximum = (
            f" · hardware ceiling {profile.maximum_dbm:g} dBm"
            if profile.maximum_dbm is not None
            else ""
        )
        summary_var.set(f"{power_text} selected{maximum}. {profile.description}")

    def _build_obstacle_form(self, body: ttk.Frame, obstacle: Obstacle) -> None:
        self._form_header(
            body, obstacle.name, f"{obstacle.kind} obstruction · {self.format_distance(obstacle.height_m)} high"
        )
        values: dict[str, Any] = {
            "name": obstacle.name,
            "kind": obstacle.kind,
            "x1": self._display_length(obstacle.x1),
            "y1": self._display_length(obstacle.y1),
            "x2": self._display_length(obstacle.x2),
            "y2": self._display_length(obstacle.y2),
            "height_m": self._display_length(obstacle.height_m),
            "base_elevation_m": self._display_length(obstacle.base_elevation_m),
            "attenuation_db": obstacle.attenuation_db,
            "loss_per_100m_db": obstacle.loss_per_100m_db,
            "behavior": obstacle.behavior,
            "max_range_beyond_miles": self._long_range_display(obstacle.max_range_beyond_m),
            "brush_radius_m": self._display_length(obstacle.brush_radius_m),
            "enabled": obstacle.enabled,
            "color": obstacle.color,
        }
        for key, value in values.items():
            self.object_vars[key] = tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value))
        section = self._section(body, "Obstruction")
        self._field(section, 0, "Name", self.object_vars["name"])
        kind_widget = self._field(section, 1, "Material / type", self.object_vars["kind"], list(OBSTACLE_DEFAULTS))
        kind_widget.bind("<<ComboboxSelected>>", self._obstacle_type_preview)
        self._field(section, 2, "Color", self.object_vars["color"])
        ttk.Button(section, text="Choose color…", command=self._choose_obstacle_color).grid(
            row=3, column=1, sticky="ew", pady=3
        )
        self._check(section, 4, "Enabled", self.object_vars["enabled"])
        section = self._section(body, "Shape")
        length_unit = self._length_unit()
        if obstacle.shape == "polygon":
            self._field(section, 0, f"Height AGL ({length_unit})", self.object_vars["height_m"])
            self._field(section, 1, f"Ground elevation ({length_unit})", self.object_vars["base_elevation_m"])
            ttk.Label(
                section,
                text=f"Imported geographic outline · {len(obstacle.points)} vertices · {obstacle.osm_id}",
                style="Muted.TLabel",
                wraplength=280,
                justify="left",
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        elif obstacle.kind == "Forest" and obstacle.shape == "brush":
            self._field(section, 0, f"Brush radius ({length_unit})", self.object_vars["brush_radius_m"])
            self._field(section, 1, f"Height AGL ({length_unit})", self.object_vars["height_m"])
            self._field(section, 2, f"Ground elevation ({length_unit})", self.object_vars["base_elevation_m"])
            ttk.Label(
                section,
                text="Drag the painted forest with Select. Paint another stroke to add more forest.",
                style="Muted.TLabel",
                wraplength=280,
                justify="left",
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        else:
            self._field(section, 0, f"Left X ({length_unit})", self.object_vars["x1"])
            self._field(section, 1, f"Top Y ({length_unit})", self.object_vars["y1"])
            self._field(section, 2, f"Right X ({length_unit})", self.object_vars["x2"])
            self._field(section, 3, f"Bottom Y ({length_unit})", self.object_vars["y2"])
            self._field(section, 4, f"Height AGL ({length_unit})", self.object_vars["height_m"])
            self._field(section, 5, f"Ground elevation ({length_unit})", self.object_vars["base_elevation_m"])
        section = self._section(body, "Signal loss")
        self._field(section, 0, "Penetration loss (dB)", self.object_vars["attenuation_db"])
        self._field(section, 1, "Loss / 100 m (dB)", self.object_vars["loss_per_100m_db"])
        self._field(section, 2, "Behavior", self.object_vars["behavior"], ["ATTENUATE", "BLOCK", "LIMIT_AFTER"])
        self._field(
            section, 3, f"Max travel beyond ({self._long_range_unit()})",
            self.object_vars["max_range_beyond_miles"]
        )
        ttk.Label(
            section,
            text="BLOCK stops the path. LIMIT_AFTER stops it after the entered distance beyond the obstruction.",
            style="Muted.TLabel",
            wraplength=280,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=6)
        self._form_actions(body)

    def _obstacle_type_preview(self, _event: tk.Event | None = None) -> None:
        kind = str(self.object_vars["kind"].get())
        if kind in OBSTACLE_DEFAULTS:
            color, loss, height, per_100, behavior, max_beyond = OBSTACLE_DEFAULTS[kind]
            self.object_vars["color"].set(color)
            self.object_vars["attenuation_db"].set(str(loss))
            self.object_vars["height_m"].set(str(self._display_length(height)))
            self.object_vars["loss_per_100m_db"].set(str(per_100))
            self.object_vars["behavior"].set(behavior)
            self.object_vars["max_range_beyond_miles"].set(str(self._long_range_display(max_beyond)))

    def _choose_obstacle_color(self) -> None:
        result = colorchooser.askcolor(str(self.object_vars["color"].get()), parent=self.root)
        if result[1]:
            self.object_vars["color"].set(result[1])

    def _form_actions(self, body: ttk.Frame) -> None:
        actions = ttk.Frame(body)
        actions.pack(fill="x", padx=12, pady=15)
        ttk.Button(actions, text="Duplicate", command=self.duplicate_selected).pack(side="left")
        ttk.Button(actions, text="Delete", style="Danger.TButton", command=self.delete_selected).pack(side="right")

    def _build_environment_form(self) -> None:
        body = self.environment_scroll.body
        self._clear_frame(body)
        self._form_header(
            body,
            "Environment",
            f"The geographic workspace is unbounded; distances are displayed in {self.unit_system.get().lower()} units. "
            "Tune clutter, fading, and weather for the deployment site.",
        )
        env = self.scenario.environment
        for key, value in vars(env).items():
            if key == "terrain_values":
                continue
            self.env_vars[key] = tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value))
        section = self._section(body, "World")
        ttk.Label(
            section,
            text="Unbounded geographic workspace\nNodes and RF paths may use any map position.",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        section = self._section(body, "Real-world map")
        if env.map_configured:
            ttk.Label(
                section,
                text=f"Center {env.map_center_lat:.6f}, {env.map_center_lon:.6f}\n"
                f"Layer: {env.map_layer}\n"
                f"Terrain: {env.terrain_source or 'not loaded'}",
                style="Muted.TLabel",
                justify="left",
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        else:
            ttk.Label(
                section,
                text="Use the map search bar to establish a geographic location.",
                style="Muted.TLabel",
                wraplength=280,
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        self._check(section, 1, "Use terrain in RF paths", self.env_vars["terrain_enabled"])
        section = self._section(body, "Propagation")
        self._field(section, 0, "Path-loss exponent", self.env_vars["path_loss_exponent"])
        self._field(section, 1, "Shadowing σ (dB)", self.env_vars["shadowing_sigma_db"])
        self._field(section, 2, "Weather loss (dB)", self.env_vars["weather_loss_db"])
        self._field(section, 3, "Capture threshold (dB)", self.env_vars["capture_threshold_db"])
        self._field(section, 4, "Random seed", self.env_vars["seed"])
        self._check(section, 5, "Stochastic fading / packet chance", self.env_vars["stochastic"])
        ttk.Label(
            section,
            text="Typical path-loss exponents: 2.0 free space, 2.4 rural, 2.7–3.5 suburban, 3–5 dense urban.",
            style="Muted.TLabel",
            wraplength=285,
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=7)
        ttk.Button(body, text="Apply world settings", style="Accent.TButton", command=self.apply_environment).pack(
            anchor="w", padx=12, pady=15
        )

    def _build_packet_form(self) -> None:
        body = self.packet_scroll.body
        self._clear_frame(body)
        self._form_header(
            body,
            "Packet generator",
            "Send a firmware-style packet and watch each receive, relay, duplicate cancellation, drop, and collision.",
        )
        packet = self.scenario.packet
        values: dict[str, Any] = {
            "source_id": packet.source_id,
            "destination_id": packet.destination_id,
            "payload": packet.payload,
            "payload_bytes": packet.payload_bytes,
            "hop_limit": packet.hop_limit,
            "port": packet.port,
            "want_ack": packet.want_ack,
            "want_response": packet.want_response,
            "channel": packet.channel,
        }
        for key, value in values.items():
            self.packet_vars[key] = tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value))
        node_names = [node.name for node in self.scenario.nodes]
        source_name = self._name_for_id(packet.source_id)
        destination_name = "BROADCAST" if packet.destination_id == "BROADCAST" else self._name_for_id(packet.destination_id)
        self.packet_vars["source_name"] = tk.StringVar(value=source_name)
        self.packet_vars["destination_name"] = tk.StringVar(value=destination_name)
        section = self._section(body, "Route")
        self._field(section, 0, "Source", self.packet_vars["source_name"], node_names)
        destination_widget = self._field(
            section,
            1,
            "Destination",
            self.packet_vars["destination_name"],
            ["BROADCAST"] + node_names,
        )
        destination_widget.bind("<<ComboboxSelected>>", self._destination_preview)
        self._field(section, 2, "Hop limit (0–7)", self.packet_vars["hop_limit"], [str(i) for i in range(8)])
        self._check(section, 3, "Request ACK (direct only)", self.packet_vars["want_ack"])
        self._check(section, 4, "Request module response (direct only)", self.packet_vars["want_response"])
        section = self._section(body, "Payload")
        self._field(
            section,
            0,
            "Port",
            self.packet_vars["port"],
            sorted(CORE_PORTS | {"TRACEROUTE_APP", "RANGE_TEST_APP", "PRIVATE_APP", "ATAK_PLUGIN"}),
        )
        self._field(section, 1, "Channel / PSK", self.packet_vars["channel"])
        self._field(section, 2, "Payload bytes", self.packet_vars["payload_bytes"])
        self._field(section, 3, "Message", self.packet_vars["payload"])
        self.packet_run_button = ttk.Button(
            body,
            text="▶  Send into live mesh" if self._live_mesh_running() else "▶  Run packet simulation",
            style="Accent.TButton",
            command=self.run_simulation,
        )
        self.packet_run_button.pack(fill="x", padx=12, pady=(16, 5))
        ttk.Label(
            body,
            text=(
                "Broadcasts use managed flooding and never request an ACK. Direct ACKs, NAKs, and requested module "
                "replies are simulated as their own RF packets, so they can collide or be blocked on the return path."
            ),
            style="Muted.TLabel",
            wraplength=295,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 15))

        section = self._section(body, "Live mesh traffic")
        self._field(
            section,
            0,
            "Traffic preset",
            self.live_mesh_preset_var,
            [*LIVE_TRAFFIC_PRESETS, "Custom"],
        )
        preset_widget = next(
            widget for widget in section.grid_slaves(row=0, column=1)
            if isinstance(widget, ttk.Combobox)
        )
        preset_widget.bind("<<ComboboxSelected>>", self._apply_live_mesh_preset)
        self._field(section, 1, "NodeInfo every (min)", self.live_mesh_nodeinfo_var)
        self._field(section, 2, "Client telemetry every (min)", self.live_mesh_telemetry_var)
        self._field(section, 3, "Router telemetry every (min)", self.live_mesh_router_telemetry_var)
        self._field(section, 4, "Sensor data every (min)", self.live_mesh_sensor_var)
        self._field(section, 5, "Message average (min)", self.live_mesh_message_var)
        self.live_mesh_button = ttk.Button(
            body,
            text="■  Stop live mesh" if self._live_mesh_running() else "▶  Start live mesh",
            style="Danger.TButton" if self._live_mesh_running() else "Accent.TButton",
            command=self.toggle_live_mesh,
        )
        self.live_mesh_button.pack(fill="x", padx=12, pady=(12, 5))
        ttk.Label(
            body,
            textvariable=self.live_mesh_status_var,
            style="Muted.TLabel",
            wraplength=295,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 4))
        ttk.Label(
            body,
            text=(
                "Runs in wall-clock time: one configured minute is one real minute. NodeInfo, telemetry, sensor "
                "data, and messages share the same RF channel with 25%/40% channel-utility gates."
            ),
            style="Muted.TLabel",
            wraplength=295,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 15))

    def _destination_preview(self, _event: tk.Event | None = None) -> None:
        if str(self.packet_vars["destination_name"].get()) != "BROADCAST":
            self.packet_vars["want_ack"].set(True)

    def _apply_live_mesh_preset(self, _event: tk.Event | None = None) -> None:
        preset = LIVE_TRAFFIC_PRESETS.get(self.live_mesh_preset_var.get())
        if preset is None:
            return
        self.live_mesh_nodeinfo_var.set(str(preset["nodeinfo_interval_minutes"]))
        self.live_mesh_telemetry_var.set(str(preset["telemetry_interval_minutes"]))
        self.live_mesh_router_telemetry_var.set(str(preset["router_telemetry_interval_minutes"]))
        self.live_mesh_sensor_var.set(str(preset["sensor_interval_minutes"]))
        self.live_mesh_message_var.set(str(preset["message_interval_minutes"]))

    def _sync_live_mesh_preset(self) -> None:
        config = self.scenario.live_mesh
        for name, preset in LIVE_TRAFFIC_PRESETS.items():
            if (
                config.traffic_profile == preset["profile"]
                and config.nodeinfo_interval_minutes == preset["nodeinfo_interval_minutes"]
                and config.telemetry_interval_minutes == preset["telemetry_interval_minutes"]
                and config.router_telemetry_interval_minutes == preset["router_telemetry_interval_minutes"]
                and config.sensor_interval_minutes == preset["sensor_interval_minutes"]
                and config.message_interval_minutes == preset["message_interval_minutes"]
            ):
                self.live_mesh_preset_var.set(name)
                return
        self.live_mesh_preset_var.set("Custom")

    def apply_object(self) -> None:
        obj = self.get_selected()
        if obj is None:
            return
        try:
            if isinstance(obj, Node):
                obj.name = str(self.object_vars["name"].get()).strip() or obj.name
                raw_num = str(self.object_vars["node_num"].get()).strip().lower().replace("0x", "")
                obj.node_num = int(raw_num, 16)
                obj.role = str(self.object_vars["role"].get())
                obj.rebroadcast_mode = str(self.object_vars["rebroadcast_mode"].get())
                obj.channel = str(self.object_vars["channel"].get())
                obj.online = bool(self.object_vars["online"].get())
                obj.favorite = bool(self.object_vars["favorite"].get())
                old_elevation = obj.elevation_m
                env = self.scenario.environment
                if env.map_configured:
                    obj.x, obj.y = latlon_to_world(
                        float(self.object_vars["latitude"].get()),
                        float(self.object_vars["longitude"].get()),
                        env.map_center_lat,
                        env.map_center_lon,
                    )
                else:
                    obj.x = self._meters_from_display(float(self.object_vars["x"].get()))
                    obj.y = self._meters_from_display(float(self.object_vars["y"].get()))
                submitted_elevation = self._meters_from_display(float(self.object_vars["elevation_m"].get()))
                elevation_was_edited = not math.isclose(
                    submitted_elevation,
                    old_elevation,
                    rel_tol=0.0,
                    abs_tol=0.001,
                )
                obj.elevation_override = bool(self.object_vars["elevation_override"].get()) or elevation_was_edited
                obj.use_live_altitude = bool(self.object_vars["use_live_altitude"].get())
                if obj.elevation_override:
                    obj.elevation_m = submitted_elevation
                else:
                    self._set_auto_node_elevation(obj)
                obj.antenna_height_m = self._meters_from_display(
                    float(self.object_vars["antenna_height_m"].get())
                )
                obj.power_profile = str(self.object_vars["power_profile"].get())
                profile = hardware_power_profile(obj.power_profile)
                requested_power = float(self.object_vars["tx_power_dbm"].get())
                if profile.maximum_dbm is not None and requested_power > profile.maximum_dbm:
                    messagebox.showerror(
                        "TX power above hardware limit",
                        f"{profile.key} is limited to {profile.maximum_dbm:g} dBm conducted output.\n\n"
                        "Choose Custom / measured output only when you have a different measured radio or amplifier.",
                        parent=self.root,
                    )
                    return
                obj.tx_power_dbm = requested_power
                for key in ["antenna_gain_dbi", "cable_loss_db", "noise_figure_db"]:
                    setattr(obj, key, float(self.object_vars[key].get()))
                obj.radio.preset = str(self.object_vars["preset"].get())
                obj.radio.region = str(self.object_vars["region"].get())
                obj.radio.frequency_mhz = float(self.object_vars["frequency_mhz"].get())
                obj.radio.bandwidth_khz = float(self.object_vars["bandwidth_khz"].get())
                obj.radio.spreading_factor = int(self.object_vars["spreading_factor"].get())
                obj.radio.coding_rate = int(self.object_vars["coding_rate"].get())
                obj.notes = str(self.object_vars["notes"].get())
            else:
                obj.name = str(self.object_vars["name"].get()).strip() or obj.name
                obj.kind = str(self.object_vars["kind"].get())
                for key in ["x1", "y1", "x2", "y2", "height_m", "base_elevation_m"]:
                    setattr(obj, key, self._meters_from_display(float(self.object_vars[key].get())))
                for key in ["attenuation_db", "loss_per_100m_db"]:
                    setattr(obj, key, float(self.object_vars[key].get()))
                obj.behavior = str(self.object_vars["behavior"].get())
                obj.max_range_beyond_m = max(
                    0.0, self._meters_from_long_range(float(self.object_vars["max_range_beyond_miles"].get()))
                )
                obj.brush_radius_m = max(
                    1.0, self._meters_from_display(float(self.object_vars["brush_radius_m"].get()))
                )
                obj.enabled = bool(self.object_vars["enabled"].get())
                obj.color = str(self.object_vars["color"].get())
        except (ValueError, TypeError) as error:
            messagebox.showerror("Invalid value", f"One of the fields is not a valid number.\n\n{error}", parent=self.root)
            return
        self._set_object_form_clean()
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_scene_change(
            packet=isinstance(obj, Node),
            geographic=isinstance(obj, Obstacle),
        )
        if isinstance(obj, Node) and not self._terrain_covers(obj.x, obj.y):
            self.status_var.set("Node coordinates updated · refreshing terrain around current scene")
            self.load_topography()
        # Editing the beacon node (height, power, position, radio…) changes its
        # coverage, so re-pulse it from the updated settings.
        if isinstance(obj, Node) and obj.id == self.beacon_node_id:
            self.selected_id = obj.id
            self.start_beacon()

    def apply_environment(self) -> None:
        env = self.scenario.environment
        try:
            for key in [
                "path_loss_exponent",
                "shadowing_sigma_db",
                "weather_loss_db",
                "capture_threshold_db",
                "grid_m",
            ]:
                setattr(env, key, float(self.env_vars[key].get()))
            env.seed = int(self.env_vars["seed"].get())
            env.stochastic = bool(self.env_vars["stochastic"].get())
            env.terrain_enabled = bool(self.env_vars["terrain_enabled"].get())
            env.grid_m = max(10.0, env.grid_m)
        except (ValueError, TypeError) as error:
            messagebox.showerror("Invalid world setting", str(error), parent=self.root)
            return
        self.mark_dirty()
        self._mark_results_stale()
        self._build_environment_form()
        self.render_canvas()
        self._refresh_mesh_graph()
        self._update_title()

    def _live_mesh_running(self) -> bool:
        return bool(
            self.live_mesh_after is not None
            or (self.live_mesh_thread is not None and self.live_mesh_thread.is_alive())
        )

    def _update_live_mesh_controls(self) -> None:
        running = self._live_mesh_running()
        self._refresh_packet_run_button()
        if hasattr(self, "live_mesh_button"):
            self.live_mesh_button.configure(
                text="■  Stop live mesh" if running else "▶  Start live mesh",
                style="Danger.TButton" if running else "Accent.TButton",
            )
        if hasattr(self, "send_button"):
            self.send_button.configure(
                state="normal",
                text="Live mesh active…" if running else "▶  Send packet",
            )

        if hasattr(self, "send_button"):
            self.send_button.configure(text="▶  Send test packet" if running else "▶  Send packet")

    def _refresh_packet_run_button(self) -> None:
        if hasattr(self, "packet_run_button"):
            self.packet_run_button.configure(
                text="▶  Send into live mesh" if self._live_mesh_running() else "▶  Run packet simulation"
            )

    def _live_mesh_toggle_changed(self) -> None:
        if self.live_mesh_enabled.get():
            self.start_live_mesh()
        else:
            self.stop_live_mesh()

    def toggle_live_mesh(self) -> None:
        self.live_mesh_enabled.set(not self._live_mesh_running())
        self._live_mesh_toggle_changed()

    def start_live_mesh(self) -> None:
        if self._live_mesh_running():
            return
        if not any(node.online for node in self.scenario.nodes):
            self.live_mesh_enabled.set(False)
            messagebox.showinfo(
                "Add an online node",
                "Place at least one online node before starting live mesh traffic.",
                parent=self.root,
            )
            return
        try:
            hop_limit = max(0, min(7, int(self.packet_vars["hop_limit"].get())))
            intervals = {
                "nodeinfo_interval_minutes": max(1.0, float(self.live_mesh_nodeinfo_var.get())),
                "telemetry_interval_minutes": max(1.0, float(self.live_mesh_telemetry_var.get())),
                "router_telemetry_interval_minutes": max(1.0, float(self.live_mesh_router_telemetry_var.get())),
                "sensor_interval_minutes": max(1.0, float(self.live_mesh_sensor_var.get())),
                "message_interval_minutes": max(1.0, float(self.live_mesh_message_var.get())),
            }
        except (TypeError, ValueError):
            self.live_mesh_enabled.set(False)
            messagebox.showerror(
                "Invalid live mesh setting",
                "Choose 1–24 simulated hours and a hop limit from 0–7.",
                parent=self.root,
            )
            return
        preset = LIVE_TRAFFIC_PRESETS.get(self.live_mesh_preset_var.get(), {})
        profile = str(preset.get("profile", self.scenario.live_mesh.traffic_profile))
        self._start_live_mesh_runtime(hop_limit, profile, intervals)
        return
        self.clear_results()
        self.scenario.live_mesh = LiveMeshConfig(
            duration_minutes=self.scenario.live_mesh.duration_minutes,
            traffic_profile=profile,
            hop_limit=hop_limit,
            playback_seconds=30,
        )
        self.mark_dirty()
        snapshot = Scenario.from_dict(self.scenario.to_dict())
        self.live_mesh_cancel_event = threading.Event()
        cancel_event = self.live_mesh_cancel_event
        self.live_mesh_request_id += 1
        request_id = self.live_mesh_request_id
        while True:
            try:
                self.live_mesh_updates.get_nowait()
            except queue.Empty:
                break
        self.live_mesh_result = None
        self.live_mesh_recent_frames = []
        self.live_mesh_frame_index = 0
        self.live_mesh_status_var.set("Preparing RF links and concurrent traffic…")
        self.status_var.set("Preparing live mesh traffic in the background…")

        def worker() -> None:
            try:
                result = LiveMeshEngine(snapshot).run(snapshot.live_mesh, cancelled=cancel_event.is_set)
                self.live_mesh_updates.put((request_id, result))
            except Exception as error:
                self.live_mesh_updates.put((request_id, error))

        self.live_mesh_thread = threading.Thread(target=worker, name="LiveMeshSimulation", daemon=True)
        self.live_mesh_thread.start()
        self._update_live_mesh_controls()
        self.root.after(25, self._poll_live_mesh)

    def _start_live_mesh_runtime(
        self, hop_limit: int, profile: str, intervals: dict[str, float]
    ) -> None:
        """Keep one event timeline alive so test packets meet routine mesh traffic."""
        self.scenario.live_mesh = LiveMeshConfig(
            duration_minutes=self.scenario.live_mesh.duration_minutes,
            traffic_profile=profile,
            hop_limit=hop_limit,
            playback_seconds=30,
            **intervals,
        )
        self.mark_dirty()
        snapshot = Scenario.from_dict(self.scenario.to_dict())
        self.live_mesh_cancel_event = threading.Event()
        cancel_event = self.live_mesh_cancel_event
        self.live_mesh_request_id += 1
        request_id = self.live_mesh_request_id
        self.live_mesh_tests = {}
        self.live_mesh_snapshot = {}
        self.live_path_test_id = None
        self.live_mesh_hidden_test_ids = set()
        self.live_mesh_recent_frames = []
        self.live_mesh_history_frames = []
        self.live_mesh_status_var.set("Preparing live RF links…")
        self.status_var.set("Preparing continuous live mesh traffic…")
        while not self.live_mesh_updates.empty():
            try:
                self.live_mesh_updates.get_nowait()
            except queue.Empty:
                break
        while not self.live_mesh_injections.empty():
            try:
                self.live_mesh_injections.get_nowait()
            except queue.Empty:
                break

        def worker() -> None:
            try:
                engine = LiveMeshEngine(snapshot)
                if not engine.prepare_runtime(snapshot.live_mesh, cancelled=cancel_event.is_set):
                    return
                self.live_mesh_updates.put((request_id, "ready", engine.runtime_snapshot()))
                simulated_time = 0.0
                previous_wall_time = time.monotonic()
                last_heartbeat = previous_wall_time
                while not cancel_event.wait(0.05):
                    while True:
                        try:
                            packet = self.live_mesh_injections.get_nowait()
                        except queue.Empty:
                            break
                        self.live_mesh_updates.put((request_id, "test", engine.inject_packet(packet)))
                    current_wall_time = time.monotonic()
                    simulated_time += max(0.0, (current_wall_time - previous_wall_time) * 1000.0)
                    previous_wall_time = current_wall_time
                    frames = engine.advance_runtime(simulated_time, cancelled=cancel_event.is_set)
                    if frames:
                        # An injected packet is normally near the beginning of this
                        # live time slice. Keep that frame instead of losing it to
                        # a later routine-traffic frame.
                        test_frames = [
                            frame for frame in frames
                            if any(kind == "TEST" for _node_id, kind in frame.transmitters)
                            or any(kind == "TEST" for _source, _target, kind, _hop in frame.receptions)
                        ]
                        visible_frames = (frames[-1:] + test_frames)[-4:]
                    else:
                        visible_frames = []
                    if visible_frames or current_wall_time - last_heartbeat >= 0.25:
                        self.live_mesh_updates.put((request_id, "frame", (visible_frames, engine.runtime_snapshot())))
                        last_heartbeat = current_wall_time
            except Exception as error:
                self.live_mesh_updates.put((request_id, "error", error))

        self.live_mesh_thread = threading.Thread(target=worker, name="LiveMeshRuntime", daemon=True)
        self.live_mesh_thread.start()
        self._update_live_mesh_controls()
        self.root.after(25, self._poll_live_mesh_runtime)

    def _poll_live_mesh_runtime(self) -> None:
        alive = bool(self.live_mesh_thread and self.live_mesh_thread.is_alive())
        while True:
            try:
                request_id, operation, payload = self.live_mesh_updates.get_nowait()
            except queue.Empty:
                break
            if request_id != self.live_mesh_request_id:
                continue
            if operation == "error":
                self.live_mesh_status_var.set(f"Live mesh failed: {payload}")
                self.status_var.set("Live mesh simulation failed")
                self.live_mesh_enabled.set(False)
                messagebox.showerror("Live mesh failed", str(payload), parent=self.root)
                continue
            if operation == "ready":
                self.live_mesh_snapshot = payload
                self._append_live_event_log(detail="Live mesh started in real time; waiting for configured traffic intervals")
                self.show_sidebar_tab("Results")
                if hasattr(self, "results_notebook"):
                    self.results_notebook.select(self.live_results_frame)
                self.live_mesh_status_var.set("Live mesh traffic running · send a packet to test it")
                self.status_var.set("Live mesh active · test packets share its channel load and collisions")
            elif operation == "test":
                self.live_mesh_tests[payload.test_id] = payload
                self.status_var.set(f"Test packet #{payload.test_id} queued in the live mesh")
            elif operation == "frame":
                frames, snapshot = payload
                visual_changed = bool(frames)
                if frames:
                    self.live_mesh_recent_frames = (self.live_mesh_recent_frames + frames)[-8:]
                    self.live_mesh_history_frames = (self.live_mesh_history_frames + frames)[-1800:]
                elif self.live_mesh_recent_frames:
                    # Quiet periods are real at 1:1 time; fade the prior burst
                    # instead of freezing it on the map.
                    self.live_mesh_recent_frames = self.live_mesh_recent_frames[1:]
                    visual_changed = True
                self.live_mesh_snapshot = snapshot
                self.live_mesh_tests = {test.test_id: test for test in snapshot["tests"]}
                learned_routes = {
                    str(key): list(route)
                    for key, route in snapshot.get("learned_routes", {}).items()
                }
                if learned_routes != self.scenario.learned_routes:
                    self.scenario.learned_routes = learned_routes
                    self.mark_dirty()
                self.live_mesh_play_counts.update({
                    "tx": snapshot["transmissions"], "rx": snapshot["receptions"],
                    "collisions": snapshot["collisions"], "dropped": snapshot["dropped"],
                    "throttled": snapshot["throttled"],
                })
                self.live_mesh_status_var.set(
                    f"T+{snapshot['time_ms'] / 3_600_000:.2f} h · {snapshot['transmissions']:,} TX · "
                    f"{snapshot['collisions']:,} collisions · {snapshot['dropped']:,} RF drops · "
                    f"{snapshot['throttled']:,} channel-gated"
                )
                if frames:
                    for live_frame in frames:
                        self._append_live_event_log(live_frame)
                if visual_changed:
                    self._render_live_mesh_frame()
                    if self.mesh_graph_canvas is not None:
                        self._schedule_mesh_graph_refresh()
            self._refresh_live_results()
        if alive and not self.live_mesh_cancel_event.is_set():
            self.root.after(25, self._poll_live_mesh_runtime)
        else:
            self._update_live_mesh_controls()

    def _poll_live_mesh(self) -> None:
        received = False
        while True:
            try:
                request_id, payload = self.live_mesh_updates.get_nowait()
            except queue.Empty:
                break
            if request_id != self.live_mesh_request_id:
                continue
            received = True
            if isinstance(payload, Exception):
                self.live_mesh_thread = None
                self.live_mesh_status_var.set(f"Live mesh failed: {payload}")
                self.status_var.set("Live mesh simulation failed")
                self._update_live_mesh_controls()
                messagebox.showerror("Live mesh failed", str(payload), parent=self.root)
            else:
                self.live_mesh_thread = None
                self._begin_live_mesh_playback(payload)
        if self.live_mesh_thread is not None and self.live_mesh_thread.is_alive():
            self.root.after(25, self._poll_live_mesh)
        elif not received:
            self._update_live_mesh_controls()

    def _begin_live_mesh_playback(self, result: LiveMeshResult) -> None:
        self.live_mesh_result = result
        self.live_mesh_frame_index = 0
        self.live_mesh_recent_frames = []
        self.live_mesh_history_frames = []
        self.live_mesh_play_counts = {
            "tx": 0,
            "rx": 0,
            "collisions": 0,
            "dropped": 0,
            "throttled": 0,
        }
        self.live_mesh_status_var.set(
            f"{result.originated_packets:,} packets scheduled · playing 30-second traffic view"
        )
        self.status_var.set("Live mesh traffic active · blue NodeInfo · green telemetry · amber sensor · purple message")
        self.live_mesh_after = self.root.after(10, self._live_mesh_tick)
        self._update_live_mesh_controls()

    def _live_mesh_tick(self) -> None:
        result = self.live_mesh_result
        if result is None or self.live_mesh_frame_index >= len(result.frames):
            self.live_mesh_after = None
            self.live_mesh_recent_frames = []
            if result is not None:
                suffix = " · safety cap reached" if result.truncated else ""
                self.live_mesh_status_var.set(
                    f"Complete · {result.transmissions:,} TX · {result.collisions:,} collisions · "
                    f"{result.dropped:,} RF drops · {result.throttled:,} channel-gated · "
                    f"peak {result.peak_channel_utilization:.1f}%{suffix}"
                )
                self.status_var.set("Live mesh traffic complete")
            self._update_live_mesh_controls()
            self._render_live_mesh_frame()
            return

        frame = result.frames[self.live_mesh_frame_index]
        self.live_mesh_recent_frames.append(frame)
        self.live_mesh_recent_frames = self.live_mesh_recent_frames[-4:]
        self.live_mesh_history_frames = (self.live_mesh_history_frames + [frame])[-1800:]
        self.live_mesh_play_counts["tx"] += frame.transmission_count
        self.live_mesh_play_counts["rx"] += frame.reception_count
        self.live_mesh_play_counts["collisions"] += frame.collision_count
        self.live_mesh_play_counts["dropped"] += frame.drop_count
        self.live_mesh_play_counts["throttled"] += frame.throttle_count
        if self.live_mesh_frame_index % 10 == 0:
            simulated_minutes = frame.time_ms / 60_000.0
            self.live_mesh_status_var.set(
                f"T+{simulated_minutes / 60.0:.1f} h · {self.live_mesh_play_counts['tx']:,} TX · "
                f"{self.live_mesh_play_counts['collisions']:,} collisions · "
                f"{self.live_mesh_play_counts['dropped']:,} RF drops · "
                f"{self.live_mesh_play_counts['throttled']:,} channel-gated"
            )
        self._render_live_mesh_frame()
        self.live_mesh_frame_index += 1
        interval_ms = max(20, round(self.scenario.live_mesh.playback_seconds * 1000 / len(result.frames)))
        self.live_mesh_after = self.root.after(interval_ms, self._live_mesh_tick)

    def stop_live_mesh(self, clear_visuals: bool = False) -> None:
        was_running = self._live_mesh_running()
        self.live_mesh_cancel_event.set()
        self.live_mesh_request_id += 1
        if self.live_mesh_after is not None:
            try:
                self.root.after_cancel(self.live_mesh_after)
            except tk.TclError:
                pass
        self.live_mesh_after = None
        self.live_mesh_thread = None
        self.live_mesh_recent_frames = []
        self.live_mesh_enabled.set(False)
        if clear_visuals:
            self.live_mesh_result = None
            self.live_mesh_tests = {}
            self.live_mesh_snapshot = {}
            self.live_mesh_history_frames = []
            self.live_path_test_id = None
            self.live_mesh_hidden_test_ids = set()
            self.live_mesh_status_var.set("Idle")
        elif was_running:
            self.live_mesh_status_var.set("Stopped")
            self.status_var.set("Live mesh traffic stopped")
        self._update_live_mesh_controls()
        if hasattr(self, "canvas"):
            self._render_live_mesh_frame()

    def _read_packet_form(self) -> PacketConfig | None:
        source = self._id_for_name(str(self.packet_vars["source_name"].get()))
        destination_name = str(self.packet_vars["destination_name"].get())
        destination = "BROADCAST" if destination_name == "BROADCAST" else self._id_for_name(destination_name)
        if not source:
            messagebox.showwarning("Choose a source", "Select an online source node.", parent=self.root)
            return None
        if destination_name != "BROADCAST" and not destination:
            messagebox.showwarning("Choose a destination", "Select a destination node.", parent=self.root)
            return None
        try:
            packet = PacketConfig(
                source_id=source,
                destination_id=destination or "BROADCAST",
                payload=str(self.packet_vars["payload"].get()),
                payload_bytes=max(1, min(239, int(self.packet_vars["payload_bytes"].get()))),
                hop_limit=max(0, min(7, int(self.packet_vars["hop_limit"].get()))),
                port=str(self.packet_vars["port"].get()),
                want_ack=bool(self.packet_vars["want_ack"].get()) and destination != "BROADCAST",
                want_response=bool(self.packet_vars["want_response"].get()) and destination != "BROADCAST",
                channel=str(self.packet_vars["channel"].get()),
            )
        except ValueError as error:
            messagebox.showerror("Invalid packet", str(error), parent=self.root)
            return None
        self.scenario.packet = packet
        return packet

    def run_simulation(self) -> None:
        if self._live_mesh_running():
            packet = self._read_packet_form()
            if packet is not None:
                # Replace only the displayed packet trace.  The background
                # mesh stays active and keeps its RF/channel state.
                self.clear_results()
                self.live_mesh_injections.put(packet)
                self.status_var.set("Packet queued for injection into the live mesh…")
            return
        if self.simulation_thread and self.simulation_thread.is_alive():
            self.status_var.set("Simulation is already running…")
            return
        if self.last_result is not None:
            self.clear_results()
        else:
            self.stop_animation()
        packet = self._read_packet_form()
        if packet is None:
            return
        self.path_focus_id = None
        self._render_simulation_layers()
        route_key = (
            dm_route_key(packet.source_id, packet.destination_id)
            if packet.destination_id != "BROADCAST"
            else ""
        )
        if route_key and self.scenario.learned_routes.get(route_key):
            self.status_var.set("Testing learned DM route…")
        elif route_key:
            self.status_var.set("Discovering a DM route with managed flooding…")
        else:
            self.status_var.set("Calculating link budgets and first-hop coverage…")
        self.send_button.configure(state="disabled", text="Calculating…")
        snapshot = Scenario.from_dict(self.scenario.to_dict())
        coverage_cap = self._coverage_range_cap()
        self.simulation_request_id += 1
        request_id = self.simulation_request_id
        self.simulation_contours_complete = False
        while True:
            try:
                self.simulation_updates.get_nowait()
            except queue.Empty:
                break

        def worker() -> None:
            try:
                engine = SimulationEngine(snapshot)
                result = engine.run(snapshot.packet)
                if not result_uses_coverage_ripples(result):
                    self.simulation_updates.put((request_id, "result", (result, {})))
                    self.simulation_updates.put((request_id, "done", None))
                    return
                grouped = transmitter_ids_by_hop(result)
                if not grouped:
                    self.simulation_updates.put((request_id, "result", (result, {})))
                else:
                    for index, hop in enumerate(sorted(grouped)):
                        contours = build_coverage_contours(
                            snapshot,
                            result,
                            engine.model,
                            transmitter_ids=grouped[hop],
                            max_range_m=coverage_cap,
                        )
                        if index == 0:
                            self.simulation_updates.put((request_id, "result", (result, contours)))
                        else:
                            self.simulation_updates.put((request_id, "contours", contours))
                self.simulation_updates.put((request_id, "done", None))
            except Exception as error:
                self.simulation_updates.put((request_id, "error", error))

        self.simulation_thread = threading.Thread(target=worker, name="MeshLabSimulation", daemon=True)
        self.simulation_thread.start()
        self.root.after(25, self._poll_simulation)

    def _poll_simulation(self) -> None:
        received_update = False
        while True:
            try:
                request_id, operation, payload = self.simulation_updates.get_nowait()
            except queue.Empty:
                break
            if request_id != self.simulation_request_id:
                continue
            received_update = True
            if operation == "result":
                result, contours = payload
                self._begin_simulation_result(result, contours)
            elif operation == "contours":
                self.animation_contours.update(payload)
            elif operation == "done":
                self.simulation_contours_complete = True
            elif operation == "error":
                self.simulation_contours_complete = True
                if self.last_result is None:
                    self.send_button.configure(state="normal", text="▶  Send packet")
                    messagebox.showerror("Simulation failed", str(payload), parent=self.root)
                    self.status_var.set("Simulation failed")
                else:
                    self.status_var.set(f"Later-hop coverage calculation failed: {payload}")

        running = bool(self.simulation_thread and self.simulation_thread.is_alive())
        if running or received_update or not self.simulation_contours_complete:
            self.root.after(25, self._poll_simulation)
        elif self.last_result is None:
            self.send_button.configure(state="normal", text="▶  Send packet")
            self.status_var.set("Simulation stopped without a result")
        else:
            self.send_button.configure(state="normal", text="▶  Send again")

    def _begin_simulation_result(
        self,
        result: SimulationResult,
        contours: dict[str, list[tuple[float, float, str]]],
    ) -> None:
        if self.last_result is not None:
            self.animation_contours.update(contours)
            return
        routes_changed = False
        if result.invalidated_route_key and result.invalidated_route_key in self.scenario.learned_routes:
            self.scenario.learned_routes.pop(result.invalidated_route_key, None)
            routes_changed = True
        if result.route_key and result.learned_route:
            if self.scenario.learned_routes.get(result.route_key) != result.learned_route:
                self.scenario.learned_routes[result.route_key] = list(result.learned_route)
                routes_changed = True
        if routes_changed:
            self.mark_dirty()
        self.last_result = result
        self.animation_contours = dict(contours)
        self.results_populated = False
        self.results_stale = False
        self._update_result_metrics()
        self._prepare_hop_animation()
        self.clear_hops_button.configure(state="normal")
        source_id = self.scenario.packet.source_id
        reached_others = [nid for nid in result.reached if nid != source_id]
        if result.routing_mode == "DM_LEARNED":
            self.result_status.configure(
                text=f"Learned DM route · {max(0, len(result.learned_route) - 1)} hop"
                f"{'s' if len(result.learned_route) - 1 != 1 else ''}"
            )
            self.status_var.set("Learned DM route ready · animating directed hop lines")
        elif result.routing_mode == "DM_FALLBACK_FLOOD":
            self.result_status.configure(
                text=f"Learned DM route failed · fallback reached {len(result.reached)} nodes"
            )
            self.status_var.set("Learned route failed · showing managed-flood fallback")
        elif result.routing_mode == "DM_DISCOVERY_FLOOD":
            self.result_status.configure(
                text=(
                    f"DM discovery reached {len(result.reached)} nodes"
                    + (" · route learned" if result.learned_route else " · no confirmed route learned")
                )
            )
            self.status_var.set("DM route discovery ready · starting managed-flood propagation")
        elif not reached_others:
            self.result_status.configure(
                text=f"0 of {max(0, len(self.scenario.nodes) - 1)} other nodes heard the packet"
            )
        else:
            self.result_status.configure(
                text=f"{len(self.last_result.reached)} of {len(self.scenario.nodes)} nodes heard the packet"
            )
            self.status_var.set("First-hop coverage ready · starting packet propagation")
        # Floods/discovery always begin with the zero-hop coverage heatmap.  It
        # then freezes (reached nobody) or clears and continues the hop animation
        # (reached another node).  A learned DM has no coverage -- animate directly.
        if result_uses_coverage_ripples(result):
            if reached_others:
                self.send_button.configure(state="disabled", text="Finishing simulation…")
            else:
                self.send_button.configure(state="normal", text="▶  Send packet")
                self.root.after_idle(self._populate_results_once)
            self._begin_source_coverage(continue_after=bool(reached_others))
        else:
            self.send_button.configure(state="disabled", text="Finishing simulation…")
            self._animate_next()

    def schedule_render(self) -> None:
        if self.pan_start is not None:
            return
        if self.render_after is None:
            self.render_after = self.root.after_idle(self._scheduled_render)

    def _scheduled_render(self) -> None:
        self.render_after = None
        self.render_canvas()

    def populate_results(self) -> None:
        result = self.last_result
        if result is None:
            return
        for tree in (self.events_tree, self.nodes_tree, self.links_tree):
            tree.delete(*tree.get_children())
        names = {node.id: node.name for node in self.scenario.nodes}
        for index, event in enumerate(result.events):
            values = (
                f"{event.time_ms:,.0f} ms",
                event.kind,
                names.get(event.node_id, event.node_id),
                names.get(event.peer_id, event.peer_id),
                event.hop,
                "" if event.rssi_dbm is None else f"{event.rssi_dbm:.1f}",
                "" if event.snr_db is None else f"{event.snr_db:.1f}",
                "" if event.margin_db is None else f"{event.margin_db:+.1f}",
                event.detail,
            )
            tag = "ok" if event.kind in {"RX", "TX"} else "warn" if event.kind in {"OPAQUE", "CANCEL"} else "bad"
            self.events_tree.insert("", "end", iid=f"event-{index}", values=values, tags=(tag,))
        self.events_tree.tag_configure("ok", foreground="#a8f5d1")
        self.events_tree.tag_configure("warn", foreground="#ffd98d")
        self.events_tree.tag_configure("bad", foreground="#ff9ba9")

        for node in self.scenario.nodes:
            info = result.reached.get(node.id)
            if info:
                status = "Decoded" if info.get("decoded") else "Opaque"
                via = names.get(info.get("via", ""), "")
                values = (
                    node.name,
                    node.role,
                    status,
                    f"{info.get('time_ms', 0):,.0f} ms",
                    info.get("hop", 0),
                    via,
                    "" if "rssi_dbm" not in info else f"{info['rssi_dbm']:.1f}",
                    "" if "margin_db" not in info else f"{info['margin_db']:+.1f}",
                )
                tag = "decoded" if info.get("decoded") else "opaque"
            else:
                values = (node.name, node.role, "Not reached", "—", "—", "—", "—", "—")
                tag = "missed"
            self.nodes_tree.insert("", "end", values=values, tags=(tag,))
        self.nodes_tree.tag_configure("decoded", foreground="#9cf2cb")
        self.nodes_tree.tag_configure("opaque", foreground="#ffd483")
        self.nodes_tree.tag_configure("missed", foreground="#8293a8")

        for index, link in enumerate(result.links):
            values = (
                names.get(link.source_id, link.source_id),
                names.get(link.target_id, link.target_id),
                self.format_distance(link.distance_m),
                f"{link.rssi_dbm:.1f}",
                f"{link.snr_db:.1f}",
                f"{link.margin_db:+.1f}",
                f"{link.probability * 100:.0f}%",
                f"{link.obstacle_loss_db:.1f} dB" + (f" · {', '.join(link.obstacles)}" if link.obstacles else ""),
                link.reason,
            )
            self.links_tree.insert("", "end", iid=f"link-{index}", values=values)

        self._update_result_metrics()
        self.results_populated = True

    def _update_result_metrics(self) -> None:
        result = self.last_result
        if result is None:
            return
        self.metric_vars["reached"].set(f"{len(result.reached)} / {len(self.scenario.nodes)}")
        self.metric_vars["tx"].set(str(result.transmissions))
        self.metric_vars["range"].set(self.format_distance(result.max_distance_m))
        self.metric_vars["airtime"].set(f"{result.total_airtime_ms / 1000:.2f} s")

    def _populate_results_once(self) -> None:
        if self.last_result is not None and not self.results_populated:
            self.populate_results()

    def replay_animation(self) -> None:
        if not self.last_result:
            return
        self.stop_animation()
        self._prepare_hop_animation(preserve_seen=True)
        self._animate_next()

    def _prepare_hop_animation(self, preserve_seen: bool = False) -> None:
        if not self.last_result:
            self.animation_waves = []
            self.animation_wave_hops = []
            self.animation_revealed_nodes = set()
            return
        first_arrivals: dict[str, SimEvent] = {}
        for event in self.last_result.events:
            if event.kind not in {"RX", "OPAQUE"} or not event.peer_id:
                continue
            reach = self.last_result.reached.get(event.node_id)
            if not reach or event.hop != int(reach.get("hop", event.hop)):
                continue
            previous = first_arrivals.get(event.node_id)
            if previous is None or event.time_ms < previous.time_ms:
                first_arrivals[event.node_id] = event
        by_hop: dict[int, list[SimEvent]] = {}
        for event in first_arrivals.values():
            by_hop.setdefault(event.hop, []).append(event)
        self.animation_transmitters = {}
        for event in self.last_result.events:
            if event.kind == "TX":
                self.animation_transmitters.setdefault(event.hop + 1, []).append(event.node_id)
        for hop, transmitter_ids in self.animation_transmitters.items():
            self.animation_transmitters[hop] = list(dict.fromkeys(transmitter_ids))
        self.animation_wave_hops = sorted(self.animation_transmitters)
        self.animation_waves = [
            sorted(by_hop.get(hop, []), key=lambda event: (event.time_ms, event.node_id))
            for hop in self.animation_wave_hops
        ]
        self.animation_index = 0
        self.retained_coverage_transmitters = []
        if not preserve_seen:
            self.animation_seen_edges = []
        self.current_wave = []
        source_id = self.scenario.packet.source_id
        if not preserve_seen:
            self.animation_revealed_nodes = {source_id} if source_id else set()

    def _animate_next(self) -> None:
        if self.animation_index >= len(self.animation_waves):
            self.current_wave = []
            self.current_wave_hop = 0
            self.animation_after = None
            self.animation_progress = 0.0
            self._render_simulation_layers()
            if self.last_result is not None:
                if self.last_result.routing_mode == "DM_LEARNED":
                    self.status_var.set(
                        f"DM delivered over learned path · {self.last_result.transmissions} directed transmissions"
                    )
                elif self.last_result.routing_mode == "DM_DISCOVERY_FLOOD" and self.last_result.learned_route:
                    self.status_var.set(
                        "DM delivered and route learned · send again to use directed lines"
                    )
                else:
                    self.status_var.set(
                        f"Complete · {self.last_result.transmissions} transmissions · "
                        f"{self.last_result.collisions} collisions · seed {self.scenario.environment.seed}"
                    )
                self.root.after_idle(self._populate_results_once)
            return
        next_hop = self.animation_wave_hops[self.animation_index]
        missing_contours = [
            node_id
            for node_id in self.animation_transmitters.get(next_hop, [])
            if node_id not in self.animation_contours
        ]
        if (
            self.last_result is not None
            and result_uses_coverage_ripples(self.last_result)
            and missing_contours
            and not self.simulation_contours_complete
        ):
            self.status_var.set(
                f"Packet propagation active · preparing hop {next_hop} coverage "
                f"({len(missing_contours)} transmitter{'s' if len(missing_contours) != 1 else ''})"
            )
            self.animation_after = self.root.after(35, self._animate_next)
            return
        self.current_wave = self.animation_waves[self.animation_index]
        self.current_wave_hop = next_hop
        self.animation_frame = 0
        self.animation_frame_count = 18
        self._animate_frame()

    def _animate_frame(self) -> None:
        self.animation_progress = self.animation_frame / max(1, self.animation_frame_count - 1)
        self._render_current_wave_frame()
        if self.animation_frame >= self.animation_frame_count - 1:
            for event in self.current_wave:
                edge = (event.peer_id, event.node_id, event.kind, event.hop)
                if edge not in self.animation_seen_edges:
                    self.animation_seen_edges.append(edge)
                self.animation_revealed_nodes.add(event.node_id)
            transmitter_ids = self.animation_transmitters.get(self.current_wave_hop, [])
            if self.last_result is not None and result_uses_coverage_ripples(self.last_result):
                for source_id in first_hop_coverage_to_retain(
                    self.current_wave_hop,
                    transmitter_ids,
                    self.current_wave,
                ):
                    retained = (self.current_wave_hop, source_id)
                    if retained not in self.retained_coverage_transmitters:
                        self.retained_coverage_transmitters.append(retained)
            self._render_simulation_layers()
            self.animation_index += 1
            self.animation_after = self.root.after(240, self._animate_next)
        else:
            self.animation_frame += 1
            self.animation_after = self.root.after(48, self._animate_frame)

    def stop_animation(self) -> None:
        was_animating = self.animation_after is not None
        if self.animation_after:
            try:
                self.root.after_cancel(self.animation_after)
            except tk.TclError:
                pass
        self.animation_after = None
        self.animation_progress = 0.0
        if was_animating and self.last_result is not None:
            self.root.after_idle(self._populate_results_once)

    def _discard_inflight_simulation(self) -> None:
        """Reject a packet worker that owns a pre-edit scenario snapshot."""
        if self.simulation_thread is not None and self.simulation_thread.is_alive():
            self.simulation_request_id += 1
            self.simulation_thread = None
            self.simulation_contours_complete = True
            if hasattr(self, "send_button"):
                self.send_button.configure(state="normal", text="▶  Send packet")

    def _standalone_packet_active(self) -> bool:
        """Return whether packet output should be rebuilt for a finalized scene."""
        return bool(
            not self._live_mesh_running()
            and (
                self.last_result is not None
                or (self.simulation_thread is not None and self.simulation_thread.is_alive())
            )
        )

    def _suspend_active_rf_for_scene_change(
        self,
        *,
        active_beacon_id: str | None,
        stop_live_mesh: bool,
    ) -> None:
        """Hide stale RF output while terrain finalizes imported obstacles."""
        self._discard_inflight_simulation()
        if stop_live_mesh:
            self.stop_live_mesh(clear_visuals=True)
        if self.last_result is not None:
            # A selected live-mesh test also uses last_result for its map trace.
            # Never leave that pre-import path painted over the finalized scene.
            self.clear_results(render=False, update_status=False)
        if active_beacon_id is not None:
            self.stop_beacon(render=False)

    def _refresh_active_rf_after_scene_change(
        self,
        *,
        active_beacon_id: str | None,
        restart_live_mesh: bool,
        restart_packet: bool,
    ) -> None:
        """Restart each active RF engine from the finalized current scene."""
        self._discard_inflight_simulation()
        if restart_live_mesh:
            # LiveMeshEngine precomputes a link cache. Rebuild it once in its worker
            # so all scene changes affect traffic without blocking Tk.
            self.stop_live_mesh(clear_visuals=True)
            if self.last_result is not None:
                self.clear_results(render=False, update_status=False)
            self.live_mesh_enabled.set(True)
            self.start_live_mesh()

        if active_beacon_id is not None:
            node = next(
                (candidate for candidate in self.scenario.nodes if candidate.id == active_beacon_id),
                None,
            )
            if node is not None and node.online:
                # A clean start intentionally matches clicking Beacon after every
                # obstacle and terrain elevation is already loaded.
                self._queue_beacon_profile(node, keep_existing=False, render_on_stop=False)

        if restart_packet and not restart_live_mesh:
            if self.last_result is not None:
                self.clear_results(render=False, update_status=False)
            # Queue after the current terrain/import callback returns so the
            # packet worker snapshots exactly the same finalized scene as a
            # packet sent manually after obstacle loading.
            self.root.after_idle(self.run_simulation)

    def _beacon_running(self) -> bool:
        return self.beacon_after is not None

    @staticmethod
    def _beacon_ray_count(obstacle_count: int) -> int:
        """Fewer rays on obstacle-dense maps keeps the one-off sweep responsive."""
        if obstacle_count <= 200:
            return 120
        if obstacle_count <= 1000:
            return 96
        if obstacle_count <= 4000:
            return 72
        return 48

    def start_beacon(self) -> None:
        """Turn the selected node into a pulsating beacon that maps its own coverage.

        The coverage sweep is computed on a worker thread and bounded to the
        viewport, so even a dense imported map cannot freeze the UI."""
        node = next((n for n in self.scenario.nodes if n.id == self.selected_id), None)
        if node is None:
            self.status_var.set("Select a node first, then pulse a beacon from it")
            return
        if not node.online:
            self.status_var.set(f"{node.name} is offline · bring it online to pulse a beacon")
            return
        self._queue_beacon_profile(node, keep_existing=False)

    def _recompute_active_beacon(self, node_id: str) -> None:
        """Reprofile a running beacon without hiding its current coverage."""
        node = next((candidate for candidate in self.scenario.nodes if candidate.id == node_id), None)
        if node is None or not node.online:
            self.stop_beacon()
            return
        self._queue_beacon_profile(node, keep_existing=True)

    def _queue_beacon_profile(
        self,
        node: Node,
        *,
        keep_existing: bool,
        render_on_stop: bool = True,
    ) -> None:
        if keep_existing:
            self.beacon_cancel.set()
        else:
            self.stop_beacon(render=render_on_stop)
            self.beacon_node_id = node.id
            self.beacon_phase = 0.0
        self.beacon_cancel = threading.Event()
        cancel = self.beacon_cancel
        self.beacon_request_id += 1
        request_id = self.beacon_request_id
        cap = self._coverage_range_cap()
        samples = max(
            144,
            self._beacon_ray_count(len(self.scenario.nodes) + len(self.scenario.obstacles)),
        )
        segment_samples = max(
            56,
            min(96, math.ceil(max(self.canvas.winfo_width(), self.canvas.winfo_height()) / 24)),
        )
        snapshot = self.scenario
        action = "Updating" if keep_existing else "Computing"
        self.status_var.set(f"{action} beacon coverage from {node.name}…")

        def worker() -> None:
            try:
                model = PropagationModel(snapshot)
                profile = model.beacon_profile(
                    node,
                    angular_samples=samples,
                    max_range_m=cap,
                    segment_samples=segment_samples,
                )
                if not cancel.is_set():
                    self.beacon_compute_queue.put((request_id, profile, None))
            except Exception as error:  # noqa: BLE001 - surfaced to the status bar
                self.beacon_compute_queue.put((request_id, None, error))

        threading.Thread(target=worker, name="BeaconProfile", daemon=True).start()
        if self.beacon_compute_after is not None:
            try:
                self.root.after_cancel(self.beacon_compute_after)
            except tk.TclError:
                pass
        self.beacon_compute_after = self.root.after(40, self._poll_beacon_compute)

    def _poll_beacon_compute(self) -> None:
        self.beacon_compute_after = None
        while True:
            try:
                request_id, profile, error = self.beacon_compute_queue.get_nowait()
            except queue.Empty:
                if self.beacon_node_id is not None and not self.beacon_cancel.is_set():
                    self.beacon_compute_after = self.root.after(50, self._poll_beacon_compute)
                return
            if request_id == self.beacon_request_id and not self.beacon_cancel.is_set():
                break
            # Drain superseded worker results without abandoning the current poll.
        if error is not None or profile is None:
            self.status_var.set(f"Beacon failed: {error}")
            self.stop_beacon()
            return
        if self.beacon_after is not None:
            try:
                self.root.after_cancel(self.beacon_after)
            except tk.TclError:
                pass
            self.beacon_after = None
        self.beacon_profile = profile
        self.beacon_segment_photo_key = None
        self._beacon_ripple_profile_id = None
        self._beacon_ripple_geometry = []
        blocking = set(profile.blocking_obstacle_ids)
        weakening = set(profile.weakening_obstacle_ids)
        self.beacon_blocking_obstacles = [o for o in self.scenario.obstacles if o.id in blocking]
        self.beacon_weakening_obstacles = [o for o in self.scenario.obstacles if o.id in weakening]
        node = next((n for n in self.scenario.nodes if n.id == self.beacon_node_id), None)
        name = node.name if node is not None else "node"
        blocked = sum(1 for ray in profile.rays if ray.kind == "blocked")
        weakened = sum(1 for ray in profile.rays if ray.kind == "weakened")
        self.status_var.set(
            f"Beacon pulsing from {name} · {blocked} blocked · {weakened} weakened directions"
        )
        self.beacon_phase = 0.0
        if hasattr(self, "canvas"):
            self.canvas.delete(BEACON_STATIC_TAG)
            self.canvas.delete(BEACON_ANIMATION_TAG)
        self._beacon_tick()

    def stop_beacon(self, render: bool = True) -> None:
        needs_full_render = self.zoom_preview_composite_active
        self.beacon_cancel.set()
        self.beacon_request_id += 1
        if self.beacon_compute_after is not None:
            try:
                self.root.after_cancel(self.beacon_compute_after)
            except tk.TclError:
                pass
        self.beacon_compute_after = None
        if self.beacon_after is not None:
            try:
                self.root.after_cancel(self.beacon_after)
            except tk.TclError:
                pass
        self.beacon_after = None
        self.beacon_node_id = None
        self.beacon_profile = None
        self.beacon_segment_photo = None
        self.beacon_segment_photo_key = None
        self.beacon_segment_source = None
        self._beacon_ripple_profile_id = None
        self._beacon_ripple_geometry = []
        self.beacon_blocking_obstacles = []
        self.beacon_weakening_obstacles = []
        self.beacon_phase = 0.0
        if render and hasattr(self, "canvas"):
            if needs_full_render:
                self.render_canvas()
            else:
                self.canvas.delete(BEACON_TAG)

    def _beacon_tick(self) -> None:
        # The node may have been deleted or taken offline while pulsing.
        node = next((n for n in self.scenario.nodes if n.id == self.beacon_node_id), None)
        if self.beacon_profile is None or node is None or not node.online:
            self.stop_beacon()
            return
        self.beacon_phase = (self.beacon_phase + 0.045) % 1.0
        if hasattr(self, "canvas"):
            # Wheel input already scales the existing pulse. Avoid rebuilding and
            # retagging animation items while a coalesced zoom preview is pending;
            # the normal 45 ms cadence resumes as soon as zoom settles.
            if self.zoom_render_after is not None or self.zoom_preview_after is not None:
                self.beacon_after = self.root.after(45, self._beacon_tick)
                return
            c = self.canvas
            if not c.find_withtag(BEACON_STATIC_TAG):
                static_start = len(c.find_all())
                self._draw_beacon(c, draw_animation=False)
                self._tag_items_created_since(c, static_start, BEACON_STATIC_TAG, BEACON_TAG)
            c.tag_raise(BEACON_STATIC_TAG)
            c.delete(BEACON_ANIMATION_TAG)
            starting_count = len(c.find_all())
            self._draw_beacon(c, draw_static=False)
            self._tag_items_created_since(c, starting_count, BEACON_ANIMATION_TAG, BEACON_TAG)
        self.beacon_after = self.root.after(45, self._beacon_tick)

    def clear_static_coverage(self, render: bool = True) -> None:
        self.static_coverage_cancel.set()
        self.static_coverage_request_id += 1
        if self.static_coverage_after is not None:
            try:
                self.root.after_cancel(self.static_coverage_after)
            except tk.TclError:
                pass
        self.static_coverage_after = None
        self.static_coverage_profile = None
        self.static_segment_photo = None
        self.static_segment_photo_key = None
        self.static_coverage_blocking = []
        self.static_coverage_weakening = []
        if render and hasattr(self, "canvas"):
            self.canvas.delete(STATIC_COVERAGE_TAG)

    def _begin_source_coverage(self, continue_after: bool) -> None:
        """Show the zero-hop coverage heatmap expanding from the sender.  When it
        finishes it either freezes (packet reached nobody) or clears itself and
        hands off to the normal hop animation (``continue_after``)."""
        result = self.last_result
        if result is None:
            return
        source_id = self.scenario.packet.source_id
        node = next((n for n in self.scenario.nodes if n.id == source_id), None)
        if node is None or not node.online:
            if continue_after:
                self._animate_next()
            return
        self.static_coverage_then_animate = continue_after
        self.clear_static_coverage(render=False)
        self.static_coverage_cancel = threading.Event()
        cancel = self.static_coverage_cancel
        self.static_coverage_request_id += 1
        request_id = self.static_coverage_request_id
        cap = self._coverage_range_cap()
        samples = max(
            144,
            self._beacon_ray_count(len(self.scenario.nodes) + len(self.scenario.obstacles)),
        )
        segment_samples = max(
            56,
            min(96, math.ceil(max(self.canvas.winfo_width(), self.canvas.winfo_height()) / 24)),
        )
        snapshot = self.scenario
        name = node.name

        def worker() -> None:
            try:
                model = PropagationModel(snapshot)
                profile = model.beacon_profile(
                    node,
                    angular_samples=samples,
                    max_range_m=cap,
                    segment_samples=segment_samples,
                )
                if not cancel.is_set():
                    self.static_coverage_queue.put((request_id, profile, None, name))
            except Exception as error:  # noqa: BLE001 - surfaced to the status bar
                self.static_coverage_queue.put((request_id, None, error, name))

        threading.Thread(target=worker, name="PacketCoverage", daemon=True).start()
        self.root.after(40, self._poll_static_coverage)

    def _poll_static_coverage(self) -> None:
        try:
            request_id, profile, error, name = self.static_coverage_queue.get_nowait()
        except queue.Empty:
            if not self.static_coverage_cancel.is_set():
                self.root.after(50, self._poll_static_coverage)
            return
        if request_id != self.static_coverage_request_id or self.static_coverage_cancel.is_set():
            return
        if error is not None or profile is None:
            return
        self.static_coverage_profile = profile
        blocking = set(profile.blocking_obstacle_ids)
        weakening = set(profile.weakening_obstacle_ids)
        self.static_coverage_blocking = [o for o in self.scenario.obstacles if o.id in blocking]
        self.static_coverage_weakening = [o for o in self.scenario.obstacles if o.id in weakening]
        blocked = sum(1 for ray in profile.rays if ray.kind == "blocked")
        weakened = sum(1 for ray in profile.rays if ray.kind == "weakened")
        if self.static_coverage_then_animate:
            self.status_var.set(f"Coverage from {name} · continuing packet propagation…")
        else:
            self.status_var.set(
                f"Packet reached no other node · coverage from {name} · "
                f"{blocked} blocked · {weakened} weakened directions"
            )
        # One beacon-style ripple over the fixed, physically sampled footprint.
        self.static_coverage_grow = 0.0
        self._animate_static_coverage()

    def _animate_static_coverage(self) -> None:
        if self.static_coverage_profile is None:
            return
        self._render_static_coverage_layer()
        if self.static_coverage_grow >= 1.0:
            self.static_coverage_after = None
            if self.static_coverage_then_animate:
                # Zero hop shown -- clear it and continue with the later hops only.
                self.static_coverage_then_animate = False
                self.clear_static_coverage()
                self._continue_after_zero_hop()
            return
        self.static_coverage_grow = min(1.0, self.static_coverage_grow + 0.045)
        self.static_coverage_after = self.root.after(45, self._animate_static_coverage)

    def _continue_after_zero_hop(self) -> None:
        """The heatmap already showed the source's own (hop-1) coverage, so reveal
        the nodes it reached directly and animate only the later relay hops -- no
        second ripple of the same zero-hop coverage."""
        if self.animation_waves:
            for event in self.animation_waves[0]:
                self.animation_revealed_nodes.add(event.node_id)
                edge = (event.peer_id, event.node_id, event.kind, event.hop)
                if edge not in self.animation_seen_edges:
                    self.animation_seen_edges.append(edge)
            self.animation_index = 1  # skip hop 1 (already shown as the heatmap)
        self._animate_next()

    def _render_static_coverage_layer(self) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(STATIC_COVERAGE_TAG)
        start = len(c.find_all())
        self._draw_static_coverage(c)
        self._tag_items_created_since(c, start, STATIC_COVERAGE_TAG)

    @staticmethod
    def _ray_half_widths(rays: list[BeaconRay]) -> list[tuple[float, float]]:
        """Each ray's (left, right) half-angle to its neighbour, not an assumed
        uniform step -- rays are no longer evenly spaced once a real node's
        exact bearing is inserted between two evenly-spaced samples."""
        count = len(rays)
        widths: list[tuple[float, float]] = []
        for index, ray in enumerate(rays):
            previous_angle = rays[(index - 1) % count].angle
            next_angle = rays[(index + 1) % count].angle
            left_gap = (ray.angle - previous_angle) % math.tau
            right_gap = (next_angle - ray.angle) % math.tau
            widths.append((left_gap / 2.0, right_gap / 2.0))
        return widths

    def _draw_segmented_coverage(
        self,
        c: tk.Canvas,
        profile: BeaconProfile,
        *,
        cache_prefix: str,
        grow: float = 1.0,
    ) -> bool:
        """Paint only sampled reachable ray sections, preserving gaps between them."""
        if len(profile.rays) < 3 or any(len(ray.samples) < 2 for ray in profile.rays):
            return False
        width = max(1, c.winfo_width())
        height = max(1, c.winfo_height())
        grow = max(0.0, min(1.0, grow))
        key = (
            id(profile),
            width,
            height,
            round(grow, 3),
            round(self.view_x, 3),
            round(self.view_y, 3),
            round(self.zoom, 6),
        )
        key_name = f"{cache_prefix}_segment_photo_key"
        photo_name = f"{cache_prefix}_segment_photo"
        photo = getattr(self, photo_name, None)
        if getattr(self, key_name, None) != key or photo is None:
            render_scale = 2
            layer = Image.new(
                "RGBA",
                (width * render_scale, height * render_scale),
                (0, 0, 0, 0),
            )
            drawing = ImageDraw.Draw(layer, "RGBA")
            ox, oy = profile.x, profile.y
            world_scale = self._base_scale() * self.zoom
            screen_ox = (ox - self.view_x) * world_scale * render_scale
            screen_oy = (oy - self.view_y) * world_scale * render_scale
            distance_scale = grow * world_scale * render_scale

            def screen_at_vector(
                cosine: float,
                sine: float,
                distance: float,
            ) -> tuple[float, float]:
                scaled_distance = distance * distance_scale
                return (
                    screen_ox + cosine * scaled_distance,
                    screen_oy + sine * scaled_distance,
                )

            ray_count = len(profile.rays)
            outer_points: list[tuple[float, float] | None] = []
            outer_distances: list[float] = []
            directions: list[tuple[float, float, float, float]] = []
            half_widths = self._ray_half_widths(profile.rays)
            for ray, (left_half, right_half) in zip(profile.rays, half_widths):
                reachable = [sample.distance_m for sample in ray.samples if sample.reachable]
                outer = max(reachable) if reachable else 0.0
                outer_distances.append(outer)
                cosine, sine = math.cos(ray.angle), math.sin(ray.angle)
                left_angle = ray.angle - left_half
                right_angle = ray.angle + right_half
                directions.append(
                    (
                        math.cos(left_angle),
                        math.sin(left_angle),
                        math.cos(right_angle),
                        math.sin(right_angle),
                    )
                )
                outer_points.append(
                    screen_at_vector(cosine, sine, outer) if outer > 0 else None
                )

            # Each sampled direction owns its angular wedge. This retains the
            # original bold ray shape instead of eroding it whenever one adjacent
            # direction differs, while every radial section still has to pass its
            # own complete calibrated link-budget check.
            for ray_index, ray in enumerate(profile.rays):
                outer = max(1.0, outer_distances[ray_index])
                left_cosine, left_sine, right_cosine, right_sine = directions[ray_index]
                crossed_gap = False
                for radial_index in range(len(ray.samples) - 1):
                    inner, outer_sample = ray.samples[radial_index : radial_index + 2]
                    if not (inner.reachable and outer_sample.reachable):
                        crossed_gap = True
                        continue
                    midpoint_distance = (inner.distance_m + outer_sample.distance_m) * 0.5
                    strength_position = max(0.0, min(1.0, midpoint_distance / outer))
                    margin = (inner.margin_db + outer_sample.margin_db) * 0.5
                    margin_position = 1.0 - max(0.0, min(1.0, margin / 24.0))
                    obstacle_loss = (
                        inner.obstacle_loss_db + outer_sample.obstacle_loss_db
                    ) * 0.5
                    obstacle_position = max(0.0, min(1.0, obstacle_loss / 24.0))
                    # Preserve the established distance bands and boundary shape,
                    # while making each accumulated penetration visible immediately.
                    # Margin controls the fade near decoding limits; cumulative
                    # obstacle loss prevents a ray from staying falsely green after
                    # it has already passed through one or more buildings.
                    local_loss_position = max(margin_position, obstacle_position)
                    strength_position = strength_position * 0.65 + local_loss_position * 0.35
                    if crossed_gap:
                        # A section that reappears on elevated terrain uses its
                        # measured margin, so a strong mountain-top path can be
                        # green even though dead ground separates it from source.
                        strength_position = margin_position
                    red, green, blue = ImageColor.getrgb(self._strength_color(strength_position))
                    drawing.polygon(
                        (
                            screen_at_vector(left_cosine, left_sine, inner.distance_m),
                            screen_at_vector(left_cosine, left_sine, outer_sample.distance_m),
                            screen_at_vector(right_cosine, right_sine, outer_sample.distance_m),
                            screen_at_vector(right_cosine, right_sine, inner.distance_m),
                        ),
                        fill=(red, green, blue, 145),
                    )

            # Keep the original crisp outer boundary, but break it wherever the
            # sampled signal itself has no adjacent reachable section.
            for ray_index, first in enumerate(outer_points):
                second = outer_points[(ray_index + 1) % ray_count]
                if first is None or second is None:
                    continue
                drawing.line((first, second), fill=(5, 8, 13, 255), width=6)
                drawing.line((first, second), fill=(255, 255, 255, 255), width=2)

            # Warning footprints used to be individual Tk canvas polygons. Dense
            # imports could add thousands of animated-layer items, making every
            # wheel scale and beacon tag_raise proportional to building count.
            # Paint the identical yellow/red overlay into the cached coverage
            # bitmap so zoom and pulse animation remain constant-time.
            if cache_prefix == "beacon":
                weakening = self.beacon_weakening_obstacles
                blocking = self.beacon_blocking_obstacles
            elif grow >= 0.999:
                weakening = self.static_coverage_weakening
                blocking = self.static_coverage_blocking
            else:
                weakening = []
                blocking = []
            self._draw_segmented_warning_obstacles(
                layer,
                weakening,
                blocking,
                profile=profile,
                render_scale=render_scale,
                grow=grow,
            )

            layer = layer.resize((width, height), Image.Resampling.LANCZOS)
            setattr(self, f"{cache_prefix}_segment_source", layer)
            photo = ImageTk.PhotoImage(layer)
            setattr(self, photo_name, photo)
            setattr(self, key_name, key)
        c.create_image(
            0,
            0,
            anchor="nw",
            image=photo,
            tags=(f"{cache_prefix}-segment-image",),
        )
        return True

    def _draw_segmented_warning_obstacles(
        self,
        layer: Image.Image,
        weakening: list[Obstacle],
        blocking: list[Obstacle],
        *,
        profile: BeaconProfile,
        render_scale: int,
        grow: float,
    ) -> None:
        """Rasterize warning footprints only inside rays that touched them."""
        if not weakening and not blocking:
            return
        scale = self._base_scale() * self.zoom * render_scale
        view_x = self.view_x
        view_y = self.view_y
        viewport = (
            view_x,
            view_y,
            view_x + layer.width / max(scale, 1e-12),
            view_y + layer.height / max(scale, 1e-12),
        )
        def point(world_x: float, world_y: float) -> tuple[float, float]:
            return ((world_x - view_x) * scale, (world_y - view_y) * scale)

        def draw_obstacle(drawing: ImageDraw.ImageDraw, obstacle: Obstacle, color: str) -> None:
            if not self._bounds_overlap(self._obstacle_bounds(obstacle), viewport):
                return
            red, green, blue = ImageColor.getrgb(color)
            fill = (red, green, blue, 128)
            outline = (red, green, blue, 255)
            line_width = max(1, 2 * render_scale)
            if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
                drawing.polygon(
                    [point(x, y) for x, y in obstacle.points],
                    fill=fill,
                    outline=outline,
                    width=line_width,
                )
                return
            if obstacle.shape == "brush" and obstacle.points:
                coordinates = [point(x, y) for x, y in obstacle.points]
                if len(coordinates) == 1:
                    coordinates.append(coordinates[0])
                drawing.line(
                    coordinates,
                    fill=fill,
                    width=max(6 * render_scale, round(obstacle.brush_radius_m * 2 * scale)),
                    joint="curve",
                )
                return
            x_min, y_min, x_max, y_max = obstacle.normalized()
            drawing.rectangle(
                (*point(x_min, y_min), *point(x_max, y_max)),
                fill=fill,
                outline=outline,
                width=line_width,
            )

        weakening_ids = {obstacle.id for obstacle in weakening}
        blocking_ids = {obstacle.id for obstacle in blocking}
        half_widths = self._ray_half_widths(profile.rays)

        def ray_mask(wanted_ids: set[str]) -> Image.Image:
            mask = Image.new("L", layer.size, 0)
            mask_drawing = ImageDraw.Draw(mask)
            source = point(profile.x, profile.y)
            for ray, (left_half, right_half) in zip(profile.rays, half_widths):
                if not wanted_ids.intersection(ray.obstacle_ids):
                    continue
                reachable = [sample.distance_m for sample in ray.samples if sample.reachable]
                if not reachable:
                    continue
                distance = min(profile.max_reach_m, max(reachable) + 60.0) * grow
                left = point(
                    profile.x + math.cos(ray.angle - left_half) * distance,
                    profile.y + math.sin(ray.angle - left_half) * distance,
                )
                right = point(
                    profile.x + math.cos(ray.angle + right_half) * distance,
                    profile.y + math.sin(ray.angle + right_half) * distance,
                )
                mask_drawing.polygon((source, left, right), fill=255)
            return mask

        def composite_group(obstacles: list[Obstacle], color: str, mask: Image.Image) -> None:
            if not obstacles or mask.getbbox() is None:
                return
            overlay = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            drawing = ImageDraw.Draw(overlay, "RGBA")
            for obstacle in obstacles:
                draw_obstacle(drawing, obstacle, color)
            overlay.putalpha(ImageChops.multiply(overlay.getchannel("A"), mask))
            layer.alpha_composite(overlay)

        composite_group(weakening, self._BEACON_SLOW, ray_mask(weakening_ids))
        # Draw blockers last so red keeps priority where footprints overlap.
        composite_group(blocking, self._BEACON_BLOCK, ray_mask(blocking_ids))

    def _draw_segmented_ripple(
        self,
        c: tk.Canvas,
        profile: BeaconProfile,
        fraction: float,
    ) -> bool:
        """Draw a broken pulse only across currently reachable radial sections."""
        if len(profile.rays) < 3 or any(len(ray.samples) < 2 for ray in profile.rays):
            return False
        points: list[tuple[float, float] | None] = []
        ox, oy = profile.x, profile.y
        profile_id = id(profile)
        if self._beacon_ripple_profile_id != profile_id:
            self._beacon_ripple_profile_id = profile_id
            self._beacon_ripple_geometry = []
            for ray in profile.rays:
                distances = tuple(sample.distance_m for sample in ray.samples)
                reachable = tuple(sample.reachable for sample in ray.samples)
                outer = max(
                    (distance for distance, is_reachable in zip(distances, reachable) if is_reachable),
                    default=0.0,
                )
                self._beacon_ripple_geometry.append(
                    (math.cos(ray.angle), math.sin(ray.angle), outer, distances, reachable)
                )
        scale = self._base_scale() * self.zoom
        screen_ox = (ox - self.view_x) * scale
        screen_oy = (oy - self.view_y) * scale
        for cosine, sine, outer, distances, reachable in self._beacon_ripple_geometry:
            distance = outer * fraction
            insertion = bisect_left(distances, distance)
            if insertion <= 0:
                nearest = 0
            elif insertion >= len(distances):
                nearest = len(distances) - 1
            else:
                lower = insertion - 1
                nearest = (
                    lower
                    if distance - distances[lower] <= distances[insertion] - distance
                    else insertion
                )
            if not reachable[nearest]:
                points.append(None)
                continue
            points.append((screen_ox + cosine * distance * scale, screen_oy + sine * distance * scale))
        valid_edges = [
            first is not None and points[(index + 1) % len(points)] is not None
            for index, first in enumerate(points)
        ]
        runs: list[list[tuple[float, float]]] = []
        if all(valid_edges):
            closed = [point for point in points if point is not None]
            closed.append(closed[0])
            runs.append(closed)
        elif any(valid_edges):
            # Start immediately after a broken edge so a run may safely wrap
            # around angle zero without joining across an unreachable section.
            broken = valid_edges.index(False)
            run: list[tuple[float, float]] = []
            for offset in range(1, len(points) + 1):
                index = (broken + offset) % len(points)
                if valid_edges[index]:
                    first = points[index]
                    second = points[(index + 1) % len(points)]
                    assert first is not None and second is not None
                    if not run:
                        run.append(first)
                    run.append(second)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
        for run in runs:
            coordinates = [coordinate for point in run for coordinate in point]
            c.create_line(
                *coordinates,
                fill=self._BEACON_EDGE,
                width=3,
                joinstyle=tk.ROUND,
            )
        return True

    def _draw_static_coverage(self, c: tk.Canvas) -> None:
        """Fixed zero-hop footprint with one beacon-style reachable-only ripple."""
        profile = self.static_coverage_profile
        if profile is None or len(profile.rays) < 3:
            return
        phase = max(0.0, min(1.0, self.static_coverage_grow))
        # The coverage is already the result of complete per-segment link checks.
        # Keep it fixed at its true location: scaling it during animation drags
        # separated reachable sections through dead ground and falsely paints that
        # ground as covered.  Only the broken reception ripple travels outward.
        if self._draw_segmented_coverage(c, profile, cache_prefix="static", grow=1.0):
            if phase > 0.02:
                self._draw_segmented_ripple(c, profile, phase)
            return
        w2s = self.world_to_screen
        ox, oy = profile.x, profile.y
        # Compatibility fallback for old profiles without radial samples: keep the
        # footprint fixed rather than implying reception in unsampled locations.
        world = [
            (ox + math.cos(ray.angle) * ray.reach_m, oy + math.sin(ray.angle) * ray.reach_m)
            for ray in profile.rays
        ]
        bands = 7
        for band in range(bands, 0, -1):
            frac = band / bands
            color = self._strength_color((band - 0.5) / bands)
            coords: list[float] = []
            for (bx, by) in world:
                coords.extend(w2s(ox + (bx - ox) * frac, oy + (by - oy) * frac))
            if len(coords) >= 6:
                c.create_polygon(*coords, fill=color, outline="", stipple="gray50")
        edge: list[float] = []
        for (bx, by) in world:
            edge.extend(w2s(bx, by))
        edge.extend(edge[:2])
        c.create_line(*edge, fill=self._BEACON_HALO, width=3, joinstyle=tk.ROUND)
        c.create_line(*edge, fill="#ffffff", width=1, joinstyle=tk.ROUND)
        for obstacle in self.static_coverage_weakening:
            self._draw_beacon_obstacle(c, obstacle, self._BEACON_SLOW, 2, fill=True)
        for obstacle in self.static_coverage_blocking:
            self._draw_beacon_obstacle(c, obstacle, self._BEACON_BLOCK, 2, fill=True)

    def clear_results(self, *, render: bool = True, update_status: bool = True) -> None:
        if self._live_mesh_running():
            self.live_mesh_hidden_test_ids.update(self.live_mesh_tests)
            self.live_path_test_id = None
        self.stop_animation()
        self.clear_static_coverage(render=False)
        self.last_result = None
        self.results_populated = True
        self.path_focus_id = None
        self.results_stale = False
        self.animation_waves = []
        self.animation_wave_hops = []
        self.current_wave_hop = 0
        self.animation_seen_edges = []
        self.retained_coverage_transmitters = []
        self.current_wave = []
        self.animation_transmitters = {}
        self.animation_contours = {}
        self.animation_revealed_nodes = set()
        if hasattr(self, "events_tree"):
            for tree in (self.events_tree, self.nodes_tree, self.links_tree):
                tree.delete(*tree.get_children())
            if hasattr(self, "live_tests_tree"):
                self.live_tests_tree.delete(*self.live_tests_tree.get_children())
                self.live_detail_tree.delete(*self.live_detail_tree.get_children())
            for variable in self.metric_vars.values():
                variable.set("—")
            self.result_status.configure(text="No packet sent")
        if hasattr(self, "send_button"):
            self.send_button.configure(state="normal", text="▶  Send packet")
        if hasattr(self, "clear_hops_button"):
            self.clear_hops_button.configure(state="disabled")
        if update_status:
            self.status_var.set(
                "Packet traces cleared · live mesh traffic continues"
                if self._live_mesh_running() else "Packet traces cleared · ready to send"
            )
        if render:
            self.render_canvas()

    def _mark_results_stale(self) -> None:
        """Retain displayed hops after edits until the user explicitly clears them."""
        if self.last_result is None:
            return
        self.results_stale = True
        if hasattr(self, "result_status"):
            self.result_status.configure(
                text=f"{len(self.last_result.reached)} nodes heard packet · retained from before edits"
            )

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.temp_obstacle = None
        self.temp_forest_points = []
        self.profile_point_a = None
        for key, button in getattr(self, "tool_buttons", {}).items():
            button.configure(style="ActiveTool.TButton" if key == tool else "Tool.TButton")
        cursors = {"select": "arrow", "node": "crosshair"}
        if hasattr(self, "canvas"):
            self.canvas.configure(cursor=cursors.get(tool, "crosshair"))
        if tool == "select":
            self.status_var.set("Select and drag objects · right-drag pans · wheel zooms")
        elif tool == "node":
            self.status_var.set("Node tool stays active: click repeatedly to place nodes")
        elif tool == "beacon":
            self.status_var.set("Beacon tool: click the map to drop a pulsating beacon")
        elif tool == "horizon":
            self.status_var.set("Horizon tool: click a node (uses its real height) or a bare point to view its 360° terrain/obstacle skyline")
        elif tool == "profile":
            self.status_var.set("Profile tool: click two nodes or points to view the terrain/obstacle cross-section between them")
        elif tool == "Forest":
            self.status_var.set("Forest brush stays active: press and drag to paint forest")
        else:
            self.status_var.set(f"{tool} tool stays active: drag repeatedly to place obstructions")

    def world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        transform = getattr(self, "_world_screen_transform", None)
        if (
            transform is None
            or transform[0] != self.view_x
            or transform[1] != self.view_y
            or transform[2] != self.zoom
        ):
            transform = (self.view_x, self.view_y, self.zoom, self._base_scale() * self.zoom)
            self._world_screen_transform = transform
        return (x - transform[0]) * transform[3], (y - transform[1]) * transform[3]

    def screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        transform = getattr(self, "_world_screen_transform", None)
        if (
            transform is None
            or transform[0] != self.view_x
            or transform[1] != self.view_y
            or transform[2] != self.zoom
        ):
            transform = (self.view_x, self.view_y, self.zoom, self._base_scale() * self.zoom)
            self._world_screen_transform = transform
        return x / max(1e-9, transform[3]) + transform[0], y / max(1e-9, transform[3]) + transform[1]

    def _coverage_range_cap(self) -> float:
        """How far coverage/beacon rays should be traced: the visible map plus a
        margin.  Tracing the full 100+ km link budget is what makes sweeps freeze,
        and reach past the viewport is not visible anyway."""
        if not hasattr(self, "canvas"):
            return 20_000.0
        left, top = self.screen_to_world(0, 0)
        right, bottom = self.screen_to_world(self.canvas.winfo_width(), self.canvas.winfo_height())
        diagonal = math.hypot(right - left, bottom - top)
        return max(3_000.0, diagonal * 1.15)

    def _base_scale(self) -> float:
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        env = self.scenario.environment
        # The reference span establishes a useful zoom scale; it is not a
        # boundary and does not participate in object coordinates.
        return min(
            width / max(1.0, env.initial_view_width_m),
            height / max(1.0, env.initial_view_height_m),
        )

    def _scene_bounds(self) -> tuple[float, float, float, float]:
        """Return object extents, or an origin-centered default for a blank scene."""
        bounds = [self._obstacle_bounds(obstacle) for obstacle in self.scenario.obstacles]
        xs = [node.x for node in self.scenario.nodes]
        ys = [node.y for node in self.scenario.nodes]
        for left, top, right, bottom in bounds:
            xs.extend((left, right))
            ys.extend((top, bottom))
        env = self.scenario.environment
        if not xs or not ys:
            return (
                -env.initial_view_width_m / 2.0,
                -env.initial_view_height_m / 2.0,
                env.initial_view_width_m / 2.0,
                env.initial_view_height_m / 2.0,
            )
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        minimum_width = max(500.0, env.initial_view_width_m * 0.05)
        minimum_height = max(350.0, env.initial_view_height_m * 0.05)
        if right - left < minimum_width:
            center = (left + right) / 2.0
            left, right = center - minimum_width / 2.0, center + minimum_width / 2.0
        if bottom - top < minimum_height:
            center = (top + bottom) / 2.0
            top, bottom = center - minimum_height / 2.0, center + minimum_height / 2.0
        padding_x = max(100.0, (right - left) * 0.08)
        padding_y = max(100.0, (bottom - top) * 0.08)
        return left - padding_x, top - padding_y, right + padding_x, bottom + padding_y

    def fit_view(self) -> None:
        left, top, right, bottom = self._scene_bounds()
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        base = self._base_scale()
        target_width = max(1.0, right - left)
        target_height = max(1.0, bottom - top)
        self.zoom = clamp(
            min(canvas_width / (target_width * base), canvas_height / (target_height * base)) * 0.96,
            MIN_CANVAS_ZOOM,
            MAX_CANVAS_ZOOM,
        )
        visible_w = canvas_width / max(1e-9, base * self.zoom)
        visible_h = canvas_height / max(1e-9, base * self.zoom)
        self.view_x = (left + right - visible_w) / 2.0
        self.view_y = (top + bottom - visible_h) / 2.0
        self.render_canvas()

    def fit_survey_view(self) -> None:
        points = [
            position
            for measurement in self.survey_measurements
            if (position := self._survey_world_position(measurement)) is not None
        ]
        base = self._survey_base_world_position()
        if base:
            points.append(base)
        if not points:
            return
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        span_x = max(120.0, right - left)
        span_y = max(120.0, bottom - top)
        left, right = (left + right - span_x) / 2.0, (left + right + span_x) / 2.0
        top, bottom = (top + bottom - span_y) / 2.0, (top + bottom + span_y) / 2.0
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        scale = self._base_scale()
        self.zoom = clamp(
            min(canvas_width / max(1.0, right - left), canvas_height / max(1.0, bottom - top))
            / max(scale, 1e-9)
            * 0.86,
            MIN_CANVAS_ZOOM,
            MAX_CANVAS_ZOOM,
        )
        visible_width = canvas_width / max(1e-9, scale * self.zoom)
        visible_height = canvas_height / max(1e-9, scale * self.zoom)
        self.view_x = (left + right - visible_width) / 2.0
        self.view_y = (top + bottom - visible_height) / 2.0
        self.map_visible.set(True)
        self.render_canvas()

    def select_survey_measurement(self, index: int, center: bool = True) -> None:
        if not 0 <= index < len(self.survey_measurements):
            return
        self.survey_selected_index = index
        position = self._survey_world_position(self.survey_measurements[index])
        if center and position:
            visible_width = self.canvas.winfo_width() / max(1e-9, self._base_scale() * self.zoom)
            visible_height = self.canvas.winfo_height() / max(1e-9, self._base_scale() * self.zoom)
            self.view_x = position[0] - visible_width / 2.0
            self.view_y = position[1] - visible_height / 2.0
        if hasattr(self, "survey_tree"):
            item = str(index)
            if self.survey_tree.exists(item):
                if self.survey_tree.selection() != (item,):
                    self.survey_tree.selection_set(item)
                self.survey_tree.see(item)
        measurement = self.survey_measurements[index]
        forward = survey_float(measurement.get("forward_rssi_dbm"))
        reverse = survey_float(measurement.get("reverse_rssi_dbm"))
        sequence = measurement.get("sequence", index + 1)
        message = (
            f"Survey point #{sequence} · forward {forward:.0f} dBm"
            if forward is not None
            else f"Survey point #{sequence} · forward packet lost"
        )
        if reverse is not None:
            message += f" · reply {reverse:.0f} dBm"
        self.status_var.set(message)
        self.render_canvas()

    def _survey_hit_test(self, screen_x: float, screen_y: float) -> int | None:
        best: tuple[float, int] | None = None
        for index, measurement in enumerate(self.survey_measurements):
            position = self._survey_world_position(measurement)
            if position is None:
                continue
            x, y = self.world_to_screen(*position)
            distance = math.hypot(screen_x - x, screen_y - y)
            if distance <= 10 and (best is None or distance < best[0]):
                best = (distance, index)
        return best[1] if best else None

    def render_canvas(self, *, reuse_geographic_layer: bool = False) -> None:
        if not hasattr(self, "canvas"):
            return
        if self.zoom_render_after is not None:
            try:
                self.root.after_cancel(self.zoom_render_after)
            except tk.TclError:
                pass
            self.zoom_render_after = None
        if self.zoom_preview_after is not None:
            try:
                self.root.after_cancel(self.zoom_preview_after)
            except tk.TclError:
                pass
            self.zoom_preview_after = None
        self.zoom_preview_composite_active = False
        self.zoom_preview_active_tags.clear()
        self._world_screen_transform = None
        c = self.canvas
        c.delete("all")
        env = self.scenario.environment
        c.configure(bg=MAPLESS_BACKGROUND)
        selected = self.get_selected()
        selected_obstacle_id = selected.id if isinstance(selected, Obstacle) else None
        geographic_key = (
            c.winfo_width(),
            c.winfo_height(),
            self.view_x,
            self.view_y,
            self.zoom,
            selected_obstacle_id,
            self.map_visible.get(),
            self.terrain_only_view.get(),
            env.map_layer,
        )
        can_reuse_geographic = (
            self.obstacle_layer_image is not None
            and (
                reuse_geographic_layer
                or self.obstacle_layer_source_key == geographic_key
            )
        )
        if can_reuse_geographic:
            c.create_image(
                0,
                0,
                image=self.obstacle_layer_image,
                anchor="nw",
                tags=(GEOGRAPHIC_LAYER_TAG,),
            )
            self._draw_vector_obstacles(c, self.obstacle_layer_vectors)
        else:
            visible_left, visible_top = self.screen_to_world(0, 0)
            visible_right, visible_bottom = self.screen_to_world(c.winfo_width(), c.winfo_height())
            visible_bounds = (
                min(visible_left, visible_right),
                min(visible_top, visible_bottom),
                max(visible_left, visible_right),
                max(visible_top, visible_bottom),
            )
            self._visible_obstacle_bounds = []
            for obstacle in self.scenario.obstacles:
                bounds = self._obstacle_bounds(obstacle)
                if self._bounds_overlap(bounds, visible_bounds):
                    self._visible_obstacle_bounds.append((obstacle, bounds))
            visible_obstacles = [obstacle for obstacle, _bounds in self._visible_obstacle_bounds]
            self._draw_obstacle_layer(c, visible_obstacles)
        packet_start = len(c.find_all())
        self._draw_packet_links(c)
        self._draw_retained_coverage(c)
        self._tag_items_created_since(c, packet_start, PACKET_LAYER_TAG)
        node_start = len(c.find_all())
        self._prepare_node_label_layout()
        for node in self.scenario.nodes:
            self._draw_node(c, node)
        self._tag_items_created_since(c, node_start, NODE_LAYER_TAG)
        self._draw_survey_overlay(c)
        if self.temp_forest_points:
            coordinates: list[float] = []
            for point_x, point_y in self.temp_forest_points:
                screen_x, screen_y = self.world_to_screen(point_x, point_y)
                coordinates.extend((screen_x, screen_y))
            if len(coordinates) == 2:
                coordinates.extend(coordinates)
            scale = self._base_scale() * self.zoom
            c.create_line(
                *coordinates,
                fill="#2f9b53",
                width=max(6, 300.0 * scale),
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
                smooth=True,
                stipple="gray50",
            )
        if self.temp_obstacle:
            sx1, sy1 = self.world_to_screen(self.temp_obstacle[0], self.temp_obstacle[1])
            sx2, sy2 = self.world_to_screen(self.temp_obstacle[2], self.temp_obstacle[3])
            c.create_rectangle(sx1, sy1, sx2, sy2, outline="#76dcff", width=2, dash=(5, 3), fill="#153a55", stipple="gray50")
        if self.profile_point_a is not None:
            point = self.profile_point_a
            world_x, world_y = (point.x, point.y) if isinstance(point, Node) else point
            marker_x, marker_y = self.world_to_screen(world_x, world_y)
            c.create_oval(
                marker_x - 7, marker_y - 7, marker_x + 7, marker_y + 7,
                outline="#76dcff", width=2, dash=(3, 2),
            )
            c.create_text(
                marker_x, marker_y - 14, text="A", fill="#76dcff", font=("Segoe UI Semibold", 10)
            )
        if self.horizon_source_xy is not None:
            self._draw_horizon_view_cone(c)
        if self.map_picked_xy is not None:
            self._draw_map_picked_marker(c)
        scale_start = len(c.find_all())
        self._draw_scale(c)
        self._draw_center_crosshair(c)
        self._tag_items_created_since(c, scale_start, HUD_LAYER_TAG)
        static_start = len(c.find_all())
        self._draw_static_coverage(c)
        self._tag_items_created_since(c, static_start, STATIC_COVERAGE_TAG)
        self._draw_full_beacon_layer(c)
        wave_start = len(c.find_all())
        self._draw_current_wave(c)
        self._draw_live_mesh_overlay(c)
        self._tag_items_created_since(c, wave_start, CURRENT_WAVE_TAG)
        if self.map_visible.get():
            attribution_start = len(c.find_all())
            self._draw_map_attribution(c)
            self._tag_items_created_since(c, attribution_start, HUD_LAYER_TAG)

    def _survey_world_position(
        self, measurement: dict[str, object], prefix: str = "mobile"
    ) -> tuple[float, float] | None:
        latitude = survey_float(measurement.get(f"{prefix}_latitude"))
        longitude = survey_float(measurement.get(f"{prefix}_longitude"))
        if latitude is None or longitude is None:
            return None
        env = self.scenario.environment
        return latlon_to_world(latitude, longitude, env.map_center_lat, env.map_center_lon)

    def _survey_base_world_position(self) -> tuple[float, float] | None:
        coordinates = [
            (
                survey_float(measurement.get("base_latitude")),
                survey_float(measurement.get("base_longitude")),
            )
            for measurement in self.survey_measurements
        ]
        valid = [(latitude, longitude) for latitude, longitude in coordinates if latitude is not None and longitude is not None]
        if not valid:
            return None
        latitude = sum(point[0] for point in valid) / len(valid)
        longitude = sum(point[1] for point in valid) / len(valid)
        env = self.scenario.environment
        return latlon_to_world(latitude, longitude, env.map_center_lat, env.map_center_lon)

    def _draw_survey_overlay(self, c: tk.Canvas) -> None:
        if not self.survey_measurements:
            return
        width, height = c.winfo_width(), c.winfo_height()
        selected = self.survey_selected_index
        base_position = self._survey_base_world_position()
        if selected is not None and 0 <= selected < len(self.survey_measurements):
            mobile_position = self._survey_world_position(self.survey_measurements[selected])
            if mobile_position and base_position:
                mx, my = self.world_to_screen(*mobile_position)
                bx, by = self.world_to_screen(*base_position)
                c.create_line(
                    mx, my, bx, by, fill="#f8fbff", width=2, dash=(5, 4),
                    tags=(SURVEY_LAYER_TAG,),
                )

        for index, measurement in enumerate(self.survey_measurements):
            position = self._survey_world_position(measurement)
            if position is None:
                continue
            x, y = self.world_to_screen(*position)
            if x < -12 or y < -12 or x > width + 12 or y > height + 12:
                continue
            color = survey_signal_color(measurement)
            radius = 8 if index == selected else 4
            tags = (SURVEY_LAYER_TAG, f"survey-point:{index}")
            if not survey_bool(measurement.get("forward_received")):
                c.create_line(x - radius, y - radius, x + radius, y + radius, fill=color, width=3, tags=tags)
                c.create_line(x - radius, y + radius, x + radius, y - radius, fill=color, width=3, tags=tags)
            else:
                outline = "#ffffff" if index == selected else "#06101c"
                c.create_oval(
                    x - radius, y - radius, x + radius, y + radius,
                    fill=color, outline=outline, width=3 if index == selected else 1, tags=tags,
                )

        if base_position:
            x, y = self.world_to_screen(*base_position)
            if -20 <= x <= width + 20 and -20 <= y <= height + 20:
                c.create_polygon(
                    x, y - 10, x + 10, y, x, y + 10, x - 10, y,
                    fill=ACCENT, outline="#ffffff", width=2, tags=(SURVEY_LAYER_TAG,),
                )
                c.create_text(
                    x, y - 17, text="SURVEY BASE", fill="#ffffff",
                    font=("Segoe UI Semibold", 8), tags=(SURVEY_LAYER_TAG,),
                )

        if selected is not None and 0 <= selected < len(self.survey_measurements):
            measurement = self.survey_measurements[selected]
            position = self._survey_world_position(measurement)
            if position:
                x, y = self.world_to_screen(*position)
                forward = survey_float(measurement.get("forward_rssi_dbm"))
                reverse = survey_float(measurement.get("reverse_rssi_dbm"))
                sequence = measurement.get("sequence", selected + 1)
                label = (
                    f"#{sequence}  out {forward:.0f} dBm"
                    if forward is not None
                    else f"#{sequence}  forward lost"
                )
                if reverse is not None:
                    label += f"  back {reverse:.0f} dBm"
                c.create_text(
                    x + 12, y - 15, text=label, anchor="sw", fill="#ffffff",
                    font=("Segoe UI Semibold", 9), tags=(SURVEY_LAYER_TAG,),
                )

        legend_x = max(10, width - 190)
        c.create_rectangle(
            legend_x, 12, width - 12, 72, fill="#081321", outline=BORDER,
            tags=(SURVEY_LAYER_TAG, HUD_LAYER_TAG),
        )
        c.create_text(
            legend_x + 9, 21, text=f"FIELD SURVEY · {len(self.survey_measurements):,} points",
            anchor="w", fill="#ffffff", font=("Segoe UI Semibold", 8),
            tags=(SURVEY_LAYER_TAG, HUD_LAYER_TAG),
        )
        legend = ((GREEN, "≥ -90"), (AMBER, "-91 to -110"), (RED, "< -110 / loss"))
        for offset, (color, text) in enumerate(legend):
            x = legend_x + 11 + offset * 55
            c.create_oval(x, 39, x + 8, 47, fill=color, outline="", tags=(SURVEY_LAYER_TAG, HUD_LAYER_TAG))
            c.create_text(
                x + 4, 58, text=text, anchor="n", fill=MUTED, font=("Segoe UI", 7),
                tags=(SURVEY_LAYER_TAG, HUD_LAYER_TAG),
            )

    @staticmethod
    def _bounds_overlap(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> bool:
        return not (
            first[2] < second[0]
            or first[0] > second[2]
            or first[3] < second[1]
            or first[1] > second[3]
        )

    @staticmethod
    def _tag_items_created_since(c: tk.Canvas, starting_count: int, *tags: str) -> None:
        for item_id in c.find_all()[starting_count:]:
            for tag in tags:
                c.addtag_withtag(tag, item_id)

    def _render_current_wave_frame(self) -> None:
        """Refresh only the moving ripple instead of rebuilding the geographic scene."""
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(CURRENT_WAVE_TAG)
        starting_count = len(c.find_all())
        self._draw_current_wave(c)
        self._draw_live_mesh_overlay(c)
        self._tag_items_created_since(c, starting_count, CURRENT_WAVE_TAG)

    def _render_live_mesh_frame(self) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(CURRENT_WAVE_TAG)
        starting_count = len(c.find_all())
        self._draw_current_wave(c)
        self._draw_live_mesh_overlay(c)
        self._tag_items_created_since(c, starting_count, CURRENT_WAVE_TAG)

    def _render_simulation_layers(self) -> None:
        """Refresh packet state and nodes while leaving expensive map obstacles untouched."""
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(PACKET_LAYER_TAG)
        c.delete(NODE_LAYER_TAG)
        c.delete(CURRENT_WAVE_TAG)

        packet_start = len(c.find_all())
        self._draw_packet_links(c)
        self._draw_retained_coverage(c)
        self._tag_items_created_since(c, packet_start, PACKET_LAYER_TAG)

        node_start = len(c.find_all())
        self._prepare_node_label_layout()
        for node in self.scenario.nodes:
            self._draw_node(c, node)
        self._tag_items_created_since(c, node_start, NODE_LAYER_TAG)

        # These layers are independent of packet/node state. Retain their
        # existing items and restore z-order instead of stacking duplicate
        # heatmap images on every partial simulation refresh.
        c.tag_raise(STATIC_COVERAGE_TAG)
        c.tag_raise(BEACON_STATIC_TAG)
        c.tag_raise(BEACON_ANIMATION_TAG)

        wave_start = len(c.find_all())
        self._draw_current_wave(c)
        self._draw_live_mesh_overlay(c)
        self._tag_items_created_since(c, wave_start, CURRENT_WAVE_TAG)

    def _resized_map_tile(
        self, key: tuple[str, int, int, int], data: bytes, pixel_size: int
    ) -> Image.Image:
        """Resize an already-decoded tile to `pixel_size`, decoding only once
        per (layer, zoom, tile) regardless of how many different pixel sizes
        a continuous zoom asks for."""
        decoded = self.map_tile_decoded.get(key)
        if decoded is None:
            decoded = decode_grayscale_tile(data)
            self.map_tile_decoded[key] = decoded
        return decoded.resize((pixel_size, pixel_size), Image.Resampling.BILINEAR)

    def _compose_map_layer(self, c: tk.Canvas) -> Image.Image:
        canvas_width = max(1, c.winfo_width())
        canvas_height = max(1, c.winfo_height())
        composed = Image.new("RGB", (canvas_width, canvas_height), MAPLESS_BACKGROUND)
        env = self.scenario.environment
        if not self.map_visible.get():
            return composed
        if self.terrain_only_view.get():
            return self._compose_terrain_only_layer(c, composed)
        if not env.map_configured:
            return composed
        layer = env.map_layer if env.map_layer in TILE_LAYERS else "Topographic"
        scale = self._base_scale() * self.zoom
        zoom = choose_tile_zoom(scale, int(TILE_LAYERS[layer]["max_zoom"]))
        center_x, center_y = latlon_to_mercator(env.map_center_lat, env.map_center_lon)
        mercator_scale = world_scale_factor(env.map_center_lat)
        world_left, world_top = self.screen_to_world(0, 0)
        world_right, world_bottom = self.screen_to_world(c.winfo_width(), c.winfo_height())
        mercator_left, mercator_top, mercator_right, mercator_bottom = world_viewport_to_mercator_bounds(
            world_left,
            world_top,
            world_right,
            world_bottom,
            env.map_center_lat,
            env.map_center_lon,
        )
        tile_left, tile_top = mercator_to_tile(mercator_left, mercator_top, zoom)
        tile_right, tile_bottom = mercator_to_tile(mercator_right, mercator_bottom, zoom)
        maximum = 2**zoom
        pixel_size = max(32, round(tile_size_m(zoom) * mercator_scale * scale))
        for tile_y in range(math.floor(tile_top), math.floor(tile_bottom) + 1):
            if tile_y < 0 or tile_y >= maximum:
                continue
            for raw_tile_x in range(math.floor(tile_left), math.floor(tile_right) + 1):
                tile_x = raw_tile_x % maximum
                key = (layer, zoom, tile_x, tile_y)
                data = self.map_tile_bytes.get(key)
                if data is None:
                    if key not in self.map_tile_failures:
                        self.map_service.request_tile(*key)
                    continue
                tile_mercator_left, _bottom, _right, tile_mercator_top = tile_bounds_mercator(
                    zoom, raw_tile_x, tile_y
                )
                world_x = (tile_mercator_left - center_x) * mercator_scale
                world_y = (center_y - tile_mercator_top) * mercator_scale
                screen_x, screen_y = self.world_to_screen(world_x, world_y)
                try:
                    if pixel_size <= MAX_CACHED_TILE_PIXELS:
                        image_key = (*key, pixel_size)
                        tile_image = self.map_tile_images.get(image_key)
                        if tile_image is None:
                            tile_image = self._resized_map_tile(key, data, pixel_size).convert("RGB")
                            self.map_tile_images[image_key] = tile_image
                        composed.paste(tile_image, (round(screen_x), round(screen_y)))
                    else:
                        # Deep zoom can make a source tile tens of thousands
                        # of pixels wide. Enlarge only its visible portion.
                        image_key = (*key, 256)
                        tile_image = self.map_tile_images.get(image_key)
                        if tile_image is None:
                            tile_image = self._resized_map_tile(key, data, 256).convert("RGB")
                            self.map_tile_images[image_key] = tile_image
                        self._paste_clipped_map_tile(
                            composed,
                            tile_image,
                            screen_x,
                            screen_y,
                            pixel_size,
                        )
                except Exception:
                    self.map_tile_failures.add(key)
                    continue
        if len(self.map_tile_images) > 300:
            active_pixel_size = pixel_size if pixel_size <= MAX_CACHED_TILE_PIXELS else 256
            current_keys = {
                key
                for key in self.map_tile_images
                if key[0] == layer and key[1] == zoom and key[4] == active_pixel_size
            }
            self.map_tile_images = {key: image for key, image in self.map_tile_images.items() if key in current_keys}
        if len(self.map_tile_decoded) > 300:
            current_decoded_keys = {key for key in self.map_tile_decoded if key[0] == layer and key[1] == zoom}
            self.map_tile_decoded = {
                key: image for key, image in self.map_tile_decoded.items() if key in current_decoded_keys
            }
        return composed

    @staticmethod
    def _paste_clipped_map_tile(
        composed: Image.Image,
        tile_image: Image.Image,
        screen_x: float,
        screen_y: float,
        pixel_size: int,
    ) -> bool:
        """Paste only the visible part of an oversized map tile."""
        destination_left = max(0, math.floor(screen_x))
        destination_top = max(0, math.floor(screen_y))
        destination_right = min(composed.width, math.ceil(screen_x + pixel_size))
        destination_bottom = min(composed.height, math.ceil(screen_y + pixel_size))
        if destination_right <= destination_left or destination_bottom <= destination_top:
            return False

        source_left = (destination_left - screen_x) / pixel_size * tile_image.width
        source_top = (destination_top - screen_y) / pixel_size * tile_image.height
        source_right = (destination_right - screen_x) / pixel_size * tile_image.width
        source_bottom = (destination_bottom - screen_y) / pixel_size * tile_image.height
        cropped = tile_image.crop(
            (
                max(0.0, source_left),
                max(0.0, source_top),
                min(float(tile_image.width), source_right),
                min(float(tile_image.height), source_bottom),
            )
        )
        destination_size = (
            destination_right - destination_left,
            destination_bottom - destination_top,
        )
        if cropped.size != destination_size:
            cropped = cropped.resize(destination_size, Image.Resampling.BILINEAR)
        composed.paste(cropped, (destination_left, destination_top))
        return True

    def _compose_terrain_only_layer(self, c: tk.Canvas, fallback: Image.Image) -> Image.Image:
        env = self.scenario.environment
        canvas_width = max(1, c.winfo_width())
        canvas_height = max(1, c.winfo_height())
        scale = self._base_scale() * self.zoom
        world_left, world_top = self.screen_to_world(0, 0)
        world_right, world_bottom = self.screen_to_world(canvas_width, canvas_height)
        mercator_left, mercator_top, mercator_right, mercator_bottom = world_viewport_to_mercator_bounds(
            world_left,
            world_top,
            world_right,
            world_bottom,
            env.map_center_lat,
            env.map_center_lon,
        )
        zoom = choose_tile_zoom(scale, int(TILE_LAYERS["TerrainDEM"]["max_zoom"]))
        tile_left, tile_top = mercator_to_tile(mercator_left, mercator_top, zoom)
        tile_right, tile_bottom = mercator_to_tile(mercator_right, mercator_bottom, zoom)
        maximum = 2**zoom
        required_keys: list[tuple[str, int, int, int]] = []
        for tile_y in range(math.floor(tile_top), math.floor(tile_bottom) + 1):
            if tile_y < 0 or tile_y >= maximum:
                continue
            for raw_tile_x in range(math.floor(tile_left), math.floor(tile_right) + 1):
                key = ("TerrainDEM", zoom, raw_tile_x % maximum, tile_y)
                required_keys.append(key)
                if key not in self.map_tile_bytes and key not in self.map_tile_failures:
                    self.map_service.request_tile(*key)
        available_keys = tuple(key for key in required_keys if key in self.map_tile_bytes)

        if canvas_width >= canvas_height:
            columns = 129
            rows = max(49, round(columns * canvas_height / canvas_width))
        else:
            rows = 129
            columns = max(49, round(rows * canvas_width / canvas_height))
        key = (
            round(mercator_left, 1),
            round(mercator_top, 1),
            round(mercator_right, 1),
            round(mercator_bottom, 1),
            zoom,
            columns,
            rows,
            available_keys,
            self.unit_system.get(),
        )
        if key != self.terrain_visual_key:
            mercator_x = np.linspace(mercator_left, mercator_right, columns, dtype=np.float64)
            mercator_y = np.linspace(mercator_top, mercator_bottom, rows, dtype=np.float64)
            tile_x_float = (
                mercator_x + WEB_MERCATOR_WORLD_M / 2.0
            ) / tile_size_m(zoom)
            tile_y_float = (
                WEB_MERCATOR_WORLD_M / 2.0 - mercator_y
            ) / tile_size_m(zoom)
            tile_x_grid, tile_y_grid = np.meshgrid(tile_x_float, tile_y_float)
            tile_x_floor = np.floor(tile_x_grid).astype(np.int64) % maximum
            tile_y_floor = np.floor(tile_y_grid).astype(np.int64)
            elevation_grid = np.full((rows, columns), np.nan, dtype=np.float32)
            for terrain_key in available_keys:
                _layer, tile_zoom, tile_x, tile_y = terrain_key
                elevation_tile_key = (tile_zoom, tile_x, tile_y)
                elevation_tile = self.terrain_tile_elevations.get(elevation_tile_key)
                if elevation_tile is None:
                    try:
                        elevation_tile = decode_terrarium_elevations(
                            self.map_tile_bytes[terrain_key]
                        )
                        self.terrain_tile_elevations[elevation_tile_key] = elevation_tile
                    except Exception:
                        self.map_tile_failures.add(terrain_key)
                        continue
                mask = (tile_x_floor == tile_x) & (tile_y_floor == tile_y)
                if not mask.any():
                    continue
                pixel_x = np.clip(
                    ((tile_x_grid[mask] - np.floor(tile_x_grid[mask])) * 255.0).astype(np.int32),
                    0,
                    255,
                )
                pixel_y = np.clip(
                    ((tile_y_grid[mask] - np.floor(tile_y_grid[mask])) * 255.0).astype(np.int32),
                    0,
                    255,
                )
                elevation_grid[mask] = elevation_tile[pixel_y, pixel_x]

            fallback_grid = self._terrain_grid_fallback(
                columns,
                rows,
                world_left,
                world_top,
                world_right,
                world_bottom,
            )
            elevation_grid = np.where(np.isfinite(elevation_grid), elevation_grid, fallback_grid)
            if not np.isfinite(elevation_grid).any():
                drawing = ImageDraw.Draw(fallback)
                drawing.text(
                    (fallback.width / 2, fallback.height / 2),
                    "Loading terrain elevation…",
                    fill="#c8d1dc",
                    anchor="mm",
                )
                return fallback
            finite_median = float(np.nanmedian(elevation_grid))
            elevation_grid = np.where(np.isfinite(elevation_grid), elevation_grid, finite_median)
            self.terrain_visual_source = build_terrain_visual(
                columns,
                rows,
                elevation_grid.astype(np.float32).ravel().tolist(),
                abs(mercator_right - mercator_left),
                abs(mercator_top - mercator_bottom),
                self.unit_system.get(),
            )
            self.terrain_visual_key = key
        if self.terrain_visual_source is None:
            drawing = ImageDraw.Draw(fallback)
            drawing.text(
                (fallback.width / 2, fallback.height / 2),
                "Loading terrain elevation…",
                fill="#c8d1dc",
                anchor="mm",
            )
            return fallback
        return self.terrain_visual_source.resize(
            (canvas_width, canvas_height),
            Image.Resampling.BILINEAR,
        )

    def _terrain_grid_fallback(
        self,
        columns: int,
        rows: int,
        world_left: float,
        world_top: float,
        world_right: float,
        world_bottom: float,
    ) -> np.ndarray:
        """Fill the whole viewport from the saved terrain grid while exact DEM tiles load."""
        env = self.scenario.environment
        if (
            env.terrain_columns < 2
            or env.terrain_rows < 2
            or len(env.terrain_values) != env.terrain_columns * env.terrain_rows
        ):
            return np.full((rows, columns), np.nan, dtype=np.float32)
        source = np.asarray(env.terrain_values, dtype=np.float32).reshape(
            (env.terrain_rows, env.terrain_columns)
        )
        terrain_left, terrain_top, terrain_right, terrain_bottom = env.terrain_bounds()
        world_x = np.linspace(world_left, world_right, columns, dtype=np.float64)
        world_y = np.linspace(world_top, world_bottom, rows, dtype=np.float64)
        valid_x = (world_x >= terrain_left) & (world_x <= terrain_right)
        valid_y = (world_y >= terrain_top) & (world_y <= terrain_bottom)
        grid_x = np.clip(
            (world_x - terrain_left) / max(1.0, terrain_right - terrain_left)
            * (env.terrain_columns - 1),
            0.0,
            env.terrain_columns - 1,
        )
        grid_y = np.clip(
            (world_y - terrain_top) / max(1.0, terrain_bottom - terrain_top)
            * (env.terrain_rows - 1),
            0.0,
            env.terrain_rows - 1,
        )
        x0 = np.floor(grid_x).astype(np.int32)
        y0 = np.floor(grid_y).astype(np.int32)
        x1 = np.minimum(env.terrain_columns - 1, x0 + 1)
        y1 = np.minimum(env.terrain_rows - 1, y0 + 1)
        fraction_x = grid_x - x0
        fraction_y = grid_y - y0
        top = (
            source[y0[:, None], x0[None, :]] * (1.0 - fraction_x)[None, :]
            + source[y0[:, None], x1[None, :]] * fraction_x[None, :]
        )
        bottom = (
            source[y1[:, None], x0[None, :]] * (1.0 - fraction_x)[None, :]
            + source[y1[:, None], x1[None, :]] * fraction_x[None, :]
        )
        interpolated = (
            top * (1.0 - fraction_y)[:, None]
            + bottom * fraction_y[:, None]
        ).astype(np.float32)
        interpolated[~(valid_y[:, None] & valid_x[None, :])] = np.nan
        return interpolated

    def _draw_map_attribution(self, c: tk.Canvas) -> None:
        env = self.scenario.environment
        if not env.map_configured:
            c.create_text(
                c.winfo_width() / 2,
                c.winfo_height() / 2,
                text="Search for an address or place to load the real-world map",
                fill="#a9c1dc",
                font=("Segoe UI Semibold", 13),
            )
            return
        if self.terrain_only_view.get():
            attribution = "Road-free terrain visualization · Elevation Mapzen/AWS"
        elif env.map_layer == "Topographic":
            attribution = "Map data © OpenStreetMap contributors · Topo © OpenTopoMap"
        else:
            attribution = "© OpenStreetMap contributors"
        if any(obstacle.osm_id.startswith("overture/") for obstacle in self.scenario.obstacles):
            attribution += " · Buildings © Overture Maps Foundation"
        if env.terrain_values:
            attribution += " · Terrain Mapzen/AWS"
        x, y = c.winfo_width() - 10, c.winfo_height() - 9
        c.create_text(x + 1, y + 1, text=attribution, anchor="se", fill="#000000", font=("Segoe UI", 8))
        c.create_text(x, y, text=attribution, anchor="se", fill="#ffffff", font=("Segoe UI", 8))

    def _draw_obstacle_layer(self, c: tk.Canvas, obstacles: list[Obstacle]) -> None:
        """Flatten map tiles and dense polygons into one fast opaque geographic image."""
        raster_obstacles = [
            obstacle
            for obstacle in obstacles
            if obstacle.shape == "polygon"
            and len(obstacle.points) >= 3
            and obstacle.id != self.selected_id
        ]
        raster_object_ids = {id(obstacle) for obstacle in raster_obstacles}
        vector_obstacles = [obstacle for obstacle in obstacles if id(obstacle) not in raster_object_ids]
        self.obstacle_layer_vectors = vector_obstacles
        layer = self._compose_map_layer(c)
        drawing = ImageDraw.Draw(layer, "RGBA")
        scale = self._base_scale() * self.zoom
        view_x, view_y = self.view_x, self.view_y
        style_cache: dict[
            tuple[str, str, bool],
            tuple[tuple[int, int, int], int, tuple[int, int, int], int],
        ] = {}
        for obstacle in raster_obstacles:
            coordinates = [
                ((point[0] - view_x) * scale, (point[1] - view_y) * scale)
                for point in obstacle.points
            ]
            is_building = obstacle.kind == "Building"
            color = obstacle.color
            if is_building and color in {"#8b5e4a", "#5a4636", "#3f3c37"}:
                color = "#33302b"
            style_key = (obstacle.kind, color, obstacle.enabled)
            style = style_cache.get(style_key)
            if style is None:
                fill_rgb = ImageColor.getrgb(color)
                if is_building:
                    # Buildings are solid dark like the basemap's own footprints.
                    fill_alpha = 255 if obstacle.enabled else 120
                    outline_rgb = fill_rgb
                    outline_alpha = fill_alpha
                else:
                    # Other obstacles stay translucent so the map remains readable.
                    fill_alpha = 86 if obstacle.enabled else 42
                    outline_rgb = ImageColor.getrgb(self._lighten(color))
                    outline_alpha = 180 if obstacle.enabled else 100
                style = (fill_rgb, fill_alpha, outline_rgb, outline_alpha)
                style_cache[style_key] = style
            fill_rgb, fill_alpha, outline_rgb, outline_alpha = style
            drawing.polygon(
                coordinates,
                fill=(*fill_rgb, fill_alpha),
                outline=(*outline_rgb, outline_alpha),
                width=1,
            )
        self.obstacle_layer_source = layer
        self.obstacle_layer_source_key = (
            c.winfo_width(),
            c.winfo_height(),
            self.view_x,
            self.view_y,
            self.zoom,
            self.selected_id if isinstance(self.get_selected(), Obstacle) else None,
            self.map_visible.get(),
            self.terrain_only_view.get(),
            self.scenario.environment.map_layer,
        )
        self.obstacle_layer_image = ImageTk.PhotoImage(layer)
        c.create_image(
            0,
            0,
            image=self.obstacle_layer_image,
            anchor="nw",
            tags=(GEOGRAPHIC_LAYER_TAG,),
        )
        self._draw_vector_obstacles(c, vector_obstacles)

    def _obstacle_bounds(self, obstacle: Obstacle) -> tuple[float, float, float, float]:
        signature = (
            id(obstacle.points),
            len(obstacle.points),
            obstacle.x1,
            obstacle.y1,
            obstacle.x2,
            obstacle.y2,
            obstacle.shape,
            obstacle.brush_radius_m,
        )
        cached = self._obstacle_bounds_cache.get(id(obstacle))
        if cached is not None and cached[0] == signature:
            return cached[1]
        bounds = obstacle.normalized()
        self._obstacle_bounds_cache[id(obstacle)] = (signature, bounds)
        return bounds

    def _draw_vector_obstacles(self, c: tk.Canvas, obstacles: list[Obstacle]) -> None:
        for obstacle in obstacles:
            if (
                obstacle.shape == "polygon"
                and len(obstacle.points) >= 3
                and obstacle.id != self.selected_id
            ):
                continue
            obstacle_start = len(c.find_all())
            self._draw_obstacle(c, obstacle)
            if obstacle.id == self.selected_id:
                self._tag_items_created_since(c, obstacle_start, SELECTED_OBSTACLE_TAG)

    def _invalidate_geographic_layer(self) -> None:
        self.obstacle_layer_image = None
        self.obstacle_layer_source = None
        self.obstacle_layer_source_key = None
        self.obstacle_layer_vectors = []
        self._visible_obstacle_bounds = []
        self.zoom_composite_source = None
        self.zoom_composite_source_key = None
        self._obstacle_bounds_cache.clear()

    def _render_selected_obstacle(self, obstacle: Obstacle) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(SELECTED_OBSTACLE_TAG)
        starting_count = len(c.find_all())
        self._draw_obstacle(c, obstacle)
        self._tag_items_created_since(c, starting_count, SELECTED_OBSTACLE_TAG)
        bounds = self._obstacle_bounds(obstacle)
        for index, (candidate, _old_bounds) in enumerate(self._visible_obstacle_bounds):
            if candidate is obstacle:
                self._visible_obstacle_bounds[index] = (obstacle, bounds)
                break

    def _draw_obstacle(self, c: tk.Canvas, obstacle: Obstacle) -> None:
        selected = obstacle.id == self.selected_id
        # Buildings render as solid dark footprints (like the basemap's own
        # buildings); other obstacles keep the translucent stipple.  Older imports
        # that still carry the light-tan default are shown in the new dark colour.
        is_building = obstacle.kind == "Building"
        fill = obstacle.color
        if is_building and obstacle.color in {"#8b5e4a", "#5a4636", "#3f3c37"}:
            fill = "#33302b"
        # Buildings use a solid dark outline matching the fill (a lightened border
        # made small footprints read pale); other obstacles keep the light edge.
        if selected:
            outline = "#78ddff"
        elif is_building:
            outline = fill
        else:
            outline = self._lighten(fill)
        stipple = "gray50" if obstacle.enabled else "gray75"
        if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
            coordinates: list[float] = []
            for point_x, point_y in obstacle.points:
                screen_x, screen_y = self.world_to_screen(point_x, point_y)
                coordinates.extend((screen_x, screen_y))
            c.create_polygon(
                *coordinates,
                fill=fill,
                outline=outline,
                width=3 if selected else 1,
                stipple="" if is_building else ("gray75" if obstacle.enabled else "gray50"),
            )
            return
        if obstacle.kind == "Forest" and obstacle.shape == "brush" and obstacle.points:
            coordinates: list[float] = []
            for point_x, point_y in obstacle.points:
                screen_x, screen_y = self.world_to_screen(point_x, point_y)
                coordinates.extend((screen_x, screen_y))
            if len(coordinates) == 2:
                coordinates.extend(coordinates)
            scale = self._base_scale() * self.zoom
            width = max(6, obstacle.brush_radius_m * 2 * scale)
            if selected:
                c.create_line(
                    *coordinates,
                    fill=outline,
                    width=width + 5,
                    capstyle=tk.ROUND,
                    joinstyle=tk.ROUND,
                    smooth=True,
                )
            c.create_line(
                *coordinates,
                fill=obstacle.color,
                width=width,
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
                smooth=True,
                stipple=stipple,
            )
            x_min, y_min, x_max, y_max = obstacle.normalized()
            center_x, center_y = self.world_to_screen((x_min + x_max) / 2, (y_min + y_max) / 2)
            c.create_text(
                center_x,
                center_y,
                text=f"{obstacle.name} - painted forest",
                fill="#e4edf7",
                font=("Segoe UI", 8),
            )
            return
        if obstacle.kind == "Mountain":
            x_min, y_min, x_max, y_max = obstacle.normalized()
            sx1, sy1 = self.world_to_screen(x_min, y_min)
            sx2, sy2 = self.world_to_screen(x_max, y_max)
            c.create_polygon(
                (sx1 + sx2) / 2,
                sy1,
                sx2,
                sy2,
                sx1,
                sy2,
                fill=obstacle.color,
                outline=outline,
                width=3 if selected else 1,
                stipple=stipple,
            )
            center_x, center_y = (sx1 + sx2) / 2, (sy1 + sy2) / 2
            c.create_text(center_x, center_y - 5, text="MOUNTAIN", fill="#f3f7fb", font=("Segoe UI Semibold", 8))
            c.create_text(center_x, center_y + 12, text=f"{obstacle.name} - blocks", fill="#e4edf7", font=("Segoe UI", 8))
            return
        sx1, sy1 = self.world_to_screen(obstacle.x1, obstacle.y1)
        sx2, sy2 = self.world_to_screen(obstacle.x2, obstacle.y2)
        c.create_rectangle(
            sx1, sy1, sx2, sy2, fill=fill, outline=outline,
            width=3 if selected else 1, stipple="" if is_building else stipple,
        )
        center_x, center_y = (sx1 + sx2) / 2, (sy1 + sy2) / 2
        symbol = {"Building": "▣", "Wall": "━", "Forest": "♣", "Mountain": "▲", "Water": "≈"}.get(obstacle.kind, "◆")
        c.create_text(center_x, center_y - 7, text=symbol, fill="#f3f7fb", font=("Segoe UI Symbol", 15))
        c.create_text(
            center_x,
            center_y + 11,
            text=f"{obstacle.name} · {self.format_distance(obstacle.height_m)}",
            fill="#e4edf7",
            font=("Segoe UI", 8),
        )
        if selected:
            for px, py in ((sx1, sy1), (sx2, sy1), (sx1, sy2), (sx2, sy2)):
                c.create_rectangle(px - 4, py - 4, px + 4, py + 4, fill="#9de8ff", outline="#12384f")

    def _prepare_node_label_layout(self) -> None:
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        visible: list[tuple[str, str, float, float, bool]] = []
        ordered_nodes = sorted(
            self.scenario.nodes,
            key=lambda node: (node.id != self.selected_id, node.name.lower(), node.node_num),
        )
        for node in ordered_nodes:
            x, y = self.world_to_screen(node.x, node.y)
            if not (-260 <= x <= canvas_width + 260 and -70 <= y <= canvas_height + 70):
                continue
            infrastructure = node.role in {
                "ROUTER",
                "ROUTER_LATE",
                "REPEATER",
                "CLIENT_BASE",
                "ROUTER_CLIENT",
            }
            visible.append((node.id, node.name, x, y, infrastructure))
        self.node_label_layout = layout_node_labels(visible, canvas_width, canvas_height)

    def _active_packet_reached(self) -> dict[str, dict[str, Any]] | None:
        if self.live_path_test_id is not None:
            test = self.live_mesh_tests.get(self.live_path_test_id)
            if test is not None:
                return test.reached
        return self.last_result.reached if self.last_result is not None else None

    def _draw_node(self, c: tk.Canvas, node: Node) -> None:
        x, y = self.world_to_screen(node.x, node.y)
        color = ROLE_COLORS.get(node.role, ACCENT)
        if not node.online:
            color = "#526175"
        active_reached = self._active_packet_reached()
        unreached = active_reached is not None and node.id not in active_reached
        if unreached and node.online:
            color = "#77818d"
        selected = node.id == self.selected_id
        selected_path = self._selected_packet_path()
        path_focus = selected_path is not None
        on_selected_path = not path_focus or node.id in selected_path or selected
        if not on_selected_path:
            color = "#4b5664"
        reached = active_reached is not None and node.id in active_reached and (
            self.live_path_test_id is not None or node.id in self.animation_revealed_nodes
        )
        show_delivery = reached and on_selected_path
        infrastructure = node.role in {"ROUTER", "ROUTER_LATE", "REPEATER", "CLIENT_BASE", "ROUTER_CLIENT"}
        marker_radius = 11 if infrastructure else 7
        if show_delivery:
            info = active_reached[node.id]
            hop = int(info.get("hop", 0))
            ring = HOP_COLORS.get(hop, TEXT)
            ring_radius = 21 if infrastructure else 13
            c.create_oval(x - ring_radius, y - ring_radius, x + ring_radius, y + ring_radius, outline="#05080d", width=5)
            c.create_oval(x - ring_radius, y - ring_radius, x + ring_radius, y + ring_radius, outline=ring, width=3)
        if selected:
            selection_radius = 27 if infrastructure else 18
            c.create_oval(
                x - selection_radius,
                y - selection_radius,
                x + selection_radius,
                y + selection_radius,
                outline="#8de4ff",
                width=2,
                dash=(3, 2),
            )
        if infrastructure:
            c.create_polygon(x, y - 16, x + 16, y, x, y + 16, x - 16, y, fill="#05080d", outline="")
            c.create_polygon(x, y - 14, x + 14, y, x, y + 14, x - 14, y, fill="#ffffff", outline="")
            c.create_polygon(
                x, y - marker_radius, x + marker_radius, y, x, y + marker_radius, x - marker_radius, y,
                fill=color, outline="#07101d", width=1
            )
        else:
            c.create_oval(x - 11, y - 11, x + 11, y + 11, fill="#05080d", outline="")
            c.create_oval(x - 9, y - 9, x + 9, y + 9, fill="#ffffff", outline="")
            c.create_oval(
                x - marker_radius,
                y - marker_radius,
                x + marker_radius,
                y + marker_radius,
                fill=color,
                outline="#e9f4ff",
                width=1,
            )
        if infrastructure:
            c.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#f8fbff", outline="")
        if node.favorite:
            c.create_text(x + (13 if infrastructure else 9), y - (12 if infrastructure else 8), text="★", fill="#ffe08a",
                          font=("Segoe UI Symbol", 8))
        if show_delivery:
            hop = int(active_reached[node.id].get("hop", 0))
            badge_color = HOP_COLORS.get(hop, TEXT)
            badge_x = x + (23 if infrastructure else 16)
            badge_y = y - (19 if infrastructure else 14)
            c.create_oval(
                badge_x - 10,
                badge_y - 8,
                badge_x + 10,
                badge_y + 8,
                fill="#081321",
                outline=badge_color,
                width=2,
            )
            c.create_text(
                badge_x,
                badge_y,
                text=f"H{hop}",
                fill=badge_color,
                font=("Segoe UI Semibold", 8),
            )
        placement = getattr(self, "node_label_layout", {}).get(node.id)
        if placement is None:
            label_x = x
            label_y = y + (25 if infrastructure else 21)
        else:
            label_x, label_y = placement[:2]
        self._draw_outlined_text(
            c,
            label_x,
            label_y,
            node.name,
            fill="#ffffff" if node.online and on_selected_path and not unreached else "#8c96a3",
            font=("Segoe UI Semibold", 10),
        )
        if infrastructure:
            self._draw_outlined_text(
                c,
                label_x,
                label_y + 16,
                node.role.replace("_", " ").title(),
                fill="#e3ebf5" if on_selected_path and not unreached else "#8c96a3",
                font=("Segoe UI Semibold", 9),
            )

    @staticmethod
    def _draw_outlined_text(
        c: tk.Canvas,
        x: float,
        y: float,
        text: str,
        *,
        fill: str,
        font: tuple[str, int] | tuple[str, int, str],
    ) -> None:
        for offset_x, offset_y in (
            (-2, 0),
            (2, 0),
            (0, -2),
            (0, 2),
            (-1, -1),
            (-1, 1),
            (1, -1),
            (1, 1),
        ):
            c.create_text(x + offset_x, y + offset_y, text=text, fill="#05080d", font=font)
        c.create_text(x, y, text=text, fill=fill, font=font)

    def _draw_packet_links(self, c: tk.Canvas) -> None:
        if not self.animation_seen_edges:
            return
        nodes = {node.id: node for node in self.scenario.nodes}
        selected_path = self._selected_packet_path()
        selected_edges = set(zip(selected_path, selected_path[1:])) if selected_path is not None else None
        for source_id, target_id, kind, hop in self.animation_seen_edges:
            if selected_edges is not None and (source_id, target_id) not in selected_edges:
                continue
            if hop in self.hop_line_vars and not self.hop_line_vars[hop].get():
                continue
            source, target = nodes.get(source_id), nodes.get(target_id)
            if not source or not target:
                continue
            color = HOP_COLORS.get(hop, TEXT)
            width = 5 if selected_edges is not None else 3
            dash = (6, 3) if kind == "OPAQUE" else None
            x1, y1 = self.world_to_screen(source.x, source.y)
            x2, y2 = self.world_to_screen(target.x, target.y)
            c.create_line(
                x1, y1, x2, y2, fill="#05080d", width=width + 4, dash=dash,
                arrow="last", arrowshape=(9, 11, 4)
            )
            c.create_line(x1, y1, x2, y2, fill=color, width=width, dash=dash, arrow="last", arrowshape=(7, 9, 3))

    def _selected_packet_path(self) -> list[str] | None:
        if not self.path_focus_id:
            return None
        reached = self._active_packet_reached()
        if reached is None or self.path_focus_id not in reached:
            return None
        path: list[str] = []
        seen: set[str] = set()
        current = self.path_focus_id
        while current and current not in seen:
            seen.add(current)
            path.append(current)
            current = str(reached.get(current, {}).get("via", ""))
        return list(reversed(path))

    def _draw_retained_coverage(self, c: tk.Canvas) -> None:
        if not self.retained_coverage_transmitters:
            return
        # The frozen heatmap replaces the old retained-coverage outline for a
        # sender that reached nobody -- don't draw both on top of each other.
        if self.static_coverage_profile is not None:
            return
        nodes = {node.id: node for node in self.scenario.nodes}
        selected_path = self._selected_packet_path()
        path_nodes = set(selected_path) if selected_path is not None else None
        for hop, source_id in self.retained_coverage_transmitters:
            if hop in self.hop_line_vars and not self.hop_line_vars[hop].get():
                continue
            if path_nodes is not None and source_id not in path_nodes:
                continue
            source = nodes.get(source_id)
            contour = self.animation_contours.get(source_id, [])
            if source is None or len(contour) < 3:
                continue
            coordinates = self._coverage_coordinates(source, contour, 1.0)
            color = HOP_COLORS.get(hop, TEXT)
            c.create_line(
                *coordinates,
                fill="#05080d",
                width=5,
                dash=(8, 5),
                joinstyle=tk.ROUND,
            )
            c.create_line(
                *coordinates,
                fill=color,
                width=2,
                dash=(8, 5),
                joinstyle=tk.ROUND,
            )
            if self.show_drops.get():
                self._draw_contour_stop_segments(c, contour, width=3)

    def _draw_live_mesh_overlay(self, c: tk.Canvas) -> None:
        result = self.live_mesh_result
        if result is None and not self.live_mesh_recent_frames and not self._live_mesh_running():
            return
        nodes = {node.id: node for node in self.scenario.nodes}
        recent = self.live_mesh_recent_frames
        for age, frame in enumerate(reversed(recent)):
            width = max(1, 4 - age)
            dash = None if age <= 1 else (3, 4)
            for source_id, target_id, traffic_kind, _hop in frame.receptions:
                if traffic_kind == "TEST":
                    continue
                source, target = nodes.get(source_id), nodes.get(target_id)
                if source is None or target is None:
                    continue
                color = TRAFFIC_COLORS.get(traffic_kind, ACCENT)
                x1, y1 = self.world_to_screen(source.x, source.y)
                x2, y2 = self.world_to_screen(target.x, target.y)
                c.create_line(x1, y1, x2, y2, fill="#05080d", width=width + 3, dash=dash)
                c.create_line(x1, y1, x2, y2, fill=color, width=width, dash=dash)
                if age == 0:
                    c.create_oval(x2 - 4, y2 - 4, x2 + 4, y2 + 4, fill=color, outline="#05080d")

            for node_id, traffic_kind in frame.transmitters:
                if traffic_kind == "TEST":
                    continue
                node = nodes.get(node_id)
                if node is None:
                    continue
                color = TRAFFIC_COLORS.get(traffic_kind, ACCENT)
                x, y = self.world_to_screen(node.x, node.y)
                radius = 14 + age * 7
                c.create_oval(
                    x - radius,
                    y - radius,
                    x + radius,
                    y + radius,
                    outline="#05080d",
                    width=width + 3,
                    dash=dash,
                )
                c.create_oval(
                    x - radius,
                    y - radius,
                    x + radius,
                    y + radius,
                    outline=color,
                    width=width,
                    dash=dash,
                )

            for node_id in frame.throttled:
                node = nodes.get(node_id)
                if node is None:
                    continue
                x, y = self.world_to_screen(node.x, node.y)
                radius = 18 + age * 3
                c.create_oval(
                    x - radius,
                    y - radius,
                    x + radius,
                    y + radius,
                    outline="#a7b0bd",
                    width=2,
                    dash=(3, 3),
                )

        current_index = min(self.live_mesh_frame_index, max(0, len(result.frames) - 1)) if result else 0
        simulated_hours = (
            result.frames[current_index].time_ms / 3_600_000.0
            if result and result.frames
            else float(self.live_mesh_snapshot.get("time_ms", 0.0)) / 3_600_000.0
        )
        active = self._live_mesh_running()
        title = (
            f"LIVE MESH  T+{simulated_hours:.1f} h"
            if active
            else "LIVE MESH COMPLETE"
        )
        stats = (
            f"{self.live_mesh_play_counts['tx']:,} TX  ·  "
            f"{self.live_mesh_play_counts['collisions']:,} collisions  ·  "
            f"{self.live_mesh_play_counts['dropped']:,} RF drops  ·  "
            f"{self.live_mesh_play_counts['throttled']:,} channel-gated"
        )
        self._draw_outlined_text(
            c,
            c.winfo_width() / 2,
            26,
            title,
            fill="#f4f8fc",
            font=("Segoe UI Semibold", 11),
        )
        self._draw_outlined_text(
            c,
            c.winfo_width() / 2,
            45,
            stats,
            fill="#b8c7d9",
            font=("Segoe UI Semibold", 9),
        )

    def _draw_live_test_paths(self, c: tk.Canvas, nodes: dict[str, Node]) -> None:
        """Retain the selected live test's first-arrival mesh links and hop badges."""
        if self.live_path_test_id is None:
            return
        test = self.live_mesh_tests.get(self.live_path_test_id)
        if test is None:
            return
        for receiver_id, arrival in test.reached.items():
            via_id = str(arrival.get("via", ""))
            hop = int(arrival.get("hop", 0))
            if not via_id or hop <= 0 or not self.hop_line_vars.get(hop, tk.BooleanVar(value=True)).get():
                continue
            source = nodes.get(via_id)
            receiver = nodes.get(receiver_id)
            if source is None or receiver is None:
                continue
            x1, y1 = self.world_to_screen(source.x, source.y)
            x2, y2 = self.world_to_screen(receiver.x, receiver.y)
            color = HOP_COLORS.get(hop, ACCENT)
            c.create_line(
                x1, y1, x2, y2, fill="#02060d", width=7, dash=(6, 3),
                arrow="last", arrowshape=(9, 11, 4),
            )
            c.create_line(
                x1, y1, x2, y2, fill=color, width=3, dash=(6, 3),
                arrow="last", arrowshape=(7, 9, 3),
            )
            self._draw_outlined_text(
                c, (x1 + x2) / 2, (y1 + y2) / 2 - 9, f"H{hop}",
                fill=color, font=("Segoe UI Bold", 9),
            )

    HORIZON_SILHOUETTE_COLOR = "#3d3d3d"  # one uniform tone for the whole skyline shape
    HORIZON_DEFAULT_FOV_DEG = 90.0  # a normal-lens field of view, not the full 360° sweep
    _BEACON_BLOCK = "#ff2d55"   # red: obstacles that BLOCK the signal
    _BEACON_SLOW = "#ffd23f"    # yellow: obstacles that SLOW / weaken the signal
    _BEACON_EDGE = "#38e1ff"    # cyan: live ripple + beacon centre
    _BEACON_HALO = "#05080d"    # dark outline that keeps thin lines readable
    # Signal-strength ramp for the coverage fill: strong (green) -> weak (red).
    _BEACON_STRENGTH_STOPS = ((0.0, (47, 209, 106)), (0.5, (255, 210, 63)), (1.0, (255, 77, 79)))

    @classmethod
    def _strength_color(cls, t: float) -> str:
        """Colour for signal strength: t=0 strong (green) .. t=1 weak (red)."""
        t = max(0.0, min(1.0, t))
        stops = cls._BEACON_STRENGTH_STOPS
        for index in range(len(stops) - 1):
            t0, c0 = stops[index]
            t1, c1 = stops[index + 1]
            if t <= t1:
                f = (t - t0) / max(1e-9, t1 - t0)
                r = round(c0[0] + (c1[0] - c0[0]) * f)
                g = round(c0[1] + (c1[1] - c0[1]) * f)
                b = round(c0[2] + (c1[2] - c0[2]) * f)
                return f"#{r:02x}{g:02x}{b:02x}"
        r, g, b = stops[-1][1]
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_full_beacon_layer(self, c: tk.Canvas) -> None:
        static_start = len(c.find_all())
        self._draw_beacon(c, draw_animation=False)
        self._tag_items_created_since(c, static_start, BEACON_STATIC_TAG, BEACON_TAG)
        animation_start = len(c.find_all())
        self._draw_beacon(c, draw_static=False)
        self._tag_items_created_since(c, animation_start, BEACON_ANIMATION_TAG, BEACON_TAG)

    def _draw_beacon(
        self,
        c: tk.Canvas,
        *,
        draw_static: bool = True,
        draw_animation: bool = True,
    ) -> None:
        """Draw the beacon coverage as a filled strong->weak heatmap, with the
        obstacles that SLOW the signal in yellow and those that BLOCK it in red."""
        profile = self.beacon_profile
        if profile is None or len(profile.rays) < 3:
            return
        phase = self.beacon_phase
        glow = 0.5 + 0.5 * math.sin(math.tau * phase)
        rays = profile.rays
        w2s = self.world_to_screen
        ox, oy = profile.x, profile.y

        segmented = len(profile.rays) >= 3 and all(len(ray.samples) >= 2 for ray in profile.rays)
        if segmented:
            if draw_static:
                self._draw_segmented_coverage(c, profile, cache_prefix="beacon")
            if draw_animation and phase > 0.02:
                self._draw_segmented_ripple(c, profile, phase)
            if draw_animation:
                cx, cy = w2s(ox, oy)
                halo = 5 + 4 * glow
                c.create_oval(
                    cx - halo,
                    cy - halo,
                    cx + halo,
                    cy + halo,
                    outline=self._BEACON_EDGE,
                    width=2,
                )
                c.create_oval(
                    cx - 3,
                    cy - 3,
                    cx + 3,
                    cy + 3,
                    fill=self._BEACON_EDGE,
                    outline=self._BEACON_HALO,
                )
            return

        # Boundary points in world space (where the signal reaches per direction).
        world = [
            (ox + math.cos(ray.angle) * ray.reach_m, oy + math.sin(ray.angle) * ray.reach_m)
            for ray in rays
        ]

        # Filled coverage heatmap: nested bands from the weak edge inwards to the
        # strong centre, each the coverage shape scaled down, so colour shows how
        # strong the signal is at every point (and the shape shows how far it got).
        if draw_static:
            bands = 7
            for band in range(bands, 0, -1):
                frac = band / bands
                color = self._strength_color((band - 0.5) / bands)
                coords: list[float] = []
                for (bx, by) in world:
                    coords.extend(w2s(ox + (bx - ox) * frac, oy + (by - oy) * frac))
                if len(coords) >= 6:
                    c.create_polygon(*coords, fill=color, outline="", stipple="gray50")

            # A crisp edge line marks the outer limit of the range.
            edge: list[float] = []
            for (bx, by) in world:
                edge.extend(w2s(bx, by))
            edge.extend(edge[:2])
            c.create_line(*edge, fill=self._BEACON_HALO, width=3, joinstyle=tk.ROUND)
            c.create_line(*edge, fill="#ffffff", width=1, joinstyle=tk.ROUND)

            # Culprit obstacles (static, no pulsing): yellow slows the signal, red blocks
            # it.  Only obstacles the signal actually reached are in these lists.
            for obstacle in self.beacon_weakening_obstacles:
                self._draw_beacon_obstacle(c, obstacle, self._BEACON_SLOW, 2, fill=True)
            for obstacle in self.beacon_blocking_obstacles:
                self._draw_beacon_obstacle(c, obstacle, self._BEACON_BLOCK, 2, fill=True)

        # A single outward ripple keeps the "beacon is live" feel without clutter.
        if draw_animation:
            frac = phase
            if frac > 0.02:
                ripple: list[float] = []
                for (bx, by) in world:
                    ripple.extend(w2s(ox + (bx - ox) * frac, oy + (by - oy) * frac))
                ripple.extend(ripple[:2])
                c.create_line(*ripple, fill=self._BEACON_EDGE, width=3, joinstyle=tk.ROUND)

            # The pulsing beacon marker at the centre.
            cx, cy = w2s(ox, oy)
            halo = 5 + 4 * glow
            c.create_oval(cx - halo, cy - halo, cx + halo, cy + halo, outline=self._BEACON_EDGE, width=2)
            c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=self._BEACON_EDGE, outline=self._BEACON_HALO)

    def _draw_beacon_obstacle(
        self, c: tk.Canvas, obstacle: Obstacle, color: str, width: float, fill: bool = False
    ) -> None:
        """Mark one blocking/weakening obstacle in the beacon's warning colour."""
        line_width = max(1, round(width))
        body = color if fill else ""
        stipple = "gray50" if fill else ""
        if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
            coordinates: list[float] = []
            for point_x, point_y in obstacle.points:
                coordinates.extend(self.world_to_screen(point_x, point_y))
            c.create_polygon(*coordinates, outline=color, width=line_width, fill=body, stipple=stipple)
            return
        if obstacle.shape == "brush" and obstacle.points:
            coordinates = []
            for point_x, point_y in obstacle.points:
                coordinates.extend(self.world_to_screen(point_x, point_y))
            if len(coordinates) == 2:
                coordinates.extend(coordinates)
            scale = self._base_scale() * self.zoom
            brush_width = max(6, obstacle.brush_radius_m * 2 * scale)
            c.create_line(
                *coordinates, fill=color, width=brush_width,
                capstyle=tk.ROUND, joinstyle=tk.ROUND, smooth=True, stipple="gray50",
            )
            return
        x_min, y_min, x_max, y_max = obstacle.normalized()
        sx1, sy1 = self.world_to_screen(x_min, y_min)
        sx2, sy2 = self.world_to_screen(x_max, y_max)
        if obstacle.kind == "Mountain":
            c.create_polygon(
                (sx1 + sx2) / 2, sy1, sx2, sy2, sx1, sy2,
                outline=color, width=line_width, fill=body, stipple=stipple,
            )
        else:
            c.create_rectangle(
                sx1, sy1, sx2, sy2, outline=color, width=line_width, fill=body, stipple=stipple
            )

    def _draw_current_wave(self, c: tk.Canvas) -> None:
        if self.current_wave_hop <= 0:
            return
        wave = self.current_wave
        selected_path = self._selected_packet_path()
        transmitter_ids = self.animation_transmitters.get(self.current_wave_hop, [])
        if selected_path is not None:
            selected_edges = set(zip(selected_path, selected_path[1:]))
            wave = [event for event in wave if (event.peer_id, event.node_id) in selected_edges]
            path_nodes = set(selected_path)
            transmitter_ids = [node_id for node_id in transmitter_ids if node_id in path_nodes]
        if not transmitter_ids:
            return
        hop = self.current_wave_hop
        color = HOP_COLORS.get(hop, TEXT)
        nodes = {node.id: node for node in self.scenario.nodes}
        progress = self.animation_progress
        if self.last_result is not None and self.last_result.routing_mode == "DM_LEARNED":
            c.create_text(
                c.winfo_width() / 2,
                28,
                text=f"DM HOP {hop}  ·  learned next-hop route",
                fill=color,
                font=("Segoe UI Semibold", 11),
            )
            for event in wave:
                source = nodes.get(event.peer_id)
                target = nodes.get(event.node_id)
                if source is None or target is None:
                    continue
                x1, y1 = self.world_to_screen(source.x, source.y)
                target_x, target_y = self.world_to_screen(target.x, target.y)
                x2 = x1 + (target_x - x1) * progress
                y2 = y1 + (target_y - y1) * progress
                arrow = "last" if progress >= 0.92 else "none"
                c.create_line(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill="#05080d",
                    width=8,
                    arrow=arrow,
                    arrowshape=(10, 12, 4),
                )
                c.create_line(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill=color,
                    width=4,
                    arrow=arrow,
                    arrowshape=(8, 10, 3),
                )
                if progress >= 0.98:
                    c.create_oval(
                        target_x - 18,
                        target_y - 18,
                        target_x + 18,
                        target_y + 18,
                        outline="#05080d",
                        width=6,
                    )
                    c.create_oval(
                        target_x - 18,
                        target_y - 18,
                        target_x + 18,
                        target_y + 18,
                        outline=color,
                        width=3,
                    )
            return
        blocked_sectors = sum(
            1
            for source_id in transmitter_ids
            for _x, _y, boundary_kind in self.animation_contours.get(source_id, [])
            if boundary_kind == "blocked"
        )
        subtitle = f"{len(transmitter_ids)} transmitter{'s' if len(transmitter_ids) != 1 else ''}"
        if wave:
            subtitle += f" · {len(wave)} new node{'s' if len(wave) != 1 else ''}"
        if blocked_sectors:
            subtitle += f" · {blocked_sectors} blocked direction{'s' if blocked_sectors != 1 else ''}"
        c.create_text(
            c.winfo_width() / 2,
            28,
            text=f"HOP {hop}  ·  {subtitle}",
            fill=color,
            font=("Segoe UI Semibold", 11),
        )
        for source_id in transmitter_ids:
            source = nodes.get(source_id)
            if source is None:
                continue
            contour = self.animation_contours.get(source_id, [])
            if len(contour) < 3:
                continue
            for delay, width, dash in (
                (0.0, 4, None),
                (0.12, 3, (7, 4)),
                (0.24, 2, (3, 5)),
            ):
                phase = clamp((progress - delay) / max(0.01, 1.0 - delay), 0.0, 1.0)
                if phase <= 0:
                    continue
                coordinates = self._coverage_coordinates(source, contour, phase)
                c.create_line(*coordinates, fill="#05080d", width=width + 3, dash=dash, joinstyle=tk.ROUND)
                c.create_line(*coordinates, fill=color, width=width, dash=dash, joinstyle=tk.ROUND)

            if progress >= 0.98 and selected_path is None and self.show_drops.get():
                self._draw_contour_stop_segments(c, contour, width=3)

        for event in wave:
            source = nodes.get(event.peer_id)
            target = nodes.get(event.node_id)
            if source is None or target is None:
                continue
            contour = self.animation_contours.get(source.id, [])
            maximum_m = self._contour_distance_toward(source, target, contour)
            receive_distance = math.hypot(target.x - source.x, target.y - source.y)
            if maximum_m <= 0 or progress < receive_distance / maximum_m:
                continue
            tx, ty = self.world_to_screen(target.x, target.y)
            c.create_oval(tx - 18, ty - 18, tx + 18, ty + 18, outline="#05080d", width=6)
            c.create_oval(tx - 18, ty - 18, tx + 18, ty + 18, outline=color, width=3)

    def _coverage_coordinates(
        self,
        source: Node,
        contour: list[tuple[float, float, str]],
        phase: float,
    ) -> list[float]:
        coordinates: list[float] = []
        for boundary_x, boundary_y, _kind in contour:
            ripple_x = source.x + (boundary_x - source.x) * phase
            ripple_y = source.y + (boundary_y - source.y) * phase
            screen_x, screen_y = self.world_to_screen(ripple_x, ripple_y)
            coordinates.extend((screen_x, screen_y))
        coordinates.extend(coordinates[:2])
        return coordinates

    def _draw_contour_stop_segments(
        self,
        c: tk.Canvas,
        contour: list[tuple[float, float, str]],
        *,
        width: int,
    ) -> None:
        for index, (_x, _y, boundary_kind) in enumerate(contour):
            if boundary_kind == "edge":
                continue
            next_point = contour[(index + 1) % len(contour)]
            x1, y1 = self.world_to_screen(contour[index][0], contour[index][1])
            x2, y2 = self.world_to_screen(next_point[0], next_point[1])
            boundary_color = RED if boundary_kind == "blocked" else AMBER
            c.create_line(x1, y1, x2, y2, fill="#05080d", width=width + 3)
            c.create_line(x1, y1, x2, y2, fill=boundary_color, width=width)

    @staticmethod
    def _contour_distance_toward(
        source: Node,
        target: Node,
        contour: list[tuple[float, float, str]],
    ) -> float:
        if not contour:
            return 0.0
        angle = math.atan2(target.y - source.y, target.x - source.x) % math.tau
        index = round(angle / math.tau * len(contour)) % len(contour)
        point_x, point_y, _kind = contour[index]
        return math.hypot(point_x - source.x, point_y - source.y)

    @staticmethod
    def _halo_line(c: tk.Canvas, *coordinates: float, width: float = 2) -> None:
        """A black line with a white outline so it reads on any background."""
        c.create_line(*coordinates, fill="white", width=width + 2)
        c.create_line(*coordinates, fill="black", width=width)

    @staticmethod
    def _halo_text(c: tk.Canvas, x: float, y: float, **kwargs: Any) -> None:
        """Black text ringed with a white halo so it reads on any background."""
        for offset_x, offset_y in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            c.create_text(x + offset_x, y + offset_y, fill="white", **kwargs)
        c.create_text(x, y, fill="black", **kwargs)

    def _draw_scale(self, c: tk.Canvas) -> None:
        scale = self._base_scale() * self.zoom
        desired_pixels = 120
        desired_meters = desired_pixels / max(scale, 1e-9)
        if self.unit_system.get() == "Imperial":
            unit_meters = METERS_PER_MILE if desired_meters >= METERS_PER_MILE / 2 else METERS_PER_FOOT
        else:
            unit_meters = 1000.0 if desired_meters >= 500.0 else 1.0
        desired_units = desired_meters / unit_meters
        magnitude = 10 ** math.floor(math.log10(max(0.001, desired_units)))
        nice_units = min(
            (1, 2, 5, 10), key=lambda factor: abs(factor * magnitude - desired_units)
        ) * magnitude
        nice_meters = nice_units * unit_meters
        pixels = nice_meters * scale
        x, y = 20, c.winfo_height() - 24

        cx, cy = c.winfo_width() / 2.0, c.winfo_height() / 2.0
        wx, wy = self.screen_to_world(cx, cy)
        env = self.scenario.environment
        coordinates = f"X {self.format_distance(wx)} · Y {self.format_distance(wy)}"
        if env.map_configured:
            latitude, longitude = world_to_latlon(wx, wy, env.map_center_lat, env.map_center_lon)
            coordinates += f" · {latitude:.5f}, {longitude:.5f}"

        self._halo_line(c, x, y, x + pixels, y)
        self._halo_line(c, x, y - 4, x, y + 4)
        self._halo_line(c, x + pixels, y - 4, x + pixels, y + 4)
        self._halo_text(
            c, x + pixels / 2, y - 9, text=self.format_distance(nice_meters), font=("Segoe UI", 8)
        )
        self._halo_text(
            c, x + pixels + 12, y, text=coordinates, anchor="w", font=("Segoe UI", 8)
        )

    def _draw_center_crosshair(self, c: tk.Canvas) -> None:
        """Mark the exact centre of the view."""
        cx, cy = c.winfo_width() / 2.0, c.winfo_height() / 2.0
        size = 10
        c.create_line(cx - size, cy, cx + size, cy, fill="black", width=1)
        c.create_line(cx, cy - size, cx, cy + size, fill="black", width=1)
        c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, outline="black", width=1)

    def _canvas_down(self, event: tk.Event) -> None:
        self.drag_start_screen = (event.x, event.y)
        self.drag_start_world = self.screen_to_world(event.x, event.y)
        if self.tool == "node":
            x, y = self.drag_start_world
            self.add_node(x, y)
            if not self._terrain_covers(x, y):
                self.status_var.set("Node placed in unrestricted workspace · refreshing local terrain")
                self.load_topography()
            return
        if self.tool == "beacon":
            x, y = self.drag_start_world
            node = self.add_node(x, y, name=f"Beacon {len(self.scenario.nodes) + 1}")
            needs_terrain = not self._terrain_covers(x, y)
            if needs_terrain:
                self.load_topography()
            self.set_tool("select")
            self.start_beacon()
            return
        if self.tool == "horizon":
            hit = self.hit_test(event.x, event.y)
            point: Node | tuple[float, float] = hit if isinstance(hit, Node) else self.drag_start_world
            self.set_tool("select")
            self.show_horizon_panorama(point)
            return
        if self.tool == "profile":
            hit = self.hit_test(event.x, event.y)
            point: Node | tuple[float, float] = hit if isinstance(hit, Node) else self.drag_start_world
            label = f"node {hit.name}" if isinstance(hit, Node) else "a ground point (missed the node?)"
            if self.profile_point_a is None:
                self.profile_point_a = point
                self.status_var.set(f"Profile point A: {label} · click a second node or point")
                self.schedule_render()
            else:
                point_a = self.profile_point_a
                self.profile_point_a = None
                self.set_tool("select")
                self.show_path_profile(point_a, point)
            return
        if self.tool == "Forest":
            x, y = self.drag_start_world
            self.temp_forest_points = [[x, y]]
            self.schedule_render()
            return
        if self.tool in OBSTACLE_DEFAULTS:
            x, y = self.drag_start_world
            self.temp_obstacle = (x, y, x, y)
            return
        survey_hit = self._survey_hit_test(event.x, event.y)
        if survey_hit is not None:
            self.select_survey_measurement(survey_hit, center=False)
            return
        hit = self.hit_test(event.x, event.y)
        self.select(hit.id if hit else None)
        if isinstance(hit, Node):
            self.drag_object_origin = (hit.x, hit.y)
        elif isinstance(hit, Obstacle):
            self.drag_object_origin = (hit.x1, hit.y1, hit.x2, hit.y2)
            if hit.points:
                self.drag_object_points = [point[:] for point in hit.points]
        else:
            self.drag_object_origin = None

    def _canvas_drag(self, event: tk.Event) -> None:
        if not self.drag_start_world:
            return
        wx, wy = self.screen_to_world(event.x, event.y)
        if self.tool == "Forest":
            if not self.temp_forest_points:
                self.temp_forest_points = [[wx, wy]]
            last_x, last_y = self.temp_forest_points[-1]
            if math.hypot(wx - last_x, wy - last_y) >= 35.0:
                self.temp_forest_points.append([wx, wy])
            self.schedule_render()
            return
        if self.tool in OBSTACLE_DEFAULTS:
            x0, y0 = self.drag_start_world
            self.temp_obstacle = (x0, y0, wx, wy)
            self.schedule_render()
            return
        if self.tool != "select" or not self.drag_object_origin:
            return
        dx, dy = wx - self.drag_start_world[0], wy - self.drag_start_world[1]
        obj = self.get_selected()
        env = self.scenario.environment
        if isinstance(obj, Node):
            obj.x = self.drag_object_origin[0] + dx
            obj.y = self.drag_object_origin[1] + dy
            self._set_auto_node_elevation(obj)
        elif isinstance(obj, Obstacle):
            if obj.points and self.drag_object_points:
                obj.points = [[point[0] + dx, point[1] + dy] for point in self.drag_object_points]
                x_min, y_min, x_max, y_max = obj.normalized()
                obj.x1, obj.y1, obj.x2, obj.y2 = x_min, y_min, x_max, y_max
            else:
                obj.x1 = self.drag_object_origin[0] + dx
                obj.y1 = self.drag_object_origin[1] + dy
                obj.x2 = self.drag_object_origin[2] + dx
                obj.y2 = self.drag_object_origin[3] + dy
            x_min, y_min, x_max, y_max = obj.normalized()
            terrain_elevation = env.terrain_elevation((x_min + x_max) / 2, (y_min + y_max) / 2)
            if terrain_elevation is not None:
                obj.base_elevation_m = terrain_elevation
        if isinstance(obj, Node):
            self._render_simulation_layers()
        elif isinstance(obj, Obstacle):
            self._render_selected_obstacle(obj)

    def _canvas_up(self, event: tk.Event) -> None:
        if self.tool == "Forest" and self.temp_forest_points:
            points = self.temp_forest_points
            self.temp_forest_points = []
            self.add_forest_stroke(points)
        elif self.tool in OBSTACLE_DEFAULTS and self.temp_obstacle:
            x1, y1, x2, y2 = self.temp_obstacle
            self.temp_obstacle = None
            if abs(x2 - x1) > 20 and abs(y2 - y1) > 20:
                self.add_obstacle(self.tool, x1, y1, x2, y2)
            else:
                self.render_canvas()
        elif self.drag_object_origin:
            obj = self.get_selected()
            if isinstance(obj, Node):
                self._set_auto_node_elevation(obj)
            self.mark_dirty()
            self._mark_results_stale()
            self._build_object_form()
            if isinstance(obj, Node) and not self._terrain_covers(obj.x, obj.y):
                self.status_var.set("Node moved freely · refreshing terrain around current scene")
                self.load_topography()
            # Moving the beacon node should re-pulse its coverage from the new spot.
            if isinstance(obj, Node) and obj.id == self.beacon_node_id:
                self.selected_id = obj.id
                self.start_beacon()
        self.drag_start_screen = None
        self.drag_start_world = None
        self.drag_object_origin = None
        self.drag_object_points = None

    def _pan_down(self, event: tk.Event) -> None:
        self.pan_start = (event.x, event.y)
        self.pan_last_screen = (event.x, event.y)
        self.pan_origin = (self.view_x, self.view_y)
        self.canvas.configure(cursor="fleur")

    def _canvas_configured(self, _event: tk.Event) -> None:
        self._world_screen_transform = None
        self.schedule_render()

    def _pan_drag(self, event: tk.Event) -> None:
        if self.pan_start is None or self.pan_origin is None or self.pan_last_screen is None:
            return
        scale = self._base_scale() * self.zoom
        self.view_x = self.pan_origin[0] - (event.x - self.pan_start[0]) / max(scale, 1e-9)
        self.view_y = self.pan_origin[1] - (event.y - self.pan_start[1]) / max(scale, 1e-9)
        delta_x = event.x - self.pan_last_screen[0]
        delta_y = event.y - self.pan_last_screen[1]
        self.canvas.move("all", delta_x, delta_y)
        self.canvas.move(HUD_LAYER_TAG, -delta_x, -delta_y)
        self.pan_last_screen = (event.x, event.y)

    def _pan_end(self) -> None:
        self.pan_start = None
        self.pan_origin = None
        self.pan_last_screen = None
        self.canvas.configure(cursor="arrow" if self.tool == "select" else "crosshair")
        self.schedule_render()

    def _zoom_preview_layer(
        self,
        source: Image.Image | None,
        source_key: tuple[object, ...] | None,
        *,
        tag: str,
        photo_attribute: str,
        segmented: bool = False,
    ) -> None:
        preview = self._transformed_zoom_source(source, source_key, segmented=segmented)
        if preview is None:
            return
        items = self.canvas.find_withtag(tag)
        if not items:
            return
        photo, replaced = self._paste_zoom_photo(photo_attribute, preview)
        for item in items:
            if replaced or tag not in self.zoom_preview_active_tags:
                self.canvas.itemconfigure(item, image=photo, state="normal")
            # canvas.scale() moves image anchors but cannot scale their pixels.
            # The preview pixels already contain the complete current transform,
            # so retaining that moved anchor applies the cursor offset twice on
            # the second and later wheel events.
            self.canvas.coords(item, 0, 0)
        if replaced or tag not in self.zoom_preview_active_tags:
            self.zoom_preview_active_tags.add(tag)

    def _paste_zoom_photo(
        self,
        photo_attribute: str,
        preview: Image.Image,
    ) -> tuple[ImageTk.PhotoImage, bool]:
        """Update an already-uploaded Tk image instead of allocating another one."""
        photo = getattr(self, photo_attribute, None)
        if (
            photo is not None
            and photo.width() == preview.width
            and photo.height() == preview.height
        ):
            try:
                photo.paste(preview)
                return photo, False
            except tk.TclError:
                pass
        photo = ImageTk.PhotoImage(preview)
        setattr(self, photo_attribute, photo)
        return photo, True

    def _transformed_zoom_source(
        self,
        source: Image.Image | None,
        source_key: tuple[object, ...] | None,
        *,
        segmented: bool = False,
    ) -> Image.Image | None:
        if source is None or source_key is None:
            return None
        if segmented:
            if len(source_key) < 7:
                return None
            source_width = int(source_key[1])
            source_height = int(source_key[2])
            source_view_x = float(source_key[-3])
            source_view_y = float(source_key[-2])
            source_zoom = float(source_key[-1])
        else:
            if len(source_key) < 5:
                return None
            source_width = int(source_key[0])
            source_height = int(source_key[1])
            source_view_x = float(source_key[2])
            source_view_y = float(source_key[3])
            source_zoom = float(source_key[4])
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        env = self.scenario.environment
        source_base = min(
            source_width / max(1.0, env.initial_view_width_m),
            source_height / max(1.0, env.initial_view_height_m),
        )
        source_scale = max(1e-12, source_base * source_zoom)
        destination_scale = max(1e-12, self._base_scale() * self.zoom)
        scale_ratio = source_scale / destination_scale
        source_x = (self.view_x - source_view_x) * source_scale
        source_y = (self.view_y - source_view_y) * source_scale
        fill = (0, 0, 0, 0) if source.mode == "RGBA" else MAPLESS_BACKGROUND
        return source.transform(
            (width, height),
            Image.Transform.AFFINE,
            (scale_ratio, 0.0, source_x, 0.0, scale_ratio, source_y),
            # Wheel previews favor interaction latency; the deferred redraw below
            # restores the exact LANCZOS-rendered map and beacon after 90 ms.
            resample=Image.Resampling.NEAREST,
            fillcolor=fill,
        )

    def _zoom_preview_composite(self) -> bool:
        geographic = self.obstacle_layer_source
        beacon = self.beacon_segment_source
        geographic_key = self.obstacle_layer_source_key
        beacon_key = self.beacon_segment_photo_key
        geographic_items = self.canvas.find_withtag(GEOGRAPHIC_LAYER_TAG)
        beacon_items = self.canvas.find_withtag("beacon-segment-image")
        if (
            geographic is None
            or beacon is None
            or geographic_key is None
            or beacon_key is None
            or len(beacon_key) < 7
            or not geographic_items
            or not beacon_items
            or geographic.size != beacon.size
        ):
            return False
        same_view = (
            abs(float(geographic_key[2]) - float(beacon_key[-3])) < 1e-6
            and abs(float(geographic_key[3]) - float(beacon_key[-2])) < 1e-6
            and abs(float(geographic_key[4]) - float(beacon_key[-1])) < 1e-9
        )
        if not same_view:
            return False
        composite_key = (id(geographic), id(beacon))
        if self.zoom_composite_source_key != composite_key or self.zoom_composite_source is None:
            combined = geographic.convert("RGBA")
            combined.alpha_composite(beacon)
            # The geographic layer is opaque, so retaining RGBA here only adds
            # upload bandwidth; RGB presents the identical composited pixels.
            self.zoom_composite_source = combined.convert("RGB")
            self.zoom_composite_source_key = composite_key
        preview = self._transformed_zoom_source(
            self.zoom_composite_source,
            geographic_key,
        )
        if preview is None:
            return False
        photo, replaced = self._paste_zoom_photo("zoom_composite_photo", preview)
        for item in geographic_items:
            if replaced or GEOGRAPHIC_LAYER_TAG not in self.zoom_preview_active_tags:
                self.canvas.itemconfigure(item, image=photo, state="normal")
            self.canvas.coords(item, 0, 0)
        if replaced or GEOGRAPHIC_LAYER_TAG not in self.zoom_preview_active_tags:
            self.zoom_preview_active_tags.add(GEOGRAPHIC_LAYER_TAG)
        for item in beacon_items:
            if "beacon-segment-image" not in self.zoom_preview_active_tags:
                self.canvas.itemconfigure(item, state="hidden")
            self.canvas.coords(item, 0, 0)
        if "beacon-segment-image" not in self.zoom_preview_active_tags:
            self.zoom_preview_active_tags.add("beacon-segment-image")
        self.zoom_preview_composite_active = True
        return True

    def _finish_zoom_render(self) -> None:
        self.zoom_render_after = None
        if self.zoom_preview_after is not None:
            try:
                self.root.after_cancel(self.zoom_preview_after)
            except tk.TclError:
                pass
            self.zoom_preview_after = None
        self.zoom_preview_composite_active = False
        self.render_canvas()

    def _render_zoom_preview(self) -> None:
        """Render one cheap raster preview for the latest wheel state, called
        synchronously on every wheel tick so it never lags the vector items
        canvas.scale() already moved."""
        self.zoom_preview_after = None
        if not hasattr(self, "canvas"):
            return
        if not self._zoom_preview_composite():
            self._zoom_preview_layer(
                self.obstacle_layer_source,
                self.obstacle_layer_source_key,
                tag=GEOGRAPHIC_LAYER_TAG,
                photo_attribute="zoom_geographic_photo",
            )
            self._zoom_preview_layer(
                self.beacon_segment_source,
                self.beacon_segment_photo_key,
                tag="beacon-segment-image",
                photo_attribute="zoom_beacon_photo",
                segmented=True,
            )
        self._zoom_preview_layer(
            getattr(self, "static_segment_source", None),
            self.static_segment_photo_key,
            tag="static-segment-image",
            photo_attribute="zoom_static_photo",
            segmented=True,
        )

    def _canvas_wheel(self, event: tk.Event) -> None:
        before = self.screen_to_world(event.x, event.y)
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        previous_zoom = self.zoom
        self.zoom = clamp(previous_zoom * factor, MIN_CANVAS_ZOOM, MAX_CANVAS_ZOOM)
        applied_factor = self.zoom / max(previous_zoom, 1e-12)
        if abs(applied_factor - 1.0) < 1e-12:
            return
        after = self.screen_to_world(event.x, event.y)
        self.view_x += before[0] - after[0]
        self.view_y += before[1] - after[1]
        if not hasattr(self, "canvas"):
            return
        self.canvas.scale("all", event.x, event.y, applied_factor, applied_factor)
        self.canvas.scale(HUD_LAYER_TAG, event.x, event.y, 1.0 / applied_factor, 1.0 / applied_factor)
        # Keep the map/obstacle raster, cached coverage, and every vector item
        # (nodes, obstacle outlines) on the same transform for every wheel
        # event. canvas.scale() above moves vector items instantly; deferring
        # this raster preview by even one frame let it lag behind them, which
        # reads as jitter/flicker on every tick -- worst with nodes/obstacles
        # on screen since they're the vectors visibly racing ahead of it.
        if self.zoom_preview_after is not None:
            try:
                self.root.after_cancel(self.zoom_preview_after)
            except tk.TclError:
                pass
            self.zoom_preview_after = None
        self._render_zoom_preview()
        if self.render_after is not None:
            try:
                self.root.after_cancel(self.render_after)
            except tk.TclError:
                pass
            self.render_after = None
        if self.zoom_render_after is not None:
            try:
                self.root.after_cancel(self.zoom_render_after)
            except tk.TclError:
                pass
        self.zoom_render_after = self.root.after(90, self._finish_zoom_render)

    def _canvas_motion(self, event: tk.Event) -> None:
        wx, wy = self.screen_to_world(event.x, event.y)
        env = self.scenario.environment
        geographic = ""
        if env.map_configured:
            latitude, longitude = world_to_latlon(
                wx, wy, env.map_center_lat, env.map_center_lon
            )
            geographic = f" · {latitude:.5f}, {longitude:.5f}"
        self.status_var.set(
            f"{self.tool.title()} · X {self.format_distance(wx)} · "
            f"Y {self.format_distance(wy)}{geographic} · zoom {self.zoom:.2f}×"
        )

    def hit_test(self, sx: float, sy: float) -> Node | Obstacle | None:
        for node in reversed(self.scenario.nodes):
            x, y = self.world_to_screen(node.x, node.y)
            if math.hypot(sx - x, sy - y) <= 17:
                return node
        world_x, world_y = self.screen_to_world(sx, sy)
        candidates = self._visible_obstacle_bounds or [
            (obstacle, self._obstacle_bounds(obstacle))
            for obstacle in self.scenario.obstacles
        ]
        for obstacle, bounds in reversed(candidates):
            x_min, y_min, x_max, y_max = bounds
            if not (x_min <= world_x <= x_max and y_min <= world_y <= y_max):
                continue
            if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
                if PropagationModel._point_in_polygon(world_x, world_y, obstacle.points):
                    return obstacle
                continue
            if obstacle.kind == "Forest" and obstacle.shape == "brush" and obstacle.points:
                segments = list(zip(obstacle.points, obstacle.points[1:])) or [(obstacle.points[0], obstacle.points[0])]
                if any(
                    PropagationModel._point_segment_distance_sq(
                        world_x, world_y, first[0], first[1], second[0], second[1]
                    )
                    <= obstacle.brush_radius_m**2
                    for first, second in segments
                ):
                    return obstacle
                continue
            if obstacle.kind == "Mountain":
                triangle = [((x_min + x_max) / 2, y_min), (x_max, y_max), (x_min, y_max)]
                if PropagationModel._point_in_polygon(world_x, world_y, triangle):
                    return obstacle
                continue
            return obstacle
        return None

    def add_node(self, x: float, y: float, *, name: str | None = None) -> Node:
        previous_selection = self.get_selected()
        number = max([node.node_num for node in self.scenario.nodes] + [0]) + 1
        node = Node(
            name=name or f"Node {len(self.scenario.nodes) + 1}",
            node_num=number,
            x=x,
            y=y,
        )
        self._set_auto_node_elevation(node)
        self.scenario.nodes.append(node)
        if not self.scenario.packet.source_id:
            self.scenario.packet.source_id = node.id
        self.selected_id = node.id
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_added_node(
            node,
            reuse_geographic_layer=not isinstance(previous_selection, Obstacle),
        )
        return node

    def add_obstacle(self, kind: str, x1: float, y1: float, x2: float, y2: float) -> None:
        color, attenuation, height, per_100, behavior, max_beyond = OBSTACLE_DEFAULTS[kind]
        base_elevation = self.scenario.environment.terrain_elevation((x1 + x2) / 2, (y1 + y2) / 2) or 0.0
        obstacle = Obstacle(
            name=f"{kind} {sum(1 for item in self.scenario.obstacles if item.kind == kind) + 1}",
            kind=kind,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            color=color,
            attenuation_db=attenuation,
            height_m=height,
            base_elevation_m=base_elevation,
            loss_per_100m_db=per_100,
            behavior=behavior,
            max_range_beyond_m=max_beyond,
        )
        self.scenario.obstacles.append(obstacle)
        self.selected_id = obstacle.id
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_scene_change(geographic=True)

    def add_forest_stroke(self, points: list[list[float]]) -> None:
        color, attenuation, height, per_100, behavior, max_beyond = OBSTACLE_DEFAULTS["Forest"]
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        obstacle = Obstacle(
            name=f"Forest {sum(1 for item in self.scenario.obstacles if item.kind == 'Forest') + 1}",
            kind="Forest",
            x1=min(x_values),
            y1=min(y_values),
            x2=max(x_values),
            y2=max(y_values),
            color=color,
            attenuation_db=attenuation,
            height_m=height,
            base_elevation_m=self.scenario.environment.terrain_elevation(
                sum(x_values) / len(x_values), sum(y_values) / len(y_values)
            )
            or 0.0,
            loss_per_100m_db=per_100,
            behavior=behavior,
            max_range_beyond_m=max_beyond,
            shape="brush",
            points=[point[:] for point in points],
            brush_radius_m=150.0,
        )
        self.scenario.obstacles.append(obstacle)
        self.selected_id = obstacle.id
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_scene_change(geographic=True)

    def add_random_nodes(self) -> None:
        count = simpledialog.askinteger(
            "Add random nodes",
            "How many nodes should be placed randomly?",
            parent=self.root,
            minvalue=1,
            maxvalue=1000,
            initialvalue=25,
        )
        if count is None:
            return
        env = self.scenario.environment
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        side_margin_px = max(30.0, canvas_width * 0.035)
        bottom_margin_px = max(30.0, canvas_height * 0.045)
        top_margin_px = max(30.0, canvas_height * 0.04)
        overlay_right_px = min(370.0, canvas_width * 0.42)
        overlay_bottom_px = min(165.0, max(115.0, canvas_height * 0.17))
        seed = env.seed + len(self.scenario.nodes) * 9973 + count
        screen_positions = spread_random_points_in_regions(
            count,
            [
                (
                    side_margin_px,
                    overlay_bottom_px,
                    canvas_width - side_margin_px,
                    canvas_height - bottom_margin_px,
                ),
                (
                    overlay_right_px,
                    top_margin_px,
                    canvas_width - side_margin_px,
                    overlay_bottom_px,
                ),
            ],
            seed,
        )
        positions = [self.screen_to_world(x, y) for x, y in screen_positions]
        next_number = max([node.node_num for node in self.scenario.nodes] + [0]) + 1
        start_count = len(self.scenario.nodes)
        template = self.scenario.nodes[0] if self.scenario.nodes else None
        added: list[Node] = []
        for index, (x, y) in enumerate(positions):
            node = Node(
                name=f"Node {start_count + index + 1}",
                node_num=next_number + index,
                x=x,
                y=y,
            )
            self._set_auto_node_elevation(node)
            if template:
                node.radio = type(template.radio)(**vars(template.radio))
                node.channel = template.channel
            self.scenario.nodes.append(node)
            added.append(node)
        if added and not self.scenario.packet.source_id:
            self.scenario.packet.source_id = added[0].id
        if added:
            self.selected_id = added[-1].id
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_scene_change(packet=True)
        if any(not self._terrain_covers(node.x, node.y) for node in added):
            self.status_var.set(
                f"Spread {count} random nodes across the visible map · refreshing terrain"
            )
            self.load_topography()
        else:
            self.status_var.set(f"Spread {count} random nodes across the visible map using seed {env.seed}")

    def duplicate_selected(self) -> None:
        obj = self.get_selected()
        if isinstance(obj, Node):
            raw = vars(obj).copy()
            raw["id"] = ""
            raw["radio"] = type(obj.radio)(**vars(obj.radio))
            copy = Node(**raw)
            copy.id = new_id("node")
            copy.name = f"{obj.name} copy"
            copy.node_num = max([node.node_num for node in self.scenario.nodes] + [0]) + 1
            copy.x += 200
            copy.y += 200
            self.scenario.nodes.append(copy)
        elif isinstance(obj, Obstacle):
            raw = vars(obj).copy()
            raw["id"] = new_id("obs")
            raw["name"] = f"{obj.name} copy"
            raw["x1"] += 150
            raw["x2"] += 150
            raw["y1"] += 150
            raw["y2"] += 150
            raw["points"] = [[point[0] + 150, point[1] + 150] for point in obj.points]
            copy = Obstacle(**raw)
            self.scenario.obstacles.append(copy)
        else:
            return
        self.selected_id = copy.id
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_scene_change(
            packet=isinstance(copy, Node),
            geographic=isinstance(copy, Obstacle),
        )
        self.show_sidebar_tab("Properties")

    def delete_selected(self) -> None:
        obj = self.get_selected()
        if obj is None:
            return
        if isinstance(obj, Node):
            self.scenario.nodes = [node for node in self.scenario.nodes if node.id != obj.id]
            self.scenario.learned_routes = {
                key: route
                for key, route in self.scenario.learned_routes.items()
                if obj.id not in route
            }
            if self.scenario.packet.source_id == obj.id:
                self.scenario.packet.source_id = self.scenario.nodes[0].id if self.scenario.nodes else ""
            if self.scenario.packet.destination_id == obj.id:
                self.scenario.packet.destination_id = "BROADCAST"
        else:
            self.scenario.obstacles = [obstacle for obstacle in self.scenario.obstacles if obstacle.id != obj.id]
        self.selected_id = None
        self.mark_dirty()
        self._mark_results_stale()
        self._refresh_scene_change(
            packet=isinstance(obj, Node),
            geographic=isinstance(obj, Obstacle),
        )

    def get_selected(self) -> Node | Obstacle | None:
        for obj in self.scenario.nodes:
            if obj.id == self.selected_id:
                return obj
        for obj in self.scenario.obstacles:
            if obj.id == self.selected_id:
                return obj
        return None

    def select(self, item_id: str | None) -> None:
        selection_changed = item_id != self.selected_id
        self.selected_id = item_id
        selected_object = self.get_selected()
        self.path_focus_id = (
            selected_object.id
            if isinstance(selected_object, Node) and self._active_packet_reached() is not None
            else None
        )
        if selection_changed:
            self._build_object_form()
        self.render_canvas()
        if selection_changed:
            self._refresh_mesh_graph()
        if isinstance(selected_object, Node) and self._active_packet_reached() is not None:
            path = self._selected_packet_path()
            if path:
                names = {node.id: node.name for node in self.scenario.nodes}
                route = " → ".join(names.get(node_id, node_id) for node_id in path)
                self.status_var.set(f"Packet path ({len(path) - 1} hops): {route}")
            else:
                self.status_var.set(f"{selected_object.name} did not receive the retained packet")
        if item_id and selection_changed:
            self.show_sidebar_tab("Properties")
        if item_id and self.scene_tree.exists(item_id) and self.scene_tree.selection() != (item_id,):
            self.scene_tree.selection_set(item_id)
            self.scene_tree.see(item_id)
        elif self.scene_tree.selection():
            self.scene_tree.selection_remove(*self.scene_tree.selection())

    def refresh_scene_tree(self) -> None:
        tree = self.scene_tree
        if not tree.exists("_nodes") or not tree.exists("_obstacles"):
            tree.delete(*tree.get_children())
            tree.insert("", "end", iid="_nodes", open=True)
            tree.insert("", "end", iid="_obstacles", open=False)
            self._scene_tree_signatures = {}
        tree.item("_nodes", text=f"  Nodes  ({len(self.scenario.nodes)})", open=True)
        tree.item("_obstacles", text=f"  Obstructions  ({len(self.scenario.obstacles)})")

        desired: list[tuple[str, str, tuple[object, ...], str]] = []
        for node in self.scenario.nodes:
            marker = "●" if node.online else "○"
            signature = (node.name, node.role, node.online)
            desired.append((node.id, "_nodes", signature, f"  {marker}  {node.name}  ·  {node.role}"))
        # Thousands of imported footprints are retained in the scenario and RF
        # model, but creating one Tk row for every footprint blocks the event loop
        # for seconds. Manual objects remain fully listed; imported objects are
        # paged on demand and map selection still works for every footprint.
        manual_obstacles = [obstacle for obstacle in self.scenario.obstacles if not obstacle.osm_id]
        imported_obstacles = [obstacle for obstacle in self.scenario.obstacles if obstacle.osm_id]
        displayed_obstacles = manual_obstacles + imported_obstacles[
            : self._scene_tree_imported_obstacle_limit
        ]
        selected = self.get_selected()
        if isinstance(selected, Obstacle) and selected not in displayed_obstacles:
            displayed_obstacles.append(selected)
        for obstacle in displayed_obstacles:
            signature = (obstacle.name, obstacle.kind)
            desired.append(
                (obstacle.id, "_obstacles", signature, f"  ▣  {obstacle.name}  ·  {obstacle.kind}")
            )

        more_id = "_obstacles_more"
        existing = set(tree.get_children("_nodes")) | (
            set(tree.get_children("_obstacles")) - {more_id}
        )
        desired_ids = {item_id for item_id, _parent, _signature, _text in desired}
        stale = existing - desired_ids
        if stale:
            tree.delete(*stale)
            for item_id in stale:
                self._scene_tree_signatures.pop(item_id, None)
        for item_id, parent, signature, text in desired:
            if item_id not in existing:
                tree.insert(parent, "end", iid=item_id, text=text)
            elif self._scene_tree_signatures.get(item_id) != signature:
                tree.item(item_id, text=text)
            self._scene_tree_signatures[item_id] = signature
        for parent, ordered_ids in (
            ("_nodes", [node.id for node in self.scenario.nodes]),
            ("_obstacles", [obstacle.id for obstacle in displayed_obstacles]),
        ):
            current_ids = [item_id for item_id in tree.get_children(parent) if item_id != more_id]
            if current_ids != ordered_ids:
                for index, item_id in enumerate(ordered_ids):
                    tree.move(item_id, parent, index)
        remaining = max(0, len(imported_obstacles) - self._scene_tree_imported_obstacle_limit)
        if remaining:
            next_count = min(SCENE_TREE_OBSTACLE_PAGE_SIZE, remaining)
            more_text = f"  …  Load {next_count:,} more imported obstructions · {remaining:,} remaining"
            if tree.exists(more_id):
                tree.item(more_id, text=more_text)
                tree.move(more_id, "_obstacles", "end")
            else:
                tree.insert("_obstacles", "end", iid=more_id, text=more_text)
        elif tree.exists(more_id):
            tree.delete(more_id)
        if self.selected_id and self.scene_tree.exists(self.selected_id):
            self.scene_tree.selection_set(self.selected_id)

    def _refresh_added_node(self, node: Node, *, reuse_geographic_layer: bool) -> None:
        """Refresh node-dependent UI without rebuilding the unchanged obstacle tree."""
        if self.scene_tree.exists("_nodes"):
            self.scene_tree.item("_nodes", text=f"  Nodes  ({len(self.scenario.nodes)})")
            marker = "●" if node.online else "○"
            self.scene_tree.insert(
                "_nodes",
                "end",
                iid=node.id,
                text=f"  {marker}  {node.name}  ·  {node.role}",
            )
            self._scene_tree_signatures[node.id] = (node.name, node.role, node.online)
            self.scene_tree.selection_set(node.id)
            self.scene_tree.see(node.id)
        else:
            self.refresh_scene_tree()
        self._build_object_form()
        self._build_packet_form()
        self.render_canvas(reuse_geographic_layer=reuse_geographic_layer)
        self._refresh_mesh_graph()
        self._update_title()

    def _scene_tree_select(self, _event: tk.Event) -> None:
        selected = self.scene_tree.selection()
        if selected == ("_obstacles_more",):
            self._scene_tree_imported_obstacle_limit += SCENE_TREE_OBSTACLE_PAGE_SIZE
            self.refresh_scene_tree()
            return
        if selected and not selected[0].startswith("_") and selected[0] != self.selected_id:
            self.select(selected[0])

    def _refresh_scene_change(
        self,
        *,
        packet: bool = False,
        geographic: bool = False,
    ) -> None:
        """Refresh only UI surfaces affected by a node/obstacle mutation."""
        if geographic:
            self._invalidate_geographic_layer()
        self.refresh_scene_tree()
        self._build_object_form()
        if packet:
            self._build_packet_form()
        self.render_canvas()
        self._refresh_mesh_graph()
        self._update_title()

    def refresh_all(self) -> None:
        self._invalidate_geographic_layer()
        if hasattr(self, "map_layer_var"):
            self.map_layer_var.set(self.scenario.environment.map_layer)
        self.refresh_scene_tree()
        self._build_object_form()
        self._build_environment_form()
        self._build_packet_form()
        self.render_canvas()
        self._refresh_mesh_graph()
        self._update_title()

    def _id_for_name(self, name: str) -> str:
        for node in self.scenario.nodes:
            if node.name == name:
                return node.id
        return ""

    def _name_for_id(self, item_id: str) -> str:
        for node in self.scenario.nodes:
            if node.id == item_id:
                return node.name
        return ""

    def mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()

    def _update_title(self) -> None:
        marker = " *" if self.dirty else ""
        path = os.path.basename(self.file_path) if self.file_path else self.scenario.name
        self.root.title(f"{path}{marker} — MeshLab RF")

    def _sync_live_mesh_interval_vars(self) -> None:
        config = self.scenario.live_mesh
        self.live_mesh_nodeinfo_var.set(str(config.nodeinfo_interval_minutes))
        self.live_mesh_telemetry_var.set(str(config.telemetry_interval_minutes))
        self.live_mesh_router_telemetry_var.set(str(config.router_telemetry_interval_minutes))
        self.live_mesh_sensor_var.set(str(config.sensor_interval_minutes))
        self.live_mesh_message_var.set(str(config.message_interval_minutes))

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved scenario",
            "Save changes to this scenario before continuing?",
            parent=self.root,
        )
        if answer is None:
            return False
        if answer:
            return self.save_scenario()
        return True

    def new_scenario(self) -> None:
        if not self._confirm_discard():
            return
        self.stop_animation()
        self.stop_live_mesh(clear_visuals=True)
        self.scenario = Scenario(name="Untitled scenario")
        self._sync_live_mesh_interval_vars()
        self._sync_live_mesh_preset()
        self.file_path = None
        self.selected_id = None
        self.dirty = False
        self.map_tile_images.clear()
        self.map_tile_decoded.clear()
        self.map_tile_failures.clear()
        self.clear_results()
        self.refresh_all()
        self.fit_view()
        self.root.after(50, self._load_startup_terrain)

    def open_scenario(self) -> None:
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open MeshLab scenario",
            filetypes=[("MeshLab scenario", "*.meshlab.json"), ("JSON files", "*.json"), ("All files", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        try:
            loaded_scenario = scenario_from_file(path)
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror("Could not open scenario", str(error), parent=self.root)
            return
        self.stop_live_mesh(clear_visuals=True)
        self.scenario = loaded_scenario
        self._sync_live_mesh_interval_vars()
        self._sync_live_mesh_preset()
        self.file_path = path
        self.selected_id = None
        self.dirty = False
        self.map_tile_images.clear()
        self.map_tile_decoded.clear()
        self.map_tile_failures.clear()
        self.clear_results()
        self.refresh_all()
        self.fit_view()
        self.root.after(50, self._load_startup_terrain)

    def save_scenario(self) -> bool:
        if not self.file_path:
            return self.save_scenario_as()
        try:
            scenario_to_file(self.file_path, self.scenario)
        except OSError as error:
            messagebox.showerror("Could not save scenario", str(error), parent=self.root)
            return False
        self.dirty = False
        self._update_title()
        self.status_var.set(f"Saved {self.file_path}")
        return True

    def save_scenario_as(self) -> bool:
        path = filedialog.asksaveasfilename(
            title="Save MeshLab scenario",
            defaultextension=".meshlab.json",
            filetypes=[("MeshLab scenario", "*.meshlab.json"), ("JSON files", "*.json")],
            parent=self.root,
        )
        if not path:
            return False
        self.file_path = path
        return self.save_scenario()

    def show_survey_viewer(self) -> None:
        if self.survey_window is not None and self.survey_window.winfo_exists():
            self.survey_window.deiconify()
            self.survey_window.lift()
            return

        window = tk.Toplevel(self.root)
        self.survey_window = window
        window.title("MeshLab RF — Survey Export & Viewer")
        window.geometry("1220x760")
        window.minsize(920, 600)
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", self._close_survey_viewer)

        header = ttk.Frame(window, style="Toolbar.TFrame")
        header.pack(fill="x")
        title = ttk.Frame(header, style="Toolbar.TFrame")
        title.pack(side="left", padx=14, pady=10)
        ttk.Label(title, text="FIELD SURVEY", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title,
            text="Select either node to load its retained log immediately; later selections merge without replacing it.",
            style="Muted.TLabel",
        ).pack(anchor="w")
        actions = ttk.Frame(header, style="Toolbar.TFrame")
        actions.pack(side="right", padx=12, pady=10)
        self.survey_export_button = ttk.Button(
            actions, text="Save captured logs…", style="Accent.TButton", command=self._start_survey_export,
            state="disabled",
        )
        self.survey_export_button.pack(side="left", padx=3)
        ttk.Button(actions, text="Open saved export…", command=self._open_survey_export).pack(side="left", padx=3)
        self.survey_folder_button = ttk.Button(
            actions, text="Open folder", command=self._open_survey_folder,
            state="normal" if self.survey_export_path else "disabled",
        )
        self.survey_folder_button.pack(side="left", padx=3)

        status = ttk.Frame(window)
        status.pack(fill="x", padx=12, pady=(9, 4))
        self.survey_status_var = tk.StringVar(
            value="Choose a mobile or base serial port; its complete log loads and plots automatically."
        )
        ttk.Label(status, textvariable=self.survey_status_var, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
        self.survey_progress = ttk.Progressbar(status, mode="determinate", maximum=100, length=250)
        self.survey_progress.pack(side="right", padx=(10, 0))

        devices_frame = ttk.LabelFrame(window, text="Survey node ports")
        devices_frame.pack(fill="x", padx=12, pady=5)
        port_picker = ttk.Frame(devices_frame)
        port_picker.pack(fill="x", padx=7, pady=(7, 2))
        ttk.Label(port_picker, text="Survey node port", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.survey_port_combo = ttk.Combobox(
            port_picker, textvariable=self.survey_port_var, state="readonly", width=42,
        )
        self.survey_port_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.survey_identify_button = ttk.Button(
            port_picker, text="Reload selected", command=self._start_survey_identify, state="disabled",
        )
        self.survey_identify_button.grid(row=1, column=1, sticky="ew")
        port_picker.columnconfigure(0, weight=1)
        self.survey_port_combo.bind("<<ComboboxSelected>>", self._survey_port_changed)
        self.survey_port_combo.bind("<Button-1>", self._refresh_survey_ports)
        self.survey_devices_tree = ttk.Treeview(
            devices_frame,
            columns=("role", "port", "node", "records", "size", "format"),
            show="headings",
            height=2,
        )
        for key, label, width in (
            ("role", "Role", 90), ("port", "Port", 90), ("node", "Node ID", 190),
            ("records", "Stored records", 115), ("size", "Log data", 100), ("format", "Format", 90),
        ):
            self.survey_devices_tree.heading(key, text=label)
            self.survey_devices_tree.column(key, width=width, minwidth=65, stretch=key == "node")
        self.survey_devices_tree.pack(fill="x", padx=5, pady=(2, 5))

        summary = ttk.Frame(window)
        summary.pack(fill="x", padx=10, pady=4)
        self.survey_metric_vars: dict[str, tk.StringVar] = {}
        for column, (key, title_text) in enumerate((
            ("probes", "Joined probes"), ("forward", "Reached base"),
            ("reply", "Replies returned"), ("gps", "Mapped GPS points"),
        )):
            card = tk.Frame(summary, bg="#101f32", highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=0, column=column, sticky="ew", padx=2)
            summary.columnconfigure(column, weight=1)
            tk.Label(card, text=title_text, bg="#101f32", fg=MUTED, anchor="w").pack(fill="x", padx=9, pady=(5, 0))
            variable = tk.StringVar(value="—")
            self.survey_metric_vars[key] = variable
            tk.Label(
                card, textvariable=variable, bg="#101f32", fg=TEXT,
                font=("Segoe UI Semibold", 15), anchor="w",
            ).pack(fill="x", padx=9, pady=(0, 5))

        controls = ttk.Frame(window)
        controls.pack(fill="x", padx=12, pady=(4, 3))
        ttk.Label(controls, text="Show", style="Muted.TLabel").pack(side="left")
        self.survey_filter_var = tk.StringVar(value="All measurements")
        filter_box = ttk.Combobox(
            controls,
            textvariable=self.survey_filter_var,
            values=("All measurements", "Complete round trips", "Forward only", "Forward lost"),
            state="readonly",
            width=22,
        )
        filter_box.pack(side="left", padx=6)
        filter_box.bind("<<ComboboxSelected>>", self._refresh_survey_table)
        ttk.Button(controls, text="Fit all points on map", command=self.fit_survey_view).pack(side="right", padx=3)
        ttk.Button(controls, text="Clear map points", command=self._clear_survey_map).pack(side="right", padx=3)
        ttk.Button(
            controls,
            text="Calibrate buildings",
            command=self._calibrate_buildings_from_survey,
        ).pack(side="right", padx=3)

        table_frame = ttk.Frame(window)
        table_frame.pack(fill="both", expand=True, padx=12, pady=(2, 12))
        self.survey_tree = self._tree(
            table_frame,
            [
                ("sequence", "Probe", 65), ("time", "Time", 145), ("location", "Mobile GPS", 190),
                ("sat", "Sat", 45), ("hdop", "HDOP", 55), ("forward", "Forward", 75),
                ("forward_rssi", "Out RSSI", 75), ("forward_snr", "Out SNR", 65),
                ("reply", "Reply", 70), ("reverse_rssi", "Back RSSI", 80),
                ("reverse_snr", "Back SNR", 70),
            ],
        )
        self.survey_tree.bind("<<TreeviewSelect>>", self._survey_tree_selected)

        self._refresh_survey_ports()
        self._survey_port_changed()
        if self.survey_measurements:
            self._apply_survey_measurements(self.survey_measurements, self.survey_export_path, fit=False)

    def _close_survey_viewer(self) -> None:
        if self.survey_window is not None:
            self.survey_window.destroy()
        self.survey_window = None

    def _survey_set_busy(self, busy: bool, message: str = "") -> None:
        if self.survey_window is None or not self.survey_window.winfo_exists():
            return
        self.survey_port_combo.configure(state="disabled" if busy else "readonly")
        selected_port = self._selected_survey_port()
        self.survey_identify_button.configure(
            state="normal" if not busy and selected_port else "disabled"
        )
        self.survey_export_button.configure(
            state="disabled" if busy or not self.survey_captures else "normal"
        )
        if message:
            self.survey_status_var.set(message)
        if busy:
            self.survey_progress.configure(mode="indeterminate")
            self.survey_progress.start(12)
        else:
            self.survey_progress.stop()
            self.survey_progress.configure(mode="determinate")

    def _refresh_survey_ports(self, _event: tk.Event | None = None) -> None:
        if self.survey_worker is not None and self.survey_worker.is_alive():
            return
        previous = self.survey_port_var.get()
        ports = list_serial_ports()
        self.survey_ports = {port.label: port for port in ports}
        labels = [SURVEY_PORT_NONE, *self.survey_ports]
        self.survey_port_combo.configure(values=labels)
        self.survey_port_var.set(previous if previous in self.survey_ports else SURVEY_PORT_NONE)
        if ports:
            self.survey_status_var.set(
                f"Found {len(ports)} serial port{'s' if len(ports) != 1 else ''}. "
                "Choose the connected survey node to load and plot it automatically."
            )
        else:
            self.survey_status_var.set("No serial ports found. Connect a survey node and open the dropdown again.")

    def _selected_survey_port(self) -> str | None:
        port = self.survey_ports.get(self.survey_port_var.get())
        return port.device if port is not None else None

    def _survey_port_changed(self, _event: tk.Event | None = None) -> None:
        self.survey_devices = []
        self._populate_survey_devices()
        port = self._selected_survey_port()
        if hasattr(self, "survey_identify_button"):
            self.survey_identify_button.configure(state="normal" if port else "disabled")
        if hasattr(self, "survey_export_button"):
            self.survey_export_button.configure(state="disabled")
        if port is not None:
            self.survey_capture_attempts.discard(port)
            if hasattr(self, "survey_status_var"):
                self.survey_status_var.set("Loading the selected survey node…")
        self._capture_next_selected()

    def _capture_next_selected(self) -> None:
        if self.survey_worker is not None and self.survey_worker.is_alive():
            return
        port = self._selected_survey_port()
        if port is None or port in self.survey_capture_attempts:
            return
        if any(capture.info.port == port for capture in self.survey_captures.values()):
            return
        self._start_survey_capture(port)

    def _start_survey_identify(self) -> None:
        if self.survey_worker is not None and self.survey_worker.is_alive():
            return
        port = self._selected_survey_port()
        if port is None:
            self.survey_status_var.set("Choose a survey-node port first.")
            return
        for role, capture in list(self.survey_captures.items()):
            if capture.info.port == port:
                self.survey_captures.pop(role, None)
        self.survey_capture_attempts.discard(port)
        self._capture_next_selected()

    def _start_survey_capture(self, port: str) -> None:
        self.survey_capture_attempts.add(port)
        self._survey_set_busy(True, f"Loading the survey log from {port}…")

        def progress(message: str, fraction: float | None) -> None:
            self.survey_updates.put(("progress", (message, fraction)))

        def worker() -> None:
            try:
                info = query_device(port)
                self.survey_updates.put(("capture", capture_device(info, progress)))
            except Exception as error:
                self.survey_updates.put(("error", error))

        self.survey_worker = threading.Thread(target=worker, name="SurveyDeviceCapture", daemon=True)
        self.survey_worker.start()
        self.root.after(50, self._poll_survey_updates)

    def _start_survey_export(self) -> None:
        if self.survey_worker is not None and self.survey_worker.is_alive():
            return
        if not self.survey_captures:
            return
        parent = Path.cwd() / "survey-data"
        parent.mkdir(parents=True, exist_ok=True)
        selected = filedialog.askdirectory(
            title="Choose parent folder for the captured survey logs",
            initialdir=str(parent),
            parent=self.survey_window,
        )
        if not selected:
            return
        destination = Path(selected) / datetime.now().strftime("%Y%m%d-%H%M%S")
        captures = tuple(self.survey_captures.values())
        self._survey_set_busy(True, "Saving captured survey logs…")

        def progress(message: str, fraction: float | None) -> None:
            self.survey_updates.put(("progress", (message, fraction)))

        def worker() -> None:
            try:
                self.survey_updates.put(("export", save_captures(captures, destination, progress)))
            except Exception as error:
                self.survey_updates.put(("error", error))

        self.survey_worker = threading.Thread(target=worker, name="SurveyDeviceExport", daemon=True)
        self.survey_worker.start()
        self.root.after(50, self._poll_survey_updates)

    def _poll_survey_updates(self) -> None:
        while True:
            try:
                operation, payload = self.survey_updates.get_nowait()
            except queue.Empty:
                break
            if operation == "progress":
                message, fraction = payload
                if self.survey_window is not None and self.survey_window.winfo_exists():
                    self.survey_status_var.set(str(message))
                    if fraction is not None:
                        self.survey_progress.stop()
                        self.survey_progress.configure(mode="determinate", value=float(fraction) * 100.0)
            elif operation == "capture":
                capture = payload
                assert isinstance(capture, DeviceCapture)
                self.survey_captures[capture.info.role] = capture
                self.survey_devices = [
                    item.info for item in sorted(
                        self.survey_captures.values(), key=lambda item: item.info.role, reverse=True
                    )
                ]
                self._populate_survey_devices()
                raw_rows = [
                    row for item in self.survey_captures.values() for row in item.rows
                ]
                measurements = merge_survey_rows(raw_rows)
                self.survey_export_path = None
                self.survey_export_roles = set(self.survey_captures)
                if self.survey_window is not None and self.survey_window.winfo_exists():
                    self.survey_folder_button.configure(state="disabled")
                self._apply_survey_measurements(measurements, None)
                roles = sorted(self.survey_captures)
                if roles == ["base", "mobile"]:
                    message = (
                        f"Loaded both retained logs in memory · {len(measurements):,} joined measurements plotted."
                    )
                elif roles in (["base"], ["mobile"]):
                    message = (
                        f"Loaded {capture.valid_records:,} {roles[0]} records in memory. "
                        "Select the other node whenever ready; this capture will be retained."
                    )
                elif not roles:
                    message = "No survey-node logs are loaded."
                else:
                    message = f"Loaded survey roles: {', '.join(roles)}."
                self._survey_set_busy(False, message)
            elif operation == "export":
                result = payload
                assert isinstance(result, SurveyExport)
                self.survey_export_path = result.destination
                self.survey_export_roles = set(result.roles)
                self._apply_survey_measurements(result.measurements, result.destination)
                if self.survey_window is not None and self.survey_window.winfo_exists():
                    self.survey_folder_button.configure(state="normal")
                self._survey_set_busy(
                    False,
                    (
                        f"Complete paired export: {len(result.measurements):,} measurements saved to {result.destination}"
                        if set(result.roles) == {"mobile", "base"}
                        else f"Single {result.roles[0]} log exported to {result.destination}. "
                        "Connect and export the other role later to complete this survey."
                    ),
                )
                self.status_var.set(f"Survey exported and plotted · {result.destination}")
            elif operation == "error":
                self._survey_set_busy(False, f"Survey operation failed: {payload}")
                if self.survey_window is not None and self.survey_window.winfo_exists():
                    messagebox.showerror("Survey operation failed", str(payload), parent=self.survey_window)
        if self.survey_worker is not None and self.survey_worker.is_alive():
            self.root.after(50, self._poll_survey_updates)
        else:
            self.survey_worker = None
            self.root.after(10, self._capture_next_selected)

    def _populate_survey_devices(self) -> None:
        if not hasattr(self, "survey_devices_tree"):
            return
        self.survey_devices_tree.delete(*self.survey_devices_tree.get_children())
        for device in sorted(self.survey_devices, key=lambda item: item.role, reverse=True):
            self.survey_devices_tree.insert(
                "",
                "end",
                values=(
                    device.role.upper(), device.port, f"{device.node_id:016x}", f"{device.slots:,}",
                    f"{device.slots * device.record_size / 1024:.1f} KB", f"v{device.version} / {device.record_size} B",
                ),
            )

    def _open_survey_export(self) -> None:
        path = filedialog.askopenfilename(
            title="Open MeshLab survey measurements",
            filetypes=[("MeshLab measurements", "measurements.csv"), ("CSV files", "*.csv")],
            parent=self.survey_window or self.root,
        )
        if not path:
            return
        try:
            measurements = read_measurements(path)
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not open survey", str(error), parent=self.survey_window or self.root)
            return
        self.survey_export_path = Path(path).parent
        manifest_path = self.survey_export_path / "survey-export.json"
        self.survey_export_roles = set()
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.survey_export_roles = {
                    str(role).lower() for role in manifest.get("roles", [])
                    if str(role).lower() in {"mobile", "base"}
                }
            except (OSError, ValueError, TypeError):
                self.survey_export_roles = set()
        self._apply_survey_measurements(measurements, self.survey_export_path)
        if self.survey_window is not None and self.survey_window.winfo_exists():
            self.survey_folder_button.configure(state="normal")
            if len(self.survey_export_roles) == 1:
                role = next(iter(self.survey_export_roles))
                self.survey_status_var.set(
                    f"Loaded single {role} export. Connect and export the other role to complete it."
                )
            else:
                self.survey_status_var.set(f"Loaded {len(measurements):,} measurements from {path}")

    def _open_survey_folder(self) -> None:
        if self.survey_export_path is None:
            return
        try:
            os.startfile(str(self.survey_export_path))
        except OSError as error:
            messagebox.showerror("Could not open export folder", str(error), parent=self.survey_window or self.root)

    def _apply_survey_measurements(
        self,
        measurements: list[dict[str, object]],
        source: Path | None,
        fit: bool = True,
    ) -> None:
        self.survey_measurements = list(measurements)
        self.survey_export_path = source
        self.survey_selected_index = None
        total = len(self.survey_measurements)
        forward = sum(survey_bool(row.get("forward_received")) for row in self.survey_measurements)
        reply = sum(survey_bool(row.get("reply_received")) for row in self.survey_measurements)
        reply_known = sum(
            survey_value_known(row.get("reply_received")) for row in self.survey_measurements
        )
        gps = sum(self._survey_world_position(row) is not None for row in self.survey_measurements)
        if hasattr(self, "survey_metric_vars"):
            self.survey_metric_vars["probes"].set(f"{total:,}")
            self.survey_metric_vars["forward"].set(f"{forward:,} · {forward / total * 100:.1f}%" if total else "0")
            if reply_known < total:
                self.survey_metric_vars["reply"].set(
                    f"{reply:,} received · {total - reply_known:,} unknown"
                )
            else:
                self.survey_metric_vars["reply"].set(
                    f"{reply:,} · {reply / total * 100:.1f}%" if total else "0"
                )
            self.survey_metric_vars["gps"].set(f"{gps:,}")
            self._refresh_survey_table()
        if fit:
            self.fit_survey_view()
        else:
            self.render_canvas()

    def _survey_filter_matches(self, measurement: dict[str, object]) -> bool:
        selected = self.survey_filter_var.get() if hasattr(self, "survey_filter_var") else "All measurements"
        forward = survey_bool(measurement.get("forward_received"))
        reply = survey_bool(measurement.get("reply_received"))
        return (
            selected == "All measurements"
            or (selected == "Complete round trips" and forward and reply)
            or (selected == "Forward only" and forward and not reply)
            or (selected == "Forward lost" and not forward)
        )

    def _refresh_survey_table(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "survey_tree"):
            return
        self.survey_tree.delete(*self.survey_tree.get_children())
        for index, measurement in enumerate(self.survey_measurements):
            if not self._survey_filter_matches(measurement):
                continue
            epoch = survey_float(measurement.get("epoch_s")) or 0.0
            timestamp = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch > 0 else "GPS time unavailable"
            latitude = survey_float(measurement.get("mobile_latitude"))
            longitude = survey_float(measurement.get("mobile_longitude"))
            location = f"{latitude:.6f}, {longitude:.6f}" if latitude is not None and longitude is not None else "No GPS"
            forward = survey_bool(measurement.get("forward_received"))
            reply = survey_bool(measurement.get("reply_received"))
            reply_known = survey_value_known(measurement.get("reply_received"))
            forward_rssi = survey_float(measurement.get("forward_rssi_dbm"))
            forward_snr = survey_float(measurement.get("forward_snr_db"))
            reverse_rssi = survey_float(measurement.get("reverse_rssi_dbm"))
            reverse_snr = survey_float(measurement.get("reverse_snr_db"))
            self.survey_tree.insert(
                "", "end", iid=str(index),
                values=(
                    measurement.get("sequence", index + 1), timestamp, location,
                    measurement.get("mobile_satellites", ""), measurement.get("mobile_hdop", ""),
                    "Received" if forward else "Lost",
                    f"{forward_rssi:.0f} dBm" if forward_rssi is not None else "—",
                    f"{forward_snr:.2f} dB" if forward_snr is not None else "—",
                    (
                        "Received" if reply
                        else "Not returned" if reply_known and forward
                        else "Not observed" if forward
                        else "—"
                    ),
                    f"{reverse_rssi:.0f} dBm" if reverse_rssi is not None else "—",
                    f"{reverse_snr:.2f} dB" if reverse_snr is not None else "—",
                ),
                tags=("lost" if not forward else "complete" if reply else "partial",),
            )
        self.survey_tree.tag_configure("lost", foreground="#ff9aaa")
        self.survey_tree.tag_configure("complete", foreground="#b9f5d8")
        self.survey_tree.tag_configure("partial", foreground="#ffd58a")

    def _survey_tree_selected(self, _event: tk.Event | None = None) -> None:
        selected = self.survey_tree.selection()
        if len(selected) != 1:
            return
        try:
            index = int(selected[0])
        except ValueError:
            return
        self.select_survey_measurement(index)

    def _clear_survey_map(self) -> None:
        self.survey_measurements = []
        self.survey_selected_index = None
        if hasattr(self, "survey_tree"):
            self.survey_tree.delete(*self.survey_tree.get_children())
        if hasattr(self, "survey_metric_vars"):
            for variable in self.survey_metric_vars.values():
                variable.set("—")
        self.render_canvas()
        self.status_var.set("Survey points cleared from the map; exported files were not deleted")

    def _calibrate_buildings_from_survey(self) -> None:
        try:
            calibration = fit_building_calibration(self.scenario, self.survey_measurements)
        except SurveyCalibrationError as error:
            messagebox.showerror(
                "Building calibration unavailable",
                str(error),
                parent=self.survey_window or self.root,
            )
            return
        message = (
            f"MeshLab fitted {calibration.sample_count:,} survey outcomes "
            f"({calibration.received_sample_count:,} received RSSI, "
            f"{calibration.lost_sample_count:,} failed probes) "
            f"({calibration.clear_sample_count:,} clear, "
            f"{calibration.obstructed_sample_count:,} building-obstructed).\n\n"
            f"Apply these measured values to all {calibration.building_count:,} buildings?\n\n"
            f"Penetration: {calibration.penetration_db:.2f} dB per crossed building\n"
            f"Inside distance: {calibration.loss_per_100m_db:.2f} dB / 100 m\n"
            "Range cutoff: none\n"
            "Propagation baseline: unchanged\n\n"
            "The sampled Building value is applied globally, including areas where no "
            "survey points were collected."
        )
        if not messagebox.askyesno(
            "Apply measured building calibration?",
            message,
            parent=self.survey_window or self.root,
        ):
            return
        changed = apply_building_calibration(self.scenario, calibration)
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_all()
        self.status_var.set(
            f"Calibrated {changed:,} buildings from {calibration.sample_count:,} survey samples; "
            "no building range cap"
        )
        if hasattr(self, "survey_status_var"):
            self.survey_status_var.set(
                f"Applied: {calibration.penetration_db:.2f} dB/building + "
                f"{calibration.loss_per_100m_db:.2f} dB/100 m, no cap, across the entire map. "
                "Save the scenario to retain it."
            )

    def export_results(self) -> None:
        if not self.last_result:
            messagebox.showinfo("No results", "Run a packet simulation first.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            title="Export event timeline",
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
            parent=self.root,
        )
        if not path:
            return
        names = {node.id: node.name for node in self.scenario.nodes}
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["time_ms", "event", "node", "peer", "hop", "rssi_dbm", "snr_db", "margin_db", "decoded", "detail"])
                for event in self.last_result.events:
                    writer.writerow(
                        [
                            event.time_ms,
                            event.kind,
                            names.get(event.node_id, event.node_id),
                            names.get(event.peer_id, event.peer_id),
                            event.hop,
                            event.rssi_dbm,
                            event.snr_db,
                            event.margin_db,
                            event.decoded,
                            event.detail,
                        ]
                    )
        except OSError as error:
            messagebox.showerror("Export failed", str(error), parent=self.root)
            return
        self.status_var.set(f"Exported results to {path}")

    def on_close(self) -> None:
        self.stop_animation()
        self.stop_live_mesh(clear_visuals=True)
        if self.live_radio.connected or self.live_radio.connecting:
            self.live_radio.disconnect()
        self.root.destroy()

    def show_model_info(self) -> None:
        messagebox.showinfo(
            "Propagation and firmware model",
            "RF links use 3D antenna distance, a log-distance link budget, both antenna/RF chains, "
            "LoRa bandwidth and spreading-factor sensitivity, editable obstruction loss, and optional "
            "seeded shadowing. Loaded Mapzen/AWS elevation can block line of sight or add loss when terrain "
            "enters 60% of the first Fresnel zone.\n\n"
            "Broadcasts and first-contact DMs use a managed-flood approximation with hop limits, "
            "role-dependent relay delay, rebroadcast modes, duplicate cancellation, opaque channel relays, "
            "airtime, collisions, and capture. An acknowledged DM can store its first-arrival path. Later "
            "DMs use directed hop lines; a failed stored path is removed and falls back to flooding. The live "
            "traffic test adds concurrent NodeInfo, telemetry, sensor, and message broadcasts with rolling "
            "25%/40% channel-utility gates.\n\n"
            "The simulator stores one complete learned path per source/destination pair. It does not import "
            "live firmware next-hop tables, reproduce every retry, enforce regional radio law, ray-trace "
            "multipath, or replace a calibrated site survey. Map and elevation data can be incomplete or "
            "outdated; calibrate important scenarios with field measurements.",
            parent=self.root,
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "About MeshLab RF",
            "MeshLab RF\n\n"
            "Unreleased Windows planning tool for Meshtastic packet propagation, terrain-aware RF coverage, "
            "obstruction import, and live read-only NodeDB plotting.\n\n"
            "Its routing and radio assumptions are based on the local Meshtastic firmware checkout. "
            "See Help → Model assumptions and the bundled project documentation for scope and limitations.",
            parent=self.root,
        )

    def format_distance(self, meters: float) -> str:
        return format_distance_value(meters, self.unit_system.get())

    def format_area(self, square_meters: float) -> str:
        return format_area_value(square_meters, self.unit_system.get())

    def _display_length(self, meters: float) -> float:
        return meters / METERS_PER_FOOT if self.unit_system.get() == "Imperial" else meters

    def _meters_from_display(self, value: float) -> float:
        return value * METERS_PER_FOOT if self.unit_system.get() == "Imperial" else value

    def _length_unit(self) -> str:
        return "ft" if self.unit_system.get() == "Imperial" else "m"

    def _long_range_unit(self) -> str:
        return "mi" if self.unit_system.get() == "Imperial" else "km"

    def _long_range_display(self, meters: float) -> float:
        divisor = METERS_PER_MILE if self.unit_system.get() == "Imperial" else 1000.0
        return meters / divisor

    def _meters_from_long_range(self, value: float) -> float:
        multiplier = METERS_PER_MILE if self.unit_system.get() == "Imperial" else 1000.0
        return value * multiplier

    @staticmethod
    def _lighten(color: str) -> str:
        try:
            color = color.lstrip("#")
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            r, g, b = min(255, r + 45), min(255, g + 45), min(255, b + 45)
            return f"#{r:02x}{g:02x}{b:02x}"
        except (ValueError, IndexError):
            return "#9fb1c7"

    @staticmethod
    def _darken(color: str, amount: int = 45) -> str:
        try:
            color = color.lstrip("#")
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            r, g, b = max(0, r - amount), max(0, g - amount), max(0, b - amount)
            return f"#{r:02x}{g:02x}{b:02x}"
        except (ValueError, IndexError):
            return "#1a1a1a"

    def _draw_shaded_block(
        self, canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, color: str
    ) -> None:
        """A flat-filled rectangle reads as a silhouette against the ground
        line; a lit top edge and a shadowed side give it just enough depth to
        read as a solid block standing on the ground, not an outline."""
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)
        canvas.create_rectangle(left, top, right, bottom, fill=color, outline="")
        cap = min(6.0, (bottom - top) * 0.3)
        if cap > 0:
            canvas.create_rectangle(left, top, right, top + cap, fill=self._lighten(color), outline="")
        side = min(5.0, (right - left) * 0.3)
        if side > 0:
            canvas.create_rectangle(right - side, top, right, bottom, fill=self._darken(color), outline="")
        canvas.create_rectangle(left, top, right, bottom, outline=self._darken(color, 70), width=1)

    def _draw_tree_icon(
        self, canvas: tk.Canvas, cx: float, base_y: float, radius: float, color: str
    ) -> None:
        """One recognizable tree: a short brown trunk topped by a pointed,
        tapered conifer canopy (two stacked triangular tiers) -- the
        classic pine silhouette that reads as an actual tree, not a round
        abstract blob."""
        trunk_w = max(1.5, radius * 0.28)
        trunk_h = radius * 0.6
        canvas.create_rectangle(
            cx - trunk_w / 2, base_y - trunk_h, cx + trunk_w / 2, base_y + 1,
            fill="#5c4128", outline="",
        )
        canopy_base = base_y - trunk_h
        tier_h = radius * 1.3
        lower_top = canopy_base - tier_h
        canvas.create_polygon(
            cx - radius, canopy_base,
            cx + radius, canopy_base,
            cx, lower_top,
            fill=self._darken(color, 15), outline=self._darken(color, 45), width=1,
        )
        upper_base = lower_top + tier_h * 0.45
        upper_w = radius * 0.7
        tip_y = upper_base - tier_h * 0.95
        canvas.create_polygon(
            cx - upper_w, upper_base,
            cx + upper_w, upper_base,
            cx, tip_y,
            fill=color, outline=self._darken(color, 45), width=1,
        )
        canvas.create_polygon(
            cx, tip_y,
            cx - upper_w * 0.4, upper_base - tier_h * 0.25,
            cx, upper_base - tier_h * 0.1,
            fill=self._lighten(color), outline="",
        )

    def _draw_profile_forest_block(
        self, canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, color: str
    ) -> None:
        """A rectangle reads as a building no matter what colour it's
        filled -- one dense row of trees, every one rooted at the real
        ground line and sized to reach up toward the canopy top, is what
        makes a forest read as trees instead of a green box. Height
        varies; the base never leaves the ground, so nothing floats."""
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)
        width = right - left
        height = max(1.0, bottom - top)
        if width < 1:
            return
        tree_w = 12.0
        columns = min(90, max(1, round(width / tree_w)))
        col_w = width / columns
        for col in range(columns):
            cx = left + col_w * (col + 0.5)
            radius = max(3.0, min(11.0, height / 2.3, col_w * 0.75))
            radius *= 0.82 if col % 3 == 1 else 1.0
            self._draw_tree_icon(canvas, cx, bottom, radius, color)


def run() -> None:
    root = tk.Tk()
    app = MeshSimulatorApp(root)
    root.after(100, app.fit_view)
    root.mainloop()
